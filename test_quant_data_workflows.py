"""Offline fixture coverage, transactional sync, and native-control workflows."""
from datetime import date
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

import pandas as pd
from streamlit.testing.v1 import AppTest

import test_quant_run_data as fixtures
from quant_coverage import coverage_report,finding_bucket,freshness,session_cutoff,usability
from quant_data_sync import build_plan,run_sync
from quant_data_sync import templates
from quant_data_quality import inventory,report_state,run_quality_check


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RunDataTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.root

    def index_day(self,day,missing=False):
        for name in ['NIFTY']+[f'NIFTY_{s}_{m}_WEEK1' for s in ('CE','PE') for m in ('ATM','ATM-1','ATM+1')]:
            self.f.store(name,day,missing=['12:34'] if missing and name=='NIFTY_CE_ATM+1_WEEK1' else ())

    def bank(self):self.f.store('BANKNIFTY','2026-10-22');self.f.ensure_audit()

    def download(self,job,stage):
        stamps=[]
        for day in pd.date_range(job['start'],job['end'],inclusive='left'):
            if day.weekday()<5:
                if job['dataset']=='daily':stamps.append(pd.Timestamp(day.date(),tz='Asia/Kolkata'))
                else:stamps.extend(pd.date_range(str(day.date())+(' 09:00' if job['meta']['exchange_segment']=='MCX_COMM' else ' 09:15'),periods=870 if job['meta']['exchange_segment']=='MCX_COMM' else 375,freq='min',tz='Asia/Kolkata'))
        frame=pd.DataFrame(dict(timestamp=pd.DatetimeIndex(stamps),open=100.,high=102.,low=98.,close=101.,volume=10.,**job['meta']))
        path=stage/(job['key']+'.parquet');path.parent.mkdir(parents=True,exist_ok=True);frame.to_parquet(path,index=False)
        return dict(dataset=job['dataset'],instrument=job['meta']['instrument'],start=job['start'],end=job['end'],status='complete',path=path.relative_to(stage).as_posix(),
                    rows=len(frame),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),first=str(frame.timestamp.min()),last=str(frame.timestamp.max()),quality={},endpoint=job['endpoint'],payload=job['payload'])

    def test_coverage_counts_and_monthly_required_window_gaps(self):
        self.index_day('2025-09-01');self.index_day('2025-09-02');self.index_day('2025-09-03',True);self.f.ensure_audit()
        report=coverage_report(self.root,'NIFTY','2025-09-01','2025-09-04')
        self.assertEqual((report['stored'],report['ready'],report['excluded']),(3,2,1))
        self.assertEqual(report['absent'],['2025-09-04'])
        self.assertEqual(report['monthly'][0]['Backtest-Eligible Sessions'],2)
        self.assertEqual(report['monthly'][0]['Entire sessions absent (engine calendar)'],1)
        self.assertEqual(report['findings'][0]['stream'],'NIFTY_CE_ATM+1_WEEK1')
        self.assertEqual(report['findings'][0]['timestamps'],['2025-09-03T12:34:00+05:30'])
        self.assertIn('2 / 3',usability(report))

    def test_month_boundary_and_outside_gap_does_not_reduce_ready(self):
        self.index_day('2025-08-29');self.f.index(missing=['15:24']);self.f.ensure_audit()
        report=coverage_report(self.root,'NIFTY','2025-08-29','2025-09-02')
        self.assertEqual((report['stored'],report['ready'],report['excluded']),(2,2,0))
        self.assertEqual([r['Stored Sessions'] for r in report['monthly']],[1,1])

    def test_mcx_coverage_preserves_prior_context_and_explicit_first_exclusion(self):
        self.f.mcx();self.f.ensure_audit()
        report=coverage_report(self.root,'CRUDEOIL','2026-09-04','2026-09-07')
        self.assertEqual((report['stored'],report['ready'],report['excluded']),(2,1,1))
        self.assertIsNone(report['absent'])
        self.assertEqual(report['records'][1]['selection_session'],'2026-09-04')

    def test_unverified_audit_and_unsupported_backtester_never_claim_zero_gaps(self):
        self.f.index();report=coverage_report(self.root,'NIFTY','2025-09-02','2025-09-02')
        self.assertIsNone(report['ready']);self.assertIsNone(report['excluded'])
        self.bank();report=coverage_report(self.root,'BANKNIFTY','2026-10-22','2026-10-22')
        self.assertIsNone(report['ready']);self.assertIn('no supported backtester',usability(report))

    def test_classification_keeps_unknown_evidence_distinct(self):
        cases=[('Exact duplicate input rows removed','validity','Info','notes'),('Out-of-hours input rows rejected','validity','Warning','notes'),
               ('Manifest record is empty: No candles','availability','Warning','notes'),('Identity unverified','identity','Warning','notes'),
               ('Missing required candles','eligibility','Warning','actionable'),('Checksum mismatch.','integrity','Error','actionable'),
               ('Conflicting input rows rejected / quarantined','validity','Warning','review'),('Minute-spacing holes between observed candles; not verified missing exchange candles.','coverage','Warning','review')]
        for problem,category,severity,expected in cases:
            self.assertEqual(finding_bucket(dict(problem=problem,category=category,severity=severity)),expected)

    def test_latest_index_boundary_excludes_open_today_and_weekend(self):
        self.assertEqual(session_cutoff(self.root,'NIFTY','2026-10-29 12:00+05:30')['date'],date(2026,10,28))
        self.assertEqual(session_cutoff(self.root,'NIFTY','2026-10-29 16:00+05:30')['date'],date(2026,10,29))
        self.assertEqual(session_cutoff(self.root,'NIFTY','2026-10-25 20:00+05:30')['date'],date(2026,10,23))

    def test_mcx_freshness_does_not_invent_holiday_lag(self):
        self.f.mcx();self.f.ensure_audit();inv=inventory(self.root)
        fresh=freshness(self.root,inv,'CRUDEOIL','2026-09-09 18:00+05:30')
        self.assertFalse(fresh['verified']);self.assertIsNone(fresh['lag'])

    def test_latest_plan_resumes_each_tail_and_exclusive_end(self):
        self.bank();plan=build_plan(self.root,'BANKNIFTY Spot',latest=True,now='2026-10-23 16:00+05:30')
        self.assertEqual(len(plan['jobs']),1)
        self.assertEqual((plan['jobs'][0]['start'],plan['jobs'][0]['end']),('2026-10-23','2026-10-24'))
        self.assertEqual(plan['jobs'][0]['payload']['securityId'],'25')

    def test_historical_plan_skips_cache_and_force_preserves_full_partition_bounds(self):
        self.bank()
        self.assertEqual(build_plan(self.root,'BANKNIFTY Spot','2026-10-22','2026-10-22',now='2026-10-23 16:00+05:30')['jobs'],[])
        plan=build_plan(self.root,'BANKNIFTY Spot','2026-10-22','2026-10-22',skip=False,now='2026-10-23 16:00+05:30')
        self.assertEqual((plan['jobs'][0]['start'],plan['jobs'][0]['end']),('2026-10-22','2026-10-23'))

    def test_options_plan_preserves_underlying_ids_and_excludes_bank_options(self):
        plan=build_plan(self.root,'NIFTY Spot + Options','2026-10-22','2026-10-22',now='2026-10-23 16:00+05:30')
        self.assertEqual(len(plan['jobs']),7)
        options=[j for j in plan['jobs'] if j['dataset']=='options_rolling_1m']
        self.assertEqual(len(options),6);self.assertTrue(all(j['payload']['securityId']==13 for j in options))
        self.assertEqual(len(build_plan(self.root,'BANKNIFTY Spot','2026-10-22','2026-10-22',now='2026-10-23 16:00+05:30')['jobs']),1)

    def test_force_refresh_expands_partial_selection_without_losing_neighbor_dates(self):
        self.bank();record=next(iter(self.f.records.values()));record['end']='2026-10-24';self.f.commit_manifest();self.f.ensure_audit()
        plan=build_plan(self.root,'BANKNIFTY Spot','2026-10-23','2026-10-23',skip=False,now='2026-10-24 12:00+05:30')
        self.assertEqual([(j['start'],j['end']) for j in plan['jobs']],[('2026-10-22','2026-10-24')])
        result=run_sync(self.root,'BANKNIFTY Spot','2026-10-23','2026-10-23',skip=False,now='2026-10-24 12:00+05:30',downloader=self.download,check_credentials=False)
        self.assertTrue(result['audit_current'])
        from quant_lake import QuantDataLake
        with QuantDataLake(self.root) as lake:frame=lake.query('BANKNIFTY','2026-10-22','2026-10-24')
        self.assertEqual(len(frame),750);self.assertEqual(frame.timestamp.dt.date.nunique(),2)

    def test_mcx_master_discovery_is_run_only_and_respects_selected_product(self):
        name='CRUDEOIL_2026-11-19_573422';self.f.store(name,'2026-10-22',mcx=True);self.f.ensure_audit()
        specs=templates({'chunks':{'new':{'instrument':'CRUDEOIL_2026-12-18_999'}}},('CRUDEOIL',))
        discovery=Mock(return_value=specs);download=Mock(side_effect=self.download)
        plan=build_plan(self.root,'CRUDEOIL',latest=True,now='2026-10-24 12:00+05:30')
        discovery.assert_not_called();self.assertTrue(plan['jobs'])
        result=run_sync(self.root,'CRUDEOIL',latest=True,now='2026-10-24 12:00+05:30',downloader=download,discovery=discovery,check_credentials=False)
        discovery.assert_called_once();self.assertTrue(result['audit_current'])
        names={call.args[0]['meta']['instrument'] for call in download.call_args_list}
        self.assertIn('CRUDEOIL_2026-12-18_999',names);self.assertTrue(all(n.startswith('CRUDEOIL_') for n in names))

    def test_sync_downloads_then_audits_current_snapshot(self):
        self.bank();audit=Mock(wraps=run_quality_check);download=Mock(side_effect=self.download)
        result=run_sync(self.root,'BANKNIFTY Spot',latest=True,now='2026-10-23 16:00+05:30',downloader=download,audit=audit,check_credentials=False)
        self.assertEqual((result['committed'],result['rows_added'],result['stream_sessions_added']),(1,375,1))
        self.assertEqual(result['product_sessions_added'],1)
        self.assertTrue(result['audit_current']);self.assertTrue(report_state(self.root)['current'])
        audit.assert_called_once_with(self.root,_writer_locked=True)
        self.assertEqual(download.call_args[0][0]['start'],'2026-10-23')

    def test_historical_custom_parameters_and_audit_refresh(self):
        self.bank();download=Mock(side_effect=self.download)
        result=run_sync(self.root,'BANKNIFTY Spot','2026-10-26','2026-10-27',now='2026-10-29 16:00+05:30',downloader=download,check_credentials=False)
        self.assertEqual((download.call_args[0][0]['start'],download.call_args[0][0]['end']),('2026-10-26','2026-10-28'))
        self.assertTrue(result['audit_current']);self.assertEqual(result['stream_sessions_added'],2)

    def test_failed_or_empty_forced_replacement_preserves_old_data(self):
        self.bank();path=next(self.root.glob('BANK*.parquet'));original=path.read_bytes()
        for status in ('failed','empty','unavailable'):
            def download(job,stage):return dict(status=status)
            result=run_sync(self.root,'BANKNIFTY Spot','2026-10-22','2026-10-22',skip=False,now='2026-10-23 16:00+05:30',downloader=download,check_credentials=False)
            self.assertEqual(path.read_bytes(),original);self.assertEqual(result['committed'],0)
            self.assertTrue(result['failures']);self.assertTrue(report_state(self.root)['current'])

    def test_partial_provider_replacement_cannot_drop_previously_stored_timestamps(self):
        self.bank();original=next(self.root.glob('BANK*.parquet')).read_bytes()
        def partial(job,stage):
            record=self.download(job,stage);path=stage/record['path']
            frame=pd.read_parquet(path).iloc[:1];frame.to_parquet(path,index=False)
            record.update(rows=1,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),last=str(frame.timestamp.iloc[-1]))
            return record
        result=run_sync(self.root,'BANKNIFTY Spot','2026-10-22','2026-10-22',skip=False,now='2026-10-23 16:00+05:30',downloader=partial,check_credentials=False)
        self.assertEqual(result['committed'],0)
        self.assertTrue(any('omitted 374 previously stored timestamps' in m for m in result['failures']))
        self.assertEqual(next(self.root.glob('BANK*.parquet')).read_bytes(),original)
        self.assertTrue(result['audit_current'])

    def test_manifest_commit_failure_rolls_back_new_files(self):
        self.bank();before=(self.root/'ingestion_manifest.json').read_bytes();files=set(self.root.rglob('*.parquet'))
        with patch('step2_build_data_lake.atomic_json',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):run_sync(self.root,'BANKNIFTY Spot',latest=True,now='2026-10-23 16:00+05:30',downloader=self.download,check_credentials=False)
        self.assertEqual((self.root/'ingestion_manifest.json').read_bytes(),before)
        self.assertEqual(set(self.root.rglob('*.parquet')),files)

    def test_missing_credentials_and_future_range_fail_without_downloading(self):
        download=Mock()
        with patch.dict('os.environ',{},clear=True),patch('dotenv.dotenv_values',return_value={}):
            with self.assertRaisesRegex(ValueError,'Missing Dhan credentials'):
                run_sync(self.root,'BANKNIFTY Spot',latest=True,downloader=download)
        download.assert_not_called()
        with self.assertRaises(ValueError):build_plan(self.root,'BANKNIFTY Spot','2026-10-30','2026-10-30',now='2026-10-29 16:00+05:30')

    def test_audit_failure_never_reports_synced_data_as_verified(self):
        self.bank()
        result=run_sync(self.root,'BANKNIFTY Spot',latest=True,now='2026-10-23 16:00+05:30',downloader=self.download,audit=Mock(side_effect=OSError()),check_credentials=False)
        self.assertFalse(result['audit_current']);self.assertFalse(report_state(self.root)['current'])

    def test_network_failure_stops_remaining_jobs_and_preserves_existing_lake(self):
        self.bank();before=(self.root/'ingestion_manifest.json').read_bytes()
        download=Mock(return_value=dict(status='failed',error='NETWORK'))
        result=run_sync(self.root,'NIFTY Spot + Options','2026-10-23','2026-10-23',now='2026-10-24 12:00+05:30',downloader=download,check_credentials=False)
        self.assertEqual(download.call_count,1)
        self.assertTrue(any('Remaining requests stopped' in m for m in result['failures']))
        self.assertEqual((self.root/'ingestion_manifest.json').read_bytes(),before)
        self.assertEqual(result['committed'],0);self.assertTrue(result['audit_current'])

    def test_ui_range_selection_syncs_across_pages_and_buttons_use_mock_downloader(self):
        self.index_day('2025-09-02');self.f.ensure_audit()
        with patch('quant_lake.ROOT',self.root),patch('quant_backtester.OUT',self.root/'backtests'):
            app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=45).run()
            app.radio(key='page').set_value('Data Lake').run();app.button(key='check_coverage').click().run()
            self.assertFalse(app.exception,[e.message for e in app.exception])
            self.assertTrue(any(m.label=='Backtest-Eligible Sessions (Ready)' and m.value=='1' for m in app.metric))
            start=app.date_input(key='coverage_from').value
            app.radio(key='page').set_value('Data Quality').run()
            self.assertEqual(app.date_input(key='coverage_from').value,start)
            self.assertTrue(any('Actionable Backtest' in item.value for item in app.markdown))
            self.assertTrue(any('Handled Ingestion' in item.label for item in app.expander))
            app.radio(key='page').set_value('Data Lake').run()
            with patch('quant_data_workflows.run_sync',return_value=dict(committed=0,rows_added=0,stream_sessions_added=0,audit_current=True,failures=[])) as run:
                app.selectbox(key='sync_selection').select('BANKNIFTY Spot').run()
                app.button(key='sync_latest').click().run()
                run.assert_called_once();self.assertTrue(run.call_args.kwargs['latest'])


if __name__=='__main__':unittest.main()
