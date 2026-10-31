"""Evidence-bound inventory metrics and separate engine checks; no broker access.

Session percentages count observed dates, not missing historical trading days.
Bar percentages count unique valid measured minutes, including labeled repairs.
Futures inventory measures observed continuity; engine checks retain full windows.
"""
import hashlib
from pathlib import Path
from functools import lru_cache

import numpy as np
import pandas as pd

from quant_data_quality import (inventory, report_state, read_document, fingerprint,
    file_stamp, calendar_document, _atomic_report)
from quant_run_data import required_windows, stream_check, rejected_evidence, select_contracts
from quant_calendar import regular_session

HEALTH_VERSION='2.0'
REQUIRED_VERSION='1.2'  # Engine dependency measurements remain unchanged.
ROW_LABELS=('NIFTY SPOT','NIFTY OPTIONS','SENSEX SPOT','SENSEX OPTIONS','BANKNIFTY SPOT','CRUDEOIL FUT','GOLDM FUT','NATURALGAS FUT')


def valid_metric(metric):
    try:
        if not isinstance(metric,dict) or not isinstance(metric['sessions'],list):return False
        if metric['error'] is not None and not isinstance(metric['error'],str):return False
        sessions=metric['sessions']
        for row in sessions:
            if type(row['ready']) is not bool or pd.Timestamp(row['date']).date().isoformat()!=row['date']:return False
            if any(type(row[k]) is not int or row[k]<0 for k in ('expected','valid_bars','repaired')) or row['valid_bars']>row['expected']:return False
            if not isinstance(row['issues'],list) or any(not isinstance(i,dict) or not isinstance(i.get('problem'),str) for i in row['issues']):return False
        if len({r['date'] for r in sessions})!=len(sessions):return False
        expected=totals(sessions,metric['error'])
        return all(metric.get(k)==expected[k] for k in ('stored','ready','excluded','percent','expected_bars','valid_bars','bars_percent','repaired'))
    except (ValueError,TypeError,KeyError,OverflowError):return False


def totals(sessions,error=None):
    count=len(sessions);ready=sum(s['ready'] for s in sessions)
    expected=sum(s['expected'] for s in sessions);valid=sum(s['valid_bars'] for s in sessions)
    return dict(stored=count,ready=ready if not error else None,excluded=count-ready if not error else None,
                percent=100*ready/count if count and not error else None,
                expected_bars=expected,valid_bars=valid if not error else None,bars_percent=100*valid/expected if expected and not error else None,
                repaired=sum(s.get('repaired',0) for s in sessions),sessions=sessions,error=error)


def quarantine_key(root,stream):
    return fingerprint([(r['quarantine_path'],file_stamp(Path(root)/r['quarantine_path'])) for r in stream['records'] if r.get('quarantine_path')])


@lru_cache(maxsize=32)
def _full_special_dates(files):
    import duckdb
    if not files:return frozenset()
    try:
        with duckdb.connect() as db:
            db.execute("SET TimeZone='Asia/Kolkata'")
            rows=db.execute("""SELECT CAST(timestamp AS DATE)::VARCHAR FROM read_parquet(?,union_by_name=true)
                WHERE CAST(timestamp AT TIME ZONE 'Asia/Kolkata' AS TIME) BETWEEN TIME '09:15' AND TIME '15:29'
                GROUP BY 1 HAVING count(*)>=370 AND count(*)=count(DISTINCT timestamp)
                AND count(*) FILTER (WHERE NOT coalesce(isfinite(open) AND isfinite(high) AND isfinite(low)
                    AND isfinite(close) AND isfinite(volume) AND open>0 AND low>0 AND close>0 AND volume>=0
                    AND high>=greatest(open,low,close) AND low<=least(open,high,close),false))=0""",[[f[0] for f in files]]).fetchall()
        return frozenset(row[0] for row in rows if not regular_session(row[0]))
    except Exception:return frozenset()


