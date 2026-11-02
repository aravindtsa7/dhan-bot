"""Synthetic annual/account-summary regressions; never reads an external lake.

Signals are injected solely to isolate cash/sizing and API contracts. Price
execution still uses the real, unchanged Phase-B target/stop/ambiguity rules.
These are not indicator-parity or actual-market backtest results.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import unittest
from unittest.mock import Mock, patch

from quant_backtester import Config, run_backtest
from quant_trademind import Contract, STRATEGIES
import trademind_backtest as engine
from trademind_signals import stamp


class SyntheticAnnualData:
    """Two 2025 +5-point exits, one 2026 entry; optional same-bar ambiguity."""
    def __init__(self, *, premium=100., ambiguous=False):
        self.premium=premium
        self.ambiguous=ambiguous
        self.inputs={}
        self.active_inputs={}
        self.calls=[]
        self.dates={2025:'2025-12-30',2026:'2026-03-02'}
        self.calendar={d:set(range(555,930)) for d in self.dates.values()}

    def begin_run(self):
        self.calls.append('begin_run')

    def underlying(self, year):
        self.calls.append(('underlying',year))
        return [dict(date=self.dates[year],bars={m:dict(open=24000.,high=24001.,low=23999.,close=24000.,volume=0.) for m in range(555,930)})]

    def known_study_dates(self):
        return []

    def owning_expiry(self, date):
        return '2025-12-30' if date.startswith('2025') else '2026-03-03'

    def select(self, date, expiry, side, spot, decision, *, exact):
        self.calls.append(('select',date,decision))
        return Contract('SYNTHETIC',date+'-24000-'+side,expiry,24000.,side,stamp(date,599))

    def option(self, date, key):
        p=self.premium
        bars={m:dict(open=p,high=p*1.01,low=p*.99,close=p,volume=0.) for m in range(555,930)}
        for m in ([601,621] if date.startswith('2025') else [601]):
            bars[m]=dict(open=p,high=p*1.06,low=p*(.94 if self.ambiguous else .99),close=p,volume=0.)
        return bars

    def verify_preserved(self):
        self.calls.append('verify_preserved')


def synthetic_signals(strategy, sessions):
    return [dict(date=s['date'],timestamp=stamp(s['date'],m),minute=m,side='PE',spot=24000.,asOfMinute=m-1,
                 evidence={'fixture':'cash-contract-only; no indicator claim'})
            for s in sessions for m in ([600,620] if s['date'].startswith('2025') else [600])],[]


def annual_config(year=2026, **settings):
    return engine.frozen_config(Config(strategy=STRATEGIES[0],capital=100000,start=f'{year}-01-01',end=f'{year+1}-01-01'),
                                trademind_provider='trademind_retained',**settings)


def synthetic_run(config, data=None):
    data=data or SyntheticAnnualData()
    with patch('trademind_backtest.generate_signals',side_effect=synthetic_signals):
        return engine.run_observed(config,data)


class TradeMindApiContractTests(unittest.TestCase):
    def test_combined_years_rejected_by_all_api_boundaries_before_data(self):
        for strategy in STRATEGIES:
            c=engine.frozen_config(Config(strategy=strategy,capital=100000,start='2025-01-01',end='2027-01-01'),
                                   trademind_provider='trademind_retained')
            data=Mock()
            with self.subTest(strategy=strategy):
                with self.assertRaisesRegex(ValueError,'single calendar year'):
                    engine.run_observed(c,data)
                self.assertEqual(data.mock_calls,[])
                with patch('trademind_backtest.availability') as availability:
                    with self.assertRaisesRegex(ValueError,'single calendar year'):
                        run_backtest(c,data,persist=False)
                    availability.assert_not_called()
                self.assertEqual(data.mock_calls,[])

    def test_exclusive_next_january_boundary_is_annual_without_truncation(self):
        c=annual_config(2025)
        before=asdict(c)
        c.validate()
        self.assertEqual(asdict(c),before)
        replace(c,start='2025-12-31').validate()
        for end in ('2026-01-02','2026-01-01T00:00:00.000000001'):
            with self.subTest(end=end):
                with self.assertRaisesRegex(ValueError,'single calendar year'):
                    replace(c,end=end).validate()

    def test_separate_annual_accounts_start_fresh_and_2026_quantity_is_975(self):
        first=synthetic_run(annual_config(2025))
        second=synthetic_run(annual_config(2026))
        a=first['_frames']['gross_equity'].to_dict('records')
        b=second['_frames']['gross_equity'].to_dict('records')
        self.assertAlmostEqual(first['metrics']['gross']['final_equity'],109750.)
        self.assertEqual(a[0]['equity_before'],100000.)
        self.assertEqual(b[0]['equity_before'],100000.)
        self.assertEqual(b[0]['quantity'],975)
        self.assertNotEqual(b[0]['quantity'],1050)
        self.assertEqual(second['actual_coverage']['requested_start'],'2026-01-01')

    def test_rescenario_recomputes_unresolved_net_summary_through_shared_helper(self):
        c=replace(annual_config(),start='2026-03-02',end='2026-03-04')
        result=synthetic_run(c,SyntheticAnnualData(premium=1400.,ambiguous=True))
        #75*1400 cannot fit100000;65*1400 can. Same unresolved unit path,
        # different funded exposure. Source-baseline portfolio skipped entry.
        self.assertEqual(result['metrics']['net_status'],'COMPUTABLE')
        self.assertEqual(result['metrics']['net_pnl'],0.)
        units=deepcopy(result['_trade_records'])
        helper=engine.apply_summary
        with patch('trademind_backtest.apply_summary',wraps=helper) as derive:
            revised=engine.rescenario(result,replace(c,trademind_scenario='allocation_100'))
        derive.assert_called_once()
        self.assertEqual(revised['metrics']['net_cost_supported_fresh_account']['status'],'UNRESOLVED')
        self.assertIsNone(revised['metrics']['net_pnl'])
        self.assertEqual(revised['metrics']['net_status'],'UNRESOLVED')
        self.assertEqual(revised['metrics']['net_cost_coverage'],'SUPPORTED_FULL_REQUESTED_PERIOD')
        self.assertEqual(revised['status'],'UNRESOLVED')
        self.assertEqual(revised['_trade_records'],units)
        self.assertEqual(revised['strategy_unit_status'],result['strategy_unit_status'])
        self.assertEqual(result['metrics']['net_status'],'COMPUTABLE')  # source result not mutated

    def test_cost_coverage_unavailable_is_distinct_from_unresolved_account(self):
        result=synthetic_run(annual_config(2025))
        self.assertIsNone(result['metrics']['net_pnl'])
        self.assertEqual(result['metrics']['net_status'],'NOT_COMPUTABLE_FOR_FULL_REQUESTED_PERIOD')
        self.assertEqual(result['metrics']['net_cost_coverage'],'UNSUPPORTED_FOR_FULL_REQUESTED_PERIOD')
        self.assertEqual(result['strategy_unit_status'],'BACKTEST_COMPLETED')

    def test_affordability_skip_does_not_relabel_resolved_strategy_units(self):
        c=replace(annual_config(),start='2026-03-02',end='2026-03-04')
        result=synthetic_run(c,SyntheticAnnualData(premium=1400.))
        self.assertEqual(result['_frames']['gross_equity'].iloc[0]['status'],'CAPITAL_NO_TRADE')
        self.assertEqual(result['_trade_records'][0]['status'],'RESOLVED')
        self.assertEqual(result['strategy_unit_status'],'BACKTEST_COMPLETED')
        self.assertEqual(result['status'],'BACKTEST_COMPLETED')
        revised=engine.rescenario(result,replace(c,trademind_scenario='allocation_100'))
        self.assertEqual(revised['_trade_records'],result['_trade_records'])
        self.assertEqual(revised['strategy_unit_status'],'BACKTEST_COMPLETED')
        self.assertEqual(revised['status'],'BACKTEST_COMPLETED')

    def test_fresh_run_and_rescenario_share_summary_and_versioned_identity(self):
        c=replace(annual_config(),start='2026-03-02',end='2026-03-04')
        helper=engine.apply_summary
        with patch('trademind_backtest.apply_summary',wraps=helper) as derive:
            result=synthetic_run(c)
        derive.assert_called_once()
        target=replace(c,trademind_scenario='allocation_100')
        fresh=synthetic_run(target)
        reused=engine.rescenario(result,target)
        for key in ('status','strategy_unit_status','metrics','summary_revision','summary_source_sha256','run_id'):
            self.assertEqual(fresh[key],reused[key],key)


if __name__=='__main__':unittest.main()
