"""Shared ingestion/audit/window boundaries; synthetic candles only."""
from datetime import date
import unittest
import pandas as pd
from step2_build_data_lake import normalize
from quant_data_quality import run_quality_check
from quant_run_data import stream_check
from test_quant_lake import sample, META
import test_quant_run_data as fixtures


class PositiveUnderlyingTests(unittest.TestCase):
    def test_ingestion_rejects_each_nonpositive_underlying_field_without_repair(self):
        for field in ('open','high','low','close'):
            for price in (0.,-1.):
                with self.subTest(field=field,price=price):
                    raw=sample(['2025-09-02 10:00'])
                    raw.update(open=[1.],high=[1.],low=[1.],close=[1.])
                    raw[field]=[price]
                    stored,(quality,rejected)=normalize(raw,META,date(2025,9,2),date(2025,9,3))
                    self.assertTrue(stored.empty)
                    self.assertEqual(quality['invalid_ohlcv'],1)
                    self.assertEqual(float(rejected[field].iloc[0]),price)
                    self.assertFalse(quality.get('repairs'))

    def test_zero_underlying_complete_fixture_fails_audit_and_window(self):
        f=fixtures.RunDataTests();f.setUp();self.addCleanup(f.doCleanups)
        def zero(frame):
            frame.loc[frame.timestamp.dt.strftime('%H:%M').eq('10:00'),['open','high','low','close']]=0.
        f.index(stream='NIFTY',change=zero)
        report=run_quality_check(f.root)
        self.assertEqual(report['streams']['spot_1m|NIFTY']['invalid_stored_candles'],1)
        frame=pd.read_parquet(f.root/'NIFTY_2025-09-02.parquet')
        reasons,_=stream_check(frame,'2025-09-02','NIFTY',['09:15','15:20'])
        self.assertEqual(next(x for x in reasons if x['problem']=='Invalid required candles')['count'],1)

    def test_option_zero_premium_is_storable_but_underlying_reference_must_be_positive(self):
        raw=sample(['2025-09-02 10:00'])
        raw.update(open=[0.],high=[0.],low=[0.],close=[0.],oi=[0.],iv=[0.],spot=[100.],strike=[100.])
        meta=dict(META,instrument='NIFTY_CE_ATM_WEEK1',option_type='CE')
        frame,(q,_)=normalize(raw,meta,date(2025,9,2),date(2025,9,3))
        self.assertEqual(len(frame),1);self.assertEqual(q['invalid_ohlcv'],0)
        reasons,_=stream_check(frame,'2025-09-02',meta['instrument'],['10:00','10:00'])
        self.assertFalse(reasons)
        raw['spot']=[0.]
        frame,(q,rejected)=normalize(raw,meta,date(2025,9,2),date(2025,9,3))
        self.assertTrue(frame.empty);self.assertEqual(q['invalid_ohlcv'],1)
        self.assertEqual(float(rejected.spot.iloc[0]),0.)

    def test_option_zero_premium_stored_audit_and_zero_reference_window(self):
        f=fixtures.RunDataTests();f.setUp();self.addCleanup(f.doCleanups)
        def zero(frame):
            frame.loc[frame.timestamp.dt.strftime('%H:%M').eq('10:00'),['open','high','low','close']]=0.
        f.index(change=zero,stream='NIFTY_CE_ATM_WEEK1')
        report=run_quality_check(f.root)
        self.assertEqual(report['streams']['options_rolling_1m|NIFTY_CE_ATM_WEEK1']['invalid_stored_candles'],0)
        frame=pd.read_parquet(f.root/'NIFTY_CE_ATM_WEEK1_2025-09-02.parquet')
        frame.loc[frame.timestamp.dt.strftime('%H:%M').eq('10:00'),'spot']=0.
        reasons,_=stream_check(frame,'2025-09-02','NIFTY_CE_ATM_WEEK1',['09:15','15:20'])
        self.assertEqual(next(x for x in reasons if x['problem']=='Invalid required candles')['count'],1)

if __name__=='__main__':unittest.main()
