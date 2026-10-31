"""Self-contained buffered requests, conservative repairs and Data Lab controls."""
from datetime import date
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

import pandas as pd
from streamlit.testing.v1 import AppTest
import test_quant_run_data as fixtures
import test_quant_data_workflows as workflows
from test_quant_lake import sample,META
from step2_build_data_lake import buffered_window,fetch_candles,normalize,Builder
from quant_data_sync import build_plan,run_sync,repair_dataset_gaps
from quant_repairs import recover_negative,fill_isolated
from quant_backtester import ResearchData,ENGINE_VERSION,Config,LEGACY_STRATEGIES as STRATEGIES,run_backtest,simulate_trade
from quant_option_model import MissingObservedPrice
from dataclasses import replace
from quant_data_quality import inventory,report_state,run_quality_check
from quant_data_lab import inventory_rows,ROWS
from quant_run_data import select_contracts
from quant_coverage import freshness


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RunDataTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.root=self.f.root

    def test_single_day_buffer_is_seven_days_and_exact_storage_filter(self):
        a,b=date(2026,10,23),date(2026,10,24)
        lo,hi=buffered_window(a,b);self.assertEqual((lo,hi),(date(2026,10,17),b))
        raw=sample(['2026-10-17 09:15','2026-10-23 09:15','2026-10-24 09:15'])
        for option in (False,True):
            api=Mock();api.post.return_value={'ce':raw} if option else raw
            frame,queries=fetch_candles(api,'/charts/rollingoption' if option else '/charts/intraday',{},dict(META,**({'option_type':'CE'} if option else {})),a,b)
            self.assertEqual(frame['timestamp'],[raw['timestamp'][1]])
            self.assertEqual(api.post.call_args.args[1]['toDate'],'2026-10-24')
            self.assertEqual(queries,[dict(fromDate='2026-10-17',toDate='2026-10-24',end_exclusive=True)])

    def test_option_buffer_respects_maximum_and_no_double_count(self):
        raw=sample([f'2026-10-{d:02d} 09:15' for d in range(1,25)])
        api=Mock();api.post.return_value={'ce':raw}
        result,queries=fetch_candles(api,'/charts/rollingoption',{},dict(META,option_type='CE'),date(2026,10,1),date(2026,10,24))
        self.assertEqual(len(result['timestamp']),23);self.assertEqual(len(set(result['timestamp'])),23)
        self.assertTrue(all(7<=(pd.Timestamp(q['toDate'])-pd.Timestamp(q['fromDate'])).days<=30 for q in queries))

    def test_latest_plan_skips_expired_and_historical_stops_at_expiry(self):
        self.f.store('NATURALGAS_2026-10-27_570750','2026-10-22',mcx=True)
        self.f.store('NATURALGAS_2026-11-24_999','2026-10-22',mcx=True)
        plan=build_plan(self.root,'NATURALGAS',latest=True,now='2026-10-29 23:59+05:30')
        self.assertTrue(plan['jobs']);self.assertTrue(all('2026-11-24' in j['meta']['instrument'] for j in plan['jobs']))
        plan=build_plan(self.root,'NATURALGAS','2026-10-23','2026-10-29',now='2026-10-30 12:00+05:30')
        self.assertTrue(all(j['end']<='2026-10-28' for j in plan['jobs'] if '2026-10-27' in j['meta']['instrument']))
        self.f.store('NATURALGAS_2026-11-24_999','2026-10-28',mcx=True)
        self.assertEqual(freshness(self.root,inventory(self.root),'NATURALGAS','2026-10-29 23:59+05:30')['last'],date(2026,10,28))

    def test_direct_builder_never_requests_after_future_expiry(self):
        with patch('step2_build_data_lake.API') as api:
            builder=Builder(self.root,date(2025,1,1),date(2026,10,30))
            meta=dict(META,instrument='NATURALGAS_2026-10-27_1',exchange_segment='MCX_COMM',contract_expiry='2026-10-27')
            record=builder.chunk('spot_1m',meta,date(2026,10,28),date(2026,10,30),'/charts/intraday',{})
            api.return_value.post.assert_not_called();self.assertEqual(record['status'],'unavailable')
            api.return_value.post.return_value=sample(['2026-10-27 09:15','2026-10-28 09:15'])
            record=builder.chunk('spot_1m',meta,date(2026,10,27),date(2026,10,30),'/charts/intraday',{})
            self.assertEqual(api.return_value.post.call_args.args[1]['toDate'],'2026-10-28')
            self.assertEqual(record['rows'],1);self.assertEqual(record['end'],'2026-10-28')

    def test_selection_never_trades_expired_prior_volume_winner(self):
        daily=pd.DataFrame([dict(date='2026-10-27',instrument='NATURALGAS_2026-10-27_1',volume=1000,invalid=False),dict(date='2026-10-27',instrument='NATURALGAS_2026-11-24_2',volume=10,invalid=False)])
        self.assertEqual(select_contracts(daily,['2026-10-28'])['2026-10-28'][0],'NATURALGAS_2026-11-24_2')

    def test_dh905_isolated_to_stream_healthy_spot_still_downloads(self):
        helper=workflows.WorkflowTests();helper.f=self.f;helper.root=self.root
        seen=[]
        def download(job,stage):
            seen.append(job['meta']['instrument'])
            if job['meta'].get('option_type')=='CE':return dict(status='failed',error='DH-905')
            return helper.download(job,stage)
        result=run_sync(self.root,'NIFTY','2026-10-23','2026-10-23',now='2026-10-24 12:00+05:30',downloader=download,check_credentials=False)
        self.assertIn('NIFTY',seen);self.assertTrue(any('_PE_' in n for n in seen))
        self.assertEqual(result['committed'],4);self.assertEqual(len(result['failures']),3)

    def test_ingestion_negative_volume_is_restored_and_raw_rejection_retained(self):
        raw=sample(['2025-09-02 09:15','2025-09-02 09:16']);raw['volume'][1]=-5
        frame,(quality,rejected)=normalize(raw,META,date(2025,9,2),date(2025,9,3))
        self.assertEqual(len(frame),2);self.assertEqual(frame.volume.tolist(),[5,0])
        self.assertEqual(quality['invalid_ohlcv'],1);self.assertEqual(len(rejected),1)
        self.assertEqual(quality['repairs'][0]['original_volume'],-5)

    def test_negative_recovery_refuses_invalid_prices_and_duplicate_conflicts(self):
        frame,_=self.f.store('NIFTY','2025-09-02',missing=['12:00'])
        row=frame.iloc[0].to_dict();row.update(timestamp=pd.Timestamp('2025-09-02 12:00',tz='Asia/Kolkata'),volume=-1,rejection_reason='invalid_ohlcv')
        for changes in ({'high':1},{'low':0},{'oi':-1},{'rejection_reason':'conflicting_duplicate_timestamp'}):
            restored,events=recover_negative(frame,pd.DataFrame([dict(row,**changes)]));self.assertFalse(events);self.assertEqual(len(restored),374)

    def test_one_to_three_holes_flat_volume_zero_with_provenance(self):
        for missing in (['12:00'],['12:00','12:01'],['12:00','12:01','12:02']):
            frame,_=self.f.store('NIFTY','2025-09-02',missing=missing)
            filled,events=fill_isolated(frame,pd.DataFrame(),'NIFTY')
            self.assertEqual(len(filled),375);self.assertEqual(len(events),len(missing))
            patched=filled[filled.timestamp.dt.strftime('%H:%M').isin(missing)]
            self.assertTrue(patched[['open','high','low','close']].eq(101.).all().all());self.assertTrue(patched.volume.eq(0).all())
            self.assertEqual(set(patched.candle_source),{'synthetic_ffill'})

    def test_no_edge_long_sparse_or_mcx_fills(self):
        for missing in (['09:15'],['15:29'],['12:00','12:01','12:02','12:03'],[f'12:{m:02d}' for m in range(10)]):
            frame,_=self.f.store('NIFTY','2025-09-02',missing=missing)
            self.assertFalse(fill_isolated(frame,pd.DataFrame(),'NIFTY')[1])
        frame,_=self.f.store('NIFTY','2025-09-02',missing=['12:00'])
        self.assertFalse(fill_isolated(frame,pd.DataFrame(),'CRUDEOIL')[1])

    def test_no_filling_conflicting_quarantine_or_changing_rolling_strike(self):
        frame,_=self.f.store('NIFTY_CE_ATM_WEEK1','2025-09-02',missing=['12:00'])
        rejected=pd.DataFrame([dict(timestamp=pd.Timestamp('2025-09-02 12:00',tz='Asia/Kolkata'),rejection_reason='conflicting_duplicate_timestamp')])
        self.assertFalse(fill_isolated(frame,rejected,'NIFTY')[1])
        frame.loc[frame.timestamp.dt.strftime('%H:%M').eq('12:01'),'strike']=25000.
        self.assertFalse(fill_isolated(frame,pd.DataFrame(),'NIFTY')[1])

    def negative_fixture(self):
        self.f.index(missing=['15:04'])
        r=next(r for r in self.f.records.values() if r['instrument']=='NIFTY_CE_ATM+1_WEEK1')
        frame=pd.read_parquet(self.root/r['path']);row=frame.iloc[0].to_dict()
        row.update(timestamp=pd.Timestamp('2025-09-02 15:04',tz='Asia/Kolkata'),volume=-10,rejection_reason='invalid_ohlcv')
        q=self.root/'quarantine.parquet';pd.DataFrame([row]).to_parquet(q,index=False)
        r.update(quarantine_path=q.name,quality=dict(invalid_ohlcv=1));self.f.commit_manifest()
        return r,q

    def test_repair_transaction_originals_quarantine_audit_and_eligibility(self):
        r,q=self.negative_fixture();original=(self.root/r['path']).read_bytes();raw=q.read_bytes();self.f.ensure_audit()
        self.assertFalse(ResearchData('NIFTY','2025-09-02','2025-09-03',self.root).days)
        result=repair_dataset_gaps(self.root,'NIFTY OPTIONS',offline=True)
        self.assertEqual((result['restored'],result['synthetic']),(1,0));self.assertTrue(result['audit_current'])
        self.assertEqual(q.read_bytes(),raw);self.assertEqual(next(self.root.glob('repair_history/*/original.parquet')).read_bytes(),original)
        data=ResearchData('NIFTY','2025-09-02','2025-09-03',self.root)
        self.assertIn('2025-09-02',data.days);self.assertTrue(data.days['2025-09-02']['repaired_inputs'])
        self.assertEqual(repair_dataset_gaps(self.root,'NIFTY',offline=True)['partitions_changed'],0)

    def test_synthetic_run_is_explicit_and_old_engine_cache_not_reused(self):
        self.f.index(missing=['14:59']);self.f.ensure_audit()
        result=repair_dataset_gaps(self.root,'NIFTY',offline=True);self.assertEqual(result['synthetic'],1)
        data=ResearchData('NIFTY','2025-09-02','2025-09-03',self.root)
        result=run_backtest(Config(instrument='NIFTY',strategy=STRATEGIES[0],start='2025-09-02',end='2025-09-03'),data,persist=False)
        self.assertEqual(result['status'],'repaired_inputs');self.assertFalse(result['verified_price_eligible']);self.assertEqual(len(result['repaired_inputs']),1)
        self.assertEqual(ENGINE_VERSION,'5.4.0')
        config=Config(instrument='NIFTY',strategy=STRATEGIES[0],start='2025-09-02',end='2025-09-03')
        with self.assertRaisesRegex(MissingObservedPrice,'synthetic repaired inputs'):
            run_backtest(replace(config,price_policy='strict_observed'),data,persist=False)
        trade,trace=simulate_trade(data,config,'2025-09-02',(899,'CE',24050.,None,0),'repair_test')
        self.assertEqual(trace[0]['price_source'],'synthetic_ffill');self.assertTrue(trade['used_modeled_prices'])
        self.assertEqual(trade['verification_status'],'repaired_inputs')

    def test_provider_retry_precedes_synthetic_fallback(self):
        self.f.index(missing=['14:59']);self.f.ensure_audit();api=Mock()
        raw=sample(['2025-09-02 14:59']);raw.update(oi=[1000],iv=[20],spot=[24000],strike=[24050])
        api.post.return_value={'ce':raw}
        result=repair_dataset_gaps(self.root,'NIFTY OPTIONS',api=api)
        self.assertEqual((result['refetched'],result['synthetic']),(1,0));api.post.assert_called_once()
        self.assertTrue(report_state(self.root)['current'])

    def test_provider_empty_retry_keeps_explicit_synthetic_fallback(self):
        self.f.index(missing=['14:59']);self.f.ensure_audit();api=Mock()
        api.post.return_value={'ce':sample([])}
        result=repair_dataset_gaps(self.root,'NIFTY OPTIONS',api=api)
        self.assertEqual((result['refetched'],result['synthetic']),(0,1));api.post.assert_called_once()
        self.assertFalse(result['failures']);self.assertTrue(result['audit_current'])

    def test_repair_refuses_changed_partition(self):
        _,r=self.f.store('NIFTY','2025-09-02',missing=['12:00']);(self.root/r['path']).write_bytes(b'corrupt')
        before=(self.root/'ingestion_manifest.json').read_bytes()
        with self.assertRaises(ValueError):repair_dataset_gaps(self.root,'NIFTY',offline=True)
        self.assertEqual((self.root/'ingestion_manifest.json').read_bytes(),before)

    def test_repair_manifest_failure_rolls_back_partition_and_raw_quarantine(self):
        r,q=self.negative_fixture()
        manifest=(self.root/'ingestion_manifest.json').read_bytes();original=(self.root/r['path']).read_bytes();raw=q.read_bytes()
        with patch('step2_build_data_lake.atomic_json',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):repair_dataset_gaps(self.root,'NIFTY',offline=True)
        self.assertEqual((self.root/'ingestion_manifest.json').read_bytes(),manifest)
        self.assertEqual((self.root/r['path']).read_bytes(),original);self.assertEqual(q.read_bytes(),raw)

    def test_full_special_sessions_pass_same_windows_partial_weekend_does_not(self):
        names=['NIFTY']+[f'NIFTY_{s}_{m}_WEEK1' for s in ('CE','PE') for m in ('ATM','ATM-1','ATM+1')]
        for day in ('2025-02-01','2026-02-01'):
            for name in names:self.f.store(name,day)
            data=self.f.load('NIFTY',day,str(pd.Timestamp(day).date()+pd.Timedelta(days=1)))
            self.assertIn(day,data.days);self.assertTrue(any('full-length' in e['problem'] for e in data.eligibility[0]['evidence']))
        self.f.store('NIFTY','2025-02-01',missing=['09:15'])
        self.assertFalse(self.f.load('NIFTY','2025-02-01','2025-02-02').days)
        self.f.store('NIFTY','2026-02-07',missing=[f'09:{m:02d}' for m in range(16,26)])
        self.assertFalse(self.f.load('NIFTY','2026-02-07','2026-02-08').days)

    def test_inventory_has_eight_rows_without_daily_double_counting(self):
        self.f.index();self.f.store('CRUDEOIL_2026-11-01_123','2026-10-22',mcx=True)
        rows=inventory_rows(inventory(self.root));self.assertEqual(tuple(r['label'] for r in rows),ROWS)
        self.assertEqual(rows[0]['rows'],375);self.assertEqual(rows[1]['rows'],2250)
        self.assertEqual(len(rows[1]['sessions']),1)

    def test_data_lab_native_actions_and_quality_repair_log_render(self):
        self.f.index();self.f.ensure_audit()
        with patch('quant_lake.ROOT',self.root),patch('quant_backtester.OUT',self.root/'backtests'):
            app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=45).run()
            app.radio(key='page').set_value('Data Lake').run();self.assertFalse(app.exception)
            self.assertEqual(len([b for b in app.button if str(b.key).startswith('lab_repair_')]),9)
            app.button(key='lab_check_health').click().run();self.assertFalse(app.exception)
            self.assertTrue(any('100.0% Clean ✓</span>' in w.value for w in app.markdown))
            with patch('quant_data_lab.repair_dataset_gaps',return_value=dict(restored=0,refetched=0,synthetic=0,partitions_changed=0,audit_current=True,failures=[])) as repair:
                app.button(key='lab_repair_NIFTY OPTIONS').click().run();repair.assert_called_once()
            app.button(key='lab_custom').click().run();self.assertFalse(app.exception)
            app.button(key='lab_backfill_SENSEX OPTIONS').click().run();self.assertFalse(app.exception)
            self.assertEqual(app.selectbox(key='sync_selection').value,'SENSEX OPTIONS')
            app.button(key='lab_close').click().run();self.assertFalse(app.exception)
            app.radio(key='page').set_value('Data Quality').run();self.assertFalse(app.exception)
            self.assertTrue(any(e.label=='Quarantine / Repair Log' for e in app.expander))
            self.assertFalse(any('lab_row_' in str(b.key) for b in app.button))


if __name__=='__main__':unittest.main()
