"""Self-contained loader regressions. No broker, real lake, or network required."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from quant_backtester import Config, ENGINE_VERSION, ResearchData, STRATEGIES, run_backtest


class RunDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = {}
        (self.root/'quality_report.json').write_text(json.dumps({'issues': []}))
        self.commit_manifest()

    def commit_manifest(self):
        (self.root/'ingestion_manifest.json').write_text(json.dumps({'chunks': self.records}))

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
        return ResearchData('NIFTY', '2025-09-02', '2025-09-03', self.root)

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
        alone = ResearchData('CRUDEOIL','2026-09-07','2026-09-08',self.root)
        broad = ResearchData('CRUDEOIL','2026-09-04','2026-09-09',self.root)
        a, b = alone.days['2026-09-07'], broad.days['2026-09-07']
        self.assertEqual(a['contract'], 'CRUDEOIL_A')
        self.assertEqual(a['contract'], b['contract'])
        pd.testing.assert_frame_equal(a['spot'].reset_index(drop=True), b['spot'].reset_index(drop=True))
        self.assertEqual(a['prices'], b['prices'])
        pd.testing.assert_frame_equal(alone.features('5m')['2026-09-07'], broad.features('5m')['2026-09-07'])
        self.assertEqual(alone.available_days, ['2026-09-07'])

    def test_mcx_weekend_uses_preceding_observed_session(self):
        self.mcx()
        data = ResearchData('CRUDEOIL','2026-09-07','2026-09-08',self.root)
        self.assertEqual(data.eligibility[0]['selection_session'],'2026-09-04')
        self.assertEqual(data.eligibility[0]['status'],'eligible')

    def test_missing_prior_is_explicit_and_all_excluded_run_persists_evidence(self):
        self.store('CRUDEOIL_A','2026-09-07',mcx=True)
        data = ResearchData('CRUDEOIL','2026-09-07','2026-09-08',self.root)
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
        first = ResearchData('CRUDEOIL','2026-09-07','2026-09-09',self.root)
        self.store('CRUDEOIL_B','2026-09-07',mcx=True,volume=1e10)
        self.store('CRUDEOIL_B','2026-09-08',mcx=True,volume=1e12)
        later = ResearchData('CRUDEOIL','2026-09-07','2026-09-09',self.root)
        self.assertEqual(first.days['2026-09-07']['contract'],later.days['2026-09-07']['contract'])
        self.assertEqual(first.days['2026-09-08']['contract'],'CRUDEOIL_B')
        self.assertEqual(first.days['2026-09-08']['contract'],later.days['2026-09-08']['contract'])

    def test_mcx_ties_preserve_lexical_contract_order(self):
        self.mcx()
        self.store('CRUDEOIL_B','2026-09-04',mcx=True,volume=100)
        data=ResearchData('CRUDEOIL','2026-09-07','2026-09-08',self.root)
        self.assertEqual(data.days['2026-09-07']['contract'],'CRUDEOIL_A')

    def test_selected_contract_missing_today_does_not_fall_back_using_today(self):
        self.store('CRUDEOIL_A','2026-09-04',mcx=True)
        self.store('CRUDEOIL_B','2026-09-07',mcx=True)
        data=ResearchData('CRUDEOIL','2026-09-07','2026-09-08',self.root)
        self.assertFalse(data.days)
        self.assertIn('no execution candles', data.exclusions[0]['reason'])

    def test_invalid_prior_volume_is_excluded(self):
        self.mcx()
        self.store('CRUDEOIL_A','2026-09-04',mcx=True,volume=-1)
        data=ResearchData('CRUDEOIL','2026-09-07','2026-09-08',self.root)
        self.assertIn('Invalid or duplicate preceding-session', data.exclusions[0]['reason'])

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
        data=ResearchData('CRUDEOIL','2026-09-07','2026-09-08',self.root)
        self.assertFalse(data.days)
        self.assertIn('T10:00:', data.eligibility[0]['reasons'][0]['timestamps'][0])

    def test_invalid_required_stored_candle_remains_excluded(self):
        def invalid(frame): frame.loc[frame.timestamp.dt.strftime('%H:%M').eq('10:00'),'volume']=-1
        self.index(change=invalid)
        data=self.load_index()
        self.assertTrue(any(r['problem']=='Invalid required candles' for r in data.eligibility[0]['reasons']))

    def test_conflicting_same_strike_streams_remain_excluded(self):
        def conflict(frame): frame['strike']=24000.;frame['close']=100.
        self.index(change=conflict)
        data=self.load_index()
        self.assertFalse(data.days)
        self.assertTrue(any(r['problem']=='Conflicting same-strike candles' for r in data.eligibility[0]['reasons']))

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
        self.index()
        (self.root/'quality_report.json').write_text(json.dumps({'issues':['integrity failure']}))
        with self.assertRaisesRegex(ValueError,'integrity issues'): self.load_index()

    def test_missing_and_changed_partition_fail_before_execution(self):
        self.index()
        record=next(iter(self.records.values())); path=self.root/record['path']
        original=path.read_bytes();path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):self.load_index()
        path.write_bytes(original);path.unlink()
        with self.assertRaisesRegex(ValueError,'Missing partition'):self.load_index()

    def test_prior_context_checksum_is_enforced(self):
        self.mcx()
        record=next(r for r in self.records.values() if r['start']=='2026-09-04')
        (self.root/record['path']).write_bytes(b'corrupt context')
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):
            ResearchData('CRUDEOIL','2026-09-07','2026-09-08',self.root)

    def test_old_engine_cache_is_preserved_and_not_reused(self):
        self.index()
        data=self.load_index();c=replace(Config(),start=data.start,end=data.end)
        old_identity=json.dumps(dict(config=c.__dict__,engine='5.2.0',data=data.fingerprint),sort_keys=True)
        old_dir=self.root/'runs'/hashlib.sha256(old_identity.encode()).hexdigest()[:16]
        old_dir.mkdir(parents=True);(old_dir/'result.json').write_text('{"old":true}')
        with patch.object(data,'events',return_value=[]):
            result=run_backtest(c,data,output=self.root)
        self.assertEqual(result['engine_version'],ENGINE_VERSION)
        self.assertNotEqual(Path(result['path']),old_dir)
        self.assertEqual((old_dir/'result.json').read_text(),'{"old":true}')
        self.assertTrue((Path(result['path'])/'eligibility.json').exists())


if __name__=='__main__':unittest.main()
