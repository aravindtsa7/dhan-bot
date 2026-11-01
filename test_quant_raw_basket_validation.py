"""Raw synchronized basket evidence must fail closed before execution costs."""
import ast
from dataclasses import replace
import json
from pathlib import Path
import unittest
import pandas as pd

from quant_backtester import simulate_trade, run_backtest
from quant_book import bounded_spread_rows
from quant_option_model import MissingObservedPrice
from test_quant_book import fixture, DATE


class RawBasketValidationTests(unittest.TestCase):
    def invalid(self, c, data, side='BULL_CALL_SPREAD', at=600):
        with self.assertRaises(MissingObservedPrice) as caught:
            simulate_trade(data,c,DATE,(at,side,100,None,1),'raw-invalid')
        return caught.exception

    def test_observed_entry_outside_width_rejected_before_allocation(self):
        for policy in ('strict_observed','model_continuity'):
            c,d,day=fixture()
            day['options'][600,'CE',100]=(25,)*4+(100,)
            with self.subTest(policy=policy):
                self.invalid(replace(c,price_policy=policy),d)

    def test_invalid_entry_is_reported_before_premium_or_affordability_filter(self):
        c,d,day=fixture()
        day['options'][600,'CE',100]=(1,)*4+(100,)
        self.invalid(replace(c,minimum_premium=50,capital=1),d)

    def test_open_position_observed_23_point_vertical_invalidates_run(self):
        c,d,day=fixture()
        day['options'][601,'CE',100]=(25,)*4+(100,)
        for policy in ('strict_observed','model_continuity'):
            with self.subTest(policy=policy):
                with self.assertRaises(MissingObservedPrice):
                    run_backtest(replace(c,price_policy=policy),d,persist=False)

    def test_observed_close_invalid_even_when_open_is_valid(self):
        c,d,day=fixture()
        day['options'][600,'CE',100]=(6,25,6,25,100)
        self.invalid(c,d)

    def test_observed_negative_vertical_is_rejected(self):
        c,d,day=fixture()
        day['options'][601,'CE',100]=(1,)*4+(100,)
        self.invalid(c,d)

    def test_butterfly_joint_bound_rejected_with_individually_valid_pairs(self):
        c,d,day=fixture('book_iron_butterfly','IRON_BUTTERFLY')
        for side in ('CE','PE'):
            day['options'][601,side,100]=(9,)*4+(100,)
        self.invalid(c,d,'IRON_BUTTERFLY')

    def test_observed_pair_in_mixed_butterfly_cannot_be_changed_to_fit(self):
        legs=[dict(side=s,strike=k) for s,k in [('CE',100),('PE',100),('CE',150),('PE',50)]]
        rows=[(.05,)*4+(0,),(61,)*4+(100,),(.05,)*4+(0,),(10,)*4+(100,)]
        day=dict(timestamp=DATE,prices={600:(100,)*4+(0,)})
        with self.assertRaises(MissingObservedPrice):
            bounded_spread_rows(day,600,'IRON_BUTTERFLY',legs,rows,
                ['model_spread_continuity','observed','model_spread_continuity','observed'],
                {(0,2):(0,100,.05),(1,3):(49,100,10)})
        self.assertEqual([r[0] for r in rows],[.05,61,.05,10])

    def test_invalid_observed_continuity_calibration_cannot_choose_other_pair(self):
        legs=[dict(side='CE',strike=100,ratio=1),dict(side='CE',strike=110,ratio=-1)]
        day=dict(timestamp=DATE,prices={600:(100,)*4+(0,)},
            rolling={('CE','ATM'):{600:120},('CE','ATM+1'):{600:130}},
            options={(600,'CE',120):(25,)*4+(100,),(600,'CE',130):(2,)*4+(100,)})
        with self.assertRaises(MissingObservedPrice):
            bounded_spread_rows(day,600,'BULL_CALL_SPREAD',legs,[(.05,)*4+(0,)]*2,
                ['model_spread_continuity']*2,{(0,1):(4,100,2)})

    def test_terminal_observed_open_is_validated(self):
        c,d,day=fixture(at=919)
        day['options'][920,'CE',100]=(25,)*4+(100,)
        self.invalid(c,d,at=919)

    def test_terminal_later_close_cannot_veto_valid_open_fill(self):
        c,d,day=fixture(at=919)
        day['options'][920,'CE',100]=(6,25,6,25,100)
        trade,path=simulate_trade(d,c,DATE,(919,'BULL_CALL_SPREAD',100,None,1),'open-only')
        self.assertEqual(trade['raw_exit_price'],4)
        self.assertEqual(path[-1]['close'],4)

    def test_valid_observed_boundary_is_preserved_despite_model_interior_floor(self):
        rows=[(12,)*4+(100,),(2,)*4+(100,)]
        legs=[dict(side='CE',strike=100,ratio=1),dict(side='CE',strike=110,ratio=-1)]
        sources=['observed','observed']
        actual,changed=bounded_spread_rows(dict(prices={600:(100,)*4+(0,)}),600,
            'BULL_CALL_SPREAD',legs,rows,sources,{(0,1):(4,100,2)})
        self.assertEqual(actual,rows)
        self.assertFalse(changed)
        self.assertEqual(sources,['observed','observed'])

    def test_subtick_observed_overshoot_is_rejected_without_projection(self):
        c,d,day=fixture(at=919)
        day['options'][920,'CE',100]=(12.05,)*4+(100,)
        self.invalid(c,d,at=919)

    def test_execution_slippage_can_cross_raw_bounds(self):
        c,d,day=fixture(at=919)
        # Raw debit 0.05 at exit is valid; adverse liquidation is negative.
        day['options'][920,'CE',100]=(2.05,)*4+(100,)
        trade,path=simulate_trade(d,replace(c,slippage=.5),DATE,
            (919,'BULL_CALL_SPREAD',100,None,1),'execution-cost')
        self.assertAlmostEqual(trade['raw_exit_price'],.05)
        self.assertLess(trade['exit_price'],0)
        from verify_strategy_audit import validate_basket_evidence
        validate_basket_evidence(pd.DataFrame([trade]).iloc[0],pd.DataFrame(path))

    def test_adverse_entry_debit_above_width_is_not_discarded(self):
        c,d,day=fixture(at=919)
        for t in (919,920): day['options'][t,'CE',100]=(12,)*4+(100,)
        trade,_=simulate_trade(d,replace(c,slippage=.5),DATE,
            (919,'BULL_CALL_SPREAD',100,None,1),'costly-entry')
        self.assertIsNotNone(trade,'A valid raw entry must not be dropped because of adverse execution')
        self.assertEqual(trade['raw_entry_price'],10)
        self.assertGreater(trade['entry_price'],10)
        self.assertAlmostEqual(trade['capital_allocated'],trade['entry_price']*trade['quantity'])

    def test_butterfly_credit_consumed_by_entry_cost_reserves_extra_debit(self):
        c,d,day=fixture('book_iron_butterfly','IRON_BUTTERFLY',at=914)
        for t in (914,915):
            for side in ('CE','PE'): day['options'][t,side,100]=(2.025,)*4+(100,)
        trade,_=simulate_trade(d,replace(c,slippage=.5),DATE,
            (914,'IRON_BUTTERFLY',100,None,1),'costly-credit')
        self.assertIsNotNone(trade,'Execution costs consuming a raw credit require more reserve, not a silent skip')
        self.assertLess(trade['raw_entry_price'],0)
        self.assertGreater(trade['entry_price'],0)
        self.assertEqual(trade['entry_value_type'],'debit')
        self.assertEqual(trade['capital_basis'],'full_wing_width_plus_execution_debit')
        self.assertAlmostEqual(trade['capital_allocated'],(10+trade['entry_price'])*trade['quantity'])

    def test_exception_preserves_raw_legs_and_identity_uncertainty(self):
        c,d,day=fixture()
        day['options'][601,'CE',100]=(25,)*4+(100,)
        exc=self.invalid(c,d)
        evidence=getattr(exc,'evidence',{})
        self.assertEqual(evidence.get('raw_basket_value'),23)
        self.assertEqual(evidence.get('trade_id'),'raw-invalid')
        self.assertTrue(evidence.get('simulation_invalidated'))
        self.assertFalse(evidence.get('contract_identity_verified',True))
        self.assertEqual([leg['raw_price'] for leg in evidence['legs']],[25,2])
        json.dumps(evidence,allow_nan=False)

    def test_auditor_cannot_skip_unflagged_observed_raw_paths(self):
        c,d,_=fixture()
        trade,path=simulate_trade(d,c,DATE,(600,'BULL_CALL_SPREAD',100,None,1),'audit')
        paths=pd.DataFrame(path)
        paths.loc[0,['open','high','close','mark']]=23
        if 'raw_open' in paths: paths.loc[0,'raw_open']=23
        source=Path(__file__).with_name('verify_strategy_audit.py').read_text()
        validator=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.If)
            and isinstance(n.test,ast.Call) and isinstance(n.test.func,ast.Name)
            and n.test.func.id=='len' and isinstance(n.test.args[0],ast.Name)
            and n.test.args[0].id=='trades')
        import verify_strategy_audit as audit
        namespace=dict(vars(audit),trades=pd.DataFrame([trade]),paths=paths,c=c,strategy=c.strategy)
        with self.assertRaises((AssertionError,MissingObservedPrice)):
            exec(compile(ast.Module(body=[validator],type_ignores=[]),'<actual audit>','exec'),namespace)

    def test_auditor_validates_raw_legs_even_when_aggregate_mark_is_plausible(self):
        c,d,_=fixture('book_iron_butterfly','IRON_BUTTERFLY')
        trade,path=simulate_trade(d,c,DATE,(600,'IRON_BUTTERFLY',100,None,1),'audit-legs')
        paths=pd.DataFrame(path)
        raw=json.loads(paths.loc[0,'raw_legs'])
        raw[0]['price_source']=raw[2]['price_source']='model_spread_continuity'
        for field in ('raw_open','raw_close'):
            raw[0][field]=raw[2][field]=None
            raw[1][field]=25
        paths.loc[0,'raw_legs']=json.dumps(raw)
        from verify_strategy_audit import validate_basket_evidence
        with self.assertRaises(MissingObservedPrice):
            validate_basket_evidence(pd.DataFrame([trade]).iloc[0],paths)


if __name__=='__main__': unittest.main()
