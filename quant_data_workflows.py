"""Native Streamlit range inspector and explicitly requested ingestion actions."""
from datetime import date
import pandas as pd
import streamlit as st

from quant_coverage import PRODUCTS,targets,date_bounds,coverage_report,freshness,usability
from quant_data_sync import CHOICES,build_plan,run_sync
from quant_data_quality import file_stamp


def evidence_token(root,inv):
    from quant_backtester import ENGINE_VERSION
    return (ENGINE_VERSION,inv['manifest_fingerprint'],inv['file_fingerprint'],file_stamp(root/'quality_report.json'),file_stamp(root/'session_calendar.json'))


@st.cache_data(show_spinner=False,max_entries=40)
def checked_range(root,target,start,end,token):
    return coverage_report(root,target,start,end)


def range_panel(root,inv):
    with st.container(border=True,key='range_inspector'):
        st.subheader('📅 Check Date-Range Coverage')
        options=targets(inv)
        target=st.selectbox('Instrument / Dataset',list(options),format_func=options.get,key='coverage_target')
        if st.session_state.get('coverage_previous_target')!=target:
            lo,hi=date_bounds(inv,target)
            st.session_state.coverage_from=lo;st.session_state.coverage_to=hi
            st.session_state.coverage_previous_target=target
        a,b=st.columns(2)
        start=a.date_input('From Date',key='coverage_from')
        end=b.date_input('To Date',key='coverage_to')
        token=evidence_token(root,inv)
        signature=(str(root),target,str(start),str(end),token)
        batch_signature=(str(root),str(start),str(end),token)
        if st.session_state.get('range_batch_signature')!=batch_signature:
            st.session_state.pop('range_batch',None)
        left,right=st.columns(2)
        if left.button('Check coverage',type='primary',key='check_coverage',disabled=end<start):
            try:
                with st.spinner('Checking required inputs month by month; no simulations or downloads…'):
                    result=checked_range(str(root),target,str(start),str(end),token)
                st.session_state.range_report=(signature,result)
            except Exception as exc:
                st.error('Coverage could not be inspected ('+type(exc).__name__+'). Check the local audit and partition findings.')
        if right.button('Check all product usability',key='check_all_coverage',disabled=end<start):
            reports={}
            try:
                with st.spinner('Checking each stored product against the same From / To dates…'):
                    for product in PRODUCTS:
                        if any(s['underlying']==product for s in inv['streams'].values()):
                            reports[product]=checked_range(str(root),product,str(start),str(end),token)
                st.session_state.range_batch=reports;st.session_state.range_batch_signature=batch_signature
                product=inv['streams'][target]['underlying'] if target in inv['streams'] else target
                if product in reports:st.session_state.range_report=(signature,reports[product])
            except Exception as exc:st.error('Product checks could not finish ('+type(exc).__name__+'). No downloads were started.')
        if end<start:st.error('To Date must be on or after From Date.')
        saved=st.session_state.get('range_report')
        report=saved[1] if saved and saved[0]==signature else None
        fresh=freshness(root,inv,target)
        text=f"Last stored (oldest stream tail): {fresh['last'] or 'No data'} • Latest completed boundary: {fresh['date'] or 'Unknown'}"
        text+=f" • {fresh['lag']} trading days behind" if fresh['lag'] is not None else ' • Trading-day lag not verified'
        st.caption(text+' · '+fresh['basis'])
        if report:
            cols=st.columns(3)
            cols[0].metric('Stored Trading Sessions in Range',report['stored'])
            cols[1].metric('Backtest-Eligible Sessions (Ready)',report['ready'] if report['ready'] is not None else 'Not verified',
                           f"{report['percent']:.1f}% of stored sessions" if report['percent'] is not None else None)
            cols[2].metric('Excluded / Required-Input Gaps',report['excluded'] if report['excluded'] is not None else 'Not verified')
            st.write(usability(report))
            if report['error']:st.warning(report['error'])
            st.caption(report['scope'])
            if report['absent']:st.warning(f"{len(report['absent'])} entire regular-index sessions have no underlying candles in this range (engine calendar): "+', '.join(report['absent']))
            st.dataframe(pd.DataFrame(report['monthly']),hide_index=True,width='stretch')
            with st.expander('Excluded dates and exact required-input evidence',expanded=bool(report['findings'])):
                if report['findings']:
                    st.caption('One excluded session may have several overlapping findings. Missing timestamps and rejected input rows are separate measurements; do not add their counts as unique missing minutes.')
                    table=pd.DataFrame([{'Date':r['date'],'Stream / contract':r['stream'],'Missing/Conflicting Minutes':', '.join(r['timestamps']) or 'Not localized',
                                         'Measured count':str(r['count']) if r['count'] is not None else 'Not measured','Reason':r['problem'],'Status':r['status']} for r in report['findings']])
                    st.dataframe(table,hide_index=True,width='stretch')
                    st.download_button('Export range eligibility CSV',table.to_csv(index=False),'range_eligibility.csv','text/csv')
                else:st.info('No excluded sessions recorded in this checked range. Entirely absent sessions still require calendar evidence.')
        else:st.caption('Choose dates and check coverage. Daily source rows and individual option streams do not by themselves establish full backtest readiness.')
        return report


