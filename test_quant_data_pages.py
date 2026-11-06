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
from quant_data_ui import findings_frame, finding_stream_label


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
    def test_finding_streams_units_and_csv_preserve_actionable_evidence(self):
        for name in ('NIFTY_CE_ATM_WEEK1','NIFTY_PE_ATM+1_WEEK1'):
            self.add(name=name,dataset='options_rolling_1m')
        inv=inventory(self.root)
        def item(stream, problem, count, dataset='NSE|NIFTY|Rolling options|1 minute'):
            return dict(stream=stream,problem=problem,count=count,dataset=dataset,severity='Warning',period='2026-01-05',action='Inspect exact stream')
        findings=[item('options_rolling_1m|NIFTY_CE_ATM_WEEK1','Missing closing candles',2),
                  item('options_rolling_1m|NIFTY_PE_ATM+1_WEEK1','Minute-spacing holes between observed candles; not verified missing exchange candles.',4),
                  item(None,'Coverage not verified: session evidence missing.',None),
                  item(None,'Lake metadata warning',None,dataset='Lake'),
                  item('unknown_recorded_id','Unknown count semantics',3)]
        table=findings_frame(findings,inv)
        self.assertEqual(table['Stream / contract'].tolist(),['NIFTY_CE_ATM_WEEK1','NIFTY_PE_ATM+1_WEEK1','Not recorded','Lake-level','unknown_recorded_id (unresolved identifier)'])
        self.assertEqual(table.Unit.tolist(),['candles','minute-spacing holes','dates unverified','Not recorded','Not recorded'])
        self.assertEqual(table.Count.tolist(),['2','4','Not measured','Not measured','3'])
        exported=pd.read_csv(io.StringIO(table.to_csv(index=False)),dtype=str,keep_default_na=False)
        pd.testing.assert_frame_equal(table,exported)

    def test_finding_units_distinguish_rows_candles_sessions_and_unknown(self):
        inv=inventory(self.root)
        cases=[('Exact duplicate input rows removed',4,'input rows'),
               ('Unresolved conflicting timestamp rows remain stored',2,'stored rows'),
               ('Entire expected session absent',1,'sessions'),
               ('Coverage not verified: no calendar',5,'dates unverified'),
               ('Invalid stored OHLCV candles',None,'stored candles'),
               ('Unrecognized measurement',7,'Not recorded')]
        for problem,count,unit in cases:
            row=findings_frame([dict(problem=problem,count=count)],inv).iloc[0]
            self.assertEqual(row.Unit,unit)
            self.assertEqual(row.Count,'Not measured' if count is None else str(count))
        self.assertEqual(finding_stream_label({},inv),'Lake-level')

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
            table=next(df.value for df in app.dataframe if 'Problem' in df.value.columns)
            self.assertIn('Stream / contract',table.columns);self.assertIn('Unit',table.columns)
            self.assertEqual(set(table['Stream / contract']),{'NIFTY'})
            self.assertTrue(all('NIFTY' in label for label in app.selectbox(key='quality_finding').options))
            self.assertTrue(any('Stream / contract: NIFTY' in c.value and 'Unit:' in c.value for c in app.caption))
            self.assertTrue(any('Next action:' in i.value for i in app.info))

    def test_empty_and_corrupt_lakes_render_both_pages(self):
        with patch('quant_lake.ROOT',self.root),patch('quant_backtester.OUT',self.root/'backtests'):
            for malformed in (False,True):
                if malformed: (self.root/'ingestion_manifest.json').write_text('{bad')
                app=self.app()
                for page in ('Data Lake','Data Quality'):
                    app.radio(key='page').set_value(page).run()
                    self.assertFalse(app.exception,[e.message for e in app.exception])


