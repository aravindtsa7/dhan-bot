"""Read-only paper account snapshots and an independently refreshing Streamlit room."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import io
import json
import math
import re
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from quant_publication import PROTOCOL_VERSION, TERMINAL

HEARTBEAT_SECONDS = 35
QUOTE_SECONDS = 15
TRADE_COLUMNS = ['trade_id', 'instrument', 'strategy', 'security_id', 'expiry', 'status',
                 'side', 'entry_time', 'exit_time', 'entry_price', 'exit_price', 'quantity',
                 'lots', 'capital_allocated', 'stop', 'target', 'net_pnl', 'exit_reason']


def epoch(value):
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.timestamp() if dt.tzinfo else None
    except (ValueError, TypeError):
        return None


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def read_json(root, name, warnings):
    path = root / name
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('Invalid snapshot')
        return data
    except (OSError, ValueError):
        warnings.append(f'{name} is temporarily unreadable; retrying on the next refresh.')
        return {}


def normalize(frame):
    if not {'trade_id', 'status', 'net_pnl'}.issubset(frame.columns):
        raise ValueError('Missing ledger columns')
    frame = frame.copy()
    if frame.trade_id.isna().any() or frame.trade_id.astype(str).eq('').any() or frame.trade_id.duplicated().any():
        raise ValueError('Invalid trade identity')
    frame['status'] = frame.status.astype(str).str.upper()
    if not frame.status.isin(['OPEN', 'CLOSED']).all():
        raise ValueError('Invalid trade status')
    frame['net_pnl'] = pd.to_numeric(frame.net_pnl, errors='coerce')
    frame.loc[~frame.net_pnl.map(lambda v: number(v) is not None), 'net_pnl'] = float('nan')
    return frame.drop(columns=['asset'], errors='ignore')


def _account_snapshot(root):
    """Immutable read buffers; never lock, recover, create or rewrite an account."""
    def read(path):
        try:return path.read_bytes()
        except FileNotFoundError:return None
    base=root/'.publication/paper'
    return dict(state=read(root/'state.json'),ledger=read(root/'ledger.csv'),
        journal=read(base/'journal.json'),
        publication_history=base.exists() and any(p.name!='lock' for p in base.iterdir()),
        intents={p.name:read(p) for p in sorted((root/'basket_invalidations').glob('*.intent.json'))})


def _publication_view(snapshot):
    """Verify the authoritative terminal journal and its exact state/ledger bytes."""
    intents=[]
    for value in snapshot['intents'].values():
        evidence=json.loads(value)
        if not isinstance(evidence,dict) or evidence.get('simulation_invalidated') is not True:
            raise ValueError('Incomplete basket invalidation intent; account frozen.')
        intents.append(evidence)
    raw=snapshot['journal']
    if raw is None:
        if snapshot['publication_history']:
            raise ValueError('Paper publication journal is missing despite publication evidence.')
        return dict(generation=None,legacy=True,status='Legacy / unverified',intents=intents)
    journal=json.loads(raw)
    if (not isinstance(journal,dict) or type(journal.get('protocol')) is not int or
        journal['protocol']!=PROTOCOL_VERSION or
        not isinstance(journal.get('transaction'),str) or not re.fullmatch('[0-9a-f]{32}',journal['transaction']) or
        journal.get('phase') not in TERMINAL or not isinstance(journal.get('entries'),list)):
        raise ValueError('Paper publication is nonterminal or malformed; recovery by the account writer is required.')
    entries=journal['entries']
    if (len(entries)!=2 or any(not isinstance(e,dict) for e in entries) or
        {e.get('path') for e in entries}!={'state.json','ledger.csv'}):
        raise ValueError('Paper publication must identify exactly one state and one ledger.')
    slots=[]
    for entry in entries:
        for field in ('old_sha256','new_sha256'):
            value=entry.get(field)
            if field not in entry or not (field=='old_sha256' and value is None) and (
                    not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value)):
                raise ValueError('Malformed paper publication hashes.')
        slot=entry.get('slot')
        if not isinstance(slot,str) or not slot.isdigit():raise ValueError('Malformed paper publication slot.')
        slots.append(slot)
        payload=snapshot['state' if entry['path']=='state.json' else 'ledger']
        actual=hashlib.sha256(payload).hexdigest() if payload is not None else None
        field='new_sha256' if journal['phase']=='COMMITTED' else 'old_sha256'
        if actual!=entry[field]:raise ValueError('Paper state/ledger hash differs from its terminal publication.')
    if len(set(slots))!=2:raise ValueError('Duplicate paper publication slot.')
    # A first failed upgrade can restore an intentionally state-only/CSV-only
    # legacy account. Its restored bytes are verified, not a joint legacy ledger.
    legacy=journal['phase']=='ROLLED_BACK' and (snapshot['state'] is None or snapshot['ledger'] is None)
    status=('Legacy / unverified; verified restored files' if legacy else 'Verified publication')+' ('+journal['phase']+')'
    return dict(generation=journal['transaction'],legacy=legacy,status=status,intents=intents)


def _blocked_room(root, now, reason, frozen=False):
    return dict(ledger=pd.DataFrame(columns=TRADE_COLUMNS),active=pd.DataFrame(),available=False,
        metrics={key:None for key in ('total','opened','closed','net','profit','loss','win_rate')},
        unrealized=None,health='Frozen / invalidated' if frozen else 'Account publication unavailable',
        running=False,stop_requested=(root/'STOP').exists(),age=None,status={},source='',
        warnings=[reason+' No current metrics or ledger download. This read did not recover or modify the account.'],
        now=now,publication_status='Blocked',generation=None,downloadable=False,frozen=frozen)


def load_room(root, now=None):
    """Return one verified generation, or an explicitly unverified legacy view.

    Revalidate after the entire read/valuation, including invalidation intents.
    Concurrent publication discards all buffers; UI reads never perform recovery.
    """
    root = Path(root)
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    before=None
    try:
        before=_account_snapshot(root)
        publication=_publication_view(before)
        room=_load_room(root,now,before,publication)
        after=_account_snapshot(root)
        if before!=after:raise ValueError('Account publication generation or invalidation evidence changed during the read; refresh to retry.')
        return room
    except (OSError,ValueError,TypeError,KeyError,AttributeError) as exc:
        frozen=bool(before and before['intents'])
        # A newly written intent must not be overlooked when the final check
        # detects a concurrent generation change.
        frozen=frozen or any((root/'basket_invalidations').glob('*.intent.json'))
        return _blocked_room(root,now,'Paper publication unavailable ('+type(exc).__name__+'): '+str(exc),frozen)


def _load_room(root,now,snapshot,publication):
    warnings = []
    state={}
    if snapshot['state'] is not None:
        try:
            state=json.loads(snapshot['state'])
            if not isinstance(state,dict):raise ValueError('Invalid account state')
        except (ValueError,TypeError):
            if not publication['legacy']:raise ValueError('Invalid published account state.')
            warnings.append('Legacy state.json is unreadable; using the unverified CSV when available.')
            state={}
    status = read_json(root, 'status.json', warnings)
    process = read_json(root, 'process.json', warnings)
    last = read_json(root, 'last_run.json', warnings)
    ledger = None
    if snapshot['ledger'] is not None:
        try:
            ledger = normalize(pd.read_csv(io.BytesIO(snapshot['ledger']), dtype={'trade_id': str, 'security_id': str}))
        except (OSError, ValueError, pd.errors.ParserError):
            if not publication['legacy']:raise ValueError('Invalid published CSV ledger.')
            warnings.append('Legacy ledger.csv is unreadable or invalid; checking the unverified state snapshot.')
    source = 'ledger.csv'
    if isinstance(state.get('trades'), list):
        try:
            records = normalize(pd.DataFrame(state['trades']) if state['trades'] else pd.DataFrame(columns=TRADE_COLUMNS))
            # Compare accounting identity, not float formatting or column order.
            def identity(f):
                return sorted((str(r.trade_id), r.status, number(r.net_pnl)) for r in f.itertuples())
            if ledger is None or identity(ledger) != identity(records):
                if not publication['legacy']:raise ValueError('Published state and CSV accounting records disagree.')
                warnings.append('Showing legacy state snapshot; joint state/CSV publication is unverified.')
                source = 'state.json'
            ledger = records
        except (ValueError, TypeError, AttributeError):
            if not publication['legacy']:raise ValueError('Invalid or inconsistent published account records.')
            warnings.append('Trade records in state.json are invalid; using the CSV snapshot when available.')
    elif not publication['legacy'] and snapshot['state'] is not None:
        raise ValueError('Published account state has no trade list.')
    available = ledger is not None
    if ledger is None:
        ledger = pd.DataFrame(columns=TRADE_COLUMNS)
    frozen=bool(publication['intents'] or state.get('simulation_status')=='INVALIDATED' or state.get('invalidation_evidence') or
        any(t.get('simulation_status')=='INVALIDATED' or t.get('invalidation_evidence') for t in state.get('trades',[]) if isinstance(t,dict)))
    closed = ledger[ledger.status.eq('CLOSED')]
    opened = ledger[ledger.status.eq('OPEN')]
    complete_pnl = bool(available and not frozen and closed.net_pnl.notna().all())
    if available and not frozen and not complete_pnl:
        warnings.append('Some closed trades have missing net P&L. Closed P&L totals and win rate are unavailable.')
    pnl = closed.net_pnl
    metrics = dict(total=len(ledger), opened=len(opened), closed=len(closed),
        net=float(pnl.sum()) if complete_pnl else None,
        profit=float(pnl[pnl > 0].sum()) if complete_pnl else None,
        loss=float(pnl[pnl < 0].sum()) if complete_pnl else None,
        win_rate=100 * int(pnl.gt(0).sum()) / len(closed) if len(closed) and complete_pnl else None)
    stamp = epoch(status.get('timestamp'))
    start = epoch(process.get('started_at'))
    stopped = epoch(last.get('stopped_at'))
    age = now - stamp if stamp is not None else None
    recent = age is not None and -2 <= age <= HEARTBEAT_SECONDS and (start is None or stamp >= start)
    stop_requested = (root / 'STOP').exists()
    finished = (status.get('running') is False and (start is None or (stamp is not None and stamp >= start))) or (
        stopped is not None and (start is None or stopped >= start) and (stamp is None or stopped >= stamp))
    running = recent and not finished and status.get('running', True) is True
    health = ('Stop requested' if stop_requested and not finished else 'Stopped' if finished else
              'Running' if running else 'Starting / heartbeat stale' if process or status else 'Not started')
    if frozen:
        running=False;health='Frozen / invalidated'
        warnings.append('Frozen / invalidated paper simulation: retained positions and reserves are historical liabilities; current P&L and ledger downloads are disabled.')
    active = []
    position_rows = status.get('positions')
    snapshots = {str(p.get('trade_id')): p for p in position_rows if isinstance(p, dict)} if isinstance(position_rows, list) else {}
    for p in opened.to_dict('records'):
        quote = snapshots.get(str(p['trade_id']), {})
        qtime = epoch(quote.get('quote_time'))
        same_contract = str(quote.get('security_id')) == str(p.get('security_id'))
        fresh = (running and status.get('connected') is True and same_contract and qtime is not None
                 and -2 <= now - qtime <= QUOTE_SECONDS)
        active.append(dict(trade_id=p['trade_id'], instrument=p.get('instrument'), security_id=p.get('security_id'),
            side=p.get('side'), quantity=p.get('quantity'), entry_price=p.get('entry_price'),
            ltp=number(quote.get('ltp')) if fresh else None,
            unrealized_net_pnl=number(quote.get('net_pnl')) if fresh else None,
            quote_time=quote.get('quote_time', p.get('last_quote_time')),
            quote_status='Live' if fresh else 'Unavailable / stale', stop=p.get('stop'), target=p.get('target'),
            pending_exit=p.get('pending_exit', '')))
    unrealized = sum(p['unrealized_net_pnl'] for p in active) if available and not frozen and all(p['unrealized_net_pnl'] is not None for p in active) else None
    return dict(ledger=ledger, active=pd.DataFrame(active), available=available, metrics=metrics,
                unrealized=unrealized, health=health, running=running, stop_requested=stop_requested,
                age=age, status=status, source=source, warnings=warnings, now=now,
                publication_status=publication['status'],generation=publication['generation'],frozen=frozen,
                downloadable=available and not frozen)


def pnl_row(row):
    value = number(row.get('Net P&L (₹)'))
    color = '#153f32' if value is not None and value > 0 else '#51252e' if value is not None and value < 0 else ''
    return [f'background-color: {color}; color: #eff6ff' if color else '' for _ in row]


def rupees(value):
    return 'N/A' if value is None else f'₹{value:,.2f}'


@st.fragment(run_every='3s')
def render_live_paper_room(root):
    room = load_room(root)
    st.subheader('Live Paper Trading Room')
    st.caption('Virtual trades · account lifetime totals · IST · refreshes every 3 seconds. Historical sidebar filters do not apply.')
    a, b, c = st.columns([2, 1, 1])
    with a:
        display = st.success if room['health'] == 'Running' else st.warning if 'stale' in room['health'] or room['health'] == 'Stop requested' else st.info
        display(f"Bot: {room['health']}")
    b.metric('STOP file', 'Present' if room['stop_requested'] else 'Absent')
    c.metric('Feed', 'Connected' if room['running'] and room['status'].get('connected') else 'Offline / stale')
    st.caption(f"Last bot update: {room['status'].get('timestamp', 'Unavailable')} · "
               f"Dashboard checked: {datetime.fromtimestamp(room['now'], ZoneInfo('Asia/Kolkata')).strftime('%H:%M:%S IST')}. "
               'STOP absence alone does not mean the bot is running. Heartbeats older than 35 seconds are stale.')
    st.button('Refresh paper account now', key='paper_refresh')
    for warning in room['warnings']:
        st.warning(warning)
    st.caption('Account snapshot: '+room['publication_status']+'. '+
               ('Legacy files have no verified joint publication.' if 'Legacy' in room['publication_status'] else
                'State and ledger hashes are checked together; this read never recovers account files.'))
    if not room['available']:
        st.info('No readable paper account snapshot. Inspect publication/recovery evidence before using the account.' if room['publication_status']=='Blocked' else
                'No readable paper account yet. Start step6_paper_trader.py to create its ledger and status files.')
        return
    m = room['metrics']
    cols = st.columns(5)
    for col, label, value in zip(cols, ['Paper trades executed', 'Open positions', 'Closed net P&L', 'Closed win rate', 'Open net P&L'],
            [str(m['total']), str(m['opened']), rupees(m['net']), 'N/A' if m['win_rate'] is None else f"{m['win_rate']:.1f}%", rupees(room['unrealized'])]):
        col.metric(label, value)
    st.caption(f"{m['closed']} closed trades · Profitable trades: {rupees(m['profit'])} · Losing trades: {rupees(m['loss'])}. "
               'Win rate = positive-net closed trades / all closed trades; breakeven trades stay in the denominator. '
               'Net values include modeled fees and slippage; open P&L is the bot’s estimated liquidation value.')
    st.subheader('Current open positions')
    if room['active'].empty:
        st.info('No open paper positions.')
    else:
        view = room['active'].rename(columns={'trade_id':'Trade ID','instrument':'Instrument','security_id':'Contract ID',
            'side':'Side','quantity':'Quantity','entry_price':'Entry price','ltp':'Live LTP','unrealized_net_pnl':'Unrealized net P&L (₹)',
            'quote_time':'Quote time (IST)','quote_status':'Quote status','stop':'Stop','target':'Target','pending_exit':'Pending exit'})
        st.dataframe(view, hide_index=True, width='stretch')
        if room['unrealized'] is None:
            st.warning('Live valuation is unavailable for one or more positions. Stale prices are not counted as current P&L.')
    st.subheader('Full paper trade ledger')
    if room['ledger'].empty:
        st.info('No paper trades have been executed in this account yet.')
    else:
        labels = {'trade_id':'Trade ID','instrument':'Instrument','strategy':'Strategy','security_id':'Contract ID',
            'expiry':'Expiry','status':'Status','side':'Side','entry_time':'Entry time (IST)','exit_time':'Exit time (IST)',
            'entry_price':'Entry price','exit_price':'Exit price','quantity':'Quantity','lots':'Lots',
            'capital_allocated':'Capital allocated (₹)','net_pnl':'Net P&L (₹)','exit_reason':'Exit reason'}
        view = room['ledger'].rename(columns=labels)
        st.dataframe(view.style.apply(pnl_row, axis=1), hide_index=True, width='stretch')
        if room['downloadable']:
            legacy='Legacy' in room['publication_status']
            st.download_button('Download legacy ledger (unverified)' if legacy else 'Download paper ledger',
                room['ledger'].to_csv(index=False), 'paper_ledger_legacy_unverified.csv' if legacy else 'paper_ledger.csv',
                'text/csv', key='paper_csv')
    with st.expander('Account status and files'):
        st.caption(f"Source: {Path(root) / room['source']}. Snapshot status: {room['publication_status']}. "
                   'This tab only reads the account; it does not start, stop or place orders.')
        st.write({'monitor_only':room['status'].get('monitor_only'), 'daily_lock':room['status'].get('daily_lock'),
                  'ticks':room['status'].get('ticks'), 'stale_or_rejected':room['status'].get('stale_or_rejected')})
