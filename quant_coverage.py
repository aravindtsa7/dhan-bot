"""Bounded, read-only coverage inspection using the versioned research loader."""
from datetime import date, timedelta
from pathlib import Path
import re

import pandas as pd

from quant_data_quality import inventory, report_state, stream_sessions, calendar_document
from quant_calendar import regular_session
from quant_inventory_health import inventory_health, clean_label

PRODUCTS = ('NIFTY','SENSEX','BANKNIFTY','CRUDEOIL','GOLDM','NATURALGAS')
LABELS = {'NIFTY':'NIFTY (Spot + Rolling Options)', 'SENSEX':'SENSEX (Spot + Rolling Options)',
          'BANKNIFTY':'BANKNIFTY Spot', **{n:n for n in PRODUCTS[3:]}}


def targets(inv):
    return {**LABELS, **{sid:s['instrument']+' / '+s['interval'] for sid,s in inv['streams'].items()}}


def target_streams(inv, target):
    if target in inv['streams']: return [inv['streams'][target]]
    return [s for s in inv['streams'].values() if s['underlying']==target and s['interval']=='1 minute']


def date_bounds(inv, target, today=None):
    streams=target_streams(inv,target)
    first=[pd.Timestamp(s['first']).date() for s in streams if s['first']]
    last=[pd.Timestamp(s['last']).date() for s in streams if s['last']]
    return min(first) if first else date(2025,1,1), max(last) if last else (today or pd.Timestamp.now(tz='Asia/Kolkata').date())


def session_cutoff(root, product, now=None):
    """Use local sourced schedules when present; disclose any calendar fallback."""
    now=pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz='Asia/Kolkata')
    now=now.tz_localize('Asia/Kolkata') if now.tzinfo is None else now.tz_convert('Asia/Kolkata')
    mcx=product in PRODUCTS[3:]
    exchange='MCX' if mcx else 'BSE' if product=='SENSEX' else 'NSE'
    calendar,error=calendar_document(root)
    sessions={s['date']:s for s in calendar.get('sessions',[]) if s.get('exchange')==exchange} if not error else {}
    day=now.date()
    for _ in range(370):
        key=str(day); evidence=sessions.get(key)
        if evidence:
            windows=evidence['windows']
            if windows and now >= pd.Timestamp(key+' '+windows[-1][1],tz='Asia/Kolkata')+pd.Timedelta(minutes=5):
                return dict(date=day,verified=True,basis='Sourced local '+exchange+' session schedule',exchange=exchange)
        elif mcx:
            # Match the ingestion pipeline's seasonal close, but never invent
            # commodity holidays or describe this weekday bound as verified.
            stamp=pd.Timestamp(key+' 12:00',tz='Asia/Kolkata').tz_convert('America/New_York')
            close='23:30' if stamp.dst() else '23:55'
            if day.weekday()<5 and now >= pd.Timestamp(key+' '+close,tz='Asia/Kolkata')+pd.Timedelta(minutes=5):
                return dict(date=day,verified=False,basis='Completed weekday candidate; MCX holiday/special-session calendar unavailable',exchange=exchange)
        elif day.year in (2025,2026) and regular_session(key) and now >= pd.Timestamp(key+' 15:35',tz='Asia/Kolkata'):
            return dict(date=day,verified=True,basis='Existing engine regular-index calendar; special sessions excluded',exchange=exchange)
        day-=timedelta(days=1)
    return dict(date=None,verified=False,basis='No supported completed-session calendar evidence',exchange=exchange)


def freshness(root, inv, target, now=None):
    product=inv['streams'][target]['underlying'] if target in inv['streams'] else target
    cutoff=session_cutoff(root,product,now)
    streams=target_streams(inv,target)
    if product in PRODUCTS[3:] and target not in inv['streams']:
        # Archived expired futures remain in inventory, but cannot keep an
        # otherwise fresh active-contract tail permanently "behind".
        today=pd.Timestamp(now).date() if now is not None else pd.Timestamp.now(tz='Asia/Kolkata').date()
        names={s['instrument']:re.search(r'_(\d{4}-\d{2}-\d{2})_\d+$',s['instrument']) for s in streams}
        active=sorted({m[1] for m in names.values() if m and m[1]>=str(today)})[:2]
        streams=[s for s in streams if not names[s['instrument']] or names[s['instrument']][1] in active]
    lasts=[pd.Timestamp(s['last']).date() for s in streams if s['last']]
    last=min(lasts) if lasts else None
    lag=None
    if last and cutoff['date'] and cutoff['verified']:
        calendar,_=calendar_document(root)
        sourced={s['date']:s for s in calendar.get('sessions',[]) if s['exchange']==cutoff['exchange']}
        dates=pd.date_range(last+timedelta(days=1),cutoff['date'])
        if product not in PRODUCTS[3:]:
            lag=sum(bool(sourced[str(d.date())]['windows']) if str(d.date()) in sourced else regular_session(str(d.date())) for d in dates)
        elif all(str(d.date()) in sourced for d in dates):
            lag=sum(bool(sourced[str(d.date())]['windows']) for d in dates)
    return dict(last=last,lag=lag,**cutoff)


