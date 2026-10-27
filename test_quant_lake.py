import unittest
import tempfile
import contextlib
import io
from pathlib import Path
from unittest.mock import Mock
from step2_build_data_lake import Builder,writer_lock
from unittest.mock import patch
from datetime import date
import pandas as pd
from step2_build_data_lake import normalize,windows
from quant_lake import resample,QuantDataLake

META=dict(instrument='TEST',security_id='13',exchange_segment='IDX_I')
def sample(times):
 return dict(timestamp=[int(pd.Timestamp(t,tz='Asia/Kolkata').timestamp()) for t in times],open=[10]*len(times),high=[12]*len(times),low=[9]*len(times),close=[11]*len(times),volume=[5]*len(times))

class LakeTests(unittest.TestCase):
 def test_window_boundaries(self):
  w=list(windows(date(2025,1,1),date(2026,10,23)))
  self.assertTrue(all((b-a).days<=28 for a,b in w))
  self.assertTrue(all(w[i][1]==w[i+1][0] for i in range(len(w)-1)))
 def test_duplicate_and_exclusive_end(self):
  d=sample(['2025-01-01 09:15','2025-01-01 09:15','2025-01-02 09:15'])
  f,(q,_)=normalize(d,META,date(2025,1,1),date(2025,1,2))
  self.assertEqual(len(f),1);self.assertEqual(q['exact_duplicates'],1);self.assertEqual(q['outside_request'],1)
 def test_conflicting_duplicates_are_quarantined_without_guessing(self):
  d=sample(['2025-01-01 09:15','2025-01-01 09:15']);d['close'][1]=10
  f,(q,rejected)=normalize(d,META,date(2025,1,1),date(2025,1,2))
  self.assertEqual(len(f),0);self.assertEqual(q['conflicting_duplicate_rows'],2)
  self.assertEqual(len(rejected),2)
 def test_hours_quarantine(self):
  f,(q,reject)=normalize(sample(['2025-01-01 09:14','2025-01-01 09:15']),META,date(2025,1,1),date(2025,1,2))
  self.assertEqual(len(f),1);self.assertEqual(len(reject),1)
 def test_mcx_dst_close(self):
  m=dict(META,exchange_segment='MCX_COMM')
  f,(_,r)=normalize(sample(['2025-01-02 23:54','2025-06-02 23:54']),m,date(2025,1,1),date(2026,1,1))
  self.assertEqual(len(f),1);self.assertEqual(len(r),1)
 def test_ohlc_and_no_overnight_fill(self):
  f,_=normalize(sample(['2025-01-01 09:15','2025-01-01 09:16','2025-01-02 09:15']),META,date(2025,1,1),date(2025,1,3))
  bars=resample(f,3)
  self.assertEqual(len(bars),2);self.assertEqual(bars.volume.tolist(),[10,5]);self.assertFalse(bars.bar_complete.any())
 def test_missing_arrays_fail(self):
  d=sample(['2025-01-01 09:15']);d['volume']=[]
  with self.assertRaises(ValueError):normalize(d,META,date(2025,1,1),date(2025,1,2))
 def test_live_lake_query_intraday_bound(self):
  with QuantDataLake() as lake:
   f=lake.query('NIFTY','2025-01-01 09:15','2025-01-01 09:20')
   self.assertEqual(len(f),5)
   bars=lake.query('NIFTY','2025-01-01','2025-01-02',timeframe='5m')
   self.assertGreater(len(bars),0)

class StorageTests(unittest.TestCase):
 def test_resume_and_checksum_repair(self):
  with tempfile.TemporaryDirectory() as directory, patch('step2_build_data_lake.API') as api, contextlib.redirect_stdout(io.StringIO()):
   api.return_value.post.return_value=sample(['2025-01-01 09:15'])
   root=Path(directory);b=Builder(root,date(2025,1,1),date(2025,1,2))
   args=('spot_1m',META,date(2025,1,1),date(2025,1,2),'/charts/intraday',{})
   record=b.chunk(*args)
   self.assertEqual(record['status'],'complete')
   api.return_value.post.reset_mock();b.chunk(*args);api.return_value.post.assert_not_called()
   (root/record['path']).write_bytes(b'corrupt')
   b.chunk(*args);api.return_value.post.assert_called_once()
 def test_failed_request_remains_retryable(self):
  with tempfile.TemporaryDirectory() as directory, patch('step2_build_data_lake.API') as api, contextlib.redirect_stdout(io.StringIO()):
   api.return_value.post.side_effect=ValueError('failure')
   b=Builder(Path(directory),date(2025,1,1),date(2025,1,2))
   args=('spot_1m',META,date(2025,1,1),date(2025,1,2),'/charts/intraday',{})
   self.assertEqual(b.chunk(*args)['status'],'failed')
   b.chunk(*args);self.assertEqual(api.return_value.post.call_count,2)
 def test_lock_excludes_second_writer(self):
  with tempfile.TemporaryDirectory() as directory:
   with writer_lock(Path(directory)):
    with self.assertRaises((RuntimeError,OSError)):
     with writer_lock(Path(directory)):pass
 def test_invalid_ohlc_is_quarantined(self):
  d=sample(['2025-01-01 09:15','2025-01-01 09:16']);d['high'][0]=8
  f,(q,rejected)=normalize(d,META,date(2025,1,1),date(2025,1,2))
  self.assertEqual(len(f),1);self.assertEqual(q['invalid_ohlcv'],1)
  self.assertEqual(rejected.rejection_reason.iloc[0],'invalid_ohlcv')

if __name__=='__main__':unittest.main()
