"""Self-contained loader regressions. No broker, real lake, or network required."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from quant_backtester import Config, ENGINE_VERSION, ResearchData, LEGACY_STRATEGIES as STRATEGIES, run_backtest
from quant_data_quality import run_quality_check, report_state
from quant_run_data import stream_check, select_contracts, verify_files


class RunDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = {}
        self.commit_manifest()

    def commit_manifest(self):
        (self.root/'ingestion_manifest.json').write_text(json.dumps({'chunks': self.records}))
        self.audit_dirty = True

    def ensure_audit(self):
        if self.audit_dirty:
            run_quality_check(self.root)
            self.audit_dirty = False

    def load(self, instrument, start, end):
        self.ensure_audit()
        return ResearchData(instrument, start, end, self.root)

    def store(self, name, date, *, mcx=False, volume=10, missing=(), change=None, quality=None):
        start, end = ('09:00', '23:29') if mcx else ('09:15', '15:29')
        stamps = pd.date_range(f'{date} {start}', f'{date} {end}', freq='min', tz='Asia/Kolkata')
        stamps = stamps[~stamps.strftime('%H:%M').isin(missing)]
        frame = pd.DataFrame(dict(timestamp=stamps, instrument=name, open=100., high=102., low=98., close=101., volume=float(volume)))
        dataset = 'spot_1m'
        if '_WEEK1' in name:
            dataset = 'options_rolling_1m'
            _, side, offset, _ = name.split('_')
            frame = frame.assign(option_type=side, moneyness=offset, oi=1000., iv=20., spot=24000.,
                                 strike=24000+{'ATM':0, 'ATM-1':-50, 'ATM+1':50}[offset])
        if change: change(frame)
        path = self.root/f'{name}_{date}.parquet'
        frame.to_parquet(path, index=False)
        record = dict(instrument=name, dataset=dataset, start=date,
                      end=str((pd.Timestamp(date)+pd.Timedelta(days=1)).date()), status='complete',
                      path=path.name, rows=len(frame), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), quality=quality or {})
        self.records[path.stem] = record
        self.commit_manifest()
        return frame, record

    def mcx(self):
        for date, av, bv in [('2026-09-04',100,50), ('2026-09-07',1,1000), ('2026-09-08',10000,1)]:
            self.store('CRUDEOIL_A', date, mcx=True, volume=av)
            self.store('CRUDEOIL_B', date, mcx=True, volume=bv)

    def index(self, missing=(), stream='NIFTY_CE_ATM+1_WEEK1', change=None):
        names = ['NIFTY']+[f'NIFTY_{s}_{m}_WEEK1' for s in ('CE','PE') for m in ('ATM','ATM-1','ATM+1')]
        for name in names:
            self.store(name, '2025-09-02', missing=missing if name==stream else (), change=change if name==stream else None)

    def load_index(self):
        return self.load('NIFTY', '2025-09-02', '2025-09-03')

    def legacy_flag(self):
        pd.DataFrame([dict(instrument='NIFTY_CE_ATM+1_WEEK1', session='2025-09-02', short_session_review=True)]).to_parquet(self.root/'session_coverage.parquet')

    def rejected(self, date, clock, *, conflict=False, broken=False):
        record = next(r for r in self.records.values() if r['instrument']=='NIFTY_CE_ATM+1_WEEK1')
        record['quality'] = {'conflicting_duplicate_rows' if conflict else 'invalid_ohlcv': 1}
        if not broken:
            path = self.root/'rejected.parquet'
            pd.DataFrame([dict(timestamp=pd.Timestamp(f'{date} {clock}', tz='Asia/Kolkata'),
                               rejection_reason='conflicting_duplicate_timestamp' if conflict else 'invalid_ohlcv')]).to_parquet(path)
            record['quarantine_path'] = path.name
        self.commit_manifest()

    def test_mcx_one_day_matches_broad_selection_and_execution_inputs(self):
        self.mcx()
        alone = self.load('CRUDEOIL','2026-09-07','2026-09-08')
        broad = self.load('CRUDEOIL','2026-09-04','2026-09-09')
        a, b = alone.days['2026-09-07'], broad.days['2026-09-07']
        self.assertEqual(a['contract'], 'CRUDEOIL_A')
        self.assertEqual(a['contract'], b['contract'])
        pd.testing.assert_frame_equal(a['spot'].reset_index(drop=True), b['spot'].reset_index(drop=True))
        self.assertEqual(a['prices'], b['prices'])
        pd.testing.assert_frame_equal(alone.features('5m')['2026-09-07'], broad.features('5m')['2026-09-07'])
        self.assertEqual(alone.available_days, ['2026-09-07'])

    def test_mcx_weekend_uses_preceding_observed_session(self):
        self.mcx()
        data = self.load('CRUDEOIL','2026-09-07','2026-09-08')
        self.assertEqual(data.eligibility[0]['selection_session'],'2026-09-04')
        self.assertEqual(data.eligibility[0]['status'],'eligible')

    def test_missing_prior_is_explicit_and_all_excluded_run_persists_evidence(self):
        self.store('CRUDEOIL_A','2026-09-07',mcx=True)
        data = self.load('CRUDEOIL','2026-09-07','2026-09-08')
        self.assertFalse(data.days)
        self.assertIn('No preceding observed session', data.exclusions[0]['reason'])
        c = replace(Config(),instrument='CRUDEOIL',strategy=STRATEGIES[4], start=data.start,end=data.end)
        result = run_backtest(c,data,output=self.root/'runs')
        self.assertEqual(result['metrics']['total_trades'],0)
        saved = json.loads((Path(result['path'])/'eligibility.json').read_text())
        self.assertEqual(saved, result['eligibility'])
        self.assertEqual(saved[0]['status'],'excluded')
        self.assertEqual(pd.read_parquet(Path(result['path'])/'equity.parquet').date.tolist(),['2026-09-07'])

    def test_same_day_and_future_final_volumes_cannot_change_selection(self):
        self.mcx()
        first = self.load('CRUDEOIL','2026-09-07','2026-09-09')
        self.store('CRUDEOIL_B','2026-09-07',mcx=True,volume=1e10)
        self.store('CRUDEOIL_B','2026-09-08',mcx=True,volume=1e12)
        later = self.load('CRUDEOIL','2026-09-07','2026-09-09')
        self.assertEqual(first.days['2026-09-07']['contract'],later.days['2026-09-07']['contract'])
        self.assertEqual(first.days['2026-09-08']['contract'],'CRUDEOIL_B')
        self.assertEqual(first.days['2026-09-08']['contract'],later.days['2026-09-08']['contract'])

    def test_mcx_ties_preserve_lexical_contract_order(self):
        self.mcx()
        self.store('CRUDEOIL_B','2026-09-04',mcx=True,volume=100)
        data=self.load('CRUDEOIL','2026-09-07','2026-09-08')
        self.assertEqual(data.days['2026-09-07']['contract'],'CRUDEOIL_A')

    def test_selected_contract_missing_today_does_not_fall_back_using_today(self):
        self.store('CRUDEOIL_A','2026-09-04',mcx=True)
        self.store('CRUDEOIL_B','2026-09-07',mcx=True)
        data=self.load('CRUDEOIL','2026-09-07','2026-09-08')
        self.assertFalse(data.days)
        self.assertIn('no execution candles', data.exclusions[0]['reason'])

    def test_invalid_prior_volume_is_excluded(self):
        self.mcx()
        self.store('CRUDEOIL_A','2026-09-04',mcx=True,volume=-1)
        with self.assertRaisesRegex(ValueError, 'integrity issues'):
            self.load('CRUDEOIL','2026-09-07','2026-09-08')
        # The causal selector also fails closed if invalid volume reaches it.
        daily=pd.DataFrame([dict(date='2026-09-04',instrument='CRUDEOIL_A',volume=-1,invalid=True)])
        self.assertIn('Invalid or duplicate preceding-session',select_contracts(daily,['2026-09-07'])['2026-09-07'][2])

    def test_index_outside_gap_and_legacy_flag_do_not_veto_required_window(self):
        self.index(missing=['15:24']); self.legacy_flag()
        data=self.load_index()
        self.assertIn('2025-09-02',data.days)
        self.assertTrue(data.eligibility[0]['evidence'])
        self.assertTrue(any('2025-09-02T15:24:00+05:30' in e.get('observed_spacing_holes_outside_window',[]) for e in data.eligibility[0]['evidence']))
        self.assertEqual(data.required_windows['options'],['09:15','15:20'])
        self.assertEqual(pd.read_parquet(self.root/'session_coverage.parquet').short_session_review.tolist(),[True])

    def test_index_internal_gap_excluded_with_stream_and_timestamp(self):
        self.index(missing=['12:34'])
        data=self.load_index()
        reason=data.eligibility[0]['reasons'][0]
        self.assertFalse(data.days)
        self.assertEqual(reason['stream'],'NIFTY_CE_ATM+1_WEEK1')
        self.assertEqual(reason['timestamps'],['2025-09-02T12:34:00+05:30'])

    def test_required_open_and_close_are_both_enforced(self):
        self.index(missing=['09:15','15:20'])
        data=self.load_index()
        self.assertEqual(data.eligibility[0]['reasons'][0]['count'],2)
        self.assertFalse(data.days)

    def test_warmup_gap_before_entry_remains_disqualifying(self):
        self.index(missing=['09:16'],stream='NIFTY')
        data=self.load_index()
        self.assertFalse(data.days)
        self.assertEqual(data.eligibility[0]['reasons'][0]['stream'],'NIFTY')

    def test_mcx_morning_indicator_warmup_is_required(self):
        self.mcx(); self.store('CRUDEOIL_A','2026-09-07',mcx=True,missing=['10:00'])
        data=self.load('CRUDEOIL','2026-09-07','2026-09-08')
        self.assertFalse(data.days)
        self.assertIn('T10:00:', data.eligibility[0]['reasons'][0]['timestamps'][0])

    def test_invalid_required_stored_candle_remains_excluded(self):
        def invalid(frame): frame.loc[frame.timestamp.dt.strftime('%H:%M').eq('10:00'),'volume']=-1
        self.index(change=invalid)
        with self.assertRaisesRegex(ValueError,'integrity issues'):self.load_index()
        record=next(r for r in self.records.values() if r['instrument']=='NIFTY_CE_ATM+1_WEEK1')
        reasons,_=stream_check(pd.read_parquet(self.root/record['path']),'2025-09-02',record['instrument'],['09:15','15:20'])
        self.assertTrue(any(r['problem']=='Invalid required candles' for r in reasons))

    def test_conflicting_same_strike_streams_remain_excluded(self):
        def conflict(frame): frame['strike']=24000.;frame['close']=100.
        self.index(change=conflict)
        data=self.load_index()
        self.assertFalse(data.days)
        self.assertTrue(any(r['problem']=='Conflicting same-strike candles' for r in data.eligibility[0]['reasons']))

    def one_conflict(self, clock, metadata_only=False):
        def conflict(frame):
            mask=frame.timestamp.dt.strftime('%H:%M').eq(clock)
            frame.loc[mask,'strike']=24000.
            frame.loc[mask,'oi' if metadata_only else 'close']=1001. if metadata_only else 100.
        self.store('NIFTY_CE_ATM+1_WEEK1','2025-09-02',change=conflict)

    def test_outside_window_conflict_is_evidence_and_never_overwrites_quote(self):
        self.index();baseline=self.load_index()
        self.one_conflict('15:24');data=self.load_index()
        self.assertTrue(data.days)
        evidence=[e for e in data.eligibility[0]['evidence'] if e['problem']=='Conflicting same-strike candles']
        self.assertEqual(len(evidence),1)
        self.assertEqual(evidence[0]['timestamps'],['2025-09-02T15:24:00+05:30'])
        self.assertEqual(evidence[0]['streams'],['NIFTY_CE_ATM+1_WEEK1','NIFTY_CE_ATM_WEEK1'])
        self.assertTrue(evidence[0]['outside_required_window'])
        day=data.days['2025-09-02']
        self.assertNotIn((924,'CE',24000.),day['options'])
        self.assertNotIn((924,'CE',24000.),day['option_meta'])
        self.assertEqual({k:v for k,v in day['options'].items() if 555<=k[0]<=920},
                         {k:v for k,v in baseline.days['2025-09-02']['options'].items() if 555<=k[0]<=920})

    def test_inside_window_conflicts_include_warmup_and_exit(self):
        for clock in ('09:16','12:34','15:20'):
            with self.subTest(clock=clock):
                self.index();self.one_conflict(clock);data=self.load_index()
                self.assertFalse(data.days)
                reasons=[r for r in data.eligibility[0]['reasons'] if r['problem']=='Conflicting same-strike candles']
                self.assertEqual(len(reasons),1)
                self.assertEqual(reasons[0]['timestamps'],[f'2025-09-02T{clock}:00+05:30'])
                self.assertFalse(reasons[0]['outside_required_window'])

    def test_required_option_metadata_conflict_remains_disqualifying(self):
        self.index();self.one_conflict('10:00',metadata_only=True)
        self.assertFalse(self.load_index().days)

    def test_absent_empty_malformed_and_unsupported_audits_are_actionable(self):
        self.index();self.ensure_audit()
        path=self.root/'quality_report.json';valid=path.read_text()
        cases=[('absent',None),('empty','{}'),('malformed','{bad'),('wrong type','[]'),('legacy','{"issues": []}'),
               ('unsupported',json.dumps(dict(json.loads(valid),audit_version='999')))]
        for label, content in cases:
            with self.subTest(label=label):
                if content is None:path.unlink(missing_ok=True)
                else:path.write_text(content)
                with self.assertRaisesRegex(ValueError,'Quality audit is not usable.*local quality check'):self.load_index()
        path.write_text(valid)
        self.assertTrue(self.load_index().days)

    def test_each_stale_fingerprint_and_unstable_snapshot_block_loading(self):
        self.index();self.ensure_audit()
        path=self.root/'quality_report.json';valid=json.loads(path.read_text())
        for field in ('manifest_fingerprint','file_fingerprint','calendar_fingerprint','stable_snapshot'):
            with self.subTest(field=field):
                stale=dict(valid);stale[field]=False if field=='stable_snapshot' else '0'*64
                path.write_text(json.dumps(stale))
                with self.assertRaisesRegex(ValueError,'Audit outdated'):self.load_index()
                self.assertFalse(report_state(self.root)['current'])

    def test_incomplete_or_malformed_audit_results_block_loading(self):
        self.index();self.ensure_audit()
        path=self.root/'quality_report.json';valid=json.loads(path.read_text())
        for variant in ('missing stream','bad session','missing severity','wrong issues type','missing count evidence'):
            report=json.loads(json.dumps(valid))
            if variant=='missing stream':report['streams'].pop('spot_1m|NIFTY')
            elif variant=='bad session':report['streams']['spot_1m|NIFTY']['sessions']=[None]
            elif variant=='missing severity':report['findings'][0].pop('severity')
            elif variant=='wrong issues type':report['issues']={}
            else:report['streams']['spot_1m|NIFTY'].pop('stored_conflicting_rows')
            with self.subTest(variant=variant):
                path.write_text(json.dumps(report))
                with self.assertRaisesRegex(ValueError,'Quality audit is not usable'):self.load_index()

    def test_current_coverage_and_identity_warnings_are_nonblocking(self):
        self.index();data=self.load_index();state=report_state(self.root)
        self.assertTrue(state['current']);self.assertEqual(state['status'],'Review needed')
        self.assertTrue(data.days)
        categories={f['category'] for f in data.quality['findings']}
        self.assertTrue({'coverage','identity'}.issubset(categories))
        self.assertEqual(data.quality['issues'],[])

    def test_error_finding_cannot_bypass_gate_with_empty_issues(self):
        self.index();self.ensure_audit()
        path=self.root/'quality_report.json';report=json.loads(path.read_text())
        report['findings'].append(dict(problem='Checksum mismatch.',severity='Error',category='integrity',dataset='Lake',stream=None,period='',action='Restore partition',count=None))
        self.assertEqual(report['issues'],[]);path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError,'integrity issues'):self.load_index()

    def test_lake_change_during_loading_blocks_execution(self):
        self.index();self.ensure_audit()
        def changed(root, records):
            verify_files(root,records)
            manifest=self.root/'ingestion_manifest.json'
            document=json.loads(manifest.read_text());document['changed_while_loading']=True
            manifest.write_text(json.dumps(document))
        with patch('quant_backtester.verify_files',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'Audit outdated'):self.load_index()

    def test_quarantined_conflict_inside_window_remains_excluded(self):
        self.index();self.rejected('2025-09-02','10:00',conflict=True)
        data=self.load_index()
        self.assertFalse(data.days)
        self.assertIn('Rejected invalid/conflicting',data.exclusions[0]['reason'])

    def test_quarantined_invalid_outside_window_retains_eligibility(self):
        self.index(missing=['15:24']); self.rejected('2025-09-02','15:24')
        self.assertTrue(self.load_index().days)

    def test_unlocalized_rejected_aggregate_fails_closed(self):
        self.index();self.rejected('2025-09-02','10:00',broken=True)
        data=self.load_index()
        self.assertIn('cannot be localized',data.exclusions[0]['reason'])

    def test_incomplete_quarantine_evidence_fails_closed(self):
        self.index();self.rejected('2025-09-02','15:24')
        record=next(r for r in self.records.values() if r['instrument']=='NIFTY_CE_ATM+1_WEEK1')
        record['quality']['invalid_ohlcv']=2
        self.commit_manifest()
        self.assertIn('cannot be localized',self.load_index().exclusions[0]['reason'])

    def test_integrity_report_errors_still_block_loader(self):
        self.index();self.ensure_audit()
        report=json.loads((self.root/'quality_report.json').read_text())
        issue=dict(problem='integrity failure',severity='Error',category='integrity',dataset='Lake',stream=None,period='',action='Restore partition',count=None)
        report['issues']=[issue];report['findings'].append(issue)
        (self.root/'quality_report.json').write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError,'integrity issues'): self.load_index()

    def test_missing_and_changed_partition_fail_before_execution(self):
        self.index();self.ensure_audit()
        record=next(iter(self.records.values())); path=self.root/record['path']
        original=path.read_bytes();stamp=path.stat()
        corrupt=bytearray(original);corrupt[8]^=1;path.write_bytes(corrupt)
        os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):self.load_index()
        path.write_bytes(original);path.unlink()
        with self.assertRaisesRegex(ValueError,'Quality audit is not usable'):self.load_index()
        with self.assertRaisesRegex(ValueError,'Missing partition'):verify_files(self.root,[record])

    def test_prior_context_checksum_is_enforced(self):
        self.mcx();self.ensure_audit()
        record=next(r for r in self.records.values() if r['start']=='2026-09-04')
        path=self.root/record['path'];stamp=path.stat();corrupt=bytearray(path.read_bytes());corrupt[8]^=1
        path.write_bytes(corrupt);os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):
            self.load('CRUDEOIL','2026-09-07','2026-09-08')

    def test_old_engine_cache_is_preserved_and_not_reused(self):
        self.index()
        data=self.load_index();c=replace(Config(),start=data.start,end=data.end)
        old_identity=json.dumps(dict(config=c.__dict__,engine='5.3.0',data=data.fingerprint),sort_keys=True)
        old_dir=self.root/'runs'/hashlib.sha256(old_identity.encode()).hexdigest()[:16]
        old_dir.mkdir(parents=True);(old_dir/'result.json').write_text('{"old":true}')
        with patch.object(data,'events',return_value=[]):
            result=run_backtest(c,data,output=self.root)
        self.assertEqual(result['engine_version'],ENGINE_VERSION)
        self.assertNotEqual(Path(result['path']),old_dir)
        self.assertEqual((old_dir/'result.json').read_text(),'{"old":true}')
        self.assertTrue((Path(result['path'])/'eligibility.json').exists())


if __name__=='__main__':unittest.main()