def coverage_report(root, target, start, end):
    """Inclusive UI dates; loader invoked one month at a time, never portfolio execution."""
    from quant_backtester import ResearchData, POINT_VALUE
    root=Path(root); start=pd.Timestamp(start).date(); end=pd.Timestamp(end).date()
    if end<start: raise ValueError('To Date must be on or after From Date.')
    inv=inventory(root);state=report_state(root,inv)
    streams=target_streams(inv,target)
    product=inv['streams'][target]['underlying'] if target in inv['streams'] else target
    # Readiness is always the complete engine bundle, not an individual option
    # stream or daily candle standing in for all required intraday inputs.
    underlying=[s for s in inv['streams'].values() if s['underlying']==product and s['dataset']=='spot_1m']
    observed=sorted({d for s in underlying for d in stream_sessions(s) if str(start)<=d<=str(end)})
    rows=[];error=None
    supported=product in ('NIFTY','SENSEX',*POINT_VALUE)
    if not supported: error='This instrument has no supported options/futures research backtester. Spot availability can be inspected; backtest eligibility is not claimed.'
    elif not state['current']: error='Run a local quality check first. '+state['explanation']
    if not error:
        cursor=pd.Timestamp(start)
        while cursor.date()<=end:
            stop=min((cursor+pd.offsets.MonthBegin(1)).date(),end+timedelta(days=1))
            dates=[d for d in observed if str(cursor.date())<=d<str(stop)]
            if dates:
                try:
                    data=ResearchData(product,str(cursor.date()),str(stop),root)
                    rows.extend(data.eligibility)
                except (ValueError,OSError,KeyError) as exc:
                    error=str(exc);break
            cursor=pd.Timestamp(stop)
    mapped={r['date']:r for r in rows}
    for d in observed:
        if d not in mapped:
            rows.append(dict(date=d,instrument=product,status='not checked',selected_contract=None,reasons=[dict(problem=error or 'No loader eligibility record')],evidence=[]))
    rows.sort(key=lambda r:r['date'])
    absent=None
    if product in ('NIFTY','SENSEX','BANKNIFTY') and start.year>=2025 and end.year<=2026:
        absent=[str(d.date()) for d in pd.date_range(start,end) if regular_session(str(d.date())) and str(d.date()) not in observed]
    ready=sum(r['status']=='eligible' for r in rows);excluded=sum(r['status']=='excluded' for r in rows)
    monthly=[]
    for month in pd.period_range(start,end,freq='M'):
        dates=[r for r in rows if r['date'].startswith(str(month))]
        monthly.append({'Month':str(month),'Stored Sessions':len(dates),'Backtest-Eligible Sessions':sum(r['status']=='eligible' for r in dates),
                        'Excluded/Gap Sessions':sum(r['status']=='excluded' for r in dates),'Not checked':sum(r['status']=='not checked' for r in dates),
                        'Entire sessions absent (engine calendar)':sum(d.startswith(str(month)) for d in absent) if absent is not None else 'Not verified',
                        'First → Last Date':(dates[0]['date']+' → '+dates[-1]['date']) if dates else 'No stored sessions'})
    findings=[]
    for r in rows:
        for reason in r.get('reasons',[]):
            findings.append(dict(date=r['date'],instrument=product,stream=reason.get('stream') or ', '.join(reason.get('streams',[])) or r.get('selected_contract') or 'Not recorded',
                                 timestamps=reason.get('timestamps',[]),count=reason.get('count'),problem=reason['problem'],status=r['status']))
    return dict(target=target,product=product,start=str(start),end=str(end),stored=len(observed),ready=ready if not error else None,
                excluded=excluded if not error else None,unchecked=sum(r['status']=='not checked' for r in rows),
                percent=100*ready/len(observed) if observed and not error else None,monthly=monthly,findings=findings,records=rows,error=error,
                absent=absent,scope='Observed underlying sessions; readiness uses the engine 5.4.0 intraday bundle, including labeled repaired inputs. Strict-observed runs reject synthetic dependency inputs. Dense full-length special sessions undergo the same dependency checks. Missing entire index sessions use the regular-index engine calendar. MCX whole-session coverage needs sourced calendar/applicability evidence. This does not prove observed quotes, fixed-contract options identity or profitability.')


def usability(report):
    if not report: return 'Check selected range'
    if report['error']: return 'Not verified — '+('no supported backtester' if report['product']=='BANKNIFTY' else 'audit/input check required')
    if not report['stored']: return 'No stored sessions'
    if report['ready']==report['stored']: return 'Ready (100% sessions usable)'
    return f"Ready ({report['ready']} / {report['stored']} sessions usable)" if report['ready'] else f"Excluded (0 / {report['stored']} sessions usable)"


def finding_bucket(item):
    """Do not turn unlocalized evidence into either a proven gap or a harmless note."""
    if item.get('category')=='eligibility': return 'actionable'
    if item.get('category')=='repair':return 'notes'
    if item.get('severity')=='Error' or item.get('category')=='integrity': return 'actionable'
    problem=item.get('problem','')
    if problem.startswith('Manifest record is failed:'):return 'actionable'
    if problem in ('Exact duplicate input rows removed','Out-of-hours input rows rejected') or item.get('category')=='identity': return 'notes'
    if problem.startswith('Coverage not verified:') or problem.startswith('Manifest record is empty:') or problem.startswith('Manifest record is unavailable:'): return 'notes'
    # Invalid/rejected/conflicting aggregates require localization. Spacing alone
    # is historical evidence, not an exact exchange/session backtest blocker.
    return 'review'
