"""Self-contained data workspace fixtures. No local lake or network required."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from quant_data_quality import (inventory, report_state, group_status, filtered_groups,
    run_quality_check, stream_sessions, preview_candles, preview_export, _hash_file)
from quant_terminal import lake_snapshot


class LakeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = {'schema_version':1,'chunks':{}}

    def tearDown(self): self.temp.cleanup()

    def save(self):
        (self.root/'ingestion_manifest.json').write_text(json.dumps(self.manifest),encoding='utf-8')

    def add(self, times=('2026-01-05 09:15','2026-01-05 09:16','2026-01-05 09:17'),
            name='NIFTY',dataset='spot_1m',start='2026-01-05',end='2026-01-06',status='complete',quality=None):
        key=f'{dataset}/{name}/{start}'
        record=dict(dataset=dataset,instrument=name,start=start,end=end,status=status,
                    endpoint='/charts/rollingoption' if dataset.startswith('options_') else '/charts/intraday',
                    quality=quality or {'exact_duplicates':0,'conflicting_duplicate_rows':0,'invalid_ohlcv':0})
        if status=='complete':
            frame=pd.DataFrame(dict(timestamp=pd.to_datetime(list(times),format='mixed').tz_localize('Asia/Kolkata'),
                                    open=10.,high=12.,low=9.,close=11.,volume=5.,instrument=name))
            path=self.root/(key+'.parquet');path.parent.mkdir(parents=True,exist_ok=True);frame.to_parquet(path,index=False)
            record.update(path=path.relative_to(self.root).as_posix(),rows=len(frame),sha256=_hash_file(path),
                          first=str(frame.timestamp.min()),last=str(frame.timestamp.max()))
        elif status in ('failed','unavailable'): record['error']='DH-907' if status=='unavailable' else 'ValueError'
        self.manifest['chunks'][key]=record;self.save()
        return record

    def calendar(self,sessions=None,contracts=None):
        if sessions is None: sessions=[dict(exchange='NSE',date='2026-01-05',windows=[['09:15','09:18']],source='Fixture exchange notice')]
        (self.root/'session_calendar.json').write_text(json.dumps(dict(sessions=sessions,contracts=contracts or {})),encoding='utf-8')

    def result(self, report, name='NIFTY', dataset='spot_1m'):
        return report['streams'][dataset+'|'+name]


class QualityTests(LakeFixture):
    def test_empty_lake_and_no_audit_are_distinct(self):
        self.assertEqual(report_state(self.root)['status'],'No data')
        report=run_quality_check(self.root)
        self.assertEqual(report['streams'],{})
        self.assertEqual(report_state(self.root)['status'],'No data')
        self.add()
        (self.root/'quality_report.json').unlink()
        self.assertEqual(report_state(self.root)['status'],'Not checked')
        self.assertIsNone(report_state(self.root)['checked_at'])

    def test_current_checks_and_outdated_manifest(self):
        self.add();self.calendar();run_quality_check(self.root)
        self.assertEqual(report_state(self.root)['status'],'Checks passed')
        self.manifest['requested_end_exclusive']='2026-01-07';self.save()
        inv=inventory(self.root);state=report_state(self.root,inv)
        self.assertEqual(state['status'],'Audit outdated');self.assertFalse(state['current'])
        self.assertEqual(group_status(inv['groups'][0],inv,state),'Audit outdated')

    def test_one_candle_never_passes_unknown_coverage(self):
        self.add(times=['2026-01-05 09:16'])
        result=self.result(run_quality_check(self.root))
        self.assertEqual(result['status'],'Review needed')
        self.assertIsNone(result['sessions'][0]['expected'])
        self.assertIsNone(result['sessions'][0]['opening_missing'])
        self.assertEqual(lake_snapshot(self.root)['coverage'][0]['status'],'Review needed')

    def test_internal_opening_closing_and_entire_session_gaps(self):
        self.add(times=['2026-01-05 09:16','2026-01-05 09:18'],end='2026-01-07')
        self.calendar([dict(exchange='NSE',date=d,windows=[['09:15','09:21']],source='Fixture') for d in ('2026-01-05','2026-01-06')])
        sessions=self.result(run_quality_check(self.root))['sessions']
        self.assertEqual((sessions[0]['internal_missing'],sessions[0]['opening_missing'],sessions[0]['closing_missing']),(1,1,2))
        self.assertEqual(sessions[1]['absent_sessions'],1)
        self.assertEqual(sessions[1]['expected'],6)

    def test_unknown_calendar_does_not_convert_missing_measurements_to_zero(self):
        self.add(times=['2026-01-05 09:15','2026-01-05 09:17'])
        result=self.result(run_quality_check(self.root))
        row=result['sessions'][0]
        self.assertIsNone(row['internal_missing']);self.assertIsNone(row['absent_sessions'])
        self.assertEqual(row['observed_minute_holes'],1)
        self.assertEqual(result['dimensions']['integrity'],'Checks passed')
        self.assertEqual(result['dimensions']['coverage'],'Review needed')

    def test_holiday_and_special_session_use_explicit_windows(self):
        self.add(times=['2026-01-06 18:00','2026-01-06 18:01'],end='2026-01-07')
        self.calendar([dict(exchange='NSE',date='2026-01-05',windows=[],source='Holiday fixture'),
                       dict(exchange='NSE',date='2026-01-06',windows=[['18:00','18:02']],source='Special session fixture')])
        result=self.result(run_quality_check(self.root))
        self.assertEqual(result['status'],'Checks passed')
        self.assertEqual([r['expected'] for r in result['sessions']],[0,2])
        self.assertEqual(sum(r['absent_sessions'] for r in result['sessions']),0)

    def test_mcx_schedule_changes_and_contract_applicability(self):
        name='CRUDEOIL_2026-12-01_123'
        self.add(times=['2026-01-06 23:54'],name=name,start='2026-01-05',end='2026-01-07')
        self.calendar([dict(exchange='MCX',date='2026-01-06',windows=[['09:00','23:55']],source='Winter schedule fixture')],
                      {name:dict(start='2026-01-06',end_exclusive='2026-01-07',source='Contract listing fixture')})
        row=self.result(run_quality_check(self.root),name)['sessions'][0]
        self.assertEqual(row['date'],'2026-01-06');self.assertEqual(row['expected'],895)
        self.assertEqual(row['opening_missing'],894)
        self.calendar([dict(exchange='MCX',date='2026-01-06',windows=[['09:00','23:30']],source='Seasonal schedule fixture')],
                      {name:dict(start='2026-01-06',end_exclusive='2026-01-07',source='Contract listing fixture')})
        seasonal=self.result(run_quality_check(self.root),name)['sessions'][0]
        self.assertEqual(seasonal['expected'],870)
        self.assertEqual(seasonal['unexpected'],1)
        self.calendar([dict(exchange='MCX',date='2026-01-06',windows=[['09:00','23:30']],source='Changed schedule fixture')])
        result=self.result(run_quality_check(self.root),name)
        self.assertTrue(all(r['expected'] is None for r in result['sessions']))

    def test_failed_unavailable_and_empty_records_are_visible(self):
        for name,status in [('NIFTY','failed'),('SENSEX','unavailable'),('BANKNIFTY','empty')]: self.add(name=name,status=status)
        report=run_quality_check(self.root)
        self.assertEqual(report['statuses'],dict(failed=1,unavailable=1,empty=1))
        self.assertEqual(sum(g['rows'] for g in inventory(self.root)['groups']),0)
        for status in ('failed','unavailable','empty'):
            self.assertTrue(any(f'is {status}' in f['problem'] for f in report['findings']))

    def test_missing_and_corrupt_partitions_do_not_crash(self):
        a=self.add();b=self.add(name='SENSEX')
        (self.root/a['path']).unlink();(self.root/b['path']).write_bytes(b'not parquet')
        inv=inventory(self.root)
        self.assertEqual(len(inv['findings']),2)
        self.assertEqual(sum(g['rows'] for g in inv['groups']),0)
        report=run_quality_check(self.root)
        self.assertTrue(any(f['category']=='integrity' for f in report['findings']))
        self.assertEqual(report_state(self.root)['status'],'Review needed')
        self.assertEqual(lake_snapshot(self.root)['markets']['NIFTY'],None)

    def test_rolling_options_identity_stays_unverified(self):
        name='NIFTY_CE_ATM+1_WEEK1'
        self.add(name=name,dataset='options_rolling_1m');self.calendar()
        result=self.result(run_quality_check(self.root),name,'options_rolling_1m')
        self.assertEqual(result['dimensions']['coverage'],'Checks passed')
        self.assertIn('Unverified',result['identity']);self.assertEqual(result['status'],'Review needed')

    def test_stored_duplicates_validity_and_removed_input_duplicates_are_separate(self):
        r=self.add(quality=dict(exact_duplicates=4,conflicting_duplicate_rows=2))
        p=self.root/r['path'];frame=pd.read_parquet(p)
        duplicate=frame.iloc[[0]].copy();conflict=duplicate.copy();conflict['close']=10
        invalid=frame.iloc[[1]].copy();invalid['high']=1
        pd.concat([frame,duplicate,conflict,invalid]).to_parquet(p,index=False)
        r['sha256']=_hash_file(p);r['rows']=6;self.save();self.calendar()
        report=run_quality_check(self.root);result=self.result(report)
        self.assertEqual(result['stored_exact_duplicates'],1)
        self.assertEqual(result['stored_conflicting_rows'],4)
        self.assertEqual(result['invalid_stored_candles'],1)
        self.assertTrue(any(f['problem']=='Exact duplicate input rows removed' and f['count']==4 and f['severity']=='Info' for f in report['findings']))

    def test_legacy_and_malformed_reports_not_promoted(self):
        self.add()
        path=self.root/'quality_report.json';path.write_text('{"issues": [], "quality_counts": {}}')
        self.assertEqual(report_state(self.root)['status'],'Not checked')
        path.write_text('{bad')
        self.assertEqual(report_state(self.root)['status'],'Review needed')
        run_quality_check(self.root)
        self.assertTrue(any(p.read_text()=='{bad' for p in (self.root/'audit_history').glob('*.json')))
        report=json.loads(path.read_text());report['streams']={'bad':None};path.write_text(json.dumps(report))
        self.assertFalse(report_state(self.root)['current'])

    def test_malformed_manifest_is_reported_not_silently_empty(self):
        (self.root/'ingestion_manifest.json').write_text('{bad')
        self.assertEqual(report_state(self.root)['status'],'Review needed')
        report=run_quality_check(self.root)
        self.assertFalse(report['stable_snapshot'])
        self.assertTrue(report['findings'])

    def test_changed_manifest_during_audit_is_not_current(self):
        r=self.add();self.calendar()
        original=_hash_file
        def changed(path):
            self.manifest['changed_during_audit']=True;self.save()
            return original(path)
        with patch('quant_data_quality._hash_file',side_effect=changed): report=run_quality_check(self.root)
        self.assertFalse(report['stable_snapshot'])
        self.assertEqual(report_state(self.root)['status'],'Audit outdated')

    def test_audit_does_not_modify_historical_files_or_manifest(self):
        self.add();self.calendar()
        before={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('step2_build_data_lake.API',side_effect=AssertionError('Broker access forbidden')):
            run_quality_check(self.root)
        for path,content in before.items(): self.assertEqual((self.root/path).read_bytes(),content)

    def test_file_or_calendar_change_invalidates_audit(self):
        r=self.add();self.calendar();run_quality_check(self.root)
        self.calendar([])
        self.assertEqual(report_state(self.root)['status'],'Audit outdated')
        run_quality_check(self.root)
        p=self.root/r['path'];frame=pd.read_parquet(p);frame.loc[0,'close']=10;frame.to_parquet(p,index=False)
        self.assertEqual(report_state(self.root)['status'],'Audit outdated')
        report=run_quality_check(self.root)
        self.assertTrue(any(f['problem']=='Checksum mismatch.' for f in report['findings']))

    def test_invalid_calendar_never_yields_verified_coverage(self):
        self.add()
        self.calendar([dict(exchange='NSE',date='2026-01-05',windows=[['15:30','09:15']],source='Invalid fixture')])
        report=run_quality_check(self.root)
        self.assertEqual(self.result(report)['dimensions']['coverage'],'Review needed')
        self.assertFalse(report_state(self.root)['current'])
        self.assertTrue(any('invalid or ambiguous' in f['problem'] for f in report['findings']))


class CatalogTests(LakeFixture):
    def test_grouping_daily_intraday_and_physical_bytes_are_not_duplicated(self):
        r=self.add()
        self.add(name='NIFTY_CE_ATM_WEEK1',dataset='options_rolling_1m')
        self.add(name='NIFTY_PE_ATM_WEEK1',dataset='options_rolling_1m')
        self.add(name='CRUDEOIL_2026-11-01_123');self.add(name='CRUDEOIL_2026-12-01_124')
        self.add(times=['2026-01-05 00:00'],name='CRUDEOIL_2026-11-01_123',dataset='daily')
        self.manifest['chunks']['duplicate-reference']=deepcopy(r);self.save()
        inv=inventory(self.root)
        self.assertEqual(len(inv['groups']),4)
        self.assertEqual(sum(g['rows'] for g in inv['groups']),16)
        self.assertEqual(sum(g['bytes'] for g in inv['groups']),sum(p.stat().st_size for p in self.root.rglob('*.parquet')))
        self.assertEqual(len([g for g in inv['groups'] if g['interval']=='1 day']),1)
        self.assertEqual(len(filtered_groups(inv,report_state(self.root,inv),exchange='MCX',segment='Commodity futures')),2)
        self.assertFalse(any('BANKNIFTY' in g['group_id'] for g in inv['groups']))

    def test_latest_session_and_bounded_csv_export(self):
        times=[str(t) for t in pd.date_range('2026-01-06 09:15',periods=600,freq='min')]
        self.add(times=['2026-01-05 09:15',*times],end='2026-01-07')
        stream=inventory(self.root)['streams']['spot_1m|NIFTY']
        sessions=stream_sessions(stream);self.assertEqual(sessions[-1],'2026-01-06')
        frame,total=preview_candles(stream,sessions[-1],100)
        self.assertEqual((len(frame),total),(100,600))
        payload,name,mime=preview_export(frame,stream,sessions[-1])
        self.assertEqual(mime,'text/csv');self.assertIn('2026-01-06_100rows.csv',name)
        self.assertEqual(len(pd.read_csv(io.BytesIO(payload))),100)
        self.assertTrue(frame.timestamp.dt.date.eq(pd.Timestamp('2026-01-06').date()).all())

    def test_date_statistics_preserve_ist_daily_date(self):
        self.add(times=['2026-01-05 00:00'],dataset='daily',name='CRUDEOIL_2026-12-01_123')
        self.assertTrue(inventory(self.root)['groups'][0]['first'].startswith('2026-01-05'))


class DataPageTests(LakeFixture):
    def app(self):
        return AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=45).run()

    def test_navigation_filters_preview_and_quality_action(self):
        self.add(times=['2026-01-05 09:15','2026-01-06 09:15'],end='2026-01-07')
        self.add(name='SENSEX');self.calendar()
        with patch('quant_lake.ROOT',self.root),patch('quant_backtester.OUT',self.root/'backtests'):
            app=self.app();self.assertFalse(app.exception)
            app.radio(key='page').set_value('Data Lake').run()
            self.assertFalse(app.exception)
            app.selectbox(key='lake_exchange').select('NSE').run()
            self.assertEqual(len(app.selectbox(key='lake_dataset').options),1)
            self.assertEqual(app.selectbox(key='lake_dataset').value,'NSE|NIFTY|Index spot|1 minute')
            self.assertEqual(app.selectbox(key='lake_stream').value,'spot_1m|NIFTY')
            self.assertEqual(app.selectbox(key='preview_session').value,'2026-01-06')
            self.assertTrue(any('2026-01-06' in c.value for c in app.caption))
            app.text_input(key='lake_search').set_value('no-matches').run()
            self.assertTrue(any('No datasets match' in i.value for i in app.info))
            app.text_input(key='lake_search').set_value('').run()
            next(b for b in app.button if b.label=='Open Data Quality →').click().run()
            self.assertEqual(app.radio(key='page').value,'Data Quality')
            self.assertEqual(app.selectbox(key='quality_dataset').value,'NSE|NIFTY|Index spot|1 minute')
            app.button(key='run_quality').click().run(timeout=45)
            self.assertFalse(app.exception)
            self.assertTrue((self.root/'quality_report.json').exists())
            self.assertTrue(any('Matches current' in c.value for c in app.caption))
            self.assertTrue(app.get('download_button'))
            self.assertTrue(app.selectbox(key='quality_finding').options)
            self.assertTrue(any('Next action:' in i.value for i in app.info))

    def test_empty_and_corrupt_lakes_render_both_pages(self):
        with patch('quant_lake.ROOT',self.root),patch('quant_backtester.OUT',self.root/'backtests'):
            for malformed in (False,True):
                if malformed: (self.root/'ingestion_manifest.json').write_text('{bad')
                app=self.app()
                for page in ('Data Lake','Data Quality'):
                    app.radio(key='page').set_value(page).run()
                    self.assertFalse(app.exception,[e.message for e in app.exception])


if __name__=='__main__': unittest.main()
