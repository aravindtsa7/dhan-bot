"""Local lake inventory and evidence-bound audits. No broker or ingestion calls."""
from collections import Counter
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

IST = 'Asia/Kolkata'
AUDIT_VERSION = '2.0'
STATUSES = ('No data', 'Not checked', 'Audit outdated', 'Review needed', 'Checks passed')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def read_document(path):
    path = Path(path)
    if not path.exists():
        return None, None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Expected a JSON object')
        return value, None
    except (OSError, ValueError) as exc:
        return None, f'{path.name} cannot be read ({type(exc).__name__}). Repair or restore this file; evidence has not been discarded.'


def finding(problem, dataset='Lake', severity='Warning', count=None, period='', action='Review the source evidence.', category='coverage', stream=None):
    return dict(severity=severity, dataset=dataset, stream=stream, period=period,
                category=category, problem=problem, count=count, action=action)


def safe_path(root, relative):
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError('Partition path is outside the lake')
    return path


def file_stamp(path):
    try:
        stat = Path(path).stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None, None


@lru_cache(maxsize=4096)
def parquet_metadata(path, stamp):
    try:
        meta = pq.read_metadata(path)
        dates = []
        for i in range(meta.num_row_groups):
            group = meta.row_group(i)
            for j in range(group.num_columns):
                col = group.column(j)
                if col.path_in_schema == 'timestamp' and col.statistics and col.statistics.has_min_max:
                    for value in (col.statistics.min, col.statistics.max):
                        stamp = pd.Timestamp(value)
                        dates.append(stamp.tz_convert(IST) if stamp.tzinfo else stamp)
        return dict(rows=meta.num_rows, first=str(min(dates)) if dates else None,
                    last=str(max(dates)) if dates else None), None
    except Exception as exc:
        return None, f'Unreadable Parquet ({type(exc).__name__})'


def stream_identity(record):
    name, dataset = record['instrument'], record['dataset']
    base = name.split('_')[0]
    payload = record.get('payload') or {}
    commodity = payload.get('exchangeSegment') == 'MCX_COMM' or base in ('CRUDEOIL', 'NATURALGAS', 'GOLDM')
    options = dataset.startswith('options_')
    segment = 'Commodity futures' if commodity else 'Rolling options' if dataset == 'options_rolling_1m' else 'Options (identity unverified)' if options else 'Index spot'
    exchange = 'MCX' if commodity else 'BSE' if base == 'SENSEX' or payload.get('exchangeSegment', '').startswith('BSE') else 'NSE' if base in ('NIFTY', 'BANKNIFTY') or payload.get('exchangeSegment', '').startswith('NSE') else 'Unknown'
    interval = '1 day' if dataset == 'daily' else '1 minute' if dataset.endswith('_1m') else 'Not recorded'
    group_id = '|'.join([exchange, base, segment, interval])
    return dict(id=dataset+'|'+name, group_id=group_id, dataset=dataset, instrument=name,
                underlying=base, exchange=exchange, segment=segment, interval=interval)