class SyncResilienceTests(LakeFixture):
    def _add_index_day(self, day='2026-10-22'):
        times=[str(t) for t in pd.date_range(f'{day} 09:15',periods=375,freq='min')]
        end=str((pd.Timestamp(day)+pd.Timedelta(days=1)).date())
        self.add(times=times,name='NIFTY',dataset='spot_1m',start=day,end=end)
        for side in ('CE','PE'):
            for offset in ('ATM','ATM-1','ATM+1'):
                name=f'NIFTY_{side}_{offset}_WEEK1'
                key=f'options_rolling_1m/{name}/{day}'
                frame=pd.DataFrame(dict(timestamp=pd.to_datetime(times).tz_localize('Asia/Kolkata'),
                                        open=100.,high=102.,low=98.,close=101.,volume=10.,
                                        oi=1000.,iv=20.,spot=24000.,strike=24000.,
                                        instrument=name,option_type=side,moneyness=offset))
                path=self.root/(key+'.parquet');path.parent.mkdir(parents=True,exist_ok=True);frame.to_parquet(path,index=False)
                self.manifest['chunks'][key]=dict(dataset='options_rolling_1m',instrument=name,start=day,end=end,
                                                  status='complete',endpoint='/charts/rollingoption',
                                                  path=path.relative_to(self.root).as_posix(),rows=len(frame),
                                                  sha256=_hash_file(path),first=str(frame.timestamp.min()),
                                                  last=str(frame.timestamp.max()),quality={})
        self.save();run_quality_check(self.root)

    def _download(self, job, stage):
        stamps=[]
        for day in pd.date_range(job['start'],job['end'],inclusive='left'):
            if day.weekday()<5:
                stamps.extend(pd.date_range(f'{day.date()} 09:15',periods=375,freq='min',tz='Asia/Kolkata'))
        extra=dict(oi=1000.,iv=20.,spot=24000.,strike=24000.) if job['dataset']=='options_rolling_1m' else {}
        frame=pd.DataFrame(dict(timestamp=pd.DatetimeIndex(stamps),open=100.,high=102.,low=98.,close=101.,volume=10.,**extra,**job['meta']))
        path=stage/(job['key']+'.parquet');path.parent.mkdir(parents=True,exist_ok=True);frame.to_parquet(path,index=False)
        return dict(dataset=job['dataset'],instrument=job['meta']['instrument'],start=job['start'],end=job['end'],
                    status='complete',path=path.relative_to(stage).as_posix(),rows=len(frame),
                    sha256=_hash_file(path),first=str(frame.timestamp.min()),last=str(frame.timestamp.max()),
                    quality={},endpoint=job['endpoint'],payload=job['payload'])

    def test_env_override_reload_and_totp_auto_refresh_on_sync(self):
        import os
        import threading
        from unittest.mock import Mock
        import quant_data_sync
        import step2_build_data_lake
        self._add_index_day('2026-10-22')
        env_file=self.root/'.env'
        env_file.write_text('DHAN_CLIENT_ID=1100000001\nDHAN_ACCESS_TOKEN=fresh-disk-token\nDHAN_PIN=123456\nDHAN_TOTP_SECRET=JBSWY3DPEHPK3PXP\n',encoding='utf-8')
        seen_tokens=[]
        def download(job,stage):
            seen_tokens.append(os.environ.get('DHAN_ACCESS_TOKEN'))
            return self._download(job,stage)
        with patch.object(quant_data_sync,'ENV_FILE',env_file), \
                patch.dict('os.environ',{'DHAN_CLIENT_ID':'1100000001','DHAN_ACCESS_TOKEN':'stale-cached-token'},clear=True):
            result=quant_data_sync.run_sync(self.root,'NIFTY SPOT',latest=True,now='2026-10-23 16:00+05:30',downloader=download,check_credentials=True)
            self.assertEqual(result['committed'],1)
            self.assertEqual(seen_tokens,['fresh-disk-token'])
        # Verify API._post_once auto-refreshes expired token via TOTP once and retries
        env_file.write_text('DHAN_CLIENT_ID=1100000001\nDHAN_ACCESS_TOKEN=expired-token\nDHAN_PIN=123456\nDHAN_TOTP_SECRET=JBSWY3DPEHPK3PXP\n',encoding='utf-8')
        api=object.__new__(step2_build_data_lake.API)
        api.rate_lock=threading.Lock();api.auth_lock=threading.Lock();api.last=0;api.calls=0;api.refreshes=0
        api.session=Mock(headers={'access-token':'expired-token','client-id':'1100000001'})
        r_401=Mock(status_code=401,ok=False);r_401.json.return_value={'errorCode':'DH-901','errorMessage':'Invalid Token'}
        r_200=Mock(status_code=200,ok=True);r_200.json.return_value={'status':'success','data':{'timestamp':[1761191100]}}
        with patch.object(step2_build_data_lake,'ENV_FILE',env_file), \
                patch.dict('os.environ',{'DHAN_CLIENT_ID':'1100000001','DHAN_ACCESS_TOKEN':'expired-token','DHAN_PIN':'123456','DHAN_TOTP_SECRET':'JBSWY3DPEHPK3PXP'},clear=True), \
                patch('step2_build_data_lake.requests.post',side_effect=[r_401,r_200]) as post_mock, \
                patch('step2_build_data_lake.new_access_token',return_value='totp-refreshed-token') as totp_mock, \
                patch('step2_build_data_lake.set_key') as set_key_mock:
            out=api.post('/charts/intraday',{'securityId':'13'})
            self.assertEqual(out,{'timestamp':[1761191100]})
            self.assertEqual(post_mock.call_count,2)
            totp_mock.assert_called_once()
            self.assertEqual(api.session.headers['access-token'],'totp-refreshed-token')
            self.assertEqual(os.environ['DHAN_ACCESS_TOKEN'],'totp-refreshed-token')
            set_key_mock.assert_called_once_with(str(env_file),'DHAN_ACCESS_TOKEN','totp-refreshed-token')

    def test_non_auth_errors_never_trigger_false_auth_abort_and_rolling_dh905_falls_back_to_daily(self):
        import threading
        from unittest.mock import Mock
        import quant_data_sync
        import step2_build_data_lake
        self._add_index_day('2026-10-22')
        # 1) Non-auth errors with empty provider_message must not abort remaining streams or masquerade as AUTH
        def partial_fail(job,stage):
            if job['meta']['instrument']=='NIFTY_CE_ATM+1_WEEK1':
                return dict(status='failed',error='DH-905',provider_message='')
            return self._download(job,stage)
        result=quant_data_sync.run_sync(self.root,'NIFTY OPTIONS',latest=True,now='2026-10-23 16:00+05:30',downloader=partial_fail,check_credentials=False)
        self.assertEqual(result['attempted'],6)
        self.assertEqual(result['committed'],5)
        self.assertFalse(any('Remaining requests stopped' in f for f in result['failures']))
        self.assertTrue(any('DH-905 — Dhan rejected the request parameters' in f for f in result['failures']))
        self.assertFalse(any('provider description unavailable' in f for f in result['failures']))
        # 2) Multi-day /charts/rollingoption DH-905 automatically falls back to 1-day windows and deduplicates
        api=object.__new__(step2_build_data_lake.API)
        api.rate_lock=threading.Lock();api.auth_lock=threading.Lock();api.last=0;api.calls=0;api.refreshes=0
        api.session=Mock(headers={'access-token':'tok','client-id':'cid'})
        ts_22=int(pd.Timestamp('2026-10-22 09:15',tz='Asia/Kolkata').timestamp())
        ts_23=int(pd.Timestamp('2026-10-23 09:15',tz='Asia/Kolkata').timestamp())
        def fake_post(url,headers=None,json=None,timeout=None,allow_redirects=False):
            lo,hi=json['fromDate'],json['toDate']
            resp=Mock()
            if (pd.Timestamp(hi)-pd.Timestamp(lo)).days>1:
                resp.status_code=400;resp.ok=False
                resp.json.return_value={'errorCode':'DH-905','errorMessage':'Missing required fields, bad values for parameters etc.'}
                return resp
            resp.status_code=200;resp.ok=True
            # Return both lo and hi timestamps to verify per-day IST deduplication
            stamps=[ts_22,ts_23]
            resp.json.return_value={'data':{'ce':{'timestamp':stamps,'open':[100.,101.],'high':[102.,103.],'low':[98.,99.],
                                                  'close':[101.,102.],'volume':[10,20],'oi':[100,200],'iv':[15.,16.],
                                                  'spot':[24000.,24050.],'strike':[24000.,24000.]},'pe':None}}
            return resp
        with patch('step2_build_data_lake.requests.post',side_effect=fake_post), \
                patch('step2_build_data_lake.time.sleep'):
            merged=api.post('/charts/rollingoption',{'fromDate':'2026-10-22','toDate':'2026-10-24','drvOptionType':'CALL'})
        self.assertEqual(merged['ce']['timestamp'],[ts_22,ts_23])

    def test_row_level_nifty_spot_and_options_sync_complete_without_runtime_error(self):
        import threading
        import time
        import quant_data_sync
        from step2_build_data_lake import writer_lock
        self._add_index_day('2026-10-22')
        # Hold writer_lock briefly in a background thread to simulate an overlapping UI audit/sync finishing
        ready=threading.Event()
        def hold_briefly():
            with writer_lock(self.root):
                ready.set()
                time.sleep(0.35)
        t=threading.Thread(target=hold_briefly)
        t.start();ready.wait(timeout=5)
        try:
            spot_res=quant_data_sync.run_sync(self.root,'NIFTY SPOT',latest=True,now='2026-10-23 16:00+05:30',downloader=self._download,check_credentials=False)
            opt_res=quant_data_sync.run_sync(self.root,'NIFTY OPTIONS',latest=True,now='2026-10-23 16:00+05:30',downloader=self._download,check_credentials=False)
        finally:
            t.join(timeout=5)
        self.assertEqual(spot_res['committed'],1)
        self.assertFalse(spot_res['failures'])
        self.assertEqual(opt_res['committed'],6)
        self.assertFalse(opt_res['failures'])


if __name__=='__main__': unittest.main()
