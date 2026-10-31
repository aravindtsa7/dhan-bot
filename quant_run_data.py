"""Read-only, run-window checks; these do not replace historical lake audits."""
from pathlib import Path
import hashlib

import numpy as np
import pandas as pd

from quant_lake import IST


def required_windows(mcx):
    # Indicators are reset daily, but EMA/ATR/ADX/VWAP consume the morning
    # history, including MCX's morning candles before the evening setup.
    # Keep the loader's six option streams: fixed-strike confirmation and the
    # continuity model can use either side and neighboring ATM offsets.
    return dict(underlying=['09:00', '23:15'] if mcx else ['09:15', '15:20'],
                options=None if mcx else ['09:15', '15:20'], inclusive=True,
                basis='Full intraday indicator warm-up, signal construction, entry, management and engine square-off; Asia/Kolkata')


def minute_of(value):
    h, m = map(int, value.split(':'))
    return h*60+m


def verify_files(root, records):
    """Verify available recorded checksums, including selection-context files."""
    for record in records:
        path = Path(root)/record['path']
        if not path.is_file():
            raise ValueError(f"Missing partition: {record['path']}")
        if record.get('sha256'):
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(1024*1024), b''):
                    digest.update(block)
            if digest.hexdigest() != record['sha256']:
                raise ValueError(f"Partition checksum mismatch: {record['path']}")


def prior_volume(lake, records, end):
    """Small daily aggregates over committed contracts; never execution-day volume.

    All earlier observed dates are available, so holidays/weekends and arbitrary
    request boundaries cannot alter the immediately preceding observed session.
    Selection keeps pandas' old sorted-column idxmax tie break (lexical name).
    """
    if not records:
        return pd.DataFrame()
    files = [str(lake.root/r['path']) for r in records]
    hi = pd.Timestamp(end, tz=IST)
    return lake.db.execute("""SELECT CAST(timestamp AS DATE)::VARCHAR AS date,
        instrument, sum(volume) AS volume,
        count(*) != count(DISTINCT timestamp) OR
        count(*) FILTER (WHERE volume IS NULL OR NOT isfinite(volume) OR volume < 0) > 0 AS invalid
        FROM read_parquet(?, union_by_name=true) WHERE timestamp < ?
        GROUP BY 1,2 ORDER BY 1,2""", [files, hi.to_pydatetime()]).df()


def select_contracts(daily, dates):
    selected = {}
    observed = sorted(daily.date.unique()) if len(daily) else []
    for date in dates:
        previous = [d for d in observed if d < date]
        if not previous:
            selected[date] = (None, None, 'No preceding observed session for contract-selection volume')
            continue
        prior = previous[-1]
        rows = daily[daily.date.eq(prior)].sort_values('instrument')
        # Recorded FUTCOM names include expiry. A previous-session winner
        # that has expired cannot be traded on the execution date.
        import re
        rows=rows[rows.instrument.map(lambda name: not (m:=re.search(r'_(\d{4}-\d{2}-\d{2})_\d+$',name)) or m[1]>=date)]
        if rows.empty:
            selected[date]=(None,prior,'No unexpired contract with preceding-session volume evidence')
            continue
        if rows.invalid.any():
            selected[date] = (None, prior, 'Invalid or duplicate preceding-session volume evidence')
            continue
        contract = rows.set_index('instrument').volume.idxmax()
        selected[date] = (contract, prior, None)
    return selected