def inventory(root):
    """Footers/stat only; no full candle scans. Physical files are counted once."""
    root = Path(root)
    manifest, error = read_document(root/'ingestion_manifest.json')
    issues = [finding(error, severity='Error', category='integrity')] if error else []
    manifest = manifest or {'chunks': {}}
    chunks = manifest.get('chunks')
    if not isinstance(chunks, dict):
        issues.append(finding('Manifest chunks must be an object.', severity='Error', category='integrity'))
        chunks = {}
    streams, records, seen = {}, [], set()
    for key, original in chunks.items():
        if not isinstance(original, dict) or not all(isinstance(original.get(k), str) for k in ('dataset', 'instrument', 'start', 'end', 'status')):
            issues.append(finding(f'Malformed manifest record: {key}', severity='Error', category='integrity'))
            continue
        record = dict(original, record_id=key)
        try:
            if pd.Timestamp(record['end']) <= pd.Timestamp(record['start']): raise ValueError('Invalid request range')
            if not isinstance(record.get('payload', {}), dict) or not isinstance(record.get('quality', {}), dict): raise ValueError('Invalid metadata object')
        except (ValueError, TypeError):
            issues.append(finding(f'Malformed period or metadata in manifest record: {key}', severity='Error', category='integrity'))
            # Preserve the record for diagnostics; it cannot contribute valid candles.
            records.append(dict(record, status='malformed'))
            continue
        item = stream_identity(record)
        stream = streams.setdefault(item['id'], dict(**item, records=[], files=[], rows=0, bytes=0, first=None, last=None, partitions=0))
        stream['records'].append(record)
        records.append(record)
        if record['status'] != 'complete':
            continue
        try:
            path = safe_path(root, record['path'])
            stamp = file_stamp(path)
            if stamp[0] is None:
                raise FileNotFoundError('Committed partition is missing')
            if str(path) in seen:
                issues.append(finding('Partition path is referenced more than once; counted once.', item['group_id'], 'Error', category='integrity', stream=item['id']))
                continue
            seen.add(str(path))
            stream['bytes'] += stamp[0]
            stream['partitions'] += 1
            meta, problem = parquet_metadata(str(path), stamp)
            if problem:
                issues.append(finding(problem, item['group_id'], 'Error', period=record['start']+' / '+record['end'], category='integrity', stream=item['id']))
                continue
            stream['files'].append(dict(path=str(path), stamp=stamp, record=record))
            stream['rows'] += meta['rows']
            for field, func in [('first', min), ('last', max)]:
                value = meta[field] or record.get(field)
                if value:
                    stream[field] = func(filter(None, [stream[field], value]), key=pd.Timestamp)
        except (OSError, ValueError, KeyError) as exc:
            issues.append(finding(f'Committed partition unavailable ({type(exc).__name__}): {record.get("path", "path not recorded")}', item['group_id'], 'Error', period=record['start']+' / '+record['end'], category='integrity', stream=item['id']))
    groups = {}
    for stream in streams.values():
        group = groups.setdefault(stream['group_id'], {k: stream[k] for k in ('group_id', 'underlying', 'exchange', 'segment', 'interval')})
        group.setdefault('streams', []).append(stream['id'])
        for key in ('rows', 'bytes', 'partitions'):
            group[key] = group.get(key, 0) + stream[key]
        for field, func in [('first', min), ('last', max)]:
            values = list(filter(None, [group.get(field), stream[field]]))
            group[field] = func(values, key=pd.Timestamp) if values else None
    return dict(root=str(root), manifest=manifest, manifest_fingerprint=fingerprint(manifest),
                streams=streams, groups=list(groups.values()), records=records, findings=issues,
                file_fingerprint=file_fingerprint(root, manifest))


def file_fingerprint(root, manifest):
    entries = manifest.get('chunks', {})
    stamps = []
    if isinstance(entries, dict):
        for record in entries.values():
            if isinstance(record, dict) and record.get('status') == 'complete':
                name = record.get('path', '')
                try:
                    stamp = file_stamp(safe_path(root, name))
                except (ValueError, TypeError):
                    stamp = (None, None)
                stamps.append((name, stamp))
    return fingerprint(sorted(stamps))


def calendar_document(root):
    document, error = read_document(Path(root)/'session_calendar.json')
    if document and not error:
        try:
            if not isinstance(document.get('sessions'), list) or not isinstance(document.get('contracts', {}), dict):
                raise ValueError('sessions/contract types')
            seen = set()
            for item in document['sessions']:
                if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ('exchange','date','source')):
                    raise ValueError('session provenance')
                day = pd.Timestamp(item['date']).date().isoformat()
                if day != item['date'] or (item['exchange'], day) in seen or not isinstance(item.get('windows'), list):
                    raise ValueError('ambiguous session')
                seen.add((item['exchange'], day))
                last = None
                for opening, closing in item['windows']:
                    lo, hi = pd.Timestamp(day+' '+opening), pd.Timestamp(day+' '+closing)
                    if hi <= lo or (last is not None and lo < last) or lo.second or hi.second:
                        raise ValueError('invalid session window')
                    last = hi
            for item in document.get('contracts', {}).values():
                if not isinstance(item, dict) or not all(isinstance(item.get(k),str) and item[k] for k in ('start','end_exclusive','source')):
                    raise ValueError('contract provenance')
                if pd.Timestamp(item['end_exclusive']) <= pd.Timestamp(item['start']): raise ValueError('contract period')
        except (ValueError, TypeError, KeyError):
            error = 'session_calendar.json has invalid or ambiguous evidence. Coverage is not verified; repair the calendar.'
    return document or {}, error