def option_special_dates(root,product):
    inv=inventory(root);underlying=inv['streams'].get('spot_1m|'+product,{})
    return _full_special_dates(tuple((f['path'],f['stamp']) for f in underlying.get('files',[])))


def measure_stream(root,stream,frame,*,error=None,observed_span=False):
    """Strict engine dependencies by default; optional MCX observed-span inventory.

    An observed span checks interior continuity, never historical opening/closing
    coverage or strategy eligibility. It cannot verify a one-candle session.
    """
    if stream['interval']!='1 minute':return None
    sessions=[];mcx=stream['underlying'] in ('CRUDEOIL','GOLDM','NATURALGAS')
    window=required_windows(mcx)['underlying'];name=stream['instrument']
    try:
        frame=frame.copy();frame['timestamp']=pd.to_datetime(frame.timestamp,utc=True).dt.tz_convert('Asia/Kolkata')
        for day,g in frame.groupby(frame.timestamp.dt.strftime('%Y-%m-%d'),sort=True):
            if observed_span:
                if not mcx:raise ValueError('Observed-span mode is only for futures inventory')
                window=(g.timestamp.min().strftime('%H:%M'),g.timestamp.max().strftime('%H:%M'))
            reasons,_=stream_check(g,day,name,window)
            rejected,_=rejected_evidence(root,stream['records'],day,name,window)
            reasons+=rejected
            expected=pd.date_range(day+' '+window[0],day+' '+window[1],freq='min',tz='Asia/Kolkata')
            required=g[g.timestamp.isin(expected)]
            duplicated=required.timestamp.duplicated(keep=False)
            if duplicated.any():reasons.append(dict(problem='Duplicate required timestamps',timestamps=[t.isoformat() for t in required.loc[duplicated,'timestamp']],count=int(duplicated.sum())))
            if stream['dataset'].startswith('options_') and not {'oi','iv','spot','strike','option_type'}.issubset(g):
                reasons.append(dict(problem='Required option quote metadata is unavailable',count=len(required)))
            if observed_span and g.timestamp.nunique()<2:
                reasons.append(dict(problem='Insufficient observations to verify intraday continuity',count=1,timestamps=[g.timestamp.min().isoformat()]))
            validity=[r for r in reasons if r['problem']!='Missing required candles']
            bad={pd.Timestamp(t) for r in validity for t in r.get('timestamps',[])}
            unknown=any(not r.get('timestamps') for r in validity)
            if unknown:error='Required-window validity evidence is unavailable or cannot be localized; run a quality check and inspect the source findings'
            valid=len(expected.intersection(pd.DatetimeIndex(required.timestamp)).difference(pd.DatetimeIndex(list(bad)))) if not unknown else 0
            if not mcx and not regular_session(day):
                regular=g[g.timestamp.dt.strftime('%H:%M').between('09:15','15:29')]
                p=regular[['open','high','low','close','volume']]
                dense=(regular.timestamp.nunique()>=370 and not regular.timestamp.duplicated().any() and np.isfinite(p).all().all()
                       and p.volume.ge(0).all() and p[['open','high','low','close']].gt(0).all().all()
                       and p.high.ge(p[['open','low','close']].max(axis=1)).all() and p.low.le(p[['open','high','close']].min(axis=1)).all())
                if stream['dataset'].startswith('options_'):dense=day in option_special_dates(root,stream['underlying'])
                if not dense:reasons.append(dict(problem='No regular or observed full-length special session',count=None))
            # Contract selection/expiry are additionally enforced at FUT row level.
            repair_count=int(required.candle_source.isin(['synthetic_ffill','quarantine_volume_zero']).sum()) if 'candle_source' in required else 0
            issues=[dict(problem=r['problem'],count=r.get('count'),timestamps=r.get('timestamps',[])[:8],
                         timestamps_truncated=len(r.get('timestamps',[]))>8) for r in reasons]
            volume=float(g.volume.sum())
            sessions.append(dict(date=day,ready=not reasons,expected=len(expected),valid_bars=valid,repaired=repair_count,
                                 validity_issues=bool(validity),issues=issues,volume=volume if np.isfinite(volume) else None,
                                 selection_invalid=bool(g.timestamp.duplicated().any() or not np.isfinite(g.volume).all() or g.volume.lt(0).any())))
    except Exception as exc:error=f'Required-window evidence unavailable ({type(exc).__name__})'
    result=totals(sessions,error)
    result.update(health_version=REQUIRED_VERSION,measurement='observed_span' if observed_span else 'required_window',quarantine_fingerprint=quarantine_key(root,stream))
    result['validity']='Review needed' if error or any(s['validity_issues'] for s in sessions) else 'Checks passed' if sessions else 'No data'
    return result


