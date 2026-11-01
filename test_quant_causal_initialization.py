"""Causal initialization regressions; synthetic lakes only, no broker/network."""
from dataclasses import replace
import unittest

import numpy as np
import pandas as pd

from quant_backtester import Config, ResearchData, research_events, run_backtest
from quant_calendar import regular_session
import test_quant_run_data as fixtures


class CausalInitializationTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.RunDataTests(methodName='runTest')
        self.helper.setUp()
        self.addCleanup(self.helper.temp.cleanup)

    def index_day(self, date, ordinal=0, *, missing=()):
        def prices(frame):
            count = len(frame)
            x = np.arange(count, dtype=float)
            price = 20000 + ordinal * 57 + .09*x + 8*np.sin(x/27+ordinal)
            frame['open'] = price
            frame['close'] = price+.1
            frame['high'] = price+.3
            frame['low'] = price-.2
        for name in ['NIFTY']+[f'NIFTY_{s}_{m}_WEEK1' for s in ('CE','PE') for m in ('ATM','ATM-1','ATM+1')]:
            self.helper.store(name, date, change=prices if name=='NIFTY' else None,
                              missing=missing if name=='NIFTY_CE_ATM+1_WEEK1' else ())

    def load(self, start, end, instrument='NIFTY'):
        return self.helper.load(instrument, start, end)

    def test_long_recursive_history_is_identical_across_narrow_broad_and_prefix_requests(self):
        dates = [str(d.date()) for d in pd.date_range('2026-09-21','2026-10-19') if regular_session(str(d.date()))]
        for i, date in enumerate(dates):
            self.index_day(date, i)
        end = str((pd.Timestamp(dates[-1])+pd.Timedelta(days=1)).date())
        broad = self.load(dates[0], end)
        shifted = self.load(dates[-3], end)
        narrow = self.load(dates[-1], end)
        target = dates[-1]
        expected = broad.research_features('15m')[target]
        for data in (shifted, narrow):
            pd.testing.assert_frame_equal(expected, data.research_features('15m')[target], check_exact=True)
            c = Config(strategy='supertrend_adx_trend', timeframe='15m', start=data.start, end=end)
            self.assertEqual(research_events(broad, replace(c,start=dates[0]),target), research_events(data,c,target))
        self.assertEqual(list(narrow.days), [target])
        self.assertEqual(narrow.available_days, [target])
        self.assertTrue(pd.notna(expected.adx.iloc[0]))
        self.assertEqual(narrow.indicator_initialization['policy'], 'canonical-validated-segment-v2')
        self.assertEqual(narrow.indicator_initialization['indicator_window'], ['09:15','15:20'])
        self.assertEqual(narrow.indicator_initialization['sessions'][target]['origin'], dates[0])
        self.assertGreater(len(narrow.warmup_eligibility), 10)
        result = run_backtest(replace(c,capital=100000), narrow, output=self.helper.root/'runs', persist=False)
        trades = result['_frames']['trades']
        self.assertGreater(len(trades), 0)
        self.assertTrue(trades.entry_time.dt.strftime('%Y-%m-%d').eq(target).all())
        self.assertTrue(trades.exit_time.dt.strftime('%Y-%m-%d').eq(target).all())
        # Later rows cannot influence an earlier common date, including seeds.
        cutoff = dates[-3]
        prefix = self.load(dates[-4], str((pd.Timestamp(cutoff)+pd.Timedelta(days=1)).date()))
        pd.testing.assert_frame_equal(broad.research_features('15m')[cutoff], prefix.research_features('15m')[cutoff], check_exact=True)

    def test_excluded_dependency_session_resets_seed_and_remains_separate_evidence(self):
        self.index_day('2026-10-21', 0)
        self.index_day('2026-10-22', 1, missing=('10:00',))
        self.index_day('2026-10-23', 2)
        broad = self.load('2026-10-21','2026-10-24')
        narrow = self.load('2026-10-23','2026-10-24')
        target = narrow.research_features('15m')['2026-10-23']
        pd.testing.assert_frame_equal(target, broad.research_features('15m')['2026-10-23'], check_exact=True)
        self.assertTrue(target.atr14.iloc[:13].isna().all())
        self.assertTrue(target.adx.isna().all())  # 25 bars cannot seed both Wilder stages.
        self.assertEqual(narrow.indicator_initialization['sessions']['2026-10-23']['origin'], '2026-10-23')
        self.assertEqual(narrow.eligibility[0]['date'], '2026-10-23')
        self.assertEqual(narrow.warmup_eligibility[-1]['status'], 'excluded')

    def test_entirely_missing_expected_session_is_not_bridged(self):
        self.index_day('2026-10-21')
        self.index_day('2026-10-23', 1)
        narrow = self.load('2026-10-23','2026-10-24')
        bars = narrow.research_features('15m')['2026-10-23']
        self.assertTrue(bars.ema21.iloc[:20].isna().all())
        self.assertEqual(narrow.indicator_initialization['sessions']['2026-10-23']['origin'], '2026-10-23')

    def test_futures_contract_change_starts_new_segment(self):
        for date, av, bv in [('2026-09-04',100,10),('2026-09-07',1,100),('2026-09-08',50,20)]:
            self.helper.store('CRUDEOIL_A', date, mcx=True, volume=av)
            self.helper.store('CRUDEOIL_B', date, mcx=True, volume=bv)
        broad = self.load('2026-09-04','2026-09-09','CRUDEOIL')
        narrow = self.load('2026-09-08','2026-09-09','CRUDEOIL')
        target = narrow.research_features('15m')['2026-09-08']
        pd.testing.assert_frame_equal(target,broad.research_features('15m')['2026-09-08'],check_exact=True)
        self.assertTrue(target.ema21.iloc[:20].isna().all())
        self.assertEqual(narrow.indicator_initialization['sessions']['2026-09-08']['origin'],'2026-09-08')
        self.assertEqual(narrow.days['2026-09-08']['contract'],'CRUDEOIL_B')

    def test_pre_request_input_changes_are_fingerprinted(self):
        self.index_day('2026-10-21')
        self.index_day('2026-10-22',1)
        before = self.load('2026-10-22','2026-10-23')
        self.index_day('2026-10-21',99)
        after = self.load('2026-10-22','2026-10-23')
        self.assertNotEqual(before.fingerprint, after.fingerprint)
        self.assertNotEqual(before.research_features('15m')['2026-10-22'].ema21.iloc[0],after.research_features('15m')['2026-10-22'].ema21.iloc[0])

    def test_mcx_does_not_infer_nse_holiday_closure_for_warmup(self):
        # NSE is closed Oct 20, but its calendar cannot prove MCX was closed.
        for date in ('2026-10-16','2026-10-19','2026-10-21'):
            self.helper.store('CRUDEOIL_A',date,mcx=True)
        data = self.load('2026-10-21','2026-10-22','CRUDEOIL')
        bars = data.research_features('15m')['2026-10-21']
        self.assertTrue(bars.ema21.iloc[:20].isna().all())
        self.assertEqual(data.indicator_initialization['sessions']['2026-10-21']['origin'],'2026-10-21')
        self.assertIn('false breaks',data.indicator_initialization['calendar_basis'])

    def test_unvalidated_predecessor_tail_cannot_change_next_session_indicators(self):
        """Isolate dependencies; ingestion/audit still reject invalid raw input."""
        def data(mutation=None, narrow=False):
            value=ResearchData.__new__(ResearchData)
            value.mcx=False;value.instrument='NIFTY';value.expiry_config=None
            value._features={};value._events={};value.days={}
            value.required_windows={'underlying':['09:15','15:20']}
            value.available_days=['2026-10-21','2026-10-22']
            for number,date in enumerate(value.available_days):
                stamps=pd.date_range(date+' 09:15',date+' 15:29',freq='min',tz='Asia/Kolkata')
                x=np.arange(len(stamps),dtype=float)
                price=20000+number*80+x*.11
                frame=pd.DataFrame(dict(timestamp=stamps,open=price,high=price+.3,low=price-.2,close=price+.1,volume=10.))
                if number==0 and mutation:frame=mutation(frame)
                value.days[date]=dict(contract='NIFTY',spot=frame,
                    rolling={('CE','ATM'):{int(t.hour*60+t.minute):24000. for t in stamps}},options={},
                    prices={int(r.timestamp.hour*60+r.timestamp.minute):(r.open,r.high,r.low,r.close,r.volume) for r in frame.itertuples()})
            value.indicator_available_days=list(value.available_days)
            value.indicator_days=dict(value.days)
            if narrow:value.days={date:value.days[date]}
            return value
        def mutate(frame,kind):
            tail=frame.timestamp.dt.strftime('%H:%M').gt('15:20')
            if kind=='missing':return frame.loc[~tail].copy()
            if kind=='out_of_order':return pd.concat([frame.loc[~tail],frame.loc[tail].iloc[::-1]])
            frame=frame.copy()
            frame.loc[tail,['open','high','low','close']]=(-100. if kind=='nonpositive' else 90000.)
            return frame
        baseline=data()
        expected=baseline.research_features('5m')['2026-10-22']
        c=Config(strategy='supertrend_adx_trend',timeframe='5m',start='2026-10-22',end='2026-10-23')
        expected_events=research_events(baseline,c,'2026-10-22')
        self.assertGreater(len(expected_events),0)
        for kind in ('changed','missing','out_of_order','nonpositive'):
            with self.subTest(tail=kind):
                narrow=data(lambda f:mutate(f,kind),narrow=True)
                pd.testing.assert_frame_equal(expected,narrow.research_features('5m')['2026-10-22'],check_exact=True)
                self.assertEqual(expected_events,research_events(narrow,c,'2026-10-22'))
        self.assertEqual(expected.timestamp.max().strftime('%H:%M'),'15:15')

    def test_validated_window_is_inclusive_and_expiry_keeps_separate_behavior(self):
        data=ResearchData.__new__(ResearchData)
        data.mcx=True;data.expiry_config=None
        frame=pd.DataFrame({'timestamp':pd.to_datetime([
            '2026-10-21 08:59','2026-10-21 09:00','2026-10-21 23:15',
            '2026-10-21 23:16','2026-10-21 23:29']).tz_localize('Asia/Kolkata')})
        self.assertEqual(data._validated_indicator_frame(frame).timestamp.dt.strftime('%H:%M').tolist(),['09:00','23:15'])
        data.mcx=False;data.expiry_config=Config(strategy='expiry_day')
        self.assertIs(data._validated_indicator_frame(frame),frame)


if __name__=='__main__':
    unittest.main()