def audit_structure_valid(report):
    """Validate the supported report shape independently of lake currentness."""
    def count(value):
        return value is None or (type(value) is int and value >= 0)
    def valid_finding(item):
        return (isinstance(item, dict) and item.get('severity') in ('Error','Warning','Info')
                and all(isinstance(item.get(k), str) for k in ('problem','dataset','category','period','action'))
                and bool(item['problem']) and (item.get('stream') is None or isinstance(item['stream'], str))
                and count(item.get('count')))
    if not isinstance(report, dict) or not report:
        return False
    if not isinstance(report.get('audit_version'), str) or type(report.get('stable_snapshot')) is not bool:
        return False
    if not all(isinstance(report.get(k), str) and re.fullmatch('[0-9a-f]{64}', report[k])
               for k in ('manifest_fingerprint','file_fingerprint','calendar_fingerprint')):
        return False
    if not all(isinstance(report.get(k), list) and all(valid_finding(f) for f in report[k]) for k in ('findings','issues')):
        return False
    if not isinstance(report.get('streams'), dict):
        return False
    try:
        checked = pd.Timestamp(report.get('checked_at'))
        if pd.isna(checked) or checked.tzinfo is None: return False
        for sid, result in report['streams'].items():
            if not isinstance(sid, str) or not isinstance(result, dict) or result.get('status') not in STATUSES:
                return False
            if not isinstance(result.get('dimensions'), dict) or not all(result['dimensions'].get(k) in STATUSES for k in ('integrity','validity','coverage')):
                return False
            if not isinstance(result.get('identity'), str) or type(result.get('rows')) is not int or result['rows'] < 0:
                return False
            if not all(k in result and count(result[k]) for k in ('stored_exact_duplicates','stored_conflicting_rows','invalid_stored_candles')):
                return False
            if not isinstance(result.get('sessions'), list): return False
            if result.get('required_window') is not None:
                from quant_inventory_health import valid_metric
                if not valid_metric(result['required_window']):return False
            for session in result['sessions']:
                if not isinstance(session, dict) or not isinstance(session.get('date'), str): return False
                if pd.Timestamp(session['date']).date().isoformat() != session['date']: return False
                if type(session.get('rows')) is not int or session['rows'] < 0: return False
                if not all(k in session and count(session[k]) for k in ('expected','internal_missing','opening_missing','closing_missing','absent_sessions','unexpected','observed_minute_holes')):
                    return False
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
    return True