def cross_conflicts(inv):
    """Find disputed same-strike quotes across rolling streams, within required hours."""
    import duckdb
    result=[]
    for product in ('NIFTY','SENSEX'):
        required={f'{product}_{side}_{offset}_WEEK1' for side in ('CE','PE') for offset in ('ATM','ATM-1','ATM+1')}
        streams=[s for s in inv['streams'].values() if s['dataset']=='options_rolling_1m' and s['instrument'] in required]
        files=[f['path'] for s in streams for f in s['files']]
        if not files:continue
        try:
            with duckdb.connect() as db:
                db.execute("SET TimeZone='Asia/Kolkata'")
                rows=db.execute("""SELECT CAST(timestamp AS VARCHAR), string_agg(DISTINCT instrument, ',') FROM read_parquet(?,union_by_name=true)
                    WHERE CAST(timestamp AT TIME ZONE 'Asia/Kolkata' AS TIME) BETWEEN TIME '09:15' AND TIME '15:20'
                    GROUP BY timestamp,option_type,strike
                    HAVING count(DISTINCT row(open,high,low,close,volume,oi,iv,spot))>1""",[files]).fetchall()
            for stamp,names in rows:result.append((stamp,names.split(',')))
        except Exception:
            # Missing schema is already a per-stream measurement failure. Never
            # advertise cross-stream verification if comparison could not run.
            result.extend((None,[s['instrument']]) for s in streams if s['rows'])
    return result


def apply_conflicts(measured,inv):
    for stamp,names in cross_conflicts(inv):
        for sid,s in inv['streams'].items():
            if s['instrument'] not in names or sid not in measured or measured[sid] is None:continue
            metric=measured[sid]
            if stamp is None:
                metric['error']='Cross-stream option quote comparison unavailable';metric.update(percent=None,ready=None,excluded=None,bars_percent=None,validity='Review needed');continue
            day=pd.Timestamp(stamp).strftime('%Y-%m-%d')
            session=next((r for r in metric['sessions'] if r['date']==day),None)
            if session:
                if any(i['problem']=='Conflicting same-strike quotes across rolling streams' and pd.Timestamp(stamp).isoformat() in i.get('timestamps',[]) for i in session['issues']):continue
                session.update(ready=False,validity_issues=True,valid_bars=max(0,session['valid_bars']-1))
                session['issues'].append(dict(problem='Conflicting same-strike quotes across rolling streams',count=1,timestamps=[pd.Timestamp(stamp).isoformat()]))
    for sid,metric in list(measured.items()):
        if metric is not None:
            measured[sid]={**metric,**totals(metric['sessions'],metric['error']),
                           'validity':'Review needed' if metric['error'] or any(s['validity_issues'] for s in metric['sessions']) else metric['validity']}