def report_for_group(group,report):
    if group['interval']=='1 day':return 'Daily source — intraday bundle required'
    if report and report['product']==group['underlying']:return usability(report)
    batch=st.session_state.get('range_batch',{})
    if group['underlying'] in batch:return usability(batch[group['underlying']])
    return 'Check selected range'


def _run(root,selection,*,latest=False,start=None,end=None,skip=True):
    try:
        status=st.empty()
        with st.spinner('Downloading requested data, preserving cached partitions, then refreshing the audit…'):
            result=run_sync(root,selection,start,end,latest=latest,skip=skip,
                            progress=lambda i,total,name:status.caption(f'Request {i} / {total}: {name}'))
        status.empty();st.session_state.sync_result=result
        st.cache_data.clear();st.session_state.pop('range_report',None)
        st.rerun()
    except ValueError as exc:st.error(str(exc))
    except Exception as exc:st.error(f'Sync could not complete ({type(exc).__name__}). Existing data was retained where possible. Check the local audit before running a backtest. No secret values are displayed.')


def sync_panel(root,*,historical_only=False):
    with st.container() if historical_only else st.expander('🔄 Sync Latest Data / ➕ Add Historical Data',expanded=False):
        st.caption('Only these download buttons contact DhanHQ. Uses local .env credentials and the existing one-time token-refresh fallback. No orders or traders are started.')
        selection=st.selectbox('Target dataset group',CHOICES,key='sync_selection')
        if not historical_only:
            try:
                plan=build_plan(root,selection,latest=True)
                st.write(f"Latest sync: {plan['start'] or 'No cached-tail request'} → {str(pd.Timestamp(plan['end_exclusive']).date()-pd.Timedelta(days=1)) if plan['end_exclusive'] else 'Check contract metadata and freshness below'}")
                st.caption(f"{len(plan['jobs'])} planned cached-stream requests. Each stream resumes from its own last stored date. Current MCX contract metadata is refreshed only after clicking.")
                st.caption('Latest sync appends missing tails. To re-fetch gaps inside an already committed partition, use the historical range controls and turn off Skip already cached chunks.')
                for info in {v['basis'] for v in plan['cutoffs'].values()}:st.caption(info)
                with st.expander('Review latest requests'):
                    st.dataframe(pd.DataFrame([{'Stream / contract':j['meta']['instrument'],'Dataset':j['dataset'],'From':j['start'],'To (exclusive)':j['end']} for j in plan['jobs']]),hide_index=True,width='stretch')
                if plan['missing_contract_metadata']:st.info('Current MCX contract IDs will be looked up when syncing: '+', '.join(plan['missing_contract_metadata']))
                if st.button('🔄 Sync Latest Data',key='sync_latest'): _run(root,selection,latest=True)
            except (ValueError,OSError,KeyError) as exc:st.warning('Cannot plan latest sync: '+str(exc))
            st.divider()
        st.write('➕ Add / Backfill Historical Data')
        a,b=st.columns(2)
        start=a.date_input('Start Date',date(2025,1,1),key='sync_start')
        end=b.date_input('End Date',pd.Timestamp.now(tz='Asia/Kolkata').date()-pd.Timedelta(days=1),key='sync_end')
        skip=st.checkbox('Skip already cached chunks (Fast incremental)',True,key='sync_skip')
        if not skip:st.warning('Force re-fetch selected range. Overlapping partitions are refreshed using their full original bounds so neighboring stored dates are preserved. Failed/empty replacements retain the old data.')
        try:
            historical=build_plan(root,selection,start,end,skip=skip)
            with st.expander(f"Review historical requests ({len(historical['jobs'])})"):
                st.dataframe(pd.DataFrame([{'Stream / contract':j['meta']['instrument'],'Dataset':j['dataset'],'From':j['start'],'To (exclusive)':j['end']} for j in historical['jobs']]),hide_index=True,width='stretch')
            if st.button('Download Historical Range',key='sync_historical'):_run(root,selection,start=start,end=end,skip=skip)
        except (ValueError,OSError,KeyError) as exc:st.info(str(exc))
        result=st.session_state.get('sync_result')
        if result:
            st.success(f"{result['committed']} request records committed • {result['rows_added']:+,} net stored rows • {result.get('product_sessions_added',0)} newly stored instrument-sessions")
            st.caption(f"{result['stream_sessions_added']} stream-sessions added. An instrument-session counts one product/date once; its spot, option sides and contracts are not added as separate trading days. New dates still need eligibility checks.")
            if result['audit_current']:st.info('Local audit refreshed against the stored lake. Review findings and re-check range eligibility; a successful download does not guarantee readiness.')
            else:st.error('Audit not verified. Run a local quality check before backtesting.')
            for message in result['failures']:st.warning(message)