def report_state(root, inv=None):
    inv = inv or inventory(root)
    report, error = read_document(Path(root)/'quality_report.json')
    calendar, calendar_error = calendar_document(root)
    status, explanation = 'Not checked', 'No audit has been recorded for this lake.'
    valid = audit_structure_valid(report)
    if error:
        status, explanation = 'Review needed', error
    elif report:
        required = ('audit_version', 'manifest_fingerprint', 'file_fingerprint', 'calendar_fingerprint', 'stable_snapshot', 'streams', 'findings', 'checked_at')
        if not all(k in report for k in required) or report.get('audit_version') != AUDIT_VERSION:
            explanation = 'Legacy or unsupported audit: its scope and manifest binding are unverified. Run a quality check.'
        elif not valid:
            status, explanation = 'Review needed', 'Malformed audit results. Run a quality check; the prior report remains available as evidence.'
        elif (not report['stable_snapshot'] or report['manifest_fingerprint'] != inv['manifest_fingerprint'] or
              report['file_fingerprint'] != inv['file_fingerprint'] or report['calendar_fingerprint'] != fingerprint(calendar)):
            status, explanation = 'Audit outdated', 'The manifest, committed files, or session evidence changed. Run a new quality check.'
        elif set(report['streams']) != set(inv['streams']):
            status, explanation = 'Review needed', 'Audit stream results do not match the manifest inventory. Run a new quality check.'
        else:
            status = 'Review needed' if any(f.get('severity') in ('Error', 'Warning') for f in report['findings']) else 'Checks passed'
            explanation = 'One or more checks need review; see the separate evidence categories below.' if status == 'Review needed' else 'Integrity, candle validity and coverage checks passed within the documented audit scope.'
    if not inv['records'] and not inv['findings'] and not error and not calendar_error:
        status, explanation = 'No data', 'No dataset manifest records are available to inspect.'
    if status == 'Not checked' and any(r['status'] != 'complete' for r in inv['records']):
        status, explanation = 'Review needed', 'Manifest requests include empty, failed, unavailable or incomplete records. A current audit is also needed.'
    if inv['findings']:
        status, explanation = 'Review needed', 'Current local file or manifest problems require attention.'
    if calendar_error:
        status, explanation = 'Review needed', calendar_error
    current = bool(valid and status in ('Checks passed', 'Review needed') and report.get('audit_version') == AUDIT_VERSION
                   and report.get('stable_snapshot') and report.get('manifest_fingerprint') == inv['manifest_fingerprint']
                   and report.get('file_fingerprint') == inv['file_fingerprint'] and report.get('calendar_fingerprint') == fingerprint(calendar)
                   and set(report['streams']) == set(inv['streams'])
                   and isinstance(report.get('streams'), dict) and isinstance(report.get('findings'), list) and not error and not calendar_error)
    return dict(status=status, explanation=explanation, current=current, report=report,
                checked_at=report.get('checked_at') if report else None, error=error or calendar_error)


def require_current_audit(root):
    """Shared UI/loader contract: current warnings are not invalid audit evidence."""
    inv = inventory(root)
    state = report_state(root, inv)
    if not state['current']:
        raise ValueError(f"Quality audit is not usable ({state['status']}): {state['explanation']} "
                         'Run the local quality check (audit_quant_lake.py --root <lake>) before backtesting; it does not download or repair candles.')
    report = state['report']
    if report['issues'] or any(f['severity'] == 'Error' for f in report['findings']+inv['findings']):
        raise ValueError('Lake integrity issues must be resolved before backtesting; inspect Data Quality findings and affected partitions.')
    return report


def group_status(group, inv, state):
    relevant = [f for f in inv['findings'] if f['dataset'] in ('Lake', group['group_id'])]
    records = [r for sid in group['streams'] for r in inv['streams'][sid]['records']]
    if relevant:
        return 'Review needed'
    if state['status'] == 'Audit outdated':
        return 'Audit outdated'
    if any(r['status'] != 'complete' for r in records):
        return 'Review needed'
    if not group['rows']:
        return 'No data'
    if not state['current']:
        return 'Not checked' if not state['error'] else 'Review needed'
    results = state['report']['streams']
    if not all(s in results for s in group['streams']):
        return 'Not checked'
    return 'Checks passed' if all(results[s].get('status') == 'Checks passed' for s in group['streams']) else 'Review needed'


def filtered_groups(inv, state, search='', exchange='All', instrument='All', segment='All', health='All'):
    result = []
    for group in inv['groups']:
        row = dict(group, status=group_status(group, inv, state))
        if exchange != 'All' and row['exchange'] != exchange: continue
        if instrument != 'All' and row['underlying'] != instrument: continue
        if segment != 'All' and row['segment'] != segment: continue
        if health != 'All' and row['status'] != health: continue
        haystack = ' '.join([row['underlying'], row['segment'], row['interval'], *row['streams']]).lower()
        if search.lower() in haystack: result.append(row)
    return sorted(result, key=lambda r: r['group_id'])