def rows_from_streams(inv,measured,*,observed_futures=False):
    rows={}
    for label in ROW_LABELS:
        product,segment=label.split();ds='options_rolling_1m' if segment=='OPTIONS' else 'spot_1m'
        streams=[s for s in inv['streams'].values() if s['underlying']==product and s['dataset']==ds]
        observed=sorted({r['date'] for s in streams for r in (measured.get(s['id']) or {}).get('sessions',[])})
        metrics={s['instrument']:measured.get(s['id']) for s in streams}
        required=[f'{product}_{side}_{offset}_WEEK1' for side in ('CE','PE') for offset in ('ATM','ATM-1','ATM+1')] if segment=='OPTIONS' else [s['instrument'] for s in streams]
        lookup={name:{r['date']:r for r in (metric or {}).get('sessions',[])} for name,metric in metrics.items()}
        selections={}
        if segment=='FUT' and not observed_futures:
            daily=pd.DataFrame([dict(date=d,instrument=name,volume=r['volume'],invalid=r['selection_invalid']) for name,days in lookup.items() for d,r in days.items()])
            selections=select_contracts(daily,observed)
        merged=[]
        for day in observed:
            names=required;selection_error=None
            if segment=='FUT':
                if observed_futures:
                    names=[name for name,days in lookup.items() if day in days]
                else:
                    contract,prior,selection_error=selections[day];names=[contract]
            expected=856 if segment=='FUT' else 366
            parts=[lookup.get(name,{}).get(day) for name in names]
            issues=[dict(r,stream=name) for name,part in zip(names,parts) if part for r in part['issues']]
            for name,part in zip(names,parts):
                if part is None:issues.append(dict(problem='Required stream has no candles on this observed row date',stream=name,count=expected,timestamps=[]))
            if selection_error:issues.append(dict(problem=selection_error,stream=names[0],count=None,timestamps=[]))
            merged.append(dict(date=day,ready=not issues,expected=sum(p['expected'] for p in parts if p) if segment=='FUT' and observed_futures else expected*len(names),valid_bars=sum(p['valid_bars'] for p in parts if p),
                               repaired=sum(p['repaired'] for p in parts if p),issues=issues,selected_contract=names[0] if segment=='FUT' else None))
        error=next((m['error'] for name,m in metrics.items() if name in required and m and m['error']),None)
        rows[label]=totals(merged,error)
    return rows


def evidence_key(root,inv,state):
    from quant_backtester import ENGINE_VERSION
    calendar,_=calendar_document(root)
    return fingerprint(dict(version=HEALTH_VERSION,engine=ENGINE_VERSION,manifest=inv['manifest_fingerprint'],files=inv['file_fingerprint'],
        calendar=fingerprint(calendar),audit=hashlib.sha256((Path(root)/'quality_report.json').read_bytes()).hexdigest() if (Path(root)/'quality_report.json').exists() else None,
        quarantine=[(r.get('quarantine_path'),file_stamp(Path(root)/r['quarantine_path'])) for r in inv['records'] if r.get('quarantine_path')]))


