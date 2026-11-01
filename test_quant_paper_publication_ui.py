"""Read-only UI generation barriers; all accounts and interruptions synthetic."""
import hashlib,json,os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import pandas as pd
from streamlit.testing.v1 import AppTest
from paper_engine import PaperAccount
from quant_publication import publish_files,recover_publication,assert_publication_ready,PublicationError
import quant_paper_ui as ui


class PaperPublicationUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.account=PaperAccount.__new__(PaperAccount);self.account.root=self.root;self.account.state=dict(trades=[])
        self.account.save()

    def trade(self,status='CLOSED'):
        return dict(trade_id='synthetic',instrument='NIFTY',strategy='fixture',status=status,
            net_pnl=1234 if status=='CLOSED' else None,security_id='123',side='CE',quantity=75,
            entry_time='2026-10-22T10:00:00+05:30',exit_time='2026-10-22T10:01:00+05:30' if status=='CLOSED' else '',
            entry_price=10,exit_price=30 if status=='CLOSED' else None,capital_allocated=750,capital_reserved=850)

    def bytes(self):return {p.relative_to(self.root).as_posix():p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def interrupt(self):
        self.account.state['trades']=[self.trade()];replace=os.replace
        def interrupted(src,dst):
            if Path(dst)==self.root/'ledger.csv':raise KeyboardInterrupt('synthetic interruption')
            return replace(src,dst)
        with patch('quant_publication.os.replace',side_effect=interrupted),self.assertRaises(KeyboardInterrupt):self.account.save()

    def blocked(self,room):
        self.assertFalse(room['available']);self.assertIsNone(room['metrics']['net']);self.assertIsNone(room['metrics']['win_rate'])
        self.assertIsNone(room['unrealized']);self.assertTrue(room['ledger'].empty);self.assertFalse(room['downloadable'])

    def test_interrupted_publish_cannot_show_uncommitted_profit_and_ui_is_readonly(self):
        self.interrupt()
        self.assertEqual(json.loads((self.root/'.publication/paper/journal.json').read_text())['phase'],'PUBLISHING')
        self.assertEqual(json.loads((self.root/'state.json').read_text())['trades'][0]['net_pnl'],1234)
        with self.assertRaises(PublicationError):assert_publication_ready(self.root,'paper')
        before=self.bytes();self.blocked(ui.load_room(self.root));self.assertEqual(before,self.bytes())

    def test_external_rollback_then_committed_restart_generation_is_readable(self):
        self.interrupt();recover_publication(self.root,'paper')
        before=self.bytes();room=ui.load_room(self.root)
        self.assertTrue(room['available']);self.assertEqual(room['metrics']['net'],0)
        self.assertIn('ROLLED_BACK',room['publication_status']);self.assertEqual(before,self.bytes())
        self.account.state['trades']=[self.trade()];self.account.save()
        recover_publication(self.root,'paper')
        before=self.bytes();room=ui.load_room(self.root)
        self.assertEqual(room['metrics']['net'],1234);self.assertEqual(room['metrics']['win_rate'],100)
        self.assertIn('COMMITTED',room['publication_status']);self.assertTrue(room['downloadable']);self.assertEqual(before,self.bytes())

    def test_concurrent_complete_generation_change_discards_entire_read(self):
        original=ui.read_json;changed=False
        def read(root,name,warnings):
            nonlocal changed
            if name=='status.json' and not changed:
                changed=True;self.account.state['trades']=[self.trade()];self.account.save()
            return original(root,name,warnings)
        with patch('quant_paper_ui.read_json',side_effect=read):self.blocked(ui.load_room(self.root))
        self.assertEqual(ui.load_room(self.root)['metrics']['net'],1234)

    def test_same_transaction_changes_to_nonterminal_during_read(self):
        original=ui.read_json
        def read(root,name,warnings):
            if name=='last_run.json':
                p=self.root/'.publication/paper/journal.json';j=json.loads(p.read_text());j['phase']='PUBLISHING';p.write_text(json.dumps(j))
            return original(root,name,warnings)
        with patch('quant_paper_ui.read_json',side_effect=read):self.blocked(ui.load_room(self.root))

    def test_malformed_journals_do_not_fall_back_to_legacy(self):
        path=self.root/'.publication/paper/journal.json';original=path.read_bytes();valid=json.loads(original)
        variants=['{torn','[]',json.dumps(dict(valid,entries=[])),json.dumps(dict(valid,transaction='bad')),
                  json.dumps(dict(valid,entries=[valid['entries'][0],valid['entries'][0]])),
                  json.dumps(dict(valid,entries=[dict(valid['entries'][0],new_sha256='bad'),valid['entries'][1]]))]
        for value in variants:
            with self.subTest(journal=value[:30]):
                path.write_text(value);before=self.bytes();self.blocked(ui.load_room(self.root));self.assertEqual(before,self.bytes())

    def test_committed_hash_mismatch_and_missing_journal_block_metrics(self):
        state=self.root/'state.json';old=state.read_bytes();state.write_text(json.dumps(dict(trades=[self.trade()])))
        self.blocked(ui.load_room(self.root));state.write_bytes(old)
        (self.root/'.publication/paper/journal.json').unlink();self.blocked(ui.load_room(self.root))

    def test_file_changes_without_journal_change_during_read_are_rejected(self):
        original=ui.read_json
        def read(root,name,warnings):
            if name=='last_run.json':(self.root/'ledger.csv').write_text('trade_id,status,net_pnl\nother,CLOSED,99\n')
            return original(root,name,warnings)
        with patch('quant_paper_ui.read_json',side_effect=read):self.blocked(ui.load_room(self.root))

    def test_valid_hashes_do_not_authorize_malformed_or_disagreeing_account_payload(self):
        for state in (b'{broken',json.dumps(dict(trades=[self.trade()])).encode()):
            with self.subTest(state=state[:20]):
                publish_files(self.root,'paper',{'state.json':state,'ledger.csv':b'trade_id,status,net_pnl\n'})
                self.blocked(ui.load_room(self.root))

    def intent(self,text=None):
        p=self.root/'basket_invalidations/frozen.intent.json';p.parent.mkdir(exist_ok=True)
        p.write_text(text if text is not None else json.dumps(dict(simulation_invalidated=True,phase='paper_open',position_open=True)))

    def test_invalidation_intent_freezes_old_committed_state_without_recovery(self):
        self.account.state['trades']=[self.trade('OPEN')];self.account.save();self.intent()
        before=self.bytes();room=ui.load_room(self.root)
        self.assertTrue(room['available']);self.assertTrue(room['frozen']);self.assertIn('Frozen',room['health'])
        self.assertIsNone(room['metrics']['net']);self.assertIsNone(room['metrics']['win_rate']);self.assertIsNone(room['unrealized'])
        self.assertEqual(room['ledger'].iloc[0].status,'OPEN');self.assertFalse(room['downloadable']);self.assertEqual(before,self.bytes())

    def test_invalidated_state_and_torn_intent_both_fail_closed(self):
        self.account.state.update(trades=[self.trade()],simulation_status='INVALIDATED');self.account.save()
        room=ui.load_room(self.root);self.assertTrue(room['frozen']);self.assertIsNone(room['metrics']['net']);self.assertFalse(room['downloadable'])
        self.intent('{partial');self.blocked(ui.load_room(self.root));self.assertTrue(ui.load_room(self.root)['frozen'])

    def test_intent_created_during_read_discards_old_valuation(self):
        original=ui.read_json
        def read(root,name,warnings):
            if name=='last_run.json':self.intent()
            return original(root,name,warnings)
        with patch('quant_paper_ui.read_json',side_effect=read):self.blocked(ui.load_room(self.root))

    def test_legacy_account_explicitly_unverified_and_no_files_created(self):
        legacy=self.root/'legacy';legacy.mkdir();(legacy/'state.json').write_text(json.dumps(dict(trades=[self.trade()])))
        before={p:p.read_bytes() for p in legacy.rglob('*') if p.is_file()};room=ui.load_room(legacy)
        self.assertTrue(room['available']);self.assertIn('Legacy',room['publication_status']);self.assertIsNone(room['generation'])
        self.assertEqual(room['metrics']['net'],1234);self.assertEqual(before,{p:p.read_bytes() for p in legacy.rglob('*') if p.is_file()})
        app=AppTest.from_string('from quant_paper_ui import render_live_paper_room\n'+f'render_live_paper_room({str(legacy)!r})',default_timeout=30).run()
        self.assertFalse(app.exception);self.assertTrue(any('Legacy / unverified' in x.value for x in app.caption))

    def test_legacy_to_journal_generation_during_read_is_not_a_legacy_bypass(self):
        legacy=self.root/'legacy';legacy.mkdir();(legacy/'state.json').write_text(json.dumps(dict(trades=[])))
        account=PaperAccount.__new__(PaperAccount);account.root=legacy;account.state=dict(trades=[self.trade()])
        original=ui.read_json;changed=False
        def read(root,name,warnings):
            nonlocal changed
            if name=='last_run.json' and not changed:changed=True;account.save()
            return original(root,name,warnings)
        with patch('quant_paper_ui.read_json',side_effect=read):self.blocked(ui.load_room(legacy))

    def test_external_rollback_to_state_only_legacy_retains_explicit_view(self):
        legacy=self.root/'legacy';legacy.mkdir();(legacy/'state.json').write_text(json.dumps(dict(trades=[self.trade()])))
        account=PaperAccount.__new__(PaperAccount);account.root=legacy;account.state=dict(trades=[])
        replace=os.replace
        def interrupted(src,dst):
            if Path(dst)==legacy/'ledger.csv':raise KeyboardInterrupt()
            return replace(src,dst)
        with patch('quant_publication.os.replace',side_effect=interrupted),self.assertRaises(KeyboardInterrupt):account.save()
        recover_publication(legacy,'paper');before={p:p.read_bytes() for p in legacy.rglob('*') if p.is_file()}
        room=ui.load_room(legacy);self.assertTrue(room['available']);self.assertEqual(room['metrics']['net'],1234)
        self.assertIn('Legacy',room['publication_status']);self.assertIn('ROLLED_BACK',room['publication_status'])
        self.assertEqual(before,{p:p.read_bytes() for p in legacy.rglob('*') if p.is_file()})

    def test_render_blocked_and_frozen_views_have_no_download_or_success_metrics(self):
        self.interrupt()
        app=AppTest.from_string('from quant_paper_ui import render_live_paper_room\n'+f'render_live_paper_room({str(self.root)!r})',default_timeout=30).run()
        self.assertFalse(app.exception);self.assertNotIn('Closed net P&L',{m.label for m in app.metric});self.assertEqual(len(app.get('download_button')),0)
        self.assertTrue(any('publication' in x.value.lower() for x in list(app.warning)+list(app.info)+list(app.error)))
        recover_publication(self.root,'paper');self.account.state['trades']=[self.trade('OPEN')];self.account.save();self.intent();app.run()
        self.assertFalse(app.exception);self.assertEqual({m.label:m.value for m in app.metric}['Closed net P&L'],'N/A')
        self.assertTrue(any('Frozen' in x.value for x in list(app.warning)+list(app.info)+list(app.error)))
        self.assertEqual(len(app.get('download_button')),0)


class BacktestFailureUiTests(unittest.TestCase):
    def test_rejected_invalidated_and_invalid_config_clear_previous_success(self):
        from quant_book import InvalidBasketObservation
        from quant_option_model import MissingObservedPrice
        from quant_backtester import run_backtest
        from test_quant_book import fixture,DATE
        with TemporaryDirectory() as directory:
            root=Path(directory)
            def execute(c,output,persist):
                _,data,_=fixture();data.start=c.start;data.end=c.end
                return run_backtest(c,data,output,persist=False)
            with patch('quant_backtester.OUT',root),patch('quant_lake.ROOT',root/'lake'),patch('quant_backtester.run_backtest',side_effect=execute):
                app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=60).run()
                app.number_input(key='bt_minimum_premium').set_value(0.)
                for error in (MissingObservedPrice('synthetic missing evidence'),InvalidBasketObservation('synthetic invalid raw basket',dict(phase='open_position')),ValueError('synthetic invalid configuration')):
                    app.button(key='run_backtest').click().run();self.assertFalse(app.exception)
                    self.assertTrue(app.session_state['active_result']);self.assertTrue(len(app.get('plotly_chart')))
                    target='quant_terminal.build_config' if type(error) is ValueError else 'quant_backtester.run_backtest'
                    with patch(target,side_effect=error) as failed:
                        app.button(key='run_backtest').click().run()
                        self.assertEqual(failed.call_count,1)
                    self.assertFalse(app.exception);self.assertFalse(app.session_state['active_result'])
                    self.assertEqual(len(app.get('plotly_chart')),0);self.assertEqual(len([b for b in app.button if b.key=='save_result']),0)
                    self.assertTrue(app.error)
                    self.assertNotIn('Backtest complete',' '.join(x.value for x in app.success))


if __name__=='__main__':unittest.main()