@lru_cache(maxsize=128)
def _session_dates(files):
    if not files: return ()
    with duckdb.connect() as db:
        db.execute("SET TimeZone='Asia/Kolkata'")
        return tuple(str(row[0]) for row in db.execute('SELECT DISTINCT timestamp::DATE FROM read_parquet(?, union_by_name=true) ORDER BY 1', [[f[0] for f in files]]).fetchall())


def stream_sessions(stream):
    return _session_dates(tuple((f['path'], f['stamp']) for f in stream['files']))


def preview_candles(stream, day, limit=500):
    """Single stored stream, one IST date, max 1000 rows. Export exactly this frame."""
    limit = max(1, min(int(limit), 1000))
    lo = pd.Timestamp(day).tz_localize(IST)
    hi = lo + pd.Timedelta(days=1)
    files = [f['path'] for f in stream['files'] if pd.Timestamp(f['record']['start']).date() < hi.date() and pd.Timestamp(f['record']['end']).date() > lo.date()]
    if not files: return pd.DataFrame(), 0
    with duckdb.connect() as db:
        db.execute("SET TimeZone='Asia/Kolkata'")
        frame = db.execute('SELECT *, count(*) OVER () AS __preview_total FROM read_parquet(?, union_by_name=true) WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp LIMIT ?', [files, lo.to_pydatetime(), hi.to_pydatetime(), limit]).df()
    totals = frame.pop('__preview_total')
    total = int(totals.iloc[0]) if len(frame) else 0
    if len(frame): frame['timestamp'] = pd.to_datetime(frame.timestamp, utc=True).dt.tz_convert(IST)
    return frame, total


def preview_export(frame, stream, day):
    name = re.sub(r'[^A-Za-z0-9_.-]+', '_', stream['dataset']+'_'+stream['instrument'])
    return frame.to_csv(index=False).encode('utf-8'), f'{name}_{day}_{len(frame)}rows.csv', 'text/csv'


