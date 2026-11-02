"""Read-only adapter for retained TradeMind canonical Parquet/manifest exports.

No DB clients, .env access, provider calls, materialization or repairs. A Dhan
rolling stream is intentionally not adapted into a dated option contract.
"""
from pathlib import Path
from collections import OrderedDict
import hashlib
import json
import math
import pandas as pd

from quant_trademind import Contract, Unavailable, atm_lower_half, usable
from trademind_signals import stamp

DEFAULT_ROOT = Path('C:/TSA/trademind-calendar/artifacts/research-lake')


class RetainedTradeMindLake:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = Path(root).resolve()
        self.inputs = {}
        self.index = {}
        self.catalog = {}
        self.cache = OrderedDict()
        self.prior_dates = {}
        self.ownership = None
        self.calendar = {}
        self.coverage = {}
        self.underlying_identity = {}
        self._indexed = False
        self.vix = {}
        self.active_inputs = {}
        self.index_inputs = {}
        self.ownership_inputs = {}
        self.option_inputs = {}

    def begin_run(self):
        self.active_inputs = {**self.index_inputs, **self.ownership_inputs}

    def load_vix(self):
        directory = self.root.parent / 'research/trademind-t3-vix-gated-iron-fly'
        paths = list(directory.glob('vix-0929-snapshot-*.json'))
        if len(paths) != 1:
            return
        snapshot = self.json(paths[0])['snapshot']
        if snapshot['sourceInstrumentKey'] != 'NSE_INDEX|India VIX' or snapshot['sourceProvider'] != 'UPSTOX':
            raise Unavailable('UNEXPECTED_VIX_REFERENCE_IDENTITY')
        values = {}
        for row in snapshot['records']:
            if row.get('vix0929Close') is None:
                continue
            timestamp = pd.Timestamp(row['referenceTimestamp']).tz_convert('Asia/Kolkata') if row.get('referenceTimestamp') else None
            if timestamp is None or str(timestamp.date()) != row['date'] or (timestamp.hour, timestamp.minute, timestamp.second, timestamp.microsecond) != (9, 29, 0, 0):
                raise Unavailable('VIX_REFERENCE_TIME_MISMATCH')
            if row['date'] in values:
                raise Unavailable('DUPLICATE_VIX_REFERENCE_DATE')
            values[row['date']] = row['vix0929Close']
        self.vix = values

    def read_bytes(self, path, expected=None):
        path = Path(path).resolve()
        allowed = (self.root, self.root.parent / 'research/trademind-t3-vix-gated-iron-fly',
                   self.root.parent / 'research/nifty-ce-pe-premium-path')
        if not any(path.is_relative_to(root) for root in allowed) or path.name.startswith('.env'):
            raise Unavailable('Input outside the explicit retained-data root')
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if expected and expected != digest:
            raise Unavailable(f'PHYSICAL_INPUT_HASH_MISMATCH:{path}')
        prior = self.inputs.get(path.as_posix())
        if prior is not None and prior != digest:
            raise Unavailable(f'INPUT_CHANGED_DURING_READ:{path}')
        self.inputs[path.as_posix()] = digest
        self.active_inputs[path.as_posix()] = digest
        return content

    def json(self, path):
        return json.loads(self.read_bytes(path).decode('utf-8-sig'))

    def verify_preserved(self):
        changed = [name for name, digest in self.active_inputs.items()
                   if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest]
        if changed:
            raise Unavailable('Inputs changed during simulation: ' + ';'.join(changed))
        return dict(files=len(self.active_inputs), changed=[])

    def known_study_dates(self):
        path = self.root.parent / 'research/nifty-ce-pe-premium-path/session-coverage.json'
        return self.json(path) if path.exists() else []

    def _read_frame(self, kind, checksum, date, expected, *, defer_option_validity=False):
        path = self.root / 'parquet' / kind / checksum / 'sessions' / (date + '.parquet')
        self.read_bytes(path, expected)
        frame = pd.read_parquet(path)
        bars = {}
        for raw in frame.to_dict('records'):
            time = pd.Timestamp(raw['candleTime'])
            if time.tzinfo is None:
                raise Unavailable(f'TIMEZONE_MISSING:{path}')
            time = time.tz_convert('Asia/Kolkata')
            if str(time.date()) != date or time.second or time.microsecond:
                raise Unavailable(f'CANDLE_TIME_INVALID:{path}')
            m = time.hour * 60 + time.minute
            if m in bars:
                raise Unavailable(f'DUPLICATE_TIMESTAMP:{path}:{m}')
            b = {k: float(raw[k]) for k in ('open', 'high', 'low', 'close', 'volume')}
            valid = usable(b)
            if not defer_option_validity and (not valid or not math.isfinite(b['volume']) or b['volume'] < 0):
                raise Unavailable(f'INVALID_OBSERVED_OHLC:{path}:{m}')
            if defer_option_validity:
                b['input_valid'] = valid
                b['volume_valid'] = math.isfinite(b['volume']) and b['volume'] >= 0
            b.update(source=str(path), row0=len(bars), provider='UPSTOX' if kind == 'UNDERLYING_1M' else 'GROWW')
            bars[m] = b
        return bars

    def underlying(self, year):
        candidates = []
        for path in sorted((self.root / 'research-underlying-certifications').glob('*.json')):
            item = self.json(path)
            if item.get('identity', {}).get('year') == year:
                candidates.append((len(item['sessions']), path, item))
        if not candidates:
            raise Unavailable(f'UNDERLYING_CERTIFICATION_UNAVAILABLE:{year}')
        candidates.sort(key=lambda x: (x[0], x[1].name))
        _, path, cert = candidates[-1]
        if len(candidates) > 1 and candidates[-2][0] == candidates[-1][0]:
            raise Unavailable('AMBIGUOUS_UNDERLYING_CERTIFICATION')
        checksum = cert['canonicalManifest']['datasetChecksum']
        self.underlying_identity[year] = path.stem
        storage = self.json(self.root / 'parquet/UNDERLYING_1M' / checksum / 'storage-manifest.json')
        physical = {s['tradingDate']: s for s in storage['sessions']}
        certified_physical = {s['tradingDate']:s for s in cert['physicalStorage']['sessions']}
        sessions = []
        break_before = False
        for session in cert['sessions']:
            date = session['tradingDate']
            expected = physical.get(date)
            if expected is None or session.get('derivedImputedRowCount', 0):
                self.coverage[date] = dict(status='DATA_UNAVAILABLE', reason='MISSING_OR_SYNTHETIC_UNDERLYING')
                break_before = True
                continue
            if expected['physicalFileChecksum'] != certified_physical.get(date,{}).get('physicalFileChecksum'):
                raise Unavailable('UNDERLYING_CERTIFICATION_STORAGE_HASH_CONFLICT:' + date)
            try:
                bars = self._read_frame('UNDERLYING_1M', checksum, date, expected['physicalFileChecksum'])
            except (Unavailable,OSError) as exc:
                self.coverage[date] = dict(status='DATA_UNAVAILABLE',reason=str(exc))
                break_before = True
                continue
            windows = session['calendarSessionWindows']
            calendar = {m for w in windows for m in range(w['openMinuteIst'], w['closeMinuteIst'])}
            self.calendar[date] = calendar
            missing = sorted(calendar - bars.keys())
            if missing:
                self.coverage[date] = dict(status='DATA_UNAVAILABLE', reason='MISSING_UNDERLYING_MINUTES', minutes=missing)
                break_before = True
                continue
            self.coverage[date] = dict(status='UNDERLYING_AVAILABLE', rows=len(bars), windows=windows,
                                       realRows=session.get('realCanonicalRowCount'),
                                       compositeObserved=session.get('sourcePrecedenceTier') != 1)
            sessions.append(dict(date=date, bars=bars,break_before=break_before))
            break_before = False
        return sessions

    def index_options(self):
        if self._indexed:
            return
        for path in sorted((self.root / 'manifests/EXPIRED_OPTION_1M').glob('*.json')):
            manifest = self.json(path)
            self.index_inputs[path.resolve().as_posix()] = self.inputs[path.resolve().as_posix()]
            checksum = manifest['datasetChecksum']
            for s in manifest['sessions']:
                i = s['identity']
                if i['provider'] != 'GROWW':
                    continue
                date, key = i['tradingDate'], i['providerContractId']
                expiry = pd.Timestamp(i['expiry']).tz_convert('Asia/Kolkata').date().isoformat()
                record = dict(dataset=checksum, session=s, manifest=path.as_posix(), expiry=expiry,
                              strike=float(i['strikePrice']), side=i['optionType'], contract_id=key)
                self.index.setdefault((date, key), []).append(record)
                self.prior_dates.setdefault(key, set()).add(date)
                previous = self.catalog.get(key)
                if previous and (previous['expiry'], previous['strike'], previous['side']) != (expiry, record['strike'], record['side']):
                    raise Unavailable('CONFLICTING_CONTRACT_IDENTITY:' + key)
                self.catalog[key] = {k: record[k] for k in ('expiry', 'strike', 'side', 'contract_id')}
        self._indexed = True

    def option(self, date, key):
        self.index_options()
        if (date, key) in self.cache:
            self.cache.move_to_end((date, key))
            self.active_inputs.update(self.option_inputs[(date,key)])
            return self.cache[(date, key)]
        versions = self.index.get((date, key), [])
        if not versions:
            return {}
        identities = {v['session']['contentChecksum'] for v in versions}
        if len(identities) != 1:
            raise Unavailable(f'CONFLICTING_OPTION_DATASET_VERSIONS:{date}:{key}')
        record = sorted(versions, key=lambda x: x['dataset'])[0]
        storage = self.json(self.root / 'parquet/EXPIRED_OPTION_1M' / record['dataset'] / 'storage-manifest.json')
        expected = next((x for x in storage['sessions'] if x['tradingDate'] == date), None)
        if expected is None:
            raise Unavailable('OPTION_STORAGE_MANIFEST_MISSING_SESSION')
        if expected['sessionContentChecksum'] != record['session']['contentChecksum']:
            raise Unavailable('OPTION_MANIFEST_STORAGE_CONTENT_CONFLICT')
        evidence = record['session'].get('sourceAcquisitionEvidence', {})
        repair = evidence.get('compositeRepair')
        if repair and any(x in str(repair).upper() for x in ('IMPUT', 'SYNTHETIC', 'FFILL')):
            raise Unavailable('SYNTHETIC_OPTION_INPUT_REJECTED')
        bars = self._read_frame('EXPIRED_OPTION_1M', record['dataset'], date, expected['physicalFileChecksum'],defer_option_validity=True)
        self.cache[(date, key)] = bars
        deps = [self.root / 'parquet/EXPIRED_OPTION_1M' / record['dataset'] / 'storage-manifest.json',
                self.root / 'parquet/EXPIRED_OPTION_1M' / record['dataset'] / 'sessions' / (date + '.parquet')]
        self.option_inputs[(date,key)] = {p.resolve().as_posix():self.inputs[p.resolve().as_posix()] for p in deps}
        if len(self.cache) > 128:
            self.cache.popitem(last=False)
        return bars

    def owning_expiry(self, date):
        """Persisted ownership only; no weekday or another-expiry rescue."""
        if self.ownership is None:
            self.ownership = {}
            for path in sorted((self.root / 'multi-expiry-option-runs').glob('*.json')):
                plan = self.json(path)
                self.ownership_inputs[path.resolve().as_posix()] = self.inputs[path.resolve().as_posix()]
                if plan.get('provider') != 'GROWW':
                    continue
                for expiry in plan.get('expiries', []):
                    for d in expiry.get('tradingDates', []):
                        self.ownership.setdefault(d, set()).add(expiry['expiry'])
        ownership = set(self.ownership.get(date, set()))
        if len(ownership) != 1:
            raise Unavailable(f'OWNING_EXPIRY_{"CONFLICTING" if ownership else "UNAVAILABLE"}:{date}')
        return ownership.pop()

    def select(self, date, expiry, side, spot, decision, *, exact):
        self.index_options()
        candidates = [c for c in self.catalog.values() if c['expiry'] == expiry and c['side'] == side]
        target = atm_lower_half(spot) if exact else spot
        if exact:
            candidates = [c for c in candidates if c['strike'] == target]
            if len(candidates) > 1:
                raise Unavailable('AMBIGUOUS_EXACT_DATED_CONTRACT')
        candidates.sort(key=lambda x: (abs(x['strike'] - target), x['strike'], x['contract_id']))
        for c in candidates:
            key = c['contract_id']
            bars = self.option(date, key)
            intraday = any(m + 1 <= decision for m in bars)
            if self.ownership is None:
                self.owning_expiry(date)
            prior = any(d < date and self.ownership.get(d) == {expiry} for d in self.prior_dates.get(key, ()))
            if not intraday and not prior:
                continue
            known = stamp(date,min(m+1 for m in bars if m+1<=decision)) if intraday else stamp(max(d for d in self.prior_dates[key] if d<date and self.ownership.get(d)=={expiry}),930)
            return Contract('GROWW', key, expiry, c['strike'], side, known,
                            evidence='COMPLETED_INTRADAY_CANDLE' if intraday else 'PRIOR_ACCEPTED_SESSION')
        return None
