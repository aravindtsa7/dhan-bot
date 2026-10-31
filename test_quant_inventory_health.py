"""Offline, self-contained row health, audit reconciliation and sync completion."""
from datetime import date
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

import pandas as pd
from streamlit.testing.v1 import AppTest
import test_quant_run_data as fixtures
import test_quant_data_workflows as workflows
import test_quant_repairs as repairs
from quant_data_quality import inventory,report_state,run_quality_check
from quant_inventory_health import inventory_health,clean_label,health_summary,valid_metric
from quant_data_sync import run_sync,build_plan,repair_dataset_gaps
from quant_backtester import ResearchData


class InventoryHealthTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RunDataTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.root=self.f.root

    def day(self,day,*,spot=True,options=True,missing=()):
        if spot:self.f.store('NIFTY',day)
        if options:
            for side in ('CE','PE'):
                for offset in ('ATM','ATM-1','ATM+1'):
                    self.f.store(f'NIFTY_{side}_{offset}_WEEK1',day,missing=missing if (side,offset)==('CE','ATM+1') else ())

    def health(self):
        self.f.ensure_audit();return inventory_health(self.root)

    def test_spot_options_have_independent_dates_and_combined_summary(self):
        self.day('2026-10-28');self.day('2026-10-29',options=False)
        health=self.health();spot=health['rows']['NIFTY SPOT'];option=health['rows']['NIFTY OPTIONS']
        self.assertEqual((spot['stored'],spot['ready'],spot['percent']),(2,2,100))
        self.assertEqual((option['stored'],option['ready'],option['percent']),(1,1,100))
        self.assertEqual((health_summary(health,inventory(self.root))['stored'],health_summary(health,inventory(self.root))['ready']),(2,1))
        self.assertEqual(spot['sessions'][-1]['date'],'2026-10-29');self.assertEqual(option['sessions'][-1]['date'],'2026-10-28')

    def test_bar_denominator_counts_six_option_streams_and_unique_required_minutes(self):
        self.day('2025-09-02',missing=['12:34']);health=self.health()
        row=health['rows']['NIFTY OPTIONS']
        self.assertEqual((row['ready'],row['excluded'],row['expected_bars'],row['valid_bars']),(0,1,2196,2195))
        self.assertAlmostEqual(row['bars_percent'],100*2195/2196)
        self.assertEqual(health['rows']['NIFTY SPOT']['percent'],100)
        self.assertNotIn('100.0%',clean_label(row))

    def test_banknifty_spot_is_measured_without_options_or_backtester(self):
        self.f.store('BANKNIFTY','2026-10-28',missing=['15:24'])
        self.f.store('BANKNIFTY','2026-10-29',missing=['15:20'])
        row=self.health()['rows']['BANKNIFTY SPOT']
        self.assertEqual((row['stored'],row['ready'],row['expected_bars'],row['valid_bars']),(2,1,732,731))
        self.assertEqual(row['percent'],50);self.assertEqual(clean_label(row,spot=True),'50.0% Clean ⚠️')

    def test_required_warmup_gap_and_outside_gap_remain_distinct(self):
        self.day('2025-09-02',missing=['15:24']);self.day('2025-09-03',missing=['09:16'])
        health=self.health();self.assertEqual(health['rows']['NIFTY OPTIONS']['ready'],1)
        data=ResearchData('NIFTY','2025-09-02','2025-09-04',self.root)
        self.assertEqual(set(data.days),{'2025-09-02'})
        self.assertEqual([s['date'] for s in health['rows']['NIFTY OPTIONS']['sessions'] if s['ready']],list(data.days))

    def test_special_session_uses_underlying_evidence_and_preserves_outside_window_rule(self):
        self.day('2025-02-01',missing=[f'15:{m}' for m in range(21,30)])
        health=self.health()
        self.assertEqual(health['rows']['NIFTY OPTIONS']['percent'],100)
        self.assertIn('2025-02-01',ResearchData('NIFTY','2025-02-01','2025-02-02',self.root).days)

    def test_nonfinite_stored_input_remains_a_blocker_without_breaking_report_json(self):
        self.f.store('BANKNIFTY','2026-10-28',change=lambda f:f.__setitem__('volume',float('inf')))
        health=self.health()
        self.assertTrue(health_summary(health,inventory(self.root))['error'])
        self.assertEqual(health['rows']['BANKNIFTY SPOT']['ready'],0)
        self.assertTrue(any(f['severity']=='Error' for f in report_state(self.root)['report']['findings']))

    def test_cross_stream_conflict_updates_both_affected_streams_and_bars(self):
        self.day('2025-09-02')
        def change(frame):
            at=frame.timestamp.dt.strftime('%H:%M').eq('12:00');frame.loc[at,'strike']=24000;frame.loc[at,'close']=100
        self.f.store('NIFTY_CE_ATM+1_WEEK1','2025-09-02',change=change)
        health=self.health();row=health['rows']['NIFTY OPTIONS']
        self.assertEqual((row['ready'],row['valid_bars']),(0,2194))
        for name in ('NIFTY_CE_ATM_WEEK1','NIFTY_CE_ATM+1_WEEK1'):
            self.assertEqual(health['streams']['options_rolling_1m|'+name]['validity'],'Review needed')
        self.assertFalse(ResearchData('NIFTY','2025-09-02','2025-09-03',self.root).days)

    def test_repaired_and_outside_rejected_rows_pass_required_validity(self):
        helper=repairs.RepairTests();helper.f=self.f;helper.root=self.root
        record,q=helper.negative_fixture();self.f.ensure_audit()
        before=inventory_health(self.root)['streams']['options_rolling_1m|NIFTY_CE_ATM+1_WEEK1']
        self.assertEqual(before['validity'],'Review needed')
        repair_dataset_gaps(self.root,'NIFTY',offline=True)
        report=json.loads((self.root/'quality_report.json').read_text())
        result=report['streams']['options_rolling_1m|NIFTY_CE_ATM+1_WEEK1']
        self.assertEqual(result['dimensions']['validity'],'Checks passed');self.assertEqual(result['required_window']['ready'],1)
        self.assertEqual(result['required_window']['repaired'],1)
        self.assertTrue(q.exists());self.assertTrue(any(f['problem']=='Invalid input candles rejected' for f in report['findings']))
        self.f.store('BANKNIFTY','2026-10-28',quality=dict(outside_market_hours=4,exact_duplicates=2));self.f.ensure_audit()
        self.assertEqual(json.loads((self.root/'quality_report.json').read_text())['streams']['spot_1m|BANKNIFTY']['dimensions']['validity'],'Checks passed')

    def test_unknown_calendar_remains_separate_from_required_window_percent(self):
        self.day('2025-09-02');health=self.health();report=report_state(self.root)['report']
        self.assertEqual(health['rows']['NIFTY SPOT']['percent'],100)
        self.assertEqual(report['streams']['spot_1m|NIFTY']['dimensions']['coverage'],'Review needed')
        self.assertIn('Unverified',report['streams']['options_rolling_1m|NIFTY_CE_ATM_WEEK1']['identity'])

    def test_persisted_cache_is_reused_without_candle_reads_after_reload(self):
        self.day('2025-09-02');first=self.health()
        self.assertTrue((self.root/'inventory_health.json').exists())
        with patch('quant_inventory_health.measure_stream',side_effect=AssertionError('Unexpected rescan')),patch('pandas.read_parquet',side_effect=AssertionError('Unexpected candle read')):
            second=inventory_health(self.root)
        self.assertEqual(second['key'],first['key']);self.assertEqual(second['rows'],first['rows'])

    def test_manifest_audit_and_calendar_changes_invalidate_cached_percentages(self):
        self.day('2025-09-02');first=self.health()
        self.day('2025-09-03',options=False)
        stale=inventory_health(self.root);self.assertFalse(stale['current']);self.assertIsNone(stale['rows']['NIFTY SPOT']['percent'])
        fresh=self.health();self.assertNotEqual(first['key'],fresh['key']);self.assertEqual(fresh['rows']['NIFTY SPOT']['stored'],2)
        (self.root/'session_calendar.json').write_text(json.dumps(dict(sessions=[])))
        changed=inventory_health(self.root);self.assertFalse(changed['current']);self.assertIsNone(changed['rows']['NIFTY SPOT']['percent'])

    def test_corrupt_cache_cannot_fabricate_clean_percentages(self):
        self.day('2025-09-02',missing=['12:34']);health=self.health();health['rows']['NIFTY OPTIONS']['percent']=100
        (self.root/'inventory_health.json').write_text(json.dumps(health))
        corrected=inventory_health(self.root);self.assertEqual(corrected['rows']['NIFTY OPTIONS']['percent'],0)
        self.assertTrue(valid_metric(corrected['rows']['NIFTY OPTIONS']))

    def test_quarantine_change_invalidates_required_evidence_cache(self):
        helper=repairs.RepairTests();helper.f=self.f;helper.root=self.root
        record,q=helper.negative_fixture();first=self.health()
        q.write_bytes(b'corrupt quarantine')
        changed=inventory_health(self.root)
        self.assertNotEqual(first['key'],changed['key'])
        metric=changed['streams']['options_rolling_1m|NIFTY_CE_ATM+1_WEEK1']
        self.assertEqual(metric['validity'],'Review needed');self.assertFalse(metric['sessions'][0]['ready'])

    def test_no_audit_empty_lake_and_corrupt_partitions_never_show_clean(self):
        for phase in ('empty','no audit','corrupt'):
            if phase=='no audit':self.day('2025-09-02')
            if phase=='corrupt':
                r=next(iter(self.f.records.values()));(self.root/r['path']).write_bytes(b'bad parquet');self.f.ensure_audit()
            health=inventory_health(self.root);metric=health['rows']['NIFTY SPOT']
            self.assertIsNone(metric['percent']);self.assertNotIn('Clean ✓',clean_label(metric));self.assertNotEqual(clean_label(metric),'Not checked')
            self.assertTrue(health_summary(health,inventory(self.root))['error'])

    def test_mcx_inventory_continuity_is_separate_from_engine_contract_selection(self):
        self.f.mcx();health=self.health();metric=health['rows']['CRUDEOIL FUT']
        self.assertEqual((metric['stored'],metric['ready'],metric['excluded']),(3,3,0))
        self.assertEqual(metric['expected_bars'],870*3*2)
        strict=health['engine_rows']['CRUDEOIL FUT']
        self.assertEqual((strict['ready'],strict['excluded']),(2,1))
        self.assertEqual(strict['sessions'][1]['selected_contract'],'CRUDEOIL_A')
        self.assertEqual(strict['expected_bars'],856*3)

    def test_mcx_partial_listing_and_evening_spans_do_not_invent_opening_gaps(self):
        for day,start in [('2026-09-04','10:24'),('2026-09-07','16:30')]:
            full,_=self.f.store('CRUDEOIL_A',day,mcx=True)
            missing=full.loc[full.timestamp.dt.strftime('%H:%M').lt(start),'timestamp'].dt.strftime('%H:%M').tolist()
            self.f.store('CRUDEOIL_A',day,mcx=True,missing=missing)
        health=self.health();metric=health['rows']['CRUDEOIL FUT']
        self.assertEqual((metric['stored'],metric['ready'],metric['bars_percent']),(2,2,100))
        self.assertEqual(health['engine_rows']['CRUDEOIL FUT']['ready'],0)
        self.assertEqual(health_summary(health,inventory(self.root))['ready'],0)

    def test_mcx_real_interior_gaps_and_one_candle_are_not_clean(self):
        self.f.store('CRUDEOIL_A','2026-09-04',mcx=True,missing=['17:10'])
        full,_=self.f.store('CRUDEOIL_A','2026-09-07',mcx=True)
        missing=full.timestamp.dt.strftime('%H:%M').iloc[1:].tolist()
        self.f.store('CRUDEOIL_A','2026-09-07',mcx=True,missing=missing)
        metric=self.health()['rows']['CRUDEOIL FUT']
        self.assertEqual((metric['stored'],metric['ready'],metric['excluded']),(2,0,2))
        self.assertLess(metric['bars_percent'],100)

    def test_global_card_averages_independent_rows_and_counts_fresh_dataset_tails(self):
        from quant_data_lab import inventory_rows,inventory_summary
        self.day('2026-10-28',missing=['12:34']);self.day('2026-10-29',options=False)
        health=self.health();inv=inventory(self.root)
        title,note=inventory_summary(self.root,inv,inventory_rows(inv),health,now='2026-10-30 10:00+05:30')
        self.assertEqual(title,'50.0% Clean ⚠️')  # 100% spot and 0% options, equal weights.
        self.assertEqual(note,'Latest stored: 2026-10-29 • 1/8 datasets at latest')

    def test_rolling_sync_payload_exact_recent_window_and_failed_query_trace(self):
        from quant_data_sync import templates
        from step2_build_data_lake import Builder
        from step1_health_check import CheckError
        spec=templates({'chunks':{}},('NIFTY',))['options_rolling_1m|NIFTY_CE_ATM_WEEK1']
        self.assertEqual(spec['payload']['interval'],1)
        self.assertEqual(spec['payload']['drvOptionType'],'CALL')
        with patch('step2_build_data_lake.API') as api:
            api.return_value.post.side_effect=CheckError('DH-905')
            builder=Builder(self.root,date(2025,1,1),date(2026,10,30))
            record=builder.chunk(spec['dataset'],spec['meta'],date(2026,10,23),date(2026,10,30),spec['endpoint'],spec['payload'])
        query=api.return_value.post.call_args.args[1]
        self.assertEqual((query['fromDate'],query['toDate']),('2026-10-22','2026-10-30'))
        self.assertEqual(record['query_windows'],[dict(fromDate='2026-10-22',toDate='2026-10-30',end_exclusive=True)])
        self.assertEqual(record['error'],'DH-905')

    def test_provider_description_is_retained_safely_for_http_and_status_errors(self):
        import threading
        from step2_build_data_lake import API
        from step1_health_check import CheckError
        for status in (200,400):
            api=object.__new__(API);api.rate_lock=threading.Lock();api.last=0;api.calls=0
            api.session=Mock(headers={'access-token':'private-fixture-token','client-id':'private-fixture-client'})
            response=Mock(status_code=status,ok=status==200)
            response.json.return_value=dict(status='failed',errorCode='DH-905',errorMessage='Recent expiry not available; private-fixture-token private-fixture-client https://example.test/?token=hidden')
            # Simulate a refreshed token held only in headers, not os.environ.
            with patch('step2_build_data_lake.requests.post',return_value=response):
                with self.assertRaises(CheckError) as caught:api.post('/charts/rollingoption',{})
            self.assertEqual(caught.exception.code,'DH-905')
            self.assertIn('Recent expiry not available',caught.exception.provider_message)
            self.assertNotIn('private-fixture-token',caught.exception.provider_message)
            self.assertNotIn('private-fixture-client',caught.exception.provider_message)
            self.assertNotIn('example.test',caught.exception.provider_message)

    def test_provider_description_and_attempted_bounds_reach_sync_results_without_aborting(self):
        self.day('2026-10-28');self.f.ensure_audit()
        def download(job,stage):
            if job['dataset']=='options_rolling_1m':
                return dict(status='failed',error='DH-905',provider_message='Recent expired contracts not available',
                            query_windows=[dict(fromDate='2026-10-23',toDate='2026-10-30',end_exclusive=True)])
            return self.download(job,stage)
        result=run_sync(self.root,'NIFTY',latest=True,now='2026-10-30 10:00+05:30',downloader=download,check_credentials=False)
        self.assertEqual(result['attempted'],7);self.assertEqual(result['committed'],1)
        failed=[o for o in result['outcomes'] if o['status']=='failed']
        self.assertEqual(len(failed),6)
        self.assertTrue(all(o['provider_message']=='Recent expired contracts not available' and o['query_windows'] for o in failed))
        self.assertTrue(all('DH-905 — Recent expired contracts not available' in f for f in result['failures']))
        saved=json.loads((self.root/result['outcome_log']).read_text())
        self.assertEqual(saved['outcomes'],result['outcomes'])

    def test_saved_provider_failures_survive_ui_reload_without_claiming_success(self):
        from quant_data_lab import latest_sync_evidence
        self.day('2025-09-02');self.f.ensure_audit()
        path=self.root/'sync_history'/'20261030T010000.json';path.parent.mkdir()
        message='NIFTY: DH-905 — Missing required fields, bad values for parameters etc.'
        saved=dict(kind='provider_check',checked_at='2026-10-30T01:00:00+05:30',failures=[message],outcomes=[])
        path.write_text(json.dumps(saved));self.assertEqual(latest_sync_evidence(self.root),saved)
        with patch('quant_lake.ROOT',self.root),patch('quant_backtester.OUT',self.root/'backtests'):
            app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=60).run()
            app.radio(key='page').set_value('Data Lake').run();self.assertFalse(app.exception)
            self.assertTrue(any(w.value==message for w in app.warning))
            self.assertTrue(any('Latest read-only provider check' in c.value for c in app.caption))
            self.assertFalse(any('Last sync:' in x.value for x in app.info))
        path.write_text('{broken')
        self.assertIn('unreadable',latest_sync_evidence(self.root)['evidence_error'])

    def download(self,job,stage):
        helper=workflows.WorkflowTests();helper.f=self.f;helper.root=self.root
        record=helper.download(job,stage)
        if job['dataset']=='options_rolling_1m':
            path=stage/record['path'];frame=pd.read_parquet(path)
            offset={'ATM':0,'ATM-1':-50,'ATM+1':50}[job['meta']['moneyness']]
            frame=frame.assign(oi=1000.,iv=20.,spot=24000.,strike=24000.+offset)
            frame.to_parquet(path,index=False)
            from step2_build_data_lake import digest
            record['sha256']=digest(path)
        return record

    def test_sync_all_completes_all_lagging_segments_and_refreshes_cached_health(self):
        self.day('2026-10-28');self.day('2026-10-29',options=False)
        for side in ('CE','PE'):
            for offset in ('ATM','ATM-1','ATM+1'):self.f.store(f'SENSEX_{side}_{offset}_WEEK1','2026-10-28')
        for name in ('SENSEX','BANKNIFTY'):self.f.store(name,'2026-10-28')
        for name in ('CRUDEOIL_2026-11-19_1','GOLDM_2026-11-05_2','NATURALGAS_2026-11-24_3'):self.f.store(name,'2026-10-28',mcx=True)
        before=self.health()['key'];download=Mock(side_effect=self.download)
        result=run_sync(self.root,'All Datasets',latest=True,now='2026-10-30 10:00+05:30',downloader=download,discovery=Mock(return_value={}),check_credentials=False)
        self.assertFalse(result['failures']);self.assertEqual(result['attempted'],len(result['plan']['jobs']))
        self.assertTrue(all(o['status']=='complete' for o in result['outcomes']))
        for s in inventory(self.root)['streams'].values():
            if s['interval']=='1 minute':self.assertTrue(s['last'].startswith('2026-10-29'),s['instrument'])
        self.assertNotEqual(inventory_health(self.root)['key'],before)
        self.assertTrue((self.root/result['outcome_log']).is_file());self.assertTrue(result['audit_current'])

    def test_segment_subscription_failure_does_not_abort_healthy_streams(self):
        self.day('2026-10-28');self.f.ensure_audit()
        def download(job,stage):
            if job['meta']['instrument']=='NIFTY_CE_ATM_WEEK1':return dict(status='failed',error='DH-902')
            return self.download(job,stage)
        result=run_sync(self.root,'NIFTY',latest=True,now='2026-10-30 10:00+05:30',downloader=download,check_credentials=False)
        self.assertEqual(result['committed'],6);self.assertEqual(len(result['outcomes']),7)
        self.assertTrue(any(o['instrument']=='NIFTY_PE_ATM_WEEK1' and o['status']=='complete' for o in result['outcomes']))
        self.assertFalse(any(o['status']=='not attempted' for o in result['outcomes']))

    def test_authentication_stop_records_every_unattempted_stream(self):
        self.day('2026-10-28');self.f.ensure_audit();download=Mock(return_value=dict(status='failed',error='AUTH'))
        result=run_sync(self.root,'NIFTY',latest=True,now='2026-10-30 10:00+05:30',downloader=download,check_credentials=False)
        self.assertEqual(download.call_count,1);self.assertEqual(sum(o['status']=='not attempted' for o in result['outcomes']),6)
        self.assertEqual(result['committed'],0);self.assertTrue(result['failures'])

    def test_partial_cached_tail_is_refetched_without_truncating_old_bounds(self):
        _,r=self.f.store('BANKNIFTY','2026-10-23');r['end']='2026-10-30';self.f.commit_manifest();self.f.ensure_audit()
        plan=build_plan(self.root,'BANKNIFTY SPOT',latest=True,now='2026-10-30 10:00+05:30')
        self.assertEqual(len(plan['jobs']),1)
        self.assertEqual((plan['jobs'][0]['start'],plan['jobs'][0]['end']),('2026-10-23','2026-10-30'))
        result=run_sync(self.root,'BANKNIFTY SPOT',latest=True,now='2026-10-30 10:00+05:30',downloader=self.download,check_credentials=False)
        self.assertEqual(result['rows_added'],4*375)
        self.assertEqual(inventory_health(self.root)['rows']['BANKNIFTY SPOT']['stored'],5)

    def test_opening_pages_displays_percentages_without_manual_health_check(self):
        self.day('2025-09-02');self.f.store('BANKNIFTY','2025-09-02');self.f.ensure_audit()
        with patch('quant_lake.ROOT',self.root),patch('quant_backtester.OUT',self.root/'backtests'):
            for _ in range(2):
                app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=60).run()
                app.radio(key='page').set_value('Data Lake').run();self.assertFalse(app.exception)
                labels=[m.value for m in app.markdown if 'lab-pill lab-' in m.value]
                self.assertEqual(sum('100.0% Clean ✓</span>' in s for s in labels),3)
                self.assertTrue(any('1/1 ready days · 0 gaps'==c.value for c in app.caption))
                self.assertTrue(any('100.0% 1m bars'==c.value for c in app.caption))
                self.assertFalse(any('excluded sessions' in m.value for m in app.markdown))
                self.assertFalse(any('Not checked' in s for s in labels))
                app.radio(key='page').set_value('Data Quality').run();self.assertFalse(app.exception)
                self.assertTrue(any(m.label=='Stored Session Readiness' and m.value=='100.0%' for m in app.metric))
                table=next(d.value for d in app.dataframe if 'Validity' in d.value.columns)
                self.assertTrue(table.Validity.eq('Checks passed ✓').all());self.assertTrue(table.Coverage.str.contains('100.0% Ready').all())


if __name__=='__main__':unittest.main()