def inventory_health(root,inv=None,state=None,*,force=False):
    """Read the fingerprint-bound disk cache or compute it once, without ingestion."""
    root=Path(root);inv=inv or inventory(root);state=state or report_state(root,inv);key=evidence_key(root,inv,state)
    path=root/'inventory_health.json';cached,_=read_document(path)
    if (not force and cached and cached.get('key')==key and cached.get('health_version')==HEALTH_VERSION
        and set(cached.get('rows',{}))==set(ROW_LABELS) and all(valid_metric(m) for m in cached['rows'].values())
        and set(cached.get('engine_rows',{}))==set(ROW_LABELS) and all(valid_metric(m) for m in cached['engine_rows'].values())
        and isinstance(cached.get('streams'),dict) and all(valid_metric(m) for m in cached['streams'].values())):return cached
    measured={}
    if state['current']:
        for sid,s in inv['streams'].items():
            if s['interval']!='1 minute':continue
            audit=state['report']['streams'][sid]
            metric=audit.get('required_window')
            if metric is None or metric.get('health_version')!=REQUIRED_VERSION or metric.get('quarantine_fingerprint')!=quarantine_key(root,s):
                try:
                    frames=[pd.read_parquet(f['path']) for f in s['files']]
                    frame=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=['timestamp'])
                    metric=measure_stream(root,s,frame,error='Partition integrity not verified' if audit['dimensions']['integrity'] not in ('Checks passed','No data') else None)
                except Exception as exc:metric=totals([],f'Stream unreadable ({type(exc).__name__})')
            measured[sid]=metric
        apply_conflicts(measured,inv)
        engine_rows=rows_from_streams(inv,measured)
        inventory_streams=dict(measured)
        for sid,stream in inv['streams'].items():
            if stream['underlying'] not in ('CRUDEOIL','GOLDM','NATURALGAS') or stream['interval']!='1 minute':continue
            try:
                frames=[pd.read_parquet(f['path']) for f in stream['files']]
                frame=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=['timestamp'])
                inventory_streams[sid]=measure_stream(root,stream,frame,error=measured[sid]['error'],observed_span=True)
            except Exception as exc:inventory_streams[sid]=totals([],f'Continuity evidence unavailable ({type(exc).__name__})')
        rows=rows_from_streams(inv,inventory_streams,observed_futures=True)
    else:
        rows={label:totals([],state['status']+': '+state['explanation']) for label in ROW_LABELS}
        engine_rows=rows
    if evidence_key(root,inventory(root),report_state(root))!=key:
        return dict(key=key,health_version=HEALTH_VERSION,rows={label:totals([],'Lake changed while measuring; reload after ingestion finishes') for label in ROW_LABELS},streams={},current=False)
    result=dict(key=key,health_version=HEALTH_VERSION,rows=rows,engine_rows=engine_rows,streams=measured,current=state['current'],
        audit_blocked=bool(state['report'] and state['report'].get('issues')) or any(f['severity']=='Error' for f in inv['findings']),
        checked_at=pd.Timestamp.now(tz='Asia/Kolkata').isoformat(),scope='Individual index rows: required candles. Futures inventory: continuity between each observed contract/session first and last candle, without inferred opening/closing coverage. Engine rows retain strict dependency windows and prior-session selection; historical holiday coverage and rolling expiry identity remain separate. Repaired inputs count as present; strict-observed runs reject synthetic bars.')
    try:
        root.mkdir(parents=True,exist_ok=True);_atomic_report(path,result)
    except OSError:result['cache_warning']='Metrics computed, but the local cache could not be saved.'
    return result


def clean_label(metric,*,spot=False):
    if metric.get('error'):return 'Audit required' if not metric['stored'] else 'Verification unavailable'
    if not metric['stored']:return 'No stored sessions'
    percent=metric['percent'];mark='✓' if percent>=95 else '⚠️'
    # Rounding never converts a nonzero exclusion count to a 100% claim.
    return f"{percent_label(percent)} Clean {mark}"


def percent_label(value):
    if value is None:return 'Unverified'
    if value<100 and round(value,1)==100:
        import math
        return f'{math.floor(value*10000)/10000:.4f}%'
    return f'{value:.1f}%'


def health_summary(health,inv):
    metrics=health.get('engine_rows',health['rows'])
    sessions={};by_row={label:{r['date']:r for r in metric['sessions']} for label,metric in metrics.items()}
    for label,metric in metrics.items():
        for row in metric['sessions']:
            key=(label.split()[0],row['date'])
            sessions[key]=sessions.get(key,True) and row['ready']
    for (product,day),ready in list(sessions.items()):
        if product in ('NIFTY','SENSEX'):
            sessions[product,day]=all(by_row[product+' '+segment].get(day,{}).get('ready',False) for segment in ('SPOT','OPTIONS'))
    ready=sum(sessions.values());total=len(sessions)
    repairs={(r['dataset'],r['instrument'],e['timestamp'],e['method']) for r in inv['records'] for e in r.get('repairs',[]) if e['method'] in ('quarantine_volume_zero','synthetic_ffill')}
    return dict(stored=total,ready=ready,excluded=total-ready,percent=100*ready/total if total else None,
                restored=sum(e[-1]=='quarantine_volume_zero' for e in repairs),filled=sum(e[-1]=='synthetic_ffill' for e in repairs),
                error=not total or not health['current'] or bool(health.get('audit_blocked')) or any(m['error'] for m in metrics.values()))