def stream_check(frame, date, stream, window, legacy_flag=False):
    begin, finish = map(minute_of, window)
    expected = pd.date_range(f'{date} {window[0]}', f'{date} {window[1]}', freq='min', tz=IST)
    stamps = pd.DatetimeIndex(frame.timestamp) if len(frame) else pd.DatetimeIndex([], tz=IST)
    missing = expected.difference(stamps)
    reasons, notes = [], []
    if len(missing):
        reasons.append(dict(problem='Missing required candles', stream=stream,
                            count=len(missing), timestamps=[t.isoformat() for t in missing]))
    if len(frame):
        minutes = frame.timestamp.dt.hour*60+frame.timestamp.dt.minute
        required = frame[minutes.between(begin, finish)]
        values = required[['open', 'high', 'low', 'close', 'volume']].apply(pd.to_numeric, errors='coerce')
        bad = ~np.isfinite(values).all(axis=1) | values.lt(0).any(axis=1)
        bad |= values.high.lt(values[['open','close','low']].max(axis=1)) | values.low.gt(values[['open','close','high']].min(axis=1))
        for field in ('oi', 'iv', 'spot', 'strike'):
            if field in required:
                numeric = pd.to_numeric(required[field], errors='coerce')
                bad |= ~np.isfinite(numeric) | numeric.lt(0)
        if bad.any():
            reasons.append(dict(problem='Invalid required candles', stream=stream, count=int(bad.sum()),
                                timestamps=[t.isoformat() for t in required.loc[bad, 'timestamp']]))
        if legacy_flag:
            # The legacy flag has no location. Re-measure stored timestamps;
            # record its presence rather than treating it as a window veto.
            spacing_holes = pd.date_range(stamps.min(), stamps.max(), freq='min').difference(stamps)
            outside = spacing_holes.difference(expected)
            notes.append(dict(stream=stream, problem='Legacy whole-session short flag re-evaluated against stored timestamps and required window; historical flag retained',
                              required_missing_count=len(missing),
                              observed_spacing_holes_outside_window=[t.isoformat() for t in outside],
                              qualification='Observed spacing only; no claim of complete exchange-session coverage or a verified original flag cause'))
    return reasons, notes


def rejected_evidence(root, records, date, stream, window):
    """Localize rejected invalid/conflicting inputs; unavailable evidence fails closed."""
    reasons, notes = [], []
    for record in records:
        if record['instrument'] != stream or not record['start'] <= date < record['end']:
            continue
        quality = record.get('quality', {})
        if not any(quality.get(k, 0) for k in ('invalid_ohlcv', 'conflicting_duplicate_rows')):
            continue
        try:
            rejected = pd.read_parquet(Path(root)/record['quarantine_path'])
            rejected['timestamp'] = pd.to_datetime(rejected.timestamp, utc=True).dt.tz_convert(IST)
            if rejected.timestamp.isna().any():
                raise ValueError('Null quarantine timestamps')
            for field, reason in [('invalid_ohlcv','invalid_ohlcv'), ('conflicting_duplicate_rows','conflicting_duplicate_timestamp')]:
                if int(rejected.rejection_reason.eq(reason).sum()) < quality.get(field, 0):
                    raise ValueError('Quarantine does not account for the rejected aggregate')
            relevant = rejected[rejected.timestamp.dt.strftime('%Y-%m-%d').eq(date)]
            lo, hi = [pd.Timestamp(f'{date} {v}', tz=IST) for v in window]
            relevant = relevant[relevant.timestamp.between(lo, hi)]
            relevant = relevant[relevant.rejection_reason.isin(['invalid_ohlcv', 'conflicting_duplicate_timestamp'])]
            # Raw quarantine remains immutable evidence. Reconcile only the
            # negative-volume-only rows explicitly restored by the writer.
            from quant_repairs import negative_only
            recovered={pd.Timestamp(e['timestamp']) for e in record.get('repairs',[]) if e.get('method')=='quarantine_volume_zero'}
            resolved=relevant.apply(lambda r:r.rejection_reason=='invalid_ohlcv' and r.timestamp in recovered and negative_only(r),axis=1) if len(relevant) else pd.Series(False,index=relevant.index)
            if resolved.any():notes.append(dict(stream=stream,problem='Negative-volume-only quarantine candles restored with volume zero; original evidence retained',count=int(resolved.sum())))
            relevant=relevant[~resolved]
            if len(relevant):
                reasons.append(dict(problem='Rejected invalid/conflicting input in required window', stream=stream,
                                    count=len(relevant), timestamps=sorted({t.isoformat() for t in relevant.timestamp})))
            else:
                notes.append(dict(stream=stream, problem='Partition rejected-input evidence inspected; no required-window invalid/conflicting input on this date'))
        except (OSError, KeyError, ValueError, TypeError):
            reasons.append(dict(problem='Rejected invalid/conflicting aggregate cannot be localized; quarantine evidence unavailable or malformed', stream=stream))
    return reasons, notes
