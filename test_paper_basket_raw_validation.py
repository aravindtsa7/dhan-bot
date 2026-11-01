"""Synthetic-only paper basket invalidation; never connected account fixtures."""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from paper_basket import BasketPaperAccount
from paper_feed import Asset, Tick, IST
from quant_book import leg_specs
from quant_option_model import MissingObservedPrice
from test_quant_book import fixture

NOW=datetime(2026,10,22,10,tzinfo=IST).timestamp()


class PaperRawBasketTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.c,_,day=fixture()
        specs=leg_specs(day,600,'BULL_CALL_SPREAD',100)
        self.assets=[Asset('NIFTY',2,101+i,'2026-10-27',10,l['strike'],l['side']) for i,l in enumerate(specs)]
        self.resolved=list(zip(specs,self.assets))
        self.quotes={a.key:Tick(a.key,p,NOW,NOW) for a,p in zip(self.assets,[6,2])}
        self.event=(600,'BULL_CALL_SPREAD',100,None,1)

    def account(self,c=None):
        return BasketPaperAccount(self.root,c or self.c,self.assets[0],lambda _:None)

    def entered(self):
        a=self.account();self.assertTrue(a.enter_basket(self.event,self.resolved,self.quotes,NOW));return a

    def bad_tick(self):return Tick(self.assets[0].key,25,NOW+1,NOW+1)

    def test_invalid_entry_is_rejected_and_durably_reported(self):
        for policy in ('model_continuity','strict_observed'):
            with self.subTest(policy=policy),TemporaryDirectory() as root:
                a=BasketPaperAccount(root,replace(self.c,price_policy=policy),self.assets[0],lambda _:None)
                bad=dict(self.quotes);bad[self.assets[0].key]=Tick(self.assets[0].key,25,NOW,NOW)
                with self.assertRaises(MissingObservedPrice):a.enter_basket(self.event,self.resolved,bad,NOW)
                self.assertIsNone(a.position)
                self.assertEqual(json.loads((Path(root)/'state.json').read_text())['simulation_status'],'INVALIDATED')

    def test_invalid_tick_freezes_open_position_without_exit_or_pnl(self):
        a=self.entered()
        with self.assertRaises(MissingObservedPrice):a.on_tick(self.bad_tick(),NOW+1)
        self.assertEqual(a.state['simulation_status'],'INVALIDATED')
        self.assertEqual(a.position['status'],'OPEN')
        self.assertIsNone(a.position['exit_price']);self.assertIsNone(a.position['net_pnl'])
        self.assertEqual(a.position['exit_time'],'')

    def test_direct_mark_cannot_accept_invalid_snapshot(self):
        a=self.entered();a.quotes[self.assets[0].key]=self.bad_tick()
        with self.assertRaises(MissingObservedPrice):a.mark(self.bad_tick())
        self.assertEqual(a.state['simulation_status'],'INVALIDATED')

    def test_direct_close_cannot_accept_invalid_snapshot(self):
        a=self.entered();a.quotes[self.assets[0].key]=self.bad_tick()
        with self.assertRaises(MissingObservedPrice):a.close(self.bad_tick(),NOW+1,'manual')
        self.assertEqual(a.position['status'],'OPEN');self.assertIsNone(a.position['net_pnl'])

    def test_favorable_quote_cannot_replace_invalidating_snapshot(self):
        a=self.entered()
        with self.assertRaises(MissingObservedPrice):a.on_tick(self.bad_tick(),NOW+1)
        with self.assertRaises(MissingObservedPrice):a.on_tick(Tick(self.assets[0].key,6,NOW+2,NOW+2),NOW+2)
        self.assertEqual(a.quotes[self.assets[0].key].price,25)
        self.assertEqual(a.position['status'],'OPEN')

    def test_restart_preserves_freeze_and_blocks_new_entries_and_exit(self):
        a=self.entered()
        with self.assertRaises(MissingObservedPrice):a.on_tick(self.bad_tick(),NOW+1)
        recovered=self.account()
        self.assertEqual(recovered.state['simulation_status'],'INVALIDATED')
        with self.assertRaises(MissingObservedPrice):recovered.enter_basket(self.event,self.resolved,self.quotes,NOW+2)
        with self.assertRaises(MissingObservedPrice):recovered.close(self.quotes[self.assets[0].key],NOW+2,'manual')
        self.assertEqual(recovered.position['status'],'OPEN')

    def test_invalid_snapshot_preserves_asset_ids_expiry_and_async_times(self):
        a=self.entered()
        with self.assertRaises(MissingObservedPrice) as caught:a.on_tick(self.bad_tick(),NOW+1)
        e=caught.exception.evidence
        self.assertEqual(e['raw_basket_value'],23)
        self.assertEqual([q['asset']['security_id'] for q in e['quote_snapshot']],[101,102])
        self.assertEqual([q['asset']['expiry'] for q in e['quote_snapshot']],['2026-10-27']*2)
        self.assertEqual([q['exchange_time'] for q in e['quote_snapshot']],[NOW+1,NOW])
        self.assertEqual(e['quote_time_spread_seconds'],1)
        self.assertFalse(e['contract_identity_verified'])

    def test_state_publication_failure_retains_recovery_freeze_evidence(self):
        a=self.entered()
        with patch.object(a,'save',side_effect=PermissionError('synthetic state publication denied')):
            with self.assertRaises(MissingObservedPrice) as caught:a.on_tick(self.bad_tick(),NOW+1)
        self.assertTrue(caught.exception.evidence.get('invalidation_publication_failure'))
        recovered=self.account()
        self.assertEqual(recovered.state['simulation_status'],'INVALIDATED')
        self.assertEqual(recovered.position['status'],'OPEN')

    def test_denied_ledger_replacement_uses_intent_after_transaction_rollback(self):
        import quant_publication
        a=self.entered();install=quant_publication._install
        def denied(source,target):
            if Path(target).name=='ledger.csv':raise PermissionError('synthetic ledger replacement denied')
            return install(source,target)
        with patch('quant_publication._install',side_effect=denied):
            with self.assertRaises(MissingObservedPrice) as caught:a.on_tick(self.bad_tick(),NOW+1)
        self.assertIn('invalidation_publication_failure',caught.exception.evidence)
        self.assertTrue(list((self.root/'basket_invalidations').glob('*.intent.json')))
        recovered=self.account()
        self.assertEqual(recovered.state['simulation_status'],'INVALIDATED')
        self.assertEqual(recovered.position['status'],'OPEN')
        self.assertIsNone(recovered.position['net_pnl'])
        self.assertIn('INVALIDATED',(self.root/'ledger.csv').read_text())

    def test_interrupted_invalidation_intent_blocks_restart(self):
        self.entered()
        directory=self.root/'basket_invalidations';directory.mkdir()
        (directory/'interrupted.intent.json').write_text('{"simulation_invalidated":')
        with self.assertRaisesRegex(ValueError,'Incomplete basket invalidation evidence'):
            self.account()

    def test_execution_depth_and_slippage_are_separate_from_raw_bounds(self):
        c=replace(self.c,slippage=.5)
        a=self.account(c)
        quotes={a.key:Tick(a.key,p,NOW,NOW,bid=bid,ask=ask) for a,p,bid,ask in
                [(self.assets[0],12,11.8,12.2),(self.assets[1],2,1.8,2.2)]}
        self.assertTrue(a.enter_basket(self.event,self.resolved,quotes,NOW))
        self.assertGreater(a.position['entry_price'],10)
        self.assertNotEqual(a.state.get('simulation_status'),'INVALIDATED')
        self.assertEqual(a.position['entry_raw_quote_snapshot']['quote_time_spread_seconds'],0)
        self.assertFalse(a.position['entry_raw_quote_snapshot']['snapshot_synchronization_verified'])
        self.assertEqual(a.position['raw_entry_price'],10)

    def test_butterfly_joint_liability_is_validated_even_with_valid_pairs(self):
        c,_,day=fixture('book_iron_butterfly','IRON_BUTTERFLY')
        specs=leg_specs(day,600,'IRON_BUTTERFLY',100)
        assets=[Asset('NIFTY',2,201+i,'2026-10-27',10,l['strike'],l['side']) for i,l in enumerate(specs)]
        quotes={a.key:Tick(a.key,p,NOW,NOW) for a,p in zip(assets,[9,9,2,2])}
        a=BasketPaperAccount(self.root,c,assets[0],lambda _:None)
        with self.assertRaises(MissingObservedPrice) as caught:
            a.enter_basket((600,'IRON_BUTTERFLY',100,None,1),list(zip(specs,assets)),quotes,NOW)
        self.assertEqual(caught.exception.evidence['raw_pair_values'],[7,7])
        self.assertEqual(caught.exception.evidence['raw_basket_value'],-14)


if __name__=='__main__':unittest.main()