def coverage_measurements(frame, stream, calendar):
    """Expected bars only from explicitly sourced date-specific session windows."""
    by_day = {}
    if len(frame):
        stamps = pd.DatetimeIndex(pd.to_datetime(frame.timestamp, utc=True)).tz_convert(IST).as_unit('ns').unique().sort_values()
        series = pd.Series(stamps)
        for day, group in series.groupby(stamps.strftime('%Y-%m-%d')):
            by_day[day] = pd.DatetimeIndex(group).asi8
    requested = set()
    for r in stream['records']:
        requested.update(str(d.date()) for d in pd.date_range(r['start'], r['end'], inclusive='left'))
    applicability = calendar.get('contracts', {}).get(stream['instrument'], {})
    contract_known = stream['segment'] != 'Commodity futures' or all(applicability.get(k) for k in ('start', 'end_exclusive', 'source'))
    if stream['segment'] == 'Commodity futures' and contract_known:
        requested = {d for d in requested if applicability['start'] <= d < applicability['end_exclusive']}
    schedules = {(r.get('exchange'), r.get('date')): r for r in calendar.get('sessions', []) if isinstance(r, dict)}
    rows = []
    for day in sorted(requested | set(by_day)):
        actual = by_day.get(day, np.array([], dtype='int64'))
        evidence = schedules.get((stream['exchange'], day), {}) if contract_known else {}
        expected = None
        if evidence.get('source') and isinstance(evidence.get('windows'), list) and day in requested:
            parts = [pd.date_range(f'{day} {opening}', f'{day} {closing}', tz=IST, freq='min', inclusive='left').as_unit('ns').asi8 for opening, closing in evidence['windows']]
            expected = np.unique(np.concatenate(parts)) if parts else np.array([], dtype='int64')
        row = dict(date=day, rows=len(actual), expected=None, internal_missing=None, opening_missing=None,
                   closing_missing=None, absent_sessions=None, unexpected=None, evidence=evidence.get('source'), observed_minute_holes=None)
        if stream['interval'] == '1 minute' and len(actual):
            row['observed_minute_holes'] = int(np.maximum(np.diff(actual)//60_000_000_000 - 1, 0).sum())
        if expected is not None:
            row['expected'] = len(expected) if stream['interval'] != '1 day' else int(bool(len(expected)))
            if stream['interval'] == '1 day':
                row.update(internal_missing=0, opening_missing=0, closing_missing=0,
                           absent_sessions=int(bool(len(expected)) and not len(actual)), unexpected=int(bool(len(actual)) and not len(expected)))
            else:
                missing = np.setdiff1d(expected, actual, assume_unique=True)
                inside = np.intersect1d(actual, expected, assume_unique=True)
                row.update(internal_missing=int(((missing > inside[0]) & (missing < inside[-1])).sum()) if len(inside) else 0,
                           opening_missing=int((missing < inside[0]).sum()) if len(inside) else 0,
                           closing_missing=int((missing > inside[-1]).sum()) if len(inside) else 0,
                           absent_sessions=int(bool(len(expected)) and not len(inside)), unexpected=len(np.setdiff1d(actual,expected,assume_unique=True)))
        rows.append(row)
    return rows


def _hash_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def _atomic_report(path, value):
    _atomic_bytes(path, json.dumps(value, indent=2, default=str, allow_nan=False).encode('utf-8'))


def _atomic_bytes(path, content):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, suffix='.tmp', delete=False) as handle:
        temp = Path(handle.name)
        handle.write(content)
        handle.flush(); os.fsync(handle.fileno())
    try: os.replace(temp, path)
    finally: temp.unlink(missing_ok=True)


def run_quality_check(root, progress=None, *, _writer_locked=False):
    """Serialize with ingestion; retain prior report bytes and verify snapshot at commit."""
    from step2_build_data_lake import writer_lock
    from contextlib import nullcontext
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with nullcontext() if _writer_locked else writer_lock(root):
        inv = inventory(root)
        calendar, calendar_error = calendar_document(root)
        findings = list(inv['findings'])
        if calendar_error: findings.append(finding(calendar_error, category='coverage', severity='Error'))
        results = {}
        for index, (sid, stream) in enumerate(inv['streams'].items(), 1):
            if progress: progress(index, len(inv['streams']), stream['instrument'])
            start = len(findings)
            label = stream['group_id']
            def add(problem, severity='Warning', count=None, period='', action='Review source records; audit never repairs data.', category='coverage'):
                findings.append(finding(problem, label, severity, count, period, action, category, sid))
            frames = []
            for record in stream['records']:
                if record['status'] != 'complete':
                    add(f'Manifest record is {record["status"]}: {record.get("error", "No committed candles for this request")}', period=record['start']+' / '+record['end']+' (end exclusive)', category='availability', action='Inspect provider availability and the requested contract period before explicitly retrying ingestion.')
                quality = record.get('quality', {})
                for method,description in [('quarantine_volume_zero','Negative-volume-only candles restored with zero volume'),('synthetic_ffill','Synthetic flat zero-volume candles inserted; these are not observed quotes'),('provider_refetch','Missing candles restored from provider retry')]:
                    repairs=[e for e in record.get('repairs',[]) if e.get('method')==method]
                    if repairs:add(description,'Info',len(repairs),record['start']+' / '+record['end'],category='repair',action='Inspect repair provenance and original partition in repair_history; assess sensitivity before relying on repaired-input results.')
                for field, text in [('exact_duplicates', 'Exact duplicate input rows removed'), ('conflicting_duplicate_rows', 'Conflicting input rows rejected / quarantined'), ('invalid_ohlcv', 'Invalid input candles rejected'), ('outside_market_hours', 'Out-of-hours input rows rejected')]:
                    if quality.get(field, 0):
                        add(text, 'Info' if field == 'exact_duplicates' else 'Warning', quality[field], record['start']+' / '+record['end'], category='validity', action='Inspect quarantine and ingestion evidence; rejected rows are excluded from stored candle counts.')
                        if field=='invalid_ohlcv':
                            restored=sum(e.get('method')=='quarantine_volume_zero' for e in record.get('repairs',[]))
                            if restored:
                                findings[-1]['resolved_count']=restored
                                findings[-1]['action']=f'{restored} negative-volume-only candles restored with volume zero; raw rejection evidence retained. Other invalid input, if any, remains unresolved. Inspect the repair log and required-window eligibility.'
                                if restored==quality[field]:findings[-1]['category']='repair'
            for file in stream['files']:
                record = file['record']
                try:
                    if not record.get('sha256'):
                        add('No recorded checksum; file integrity is not verified.', category='integrity')
                    elif _hash_file(file['path']) != record['sha256']:
                        add('Checksum mismatch.', 'Error', period=record['start']+' / '+record['end'], category='integrity', action='Restore the expected partition or investigate the changed file; do not trust its candles.')
                    frame = pd.read_parquet(file['path'])
                    if not {'timestamp', 'open', 'high', 'low', 'close', 'volume'}.issubset(frame):
                        raise ValueError('Required candle columns are missing')
                    frame['timestamp'] = pd.to_datetime(frame.timestamp, utc=True, errors='raise').dt.tz_convert(IST)
                    if frame.timestamp.isna().any(): raise ValueError('Null timestamps')
                    if len(frame) != record.get('rows'):
                        add('Stored row count differs from manifest.', 'Error', count=len(frame), category='integrity')
                    outside = (frame.timestamp.dt.date < pd.Timestamp(record['start']).date()) | (frame.timestamp.dt.date >= pd.Timestamp(record['end']).date())
                    if outside.any(): add('Stored candles fall outside the requested partition period.', 'Error', int(outside.sum()), category='validity')
                    frames.append(frame)
                except Exception as exc:
                    add(f'Partition could not be audited ({type(exc).__name__}).', 'Error', period=record['start']+' / '+record['end'], category='integrity', action='Inspect or restore the partition; its session coverage is not verified.')
            frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=['timestamp'])
            exact = conflicts = invalid = None
            if len(frame):
                exact = int(frame.duplicated().sum())
                distinct = frame.drop_duplicates()
                conflicts = int(distinct.timestamp.duplicated(keep=False).sum())
                values = frame[['open', 'high', 'low', 'close', 'volume']].apply(pd.to_numeric, errors='coerce')
                bad = ~np.isfinite(values).all(axis=1) | values.lt(0).any(axis=1) | values.high.lt(values[['open','close','low']].max(axis=1)) | values.low.gt(values[['open','close','high']].min(axis=1))
                if 'oi' in frame: bad |= pd.to_numeric(frame.oi, errors='coerce').lt(0)
                invalid = int(bad.sum())
                for count, text in [(exact, 'Exact duplicates remain in stored candles'), (conflicts, 'Unresolved conflicting timestamp rows remain stored'), (invalid, 'Invalid stored OHLCV candles')]:
                    if count: add(text, 'Error', count, category='validity')
            complete_read = len(frames) == sum(r['status'] == 'complete' for r in stream['records'])
            try:
                sessions = coverage_measurements(frame, stream, calendar if not calendar_error else {}) if complete_read else []
            except (ValueError, TypeError, KeyError) as exc:
                sessions = []
                add(f'Session evidence is malformed ({type(exc).__name__}); coverage not verified.', category='coverage')
            unknown = sum(r['expected'] is None for r in sessions)
            if unknown or not sessions:
                add('Coverage not verified: date-specific exchange sessions or contract applicability evidence is missing.', count=unknown or None, action='Supply sourced session_calendar.json entries, including holidays/special sessions and contract applicability; rerun the audit.')
            for row in sessions:
                for field, text in [('internal_missing','Internal expected candle gaps'), ('opening_missing','Missing opening candles'), ('closing_missing','Missing closing candles'), ('absent_sessions','Entire expected session absent'), ('unexpected','Candles outside the evidenced session')]:
                    if row[field]: add(text, count=row[field], period=row['date'], action='Compare this session with exchange evidence and the requested stream. Missing data is not filled.')
                if row['expected'] is None and row['rows'] == 1 and stream['interval'] != '1 day':
                    add('Only one candle observed; session coverage cannot pass.', count=1, period=row['date'])
                if row['expected'] is None and row['observed_minute_holes']:
                    add('Minute-spacing holes between observed candles; not verified missing exchange candles.', count=row['observed_minute_holes'], period=row['date'], action='Compare the observed spacing with sourced session breaks before interpreting it as missing candles.')
            identity = 'Not applicable'
            if stream['dataset'].startswith('options_'):
                identity = 'Unverified — rolling ATM-relative stream' if stream['dataset'] == 'options_rolling_1m' else 'Unverified — recorded identifiers require validation'
                add('Options contract / expiry identity is unverified. Calendar-inferred expiry is not contract evidence.', category='identity', action='Obtain validated fixed-contract identifiers and expiry provenance before claiming contract-level history.')
            own = findings[start:] + [f for f in inv['findings'] if f.get('stream') == sid]
            dims = {category: 'Review needed' if any(f['category'] == category and f['severity'] != 'Info' for f in own) else 'Checks passed' for category in ('integrity', 'validity', 'coverage')}
            if not len(frame): dims['validity'] = 'No data'
            if not stream['files'] and not any(f['category']=='integrity' for f in own): dims['integrity']='No data'
            from quant_inventory_health import measure_stream
            required=measure_stream(root,stream,frame,error='Partition integrity not verified' if not complete_read or dims['integrity']=='Review needed' else None)
            # Rejected out-of-window rows and resolved quarantine are historical
            # evidence, not unresolved required-window candle invalidity.
            if required is not None and not any(f['severity']=='Error' and f['category']=='validity' for f in own):dims['validity']=required['validity']
            results[sid] = dict(status='Review needed' if any(f['severity'] != 'Info' for f in own) else 'Checks passed',
                                dimensions=dims, identity=identity, sessions=sessions, rows=len(frame),
                                stored_exact_duplicates=exact, stored_conflicting_rows=conflicts, invalid_stored_candles=invalid,required_window=required)
        from quant_inventory_health import apply_conflicts
        measured={sid:r['required_window'] for sid,r in results.items() if r['required_window'] is not None}
        apply_conflicts(measured,inv)
        for sid,metric in measured.items():
            results[sid]['required_window']=metric
            if metric['validity']=='Review needed':results[sid]['dimensions']['validity']='Review needed'
        current, read_error = read_document(root/'ingestion_manifest.json')
        current = current or {'chunks': {}}
        current_calendar, current_calendar_error = calendar_document(root)
        stable = not read_error and not current_calendar_error and fingerprint(current) == inv['manifest_fingerprint'] and file_fingerprint(root, current) == inv['file_fingerprint'] and fingerprint(current_calendar) == fingerprint(calendar)
        if not stable: findings.append(finding('The lake changed during this audit. Results are outdated; rerun after ingestion finishes.', category='integrity'))
        report = dict(audit_version=AUDIT_VERSION, checked_at=pd.Timestamp.now(tz=IST).isoformat(),
                      manifest_fingerprint=inv['manifest_fingerprint'], file_fingerprint=inv['file_fingerprint'],
                      calendar_fingerprint=fingerprint(calendar), stable_snapshot=stable,
                      streams=results, findings=findings, statuses=dict(Counter(r['status'] for r in inv['records'])),
                      # Retain the existing backtest reader's integrity gate. Its execution rules are unchanged.
                      issues=[f for f in findings if f['severity']=='Error' or (f['category']=='availability' and 'is failed:' in f['problem'])],
                      scope='Committed manifest files; expected sessions only from explicit local session evidence. No downloads, repairs or broker calls.')
        previous = root/'quality_report.json'
        if previous.exists():
            archive = root/'audit_history'; archive.mkdir(exist_ok=True)
            content = previous.read_bytes()
            target = archive/(hashlib.sha256(content).hexdigest()+'.json')
            if not target.exists(): _atomic_bytes(target, content)
        _atomic_report(previous, report)
        from quant_inventory_health import inventory_health
        inventory_health(root,force=True)
        return report
