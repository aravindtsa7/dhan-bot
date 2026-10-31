"""Compact instrument inventory; every action is a native Streamlit control."""
from html import escape
import pandas as pd
import streamlit as st

from quant_coverage import freshness, inventory_health,clean_label
from quant_inventory_health import percent_label
from quant_data_quality import stream_sessions
from quant_data_sync import repair_dataset_gaps
from quant_data_workflows import _run, sync_panel

ROWS=('NIFTY SPOT','NIFTY OPTIONS','SENSEX SPOT','SENSEX OPTIONS','BANKNIFTY SPOT','CRUDEOIL FUT','GOLDM FUT','NATURALGAS FUT')


def latest_sync_evidence(root):
    """Persist request failures across reruns/restarts without running ingestion."""
    from pathlib import Path
    from quant_data_quality import read_document
    paths=sorted((Path(root)/'sync_history').glob('*.json'))
    if not paths:return None
    report,error=read_document(paths[-1])
    if (error or not isinstance(report,dict) or not isinstance(report.get('outcomes',[]),list)
        or any(not isinstance(r,dict) for r in report.get('outcomes',[]))
        or not isinstance(report.get('failures',[]),list) or any(not isinstance(r,str) for r in report.get('failures',[]))):
        return dict(evidence_error='Latest saved sync evidence is unreadable; no successful sync is inferred.')
    return report


def inventory_rows(inv):
    result=[]
    for label in ROWS:
        product,segment=label.split(' ')
        ds='options_rolling_1m' if segment=='OPTIONS' else 'spot_1m'
        streams=[s for s in inv['streams'].values() if s['underlying']==product and s['dataset']==ds]
        stored=[s for s in streams if s['rows']]
        sessions=set();errors=[]
        for stream in streams:
            try:sessions.update(stream_sessions(stream))
            except Exception:errors.append(stream['instrument']+': session index unreadable; inspect audit evidence')
        sessions=sorted(sessions)
        result.append(dict(label=label,product=product,segment=segment,streams=streams,sessions=sessions,errors=errors,
            rows=sum(s['rows'] for s in streams),bytes=sum(s['bytes'] for s in streams),
            first=min((s['first'] for s in stored if s['first']),default=None),last=max((s['last'] for s in stored if s['last']),default=None)))
    return result


def do_repair(root,selection):
    try:
        progress=st.empty()
        with st.spinner('Retrying recoverable holes, preserving original evidence, then auditing…'):
            result=repair_dataset_gaps(root,selection,progress=lambda i,n,name:progress.caption(f'Inspecting {i}/{n}: {name}'))
        progress.empty();st.session_state.repair_result=result
        st.cache_data.clear();st.session_state.pop('range_report',None);st.session_state.pop('lab_checks',None)
        st.rerun()
    except Exception as exc:st.error(f'Repair could not complete ({type(exc).__name__}). Inspect the audit before backtesting; no secret values are shown.')


def repair_log(inv):
    entries=[dict(Instrument=r['instrument'],Timestamp=e['timestamp'],Method=e['method'],Recorded=e.get('recorded_at','Ingestion normalization'),
                  Original=e.get('original_sha256','Original raw quarantine retained'),Original_volume=str(e.get('original_volume','Not applicable')),
                  Source_timestamp=e.get('source_timestamp','Not applicable')) for r in inv['records'] for e in r.get('repairs',[])]
    st.caption('Negative-volume corrections and synthetic flat candles retain original evidence. Synthetic candles are modeled inputs, not recovered market quotes. Long gaps and conflicts require source investigation.')
    if entries:
        table=pd.DataFrame(entries);st.dataframe(table,hide_index=True,width='stretch',height=min(330,40+35*len(table)))
        st.download_button('Export repair log CSV',table.to_csv(index=False),'repair_log.csv','text/csv',key='repair_log_export')
    else:st.info('No repair events recorded. Quarantine evidence is available in the audit findings and manifest records below.')


def quarantine_preview(root,inv):
    from quant_data_quality import safe_path
    records=[r for r in inv['records'] if r.get('quarantine_path')]
    with st.expander('Inspect rejected input candles',expanded=False):
        if not records:st.info('No quarantine files recorded in this manifest.');return
        index=st.selectbox('Quarantined stream / partition',range(len(records)),format_func=lambda i:records[i]['instrument']+' · '+records[i]['start'],key='quality_quarantine')
        record=records[index]
        try:
            import duckdb
            with duckdb.connect() as db:
                db.execute("SET TimeZone='Asia/Kolkata'")
                frame=db.execute('SELECT * FROM read_parquet(?) ORDER BY timestamp LIMIT 100',[str(safe_path(root,record['quarantine_path']))]).df()
            st.dataframe(frame,hide_index=True,width='stretch',height=230)
            st.caption('First 100 raw rejected input rows for this manifest partition. Recovery does not delete these records; compare timestamps with the repair log. These rows are separate from committed candle totals.')
            st.download_button('Export displayed quarantine CSV',frame.to_csv(index=False),record['instrument']+'_'+record['start']+'_quarantine_preview.csv','text/csv',key='quarantine_export')
        except Exception as exc:st.error(f'Quarantine evidence is unreadable ({type(exc).__name__}). It has not been discarded. Inspect the recorded path in manifest details.')


