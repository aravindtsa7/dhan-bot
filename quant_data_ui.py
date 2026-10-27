"""Data Lake and Data Quality pages; native controls and bounded local previews."""
from html import escape
from pathlib import Path
import re
import pandas as pd
import streamlit as st
from quant_data_quality import (STATUSES, inventory, report_state, filtered_groups, group_status,
                               stream_sessions, preview_candles, preview_export, run_quality_check)


def size_label(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if value < 1024 or unit == 'GiB': return f'{value:,.1f} {unit}'
        value /= 1024


def checked_label(state):
    try:
        value = pd.Timestamp(state['checked_at'])
        if pd.isna(value): return 'Not checked'
        if value.tzinfo is None: return str(value)+' (timezone not recorded)'
        return value.tz_convert('Asia/Kolkata').strftime('%d %b %Y · %H:%M IST')
    except (ValueError, TypeError): return 'Audit time unavailable'


def title(value, note=''):
    st.markdown(f'<div class="data-panel-title"><h3>{escape(value)}</h3><span>{escape(note)}</span></div>', unsafe_allow_html=True)


def summary_cards(inv, state):
    groups = inv['groups']
    intraday = sum(g['rows'] for g in groups if g['interval'] != '1 day')
    daily = sum(g['rows'] for g in groups if g['interval'] == '1 day')
    values = [('Available datasets', str(sum(g['rows'] > 0 for g in groups)), 'Instrument × segment × stored interval'),
              ('Stored candles', f'{intraday+daily:,}', f'{intraday:,} intraday · {daily:,} daily'),
              ('Committed storage', size_label(sum(g['bytes'] for g in groups)), 'Parquet only · excludes quarantine'),
              ('Data health', state['status'], checked_label(state))]
    cards = ''.join(f'<div class="data-summary-card"><label>{escape(a)}</label><strong>{escape(b)}</strong><small>{escape(c)}</small></div>' for a,b,c in values)
    st.markdown('<div class="data-summary-grid">'+cards+'</div>', unsafe_allow_html=True)


def choose(label, options, key, format_func=str):
    current = st.session_state.get(key)
    st.session_state[key] = current if current in options else options[0]
    return st.selectbox(label, options, index=None, key=key, format_func=format_func)


def filters(inv, prefix, with_health=False):
    columns = st.columns(4 if with_health else 3)
    values = {}
    for col, field, label in zip(columns, ('exchange','underlying','segment'), ('Exchange','Instrument','Segment')):
        with col: values[field] = choose(label, ['All']+sorted({g[field] for g in inv['groups']}), prefix+'_'+field)
    health = 'All'
    if with_health:
        with columns[3]: health = choose('Data health', ['All', *STATUSES], prefix+'_health')
    return dict(exchange=values['exchange'], instrument=values['underlying'], segment=values['segment'], health=health)


def dataset_table(groups):
    return pd.DataFrame([{'Instrument': g['underlying'], 'Segment': g['segment'], 'Health': g['status'], 'First stored': g['first'][:10] if g['first'] else '—',
                          'Last stored': g['last'][:10] if g['last'] else '—', 'Interval': g['interval'],
                          'Rows': g['rows']} for g in groups])


def select_dataset(groups, key, all_option=False):
    ids = [g['group_id'] for g in groups]
    choices = (['All datasets'] if all_option else []) + ids
    retained = st.session_state.get('selected_dataset')
    if key not in st.session_state and retained in choices: st.session_state[key] = retained
    labels = {g['group_id']: f"{g['underlying']} · {g['segment']} · {g['interval']}" for g in groups}
    if not choices: return None
    current = st.session_state.get(key)
    st.session_state[key] = current if current in choices else choices[0]
    selection = st.selectbox('Selected dataset' if not all_option else 'Dataset findings', choices, index=None, key=key,
                              format_func=lambda v: labels.get(v,v))
    if selection != 'All datasets': st.session_state.selected_dataset = selection
    return selection


def open_quality(group_id):
    st.session_state.selected_dataset = group_id
    st.session_state['quality_dataset'] = group_id
    for key in ('quality_exchange', 'quality_underlying', 'quality_segment', 'quality_health'):
        st.session_state[key] = 'All'
    st.session_state.quality_search = ''
    st.session_state.page = 'Data Quality'


def relevant_findings(inv, state, group_id=None):
    findings = list(inv['findings'])
    report = state['report']
    if report and isinstance(report.get('findings'), list):
        findings += [f for f in report['findings'] if isinstance(f, dict) and 'problem' in f]
    if group_id: findings = [f for f in findings if f.get('dataset') in ('Lake', group_id)]
    return findings


def findings_frame(findings):
    return pd.DataFrame([{'Severity': f.get('severity','Unknown'), 'Dataset': f.get('dataset','Lake'),
                          'Affected date / range': f.get('period') or 'Not measured', 'Problem': f['problem'],
                          'Count': (f"{f['count']} dates unverified" if f['problem'].startswith('Coverage not verified:') and f.get('count') is not None else f.get('count')),
                          'Next action': f.get('action','Review source evidence.')} for f in findings])


def render_details(group, inv, state):
    title(group['underlying'], group['segment']+' · '+group['interval'])
    st.caption(f"{group_status(group, inv, state)} · {group['rows']:,} stored rows · {group['partitions']:,} files · {size_label(group['bytes'])}")
    streams = [inv['streams'][s] for s in group['streams']]
    stream_id = choose('Stream / contract', [s['id'] for s in streams], 'lake_stream',
                       format_func=lambda sid: inv['streams'][sid]['instrument'])
    stream = inv['streams'][stream_id]
    endpoints = sorted({r.get('endpoint') for r in stream['records'] if r.get('endpoint')})
    known = {'/charts/intraday','/charts/historical','/charts/rollingoption'}
    source = 'DhanHQ · '+', '.join(endpoints) if endpoints and set(endpoints).issubset(known) else 'Source not established by this manifest'
    identity = 'Unverified — rolling ATM-relative series; calendar inference does not verify expiry.' if stream['dataset'] == 'options_rolling_1m' else 'Unverified — validate recorded contract identifiers.' if stream['dataset'].startswith('options_') else 'Not applicable'
    try: sessions = stream_sessions(stream)
    except Exception as exc:
        st.error(f'Session index could not be read ({type(exc).__name__}). Run a quality check to locate affected partitions.')
        sessions = ()
    facts = [('Source / provenance',source), ('Exact first stored',stream['first'] or 'No stored candles'),
             ('Exact last stored',stream['last'] or 'No stored candles'), ('Available sessions',f'{len(sessions):,}' if stream['files'] else 'No data'),
             ('Stored interval',stream['interval']), ('Derived intervals','3 / 5 / 15 minutes on query; not additional stored candles' if stream['interval']=='1 minute' else 'None'),
             ('Timezone','Asia/Kolkata (IST)'), ('Selected stream',f"{stream['rows']:,} rows · {stream['partitions']} partitions · {size_label(stream['bytes'])}"),
             ('Options expiry identity',identity)]
    if stream['dataset'].startswith('options_'):
        names = [s['instrument'] for s in streams]
        sides = sorted({m.group(1) for n in names if (m := re.search(r'_(CE|PE)_', n))})
        offsets = sorted({m.group(1) for n in names if (m := re.search(r'_(ATM(?:[+-]\d+)?)_', n))})
        facts.extend([('Available sides', ', '.join(sides) or 'Not recorded'), ('Available ATM offsets', ', '.join(offsets) or 'Not recorded')])
    st.markdown('<dl class="data-facts">'+''.join(f'<div><dt>{escape(k)}</dt><dd>{escape(str(v))}</dd></div>' for k,v in facts)+'</dl>', unsafe_allow_html=True)
    with st.expander(f'Individual streams / contracts ({len(streams)})'):
        st.dataframe(pd.DataFrame([{'Stream / contract': s['instrument'], 'Rows':s['rows'], 'Interval':s['interval']} for s in streams]), hide_index=True, width='stretch')
        if stream['dataset'].startswith('options_'):
            st.caption('Side and ATM offset are encoded in each stream name and in its manifest request. These streams are not a fixed-contract price history.')
        if sessions: st.write(', '.join(sessions))
    findings = relevant_findings(inv, state, group['group_id'])
    if findings:
        st.caption(('Current findings' if state['current'] else 'Evidence requiring review; prior audit may be unverified or outdated')+f' · {len(findings)}')
        for item in findings[:2]: st.warning(item['problem'])
    elif not state['current']: st.info('Not checked against the current lake. Open Data Quality to run a local audit.')
    st.button('Open Data Quality →', on_click=open_quality, args=(group['group_id'],), width='stretch')
    return stream, sessions


def ingestion_panel(inv):
    with st.expander('Ingestion status · manifest records', expanded=False):
        records=inv['records']
        status = choose('Manifest status', ['All','complete','failed','empty','unavailable','pending'], 'manifest_status')
        counts = {s: sum(r['status']==s for r in records) for s in ('complete','failed','empty','unavailable','pending')}
        st.caption(' · '.join(f'{s.title()}: {n}' for s,n in counts.items()))
        visible = [r for r in records if status == 'All' or r['status'] == status]
        st.dataframe(pd.DataFrame([{'Dataset':r['dataset'], 'Stream / contract':r['instrument'], 'Requested start':r['start'],
                                  'Requested end (exclusive)':r['end'], 'Status':r['status'],
                                  'Explanation':r.get('error') or ('Successful empty response; not proof of full coverage' if r['status']=='empty' else 'Committed' if r['status']=='complete' else 'No explanation recorded'),
                                  'Quarantine':r.get('quarantine_path','None recorded')} for r in visible]), hide_index=True, width='stretch', height=260)
        st.caption('Request records, not job history. Quarantined/rejected rows are excluded from stored candle totals. No download or retry is triggered here.')


def render_data_lake(root):
    inv = inventory(root); state = report_state(root, inv)
    summary_cards(inv,state)
    if inv['findings']: st.warning('Some committed files or metadata need attention. Counts include readable committed files; storage includes existing committed Parquet files. See Data Quality.')
    with st.container(key='lake_browser'):
        search = st.text_input('Search datasets', placeholder='Instrument, segment, stream or contract…', key='lake_search')
        selections = filters(inv,'lake')
        groups = filtered_groups(inv,state,search,**selections)
        left,right = st.columns([1.65,1],gap='medium')
        with left, st.container(border=True,key='lake_table'):
            title('Dataset catalog',f'{len(groups)} logical datasets')
            if groups:
                st.dataframe(dataset_table(groups), hide_index=True, width='stretch', height=min(440,40+len(groups)*35))
                selected = select_dataset(groups,'lake_dataset')
                st.caption('Counts are stored stream observations. Daily and intraday datasets are separate; related option streams and futures contracts are grouped. Files referenced twice count once.')
            else:
                selected = None
                st.info('No datasets match these filters.' if inv['records'] else 'No data yet. No committed or requested datasets are present in this lake.')
        stream,sessions = None,()
        with right, st.container(border=True,key='lake_details'):
            if selected: stream,sessions = render_details(next(g for g in groups if g['group_id']==selected),inv,state)
            else: st.info('Select an available dataset to inspect its provenance and stored candles.')
    if stream:
        with st.container(border=True,key='lake_preview'):
            title('Candle preview','ONE STREAM · ONE SESSION · BOUNDED EXPORT')
            if sessions:
                if st.session_state.get('preview_stream_id') != stream['id']:
                    st.session_state.preview_stream_id = stream['id']
                    st.session_state.preview_session = sessions[-1]
                a,b=st.columns([3,1])
                with a: day = choose('Available session',list(reversed(sessions)),'preview_session')
                with b: limit = st.selectbox('Maximum rows',[100,500,1000],index=1,key='preview_limit')
                try:
                    frame,total = preview_candles(stream,day,limit)
                    st.dataframe(frame,hide_index=True,width='stretch',height=260)
                    st.caption(f'{len(frame):,} of {total:,} stored rows for {day} · {stream["interval"]} · Asia/Kolkata. Export contains only these displayed rows.')
                    payload,name,mime = preview_export(frame,stream,day)
                    st.download_button('Export preview CSV',payload,name,mime,disabled=frame.empty,key='preview_export')
                except Exception as exc: st.error(f'Preview unavailable ({type(exc).__name__}). Run a quality check; no historical files were changed.')
            else: st.info('No readable sessions for this stream. Inspect manifest records and quality findings.')
    ingestion_panel(inv)


def render_data_quality(root):
    inv = inventory(root); state = report_state(root,inv)
    with st.container(border=True,key='quality_overview'):
        title('Data health',state['status'])
        st.write(state['explanation'])
        st.caption(f'Last audit: {checked_label(state)} · '+('Matches current manifest and file state' if state['current'] else 'Current lake audit not verified'))
        a,b=st.columns([1,2])
        with a:
            if st.button('Run quality check',type='primary',width='stretch',key='run_quality'):
                try:
                    progress = st.empty()
                    with st.spinner('Checking local committed files and session evidence…'):
                        run_quality_check(root, progress=lambda n,total,name: progress.caption(f'Checking stream {n} of {total}: {name}'))
                    progress.empty()
                    st.cache_data.clear()
                    st.rerun()
                except (OSError,RuntimeError,ValueError) as exc: st.error(f'Quality check could not complete: {exc}')
        with b: st.caption('Local read-only checks on historical partitions. Only audit artifacts are written. No broker calls, downloads, gap filling or repairs.')
        report_path=Path(root)/'quality_report.json'
        if report_path.exists(): st.download_button('Export audit report JSON',report_path.read_bytes(),'quality_report.json','application/json',key='audit_export')
    with st.container(border=True,key='quality_results'):
        title('Dataset checks','INTEGRITY ≠ COVERAGE ≠ CONTRACT IDENTITY')
        search=st.text_input('Search quality results',key='quality_search',placeholder='Instrument, segment or stream…')
        choices=filters(inv,'quality',with_health=True)
        groups=filtered_groups(inv,state,search,**choices)
        if groups:
            st.dataframe(dataset_table(groups),hide_index=True,width='stretch',height=min(400,40+35*len(groups)))
            selected=select_dataset(groups,'quality_dataset',all_option=True)
        else:
            selected=None; st.info('No datasets match these filters.' if inv['records'] else 'No data. An audit can report this empty state without inventing measurements.')
        selected_ids={g['group_id'] for g in groups} if selected in (None,'All datasets') else {selected}
        report=state['report'] or {}
        if state['current']:
            rows=[]
            for g in groups:
                if g['group_id'] not in selected_ids: continue
                for sid in g['streams']:
                    result=report.get('streams',{}).get(sid,{})
                    rows.append({'Stream / contract':inv['streams'][sid]['instrument'], **{k.title():v for k,v in result.get('dimensions',{}).items()}, 'Options identity':result.get('identity','Not checked')})
            st.dataframe(pd.DataFrame(rows),hide_index=True,width='stretch',height=min(260,40+len(rows)*35))
        else: st.info('A current audit is needed before per-check results can be presented as verified.')
    with st.container(border=True,key='quality_findings'):
        title('Findings & next actions')
        severity=st.selectbox('Severity',['All','Error','Warning','Info'],key='quality_severity')
        findings=[f for f in relevant_findings(inv,state) if f.get('dataset')=='Lake' or f.get('dataset') in selected_ids]
        findings=[f for f in findings if severity=='All' or f.get('severity')==severity]
        if not state['current'] and report: st.warning('Prior report evidence is shown for review only; it is not a current verification.')
        if findings:
            table=findings_frame(findings)
            table['Count']=table['Count'].map(lambda v:'Not measured' if pd.isna(v) else str(v))
            st.dataframe(table,hide_index=True,width='stretch',height=min(350,40+35*len(table)),
                         column_config={'Problem':st.column_config.TextColumn(width='large'),
                                        'Next action':st.column_config.TextColumn(width='large')})
            current=st.session_state.get('quality_finding',0)
            st.session_state.quality_finding=current if isinstance(current,int) and 0<=current<len(findings) else 0
            chosen=st.selectbox('Inspect finding',list(range(len(findings))),index=None,key='quality_finding',
                                format_func=lambda i:f"{i+1}. {findings[i]['problem'][:95]}")
            if chosen is not None:
                item=findings[chosen]
                st.write(item['problem'])
                st.info('Next action: '+item.get('action','Review source evidence.'))
            st.download_button('Export filtered findings CSV',table.to_csv(index=False),'quality_findings.csv','text/csv')
        else: st.info('No matching recorded findings.' if report else 'No audit findings yet. Run a quality check to measure what can be verified.')
        with st.expander('Technical evidence & measurement scope'):
            st.write('File integrity compares SHA-256 to the manifest. Candle validity checks stored OHLCV and duplicate timestamps. Removed input duplicates and quarantined conflicts are separate evidence. Coverage requires sourced, date-specific exchange sessions and futures applicability. Rolling options expiry identity remains unverified.')
            st.json(dict(audit_version=report.get('audit_version','Unversioned'), manifest_fingerprint=inv['manifest_fingerprint'],
                         audited_manifest=report.get('manifest_fingerprint','Not recorded'),stable_snapshot=report.get('stable_snapshot','Not recorded')))
            if report:
                st.caption('Full evidence, including every measured session, is in the JSON export. This panel shows a bounded sample.')
                if isinstance(report.get('streams'),dict):
                    sample={sid:dict(status=value.get('status'),dimensions=value.get('dimensions'),identity=value.get('identity'),
                                     session_count=len(value.get('sessions',[])),sessions=value.get('sessions',[])[:10])
                            for sid,value in list(report['streams'].items())[:5] if isinstance(value,dict)}
                    st.json(dict(scope=report.get('scope'),sample_streams=sample),expanded=False)
                else:
                    st.json({k:v for k,v in report.items() if k not in ('findings','streams')},expanded=False)