def inventory_summary(root,inv,rows,health,now=None):
    """Equal-weight mean across populated rows; freshness is per dataset row."""
    populated=[r for r in rows if r['rows']]
    metrics=[health['rows'][r['label']] for r in populated]
    known=bool(metrics) and health['current'] and all(m['percent'] is not None for m in metrics)
    mean=sum(m['percent'] for m in metrics)/len(metrics) if known else None
    status=f"{percent_label(mean)} Clean {'✓' if mean>=95 else '⚠️'}" if known else 'Health unverified' if populated else 'No data'
    latest=max((pd.Timestamp(r['last']).date() for r in populated if r['last']),default=None)
    at_latest=0
    for row in rows:
        if row['segment']=='FUT':
            tail=freshness(root,inv,row['product'],now)['last']
        else:
            tails=[pd.Timestamp(s['last']).date() if s['last'] else None for s in row['streams']]
            expected=6 if row['segment']=='OPTIONS' else 1
            tail=min(tails) if len(tails)>=expected and all(tails) else None
        at_latest+=bool(tail and latest and tail>=latest)
    return status,f"Latest stored: {latest or 'None'} • {at_latest}/{len(rows)} datasets at latest"


def render_inventory(root,inv,state):
    from quant_data_ui import size_label
    pending=st.session_state.pop('lab_pending_backfill',None)
    if pending:st.session_state.sync_selection=pending
    rows=inventory_rows(inv)
    a,b,c=st.columns(3)
    if a.button('⚡ Sync All to Latest',type='primary',width='stretch',key='lab_sync_all'):_run(root,'All Datasets',latest=True)
    if b.button('🔧 Repair All Gaps',width='stretch',key='lab_repair_all'):do_repair(root,'All Datasets')
    if c.button('📅 Custom Backfill',width='stretch',key='lab_custom'):
        st.session_state.lab_backfill='All Datasets';st.session_state.sync_selection='All Datasets'
    st.caption('Repair retries provider data when configured, then may insert labeled flat zero-volume bars for isolated 1–3 minute interior holes. It never resolves disputed prices or fills long/edge gaps. Review sensitivity before using repaired results.')
    if st.session_state.get('lab_backfill'):
        with st.expander('Custom backfill / sync request details',expanded=True):
            st.session_state.setdefault('sync_selection',st.session_state.lab_backfill)
            sync_panel(root,historical_only=True)
            if st.button('Close backfill panel',key='lab_close'):st.session_state.pop('lab_backfill',None);st.rerun()
    force=st.button('Refresh cached health',key='lab_check_health',help='Health is measured automatically and persisted against lake/audit fingerprints. This forces a local recalculation; no downloads.')
    with st.spinner('Loading calculated row health…'):
        health=inventory_health(root,inv,state,force=force)
    reports=health['rows']
    if health.get('cache_warning'):st.warning(health['cache_warning'])
    products={r['product'] for r in rows if r['rows']}
    status,note=inventory_summary(root,inv,rows,health)
    values=[('Instruments Tracked',f'{len(products)} Instruments',' · '.join(sorted(products)) or 'No committed 1m candles'),
            ('Total 1m Candles',f'{sum(r["rows"] for r in rows):,}','Stored Parquet rows · daily candles excluded'),
            ('Disk Storage',size_label(sum(r['bytes'] for r in rows)),'Across committed 1m Parquet partitions'),
            ('Global Sync Status',status,note)]
    st.caption('Lake cleanliness is the equal-weight average of populated inventory rows; MCX continuity is not engine eligibility. At latest compares dataset tails with the latest stored date, not a verified exchange calendar.')
    st.markdown('<div class="data-summary-grid">'+''.join(f'<div class="data-summary-card"><label>{escape(k)}</label><strong>{escape(v)}</strong><small>{escape(n)}</small></div>' for k,v,n in values)+'</div>',unsafe_allow_html=True)
    result=st.session_state.get('repair_result')
    if result:
        st.info(f"Last repair: {result['restored']} zero-volume corrections · {result['refetched']} provider candles · {result['synthetic']} synthetic candles · {result['partitions_changed']} partitions. Audit {'refreshed' if result['audit_current'] else 'not verified'}. Row health refreshed automatically.")
        for message in result['failures']:st.warning(message)
    synced_result=st.session_state.get('sync_result') or latest_sync_evidence(root)
    if synced_result:
        if synced_result.get('evidence_error'):st.warning(synced_result['evidence_error'])
        elif 'committed' in synced_result:
            st.info(f"Last sync: {synced_result['committed']} records committed · {synced_result['rows_added']:+,} stored rows · audit {'refreshed' if synced_result['audit_current'] else 'not verified'}. Row health refreshed automatically.")
        else:st.caption(('Latest read-only provider check' if synced_result.get('kind')=='provider_check' else 'Latest saved sync request evidence')+' · '+synced_result.get('checked_at','Time not recorded')+' · request outcomes below; no successful catch-up is inferred.')
        if synced_result.get('control_evidence'):st.caption(synced_result['control_evidence'])
        if synced_result.get('outcomes'):
            with st.expander('Last sync request summary'):
                st.dataframe(pd.DataFrame(synced_result['outcomes']),hide_index=True,width='stretch')
        if synced_result.get('failures'):
            with st.expander(f"Sync messages ({len(synced_result['failures'])})",expanded=True):
                for message in synced_result['failures']:st.warning(message)
    st.subheader('Historical Data Inventory')
    st.caption('Rows are independent: spot uses only its own 09:15–15:20 candles; options uses its own six required rolling streams. MCX Clean measures continuity within each observed contract/session span, including partial listing days; it does not certify missing opening/closing hours or backtest readiness. Bars count unique valid minutes within these respective windows. The inspection drawer and Data Quality retain strict engine windows and prior-session selection. Absent history and expiry identity remain unverified. Repaired inputs count as present.')
    query=st.text_input('Filter inventory',placeholder='NIFTY, OPTIONS, MCX…',key='lab_filter').upper()
    for row in rows:
        if query and query not in row['label'] and not (query=='MCX' and row['segment']=='FUT'):continue
        with st.container(border=True,key='lab_row_'+row['label'].replace(' ','_')):
            cols=st.columns([1.6,1,1.6,1,1.15,.7,1.8],gap='small')
            badge='NIFTY50' if row['product']=='NIFTY' else row['product']
            cols[0].markdown(f'<span class="lab-badge">{badge}</span> <strong>{row["segment"]}</strong>',unsafe_allow_html=True)
            cols[0].caption(('MCX · commodity futures' if row['segment']=='FUT' else ('BSE' if row['product']=='SENSEX' else 'NSE')+' · '+('rolling CE + PE' if row['segment']=='OPTIONS' else 'index spot'))+' · 1 minute')
            report=reports[row['label']];days=len(row['sessions'])
            cols[1].caption('Coverage (Days)');cols[1].markdown(f'<span class="lab-pill">{days}d</span>',unsafe_allow_html=True)
            if report['percent'] is not None:
                cols[1].caption(f"{percent_label(report['bars_percent'])} 1m bars")
                cols[1].caption(f"{report['valid_bars']:,}/{report['expected_bars']:,} measured bars")
            cols[2].caption('Date Range (IST)');cols[2].write((str(row['first'])[:16]+' → '+str(row['last'])[:16]) if row['first'] else 'No stored candles')
            cols[3].caption('1m Candles');cols[3].write(f"{row['rows']:,}")
            if row['segment']=='OPTIONS':
                sides={side:sum(s['rows'] for s in row['streams'] if '_'+side+'_' in s['instrument']) for side in ('CE','PE')}
                cols[3].caption(f"CE {sides['CE']:,} · PE {sides['PE']:,}")
            cols[4].caption('Gaps & Health')
            label=clean_label(report)
            label='Session index unreadable' if row['errors'] else label
            tone='ready' if report['percent'] is not None and report['percent']>=95 and not row['errors'] else 'review'
            cols[4].markdown(f'<span class="lab-pill lab-{tone}">{escape(label)}</span>',unsafe_allow_html=True)
            if report['ready'] is not None:cols[4].caption(f"{report['ready']}/{report['stored']} ready days · {report['excluded']} {'gap' if report['excluded']==1 else 'gaps'}")
            if report['error']:cols[4].caption(report['error'])
            for error in row['errors']:cols[4].caption(error)
            if report['repaired']:cols[4].caption(f"Includes {report['repaired']} repaired inputs")
            cols[5].caption('Size');cols[5].write(size_label(row['bytes']))
            with cols[6]:
                x,y,z=st.columns(3)
                if x.button('⚡ Sync',key='lab_sync_'+row['label'],help='Sync this row to the latest completed boundary'):_run(root,row['label'],latest=True)
                if y.button('📅 Backfill',key='lab_backfill_'+row['label']):
                    st.session_state.lab_backfill=row['label'];st.session_state.lab_pending_backfill=row['label'];st.rerun()
                if z.button('🔧 Repair',key='lab_repair_'+row['label']):do_repair(root,row['label'])
    if not products:st.info('No data yet. Choose Custom Backfill to add a supported historical range.')
