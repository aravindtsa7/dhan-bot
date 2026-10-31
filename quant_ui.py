"""Dark terminal for the local Indian market research lake."""
from datetime import date
from html import escape
import json
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from quant_backtester import OUT, run_backtest
from quant_lake import ROOT, IST, QuantDataLake
from quant_paper_ui import render_live_paper_room
from quant_data_ui import render_data_lake, render_data_quality, checked_label
from quant_data_quality import read_document, fingerprint, file_fingerprint
from quant_terminal import (PAGES, INSTRUMENT_LABELS, STRATEGY_LABELS, STRATEGY_NOTES,
                            INTERVALS, EXITS, build_config, lake_snapshot, load_registry,
                            save_result, save_strategy, form_values, read_json, load_saved_strategies, saved_strategy_path, instrument_defaults)
from quant_results import trade_ledger, ledger_html, trade_chart
from quant_terminal import EXPIRY_EXITS
from quant_deployment import render_strategy_deployment

st.set_page_config(page_title='Indian Market Research Lake', page_icon='📊', layout='wide', initial_sidebar_state='expanded')
st.markdown('<style>'+Path(__file__).with_name('quant_terminal.css').read_text(encoding='utf-8')+'</style>', unsafe_allow_html=True)


@st.cache_data(ttl=120, show_spinner=False)
def snapshot(root, evidence_token=None):
    return lake_snapshot(root)


@st.cache_data(ttl=15, show_spinner=False)
def history(output):
    return load_registry(output)


@st.cache_data(ttl=30, show_spinner=False)
def artifact(output, run_id, name):
    path = Path(output) / 'runs' / run_id / f'{name}.parquet'
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def html(value):
    st.markdown(value, unsafe_allow_html=True)


def money(value, signed=False, decimals=0):
    if value is None or pd.isna(value): return '—'
    sign = '+' if value > 0 and signed else '−' if value < 0 else ''
    return f'{sign}₹{abs(value):,.{decimals}f}'


def go_page(page):
    st.session_state.page = page


def select_run(widget):
    st.session_state.selected_run = st.session_state[widget]


def theme(fig, height=350):
    fig.update_layout(template='plotly_dark', paper_bgcolor='#111a2b', plot_bgcolor='#111a2b', height=height,
                      margin=dict(l=12, r=12, t=30, b=12), font=dict(family='Inter, system-ui, sans-serif', color='#94a3b8', size=12),
                      legend=dict(orientation='h', y=1.12), hovermode='x unified')
    fig.update_xaxes(gridcolor='#1e2d45', zerolinecolor='#1e2d45')
    fig.update_yaxes(gridcolor='#1e2d45', zerolinecolor='#1e2d45')
    return fig


def card_title(title, detail=''):
    html(f'<div class="card-heading"><h3>{escape(title)}</h3><span>{escape(detail)}</span></div>')


def coverage_card(data):
    with st.container(border=True, key='coverage'):
        card_title('📊 Dataset Coverage', 'COMMITTED PARTITIONS')
        rows = ''
        for row in data['coverage']:
            state = 'positive' if row['status'] == 'Checks passed' else 'amber'
            rows += (f'<tr><td class="strong">{escape(row["dataset"])}</td><td>{row["interval"]}</td>'
                     f'<td>{row["coverage"]}</td><td><span class="{state} dot">{row["status"]}</span></td></tr>')
        html('<div class="table-scroll"><table class="terminal-table"><thead><tr><th>Dataset</th><th>Interval</th>'
             '<th>Coverage</th><th>Status</th></tr></thead><tbody>'+rows+'</tbody></table></div>')
        st.caption(f'{data["rows"]:,} candles · {data["partitions"]:,} partitions · Historical lake snapshot')


def quality_card(data):
    state = data['quality_state']
    with st.container(border=True, key='quality'):
        card_title('🛡️ Data Quality', checked_label(state))
        st.write(state['status'])
        st.caption(state['explanation'])
        st.caption('File integrity, candle validity, historical session coverage and options identity are separate checks.')
        st.caption('Options expiry identity: '+data['expiry_mapping'])


def run_picker(runs, key, default=False):
    ids = [r['run_id'] for r in runs]
    labels = {r['run_id']: f'{r["display_id"]} · {r.get("note") or r["strategy_label"]} · {r["config"]["instrument"]}' for r in runs}
    current = st.session_state.get('selected_run')
    if current in ids and st.session_state.get(key) != current:
        st.session_state[key] = current
    index = ids.index(current) if current in ids else 0 if default and ids else None
    selected = st.selectbox('Open run', ids, index=None if key in st.session_state else index, format_func=lambda v: labels[v],
                            placeholder='Select a run to inspect', key=key, on_change=select_run, args=(key,))
    if selected and current is None: st.session_state.selected_run = selected
    return selected


def recent_card(runs, search=''):
    visible = [r for r in runs if search.lower() in f'{r["display_id"]} {r["run_id"]} {r["strategy_label"]} {r["config"]["instrument"]}'.lower()]
    with st.container(border=True, key='recent'):
        a, b = st.columns([1, 1], vertical_alignment='center')
        with a: card_title('🕒 Recent Runs')
        with b: st.button('View all runs →', type='tertiary', width='stretch', on_click=go_page, args=('Reports',))
        rows = ''
        for row in visible[:5]:
            metric = row['metrics']
            color = 'positive' if metric['net_pnl'] >= 0 else 'negative'
            dd = metric.get('max_drawdown_pct')
            drawdown = f'{abs(dd):.1f}%' if dd is not None else '—'
            rows += (f'<tr><td class="run-id">{escape(row["display_id"])}</td><td>{escape(row["strategy_label"])}</td>'
                     f'<td class="{color} nowrap">{money(metric["net_pnl"], True)}</td><td>{drawdown}</td><td class="positive nowrap">● Complete</td></tr>')
        if rows:
            html('<div class="table-scroll"><table class="terminal-table runs-table"><thead><tr><th>Run</th><th>Strategy</th>'
                 '<th>Net P&amp;L</th><th>Max drawdown</th><th>Status</th></tr></thead><tbody>'+rows+'</tbody></table></div>')
            run_picker(visible, 'recent_run')
        else: st.info('No matching runs yet. Configure a backtest to create your first result.')


def configure_card():
    defaults = st.session_state.get('form_defaults', dict(instrument='NIFTY 50', strategy=next(iter(STRATEGY_LABELS)),
        period=(date(2025, 1, 1), date(2026, 10, 22)), capital=100000, allocation='25%', interval='5 minutes',
        take_profit='25%', stop_loss='15%', minimum_premium=50., exit_label='Target / EOD'))
    pending = st.session_state.pop('pending_strategy', None)
    if pending:
        defaults = form_values(pending['config'], pending['name'])
        st.session_state.form_defaults = defaults
        st.session_state.defaults_instrument = defaults['instrument']
        for key in defaults:
            st.session_state.pop('bt_'+key, None)
        for key in ('bt_expiry_tp','bt_expiry_sl','bt_expiry_exit_label'):
            st.session_state.pop(key,None)
    with st.container(border=True, key='configure'):
        card_title('⚙️ Configure backtest', 'SIMULATION')
        a, b = st.columns(2)
        with a: instrument = st.selectbox('Instrument', list(INSTRUMENT_LABELS), index=list(INSTRUMENT_LABELS).index(defaults['instrument']), key='bt_instrument')
        with b: strategy = st.selectbox('Strategy', list(STRATEGY_LABELS), index=list(STRATEGY_LABELS).index(defaults['strategy']), key='bt_strategy')
        prior_instrument = st.session_state.get('defaults_instrument', instrument)
        if prior_instrument != instrument:
            values = instrument_defaults(INSTRUMENT_LABELS[instrument])
            defaults.update(values)
            for key in values: st.session_state.pop('bt_'+key, None)
        st.session_state.defaults_instrument = instrument
        a, b = st.columns(2)
        with a:
            period = st.date_input('Period', defaults['period'], min_value=date(2025, 1, 1),
                                   max_value=date(2026, 12, 31), format='DD/MM/YYYY', key='bt_period')
        with b:
            capital = st.number_input('Capital', min_value=1000, value=defaults['capital'], step=10000, key='bt_capital',
                                       help='Trading capital in Indian rupees. Whole lots include a fee reserve.')
        a, b = st.columns(2)
        with a: allocation = st.selectbox('Capital allocation', ['100%', '75%', '50%', '25%'], index=['100%', '75%', '50%', '25%'].index(defaults['allocation']), key='bt_allocation')
        with b: interval = st.selectbox('Candle interval', list(INTERVALS), index=list(INTERVALS).index(defaults['interval']), key='bt_interval')
        a, b = st.columns(2)
        if strategy=='Expiry Day':
            with a: take_profit = f"{st.number_input('Take Profit %',min_value=0.1,value=float(defaults['take_profit'].rstrip('%')),step=5.,key='bt_expiry_tp'):g}%"
            with b: stop_loss = f"{st.number_input('Stop Loss %',min_value=0.1,max_value=99.9,value=float(defaults['stop_loss'].rstrip('%')),step=5.,key='bt_expiry_sl'):g}%"
        else:
            targets=list(dict.fromkeys(['5%', '10%', '15%', '25%', '50%', '100%',defaults['take_profit']]))
            stops=list(dict.fromkeys(['50%', '30%', '25%', '20%', '15%', '10%', '5%',defaults['stop_loss']]))
            with a: take_profit = st.selectbox('Take profit', targets, index=targets.index(defaults['take_profit']), key='bt_take_profit')
            with b: stop_loss = st.selectbox('Stop loss', stops, index=stops.index(defaults['stop_loss']), key='bt_stop_loss')
        a, b = st.columns(2)
        with a:
            premium = st.number_input('Minimum premium', min_value=0., value=defaults['minimum_premium'], step=10., format='%.0f', key='bt_minimum_premium',
                                       disabled=instrument in ('CRUDEOIL', 'NATURALGAS', 'GOLDM'),
                                       help='₹ per option unit. Observed entry prices below this value are skipped before slippage. Options only.')
        with b:
            choices=EXPIRY_EXITS if strategy=='Expiry Day' else EXITS
            prior_mode={**EXITS,**EXPIRY_EXITS}.get(defaults['exit_label'],'target_eod')
            default_exit=next(k for k,v in choices.items() if v==prior_mode)
            exit_label = st.selectbox('Exit', list(choices), index=list(choices).index(default_exit), key='bt_expiry_exit_label' if strategy=='Expiry Day' else 'bt_exit_label',
                help='End of day: protective stop, no target. Trailing SL: target plus a trail at the selected stop percentage. Target / EOD: fixed stop and target. Every mode closes by session end.')
        expiry_settings={}
        if strategy=='Expiry Day':
            with st.expander('Expiry Day timing and date window',expanded=True):
                modes={'Pre-expiry close → expiry day (BTST)':'btst','Expiry-day intraday (09:15 signal / 09:16 fill)':'expiry_intraday'}
                selected=st.selectbox('Entry mode',list(modes),index=list(modes.values()).index(defaults.get('expiry_entry_mode','btst')),key='bt_expiry_entry_mode')
                a,b=st.columns(2)
                with a: signal=st.selectbox('Pre-expiry signal (IST)',['15:25','15:26','15:27','15:28'],index=['15:25','15:26','15:27','15:28'].index(defaults.get('expiry_entry_signal','15:25')),key='bt_expiry_entry_signal')
                with b: cutoff=st.selectbox('Expiry EOD cutoff (IST)',['15:20','15:15'],index=['15:20','15:15'].index(defaults.get('expiry_exit_cutoff','15:20')),key='bt_expiry_exit_cutoff')
                include=st.checkbox('Include prior entry session when Period starts on expiry day',value=defaults.get('expiry_include_prior_session',True),key='bt_expiry_include_prior_session')
                basis=st.selectbox('Expiry session evidence',['calendar','observed_reset'],index=['calendar','observed_reset'].index(defaults.get('expiry_calendar_mode','calendar')),key='bt_expiry_calendar_mode',
                    format_func=lambda v:'Dated exchange calendar + holiday shifts' if v=='calendar' else 'Recorded expiry / weekly-reset evidence only')
                expiry_settings=dict(expiry_entry_mode=modes[selected],expiry_entry_signal=signal,expiry_exit_cutoff=cutoff,expiry_include_prior_session=include,expiry_calendar_mode=basis)
                st.caption('Period selects expiry exit dates. BTST fills one minute after the signal and holds both legs overnight. Candle interval changes chart aggregation; basket risk is checked every minute. EOD keeps the combined protective stop active.')
                if basis=='observed_reset': st.caption('ATM strike changes alone are insufficient. The current WEEK1 schema has no dated expiry/reset evidence; this mode may produce no eligible pairs.')
        st.session_state.form_defaults = dict(instrument=instrument, strategy=strategy, period=period, capital=capital,
            allocation=allocation, interval=interval, take_profit=take_profit, stop_loss=stop_loss,
            minimum_premium=premium, exit_label=exit_label,**expiry_settings)
        html('<div class="cost-bar">ⓘ &nbsp; Costs: Brokerage + taxes + slippage</div>')
        st.caption('Allocation caps committed capital, not maximum loss. Default allocation is 25%; entries require whole lots plus fees.')
        a, b = st.columns(2)
        with a: submitted = st.button('▶ Run Backtest', type='primary', width='stretch', key='run_backtest')
        with b: strategy_name = st.text_input('Strategy Name', placeholder='Nifty_5m_ORB_1L', key='strategy_name')
        saved = st.button('📌 Save Strategy', width='stretch', key='save_strategy')
        st.caption(STRATEGY_NOTES[strategy])
        if exit_label == 'End of day': st.caption('End of day keeps the protective stop active; take profit is inactive.')
        if instrument in ('CRUDEOIL', 'NATURALGAS', 'GOLDM'):
            st.caption('MCX: existing strategies use percentage risk; book strategies cap stop distance by ATR and the selected stop percentage, with reward/risk targets. Allocation reserves 100% of contract notional plus fees.')
        if instrument == 'BANKNIFTY':
            st.info('Spot data is available. BANKNIFTY option history and contract schedules must be added before simulation.')
        if submitted or saved:
            try:
                config = build_config(instrument, strategy, period, capital, allocation, interval, take_profit, stop_loss, premium, exit_label,**expiry_settings)
                if submitted:
                    with st.spinner('Simulating eligible historical candles…'):
                        result = run_backtest(config, output=OUT, persist=False)
                    st.session_state.active_result = result
                    st.session_state.selected_run = None
                    st.session_state.notice = 'Backtest complete · Unsaved session result'
                    st.rerun()
                else:
                    record = save_strategy(OUT, config, strategy_name)
                    st.session_state.notice = 'Strategy saved: '+record['name']
                    st.rerun()
            except (ValueError, OSError, RuntimeError) as exc: st.error(str(exc))


def render_result(run_id=None, full=True, result=None):
    if result is None:
        path = Path(OUT) / 'runs' / run_id / 'result.json'
        if not path.exists():
            st.info('This result is no longer available locally.'); return
        result = read_json(path)
    run_id = result['run_id']
    config, metrics = result['config'], result['metrics']
    frames = result.get('_frames')
    def table(name):
        return frames.get(name, pd.DataFrame()) if frames is not None else artifact(str(OUT), run_id, name)
    trades = table('trades')
    with st.container(border=True, key='results'):
        card_title('Backtest results', f'{config["instrument"]} · {config["timeframe"]} · Engine {result["engine_version"]}')
        note = st.text_input('Result label / note (optional)', key='result_note_'+run_id)
        if st.button('💾 Save Result', key='save_result', disabled=frames is None):
            try:
                entry = save_result(OUT, result, note)
                history.clear(); artifact.clear()
                st.session_state.notice = entry['display_id']+' saved to Reports'
                st.rerun()
            except (ValueError, OSError) as exc: st.error(str(exc))
        if frames is not None: st.caption('Session result · Reports contains only results you explicitly save.')
        wins = trades[trades.net_pnl.gt(0)] if len(trades) else trades
        losses = trades[trades.net_pnl.lt(0)] if len(trades) else trades
        ce = trades[trades.side.isin(['CE','LONG','BULL_CALL_SPREAD','STRAP'])] if len(trades) else trades
        pe = trades[trades.side.isin(['PE','SHORT','BEAR_PUT_SPREAD','STRIP'])] if len(trades) else trades
        basket = len(trades) and 'structure_label' in trades
        slippage = float(trades.slippage_cost.sum()) if len(trades) and 'slippage_cost' in trades else 0.
        pf = '∞' if metrics.get('profit_factor_infinite') else '—' if metrics['profit_factor'] is None else f'{metrics["profit_factor"]:.2f}'
        items = [
            ('Net P&L', money(metrics['net_pnl'], True), f'{metrics["roi_pct"]:+.2f}% return', 'positive' if metrics['net_pnl']>=0 else 'negative'),
            ('Win Rate %', f'{metrics["win_rate"]:.1f}%', f'{len(wins)} wins / {len(trades)} trades', ''),
            ('Profit Factor', pf, 'Net winners / net losers', ''),
            ('Max Drawdown', money(metrics['max_drawdown']), f'{metrics["max_drawdown_pct"]:.2f}% · daily realized', 'negative'),
            ('Bullish Structures P&L' if basket else 'CE Trades P&L' if config['instrument'] not in ('CRUDEOIL','NATURALGAS','GOLDM') else 'Long Trades P&L', money(ce.net_pnl.sum() if len(ce) else 0, True), f'{len(ce)} trades', ''),
            ('Bearish Structures P&L' if basket else 'PE / Short Trades P&L', money(pe.net_pnl.sum() if len(pe) else 0, True), f'{len(pe)} trades', ''),
            ('Avg Win / Avg Loss', money(wins.net_pnl.mean() if len(wins) else 0)+' / '+money(losses.net_pnl.mean() if len(losses) else 0), 'After fees and slippage', ''),
            ('Total Costs', money(metrics['total_cost']+slippage), f'Fees {money(metrics["total_cost"])} + slippage {money(slippage)}', ''),
        ]
        if config['strategy']=='expiry_day':
            items[4]=('CE + PE baskets',str(len(trades)),'Every exit closes both legs','')
            items[5]=('Average combined return',f'{trades.combined_return_pct.mean() if len(trades) else 0:+.2f}%','Before fees; after fill slippage','')
        html('<div class="result-kpis">'+''.join(f'<div class="result-kpi"><label>{escape(label)}</label><strong class="{color}">{value}</strong><small>{escape(detail)}</small></div>' for label,value,detail,color in items)+'</div>')
        if len(trades) and 'structure_label' in trades:
            for label, group in trades.groupby('structure_label'):
                html(f'<span class="trade-badge buy">{escape(label)}</span> {len(group)} trades · {money(group.net_pnl.sum(), True)}')
        st.caption(f'Capital {money(config["capital"])} · Allocation {config.get("capital_usage_pct",100):g}% · {result["eligible_sessions"]} eligible / {result["excluded_sessions"]} excluded sessions')
        if result.get('repaired_inputs'):
            st.warning(f"Uses {len(result['repaired_inputs'])} repaired input candles. Synthetic bars can affect signals and fills; results are not verified observed-data performance.")
            with st.expander('Repaired input provenance'): st.dataframe(pd.DataFrame(result['repaired_inputs']), hide_index=True)
        if config['instrument'] not in ('CRUDEOIL','NATURALGAS','GOLDM'):
            st.caption(f'Rolling option expiry identity is unverified; {metrics.get("modeled_trades",0)} trades use modeled execution prices. Zero-volume spot VWAP is a disclosed TWAP proxy.')
            st.caption(f"Wing fallback: {metrics.get('modeled_wing_fallback_trades',0)} trades. Estimates use intrinsic plus bounded wing time value. Basket credits are signed negative values; rising combined value is profit.")
        if not trades.empty:
            default = trades.net_pnl.idxmax()
            ids = trades.trade_id.tolist()
            selected = st.selectbox('Inspect trade', ids, index=ids.index(trades.loc[default,'trade_id']), key='trade_replay_'+run_id,
                format_func=lambda v: f'{v.rsplit("_",1)[-1]} · {trades.loc[trades.trade_id.eq(v),"side"].iloc[0]} · {money(trades.loc[trades.trade_id.eq(v),"net_pnl"].iloc[0], True)}')
            trade = trades[trades.trade_id.eq(selected)].iloc[0]
            st.plotly_chart(theme(trade_chart(trade,table('underlying'),table('stop_paths'),config),650), width='stretch', key='inspection_'+run_id)
            if config['strategy']=='expiry_day':
                st.caption(f"Signal {trade.signal_time:%Y-%m-%d %H:%M} · Entry {trade.entry_time:%Y-%m-%d %H:%M} · Exit {trade.exit_time:%Y-%m-%d %H:%M} IST. "
                    f"CE ₹{trade.ce_entry_price:.2f} → ₹{trade.ce_exit_price:.2f} · PE ₹{trade.pe_entry_price:.2f} → ₹{trade.pe_exit_price:.2f}. "
                    f"Combined {money(trade.combined_entry_value)} → {money(trade.combined_exit_value)} ({trade.combined_return_pct:+.2f}% before fees).")
            st.caption('IST · Entry and exit markers use exact execution timestamps and fills. Dashed levels span the holding period; the active stop applies on the next candle. Amber diamonds identify modeled prices.')
        equity = table('equity')
        if not equity.empty:
            fig = make_subplots(rows=2,cols=1,shared_xaxes=True,row_heights=[.72,.28],vertical_spacing=.08)
            fig.add_trace(go.Scatter(x=equity.date,y=equity.equity,name='Equity',line=dict(color='#22d3ee')),row=1,col=1)
            fig.add_trace(go.Scatter(x=equity.date,y=equity.drawdown_pct,name='Drawdown %',line=dict(color='#ef4444'),fill='tozeroy'),row=2,col=1)
            fig.update_yaxes(tickprefix='₹',row=1,col=1)
            fig.update_yaxes(ticksuffix='%',row=2,col=1)
            st.plotly_chart(theme(fig),width='stretch',key='equity_'+run_id)
        monthly = table('monthly')
        if not monthly.empty:
            fig = go.Figure(go.Bar(x=monthly.month,y=monthly.net_pnl,name='Monthly P&L',marker_color=['#10b981' if v>=0 else '#ef4444' for v in monthly.net_pnl]))
            fig.update_layout(title='Monthly net P&L'); fig.update_yaxes(tickprefix='₹')
            st.plotly_chart(theme(fig,250),width='stretch',key='monthly_'+run_id)
        card_title('Trade ledger', f'{len(trades):,} trades')
        if trades.empty: st.info('No trades met the signal, premium, and capital requirements for this configuration.')
        else:
            ledger = trade_ledger(trades,config)
            html(ledger_html(ledger))
            st.caption('Gross P&L is before slippage; Costs includes fees, taxes and slippage. Net P&L = Gross P&L − Costs. Return uses allocated capital.')
            st.download_button('Export trade ledger CSV',ledger.to_csv(index=False),f'{run_id}_trades.csv','text/csv')
        with st.expander('Eligibility and execution evidence'):
            st.dataframe(pd.DataFrame(result.get('eligibility',[])),hide_index=True)
            st.dataframe(table('skipped_entries'),hide_index=True)
            st.write(result.get('limitations',[]))
        st.download_button('Download run configuration',json.dumps(config,indent=2),f'{run_id}_config.json','application/json')



def load_saved(record):
    st.session_state.pending_strategy = record
    st.session_state.page = 'Backtests'


def render_saved(records, search):
    with st.container(border=True, key='saved'):
        card_title('Saved strategies', f'{len(records)} configurations')
        matched = [r for r in records if search.lower() in (r['name']+' '+r['config']['instrument']).lower()]
        if not matched: st.info('Save a configuration to reuse its exact parameters here.')
        for record in reversed(matched):
            a, b = st.columns([3, 1], vertical_alignment='center')
            with a:
                st.markdown(f'**{record["name"]}** · {record["config"]["instrument"]}')
                st.caption(f'{record["config"]["timeframe"]} · {money(record["config"]["capital"])} · {record["last_run"] or "Not run yet"}')
            b.button('Load', key='load_'+record['strategy_id'], width='stretch', on_click=load_saved, args=(record,))


try:
    manifest, _ = read_document(ROOT / 'ingestion_manifest.json')
    evidence_token = (fingerprint(manifest), file_fingerprint(ROOT, manifest or {}),
        tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in
              (ROOT / 'quality_report.json', ROOT / 'session_calendar.json')))
    data = snapshot(str(ROOT), evidence_token)
    registry = history(str(OUT))
    registry['strategies'] = load_saved_strategies(saved_strategy_path(OUT))
except (ValueError, OSError) as exc:
    st.error(str(exc)); st.stop()
runs = sorted(registry['runs'], key=lambda r: r.get('last_opened', r['created_at']), reverse=True)
st.session_state.setdefault('page', 'Backtests')
if st.session_state.pop('reset_run_picker', False):
    for key in ('recent_run', 'report_run', 'search_run'): st.session_state.pop(key, None)

with st.sidebar:
    html('<div class="brand"><div class="brand-icon">▥</div><div>INDIAN MARKET<br>RESEARCH LAKE</div></div>')
    html('<div class="nav-label">WORKSPACE</div>')
    icons = {'Overview': '◫', 'Data Lake': '▤', 'Data Quality': '◇', 'Backtests': '▥', 'Reports': '▧', 'Live Paper Trading': '◉'}
    st.radio('Navigation', PAGES, key='page', format_func=lambda v: f'{icons[v]}   {v}', label_visibility='collapsed')
    html('<div class="sidebar-footer"><span class="positive dot">LOCAL WORKSPACE</span><p>Historical research<br>Asia/Kolkata · IST</p></div>')

with st.container(key='topbar'):
    a, b, c = st.columns([6, 1.35, 1.55], vertical_alignment='center')
    with a: search = st.text_input('Search', placeholder='Search datasets, strategies, or runs...', label_visibility='collapsed', key='search')
    if st.session_state.page not in ('Data Lake', 'Data Quality'):
        with b: html(f'<div class="lake-badge">● &nbsp; {"LOCAL LAKE" if data["available"] else "EMPTY LAKE"}</div>')
        with c: html('<div class="profile"><span>AR</span><div>Researcher<small>Local workspace</small></div></div>')

page = st.session_state.page
titles = {'Overview': ('Workspace Overview', 'Indian markets • Research at a glance'),
          'Data Lake': ('Data Lab — Historical 1m Inventory & Backfill Engine', 'Manage local 1-minute Parquet archives (SPOT · OPTIONS · MCX FUTURES), repair gaps, or sync to the latest trading session.'),
          'Data Quality': ('Data Quality — Audit Inspector', 'Audit evidence · Quarantine & repair provenance'),
          'Backtests': ('Research & Backtest', 'Indian index options • Historical simulation'),
          'Reports': ('Research Reports', 'Saved runs • Trade ledgers & equity'),
          'Live Paper Trading': ('Live Paper Trading', 'Saved strategies • Virtual execution & monitoring')}
title, subtitle = titles[page]
html(f'<div class="page-heading"><div class="eyebrow">01 / DARK TERMINAL</div><h1>{title}</h1><p>{subtitle}</p></div>')

tickers = []
for code, label in [('NIFTY','NIFTY 50'),('BANKNIFTY','BANKNIFTY'),('SENSEX','SENSEX')]:
    market = data['markets'][code]
    if market:
        pct = market['change_pct']
        color = 'positive' if pct is not None and pct>=0 else 'negative'
        change = f'{pct:+.2f}%' if pct is not None else '—'
        tickers.append(f'<span class="ticker" title="Historical lake close · {escape(market["asof"])}"><b>{label}</b> {market["price"]:,.2f} <em class="{color}">{change}</em></span>')
    else: tickers.append(f'<span class="ticker"><b>{label}</b> · <em>No data</em></span>')
if page not in ('Data Lake','Data Quality'):
    html('<div class="ticker-bar">'+''.join(tickers)+'<span class="ticker-pills"><i>DhanHQ</i><i>Parquet</i><i>IST</i><small>Historical close</small></span></div>')

notice = st.session_state.pop('notice', None)
if notice: st.success(notice)

if search.strip():
    with st.container(border=True, key='search_results'):
        card_title('Search results', search)
        matches = [r for r in data['catalog'] if search.lower() in f'{r["dataset"]} {r["instrument"]}'.lower()]
        strategies = [s for s in STRATEGY_LABELS if search.lower() in s.lower()]
        matching_runs = [r for r in runs if search.lower() in f'{r["display_id"]} {r["run_id"]} {r["strategy_label"]} {r["config"]["instrument"]}'.lower()]
        st.caption(f'{len(matches)} datasets · {len(strategies)} strategies · {len(matching_runs)} runs')
        if matches:
            st.write('Datasets: '+', '.join(r['instrument'] for r in matches[:5]))
            st.button('Explore matching datasets →', on_click=go_page, args=('Data Lake',))
        if strategies: st.write('Strategies: '+', '.join(strategies))
        if matching_runs:
            run_picker(matching_runs, 'search_run')
            st.button('Open selected report →', on_click=go_page, args=('Reports',))
        if not (matches or strategies or matching_runs): st.info('No matches. Try an instrument, strategy name, or BT run number.')

if page == 'Backtests':
    configure_card()
    if st.session_state.get('active_result'):
        render_result(result=st.session_state.active_result)
    with st.expander('Saved strategies'):
        render_saved(registry['strategies'], search)
elif page == 'Overview':
    cols = st.columns(3)
    for col, label, value in zip(cols, ['Stored candles', 'Saved runs', 'Saved strategies'], [f'{data["rows"]:,}', f'{len(runs):,}', str(len(registry['strategies']))]): col.metric(label, value)
    a, b = st.columns([1.3, 1])
    with a: coverage_card(data); recent_card(runs, search)
    with b:
        quality_card(data)
        st.button('Configure a backtest →', type='primary', on_click=go_page, args=('Backtests',))
elif page == 'Data Lake': render_data_lake(ROOT)
elif page == 'Data Quality': render_data_quality(ROOT)
elif page == 'Reports':
    with st.container(border=True, key='library'):
        card_title('Run library', f'{len(runs)} completed runs')
        visible = [r for r in runs if search.lower() in f'{r["display_id"]} {r["run_id"]} {r["strategy_label"]} {r["config"]["instrument"]}'.lower()]
        if visible:
            selected = run_picker(visible, 'report_run', default=True)
            frame = pd.DataFrame([{'Run': r['display_id'], 'Label / note': r.get('note',''), 'Strategy': r['strategy_label'], 'Instrument': r['config']['instrument'],
                                   'Net P&L ₹': r['metrics']['net_pnl'], 'Max drawdown %': r['metrics'].get('max_drawdown_pct'), 'Status': r['status'], 'Engine': r['engine_version']} for r in visible])
            st.dataframe(frame, hide_index=True, width='stretch', height=240)
            st.download_button('Export runs', frame.to_csv(index=False), 'backtest_runs.csv', 'text/csv')
        else:
            selected = None
            st.info('No saved runs match this search.')
    if selected: render_result(selected, full=True)
elif page == 'Live Paper Trading':
    with st.container(border=True, key='paper'):
        paper_root = render_strategy_deployment(ROOT / 'paper_trades', saved_strategy_path(OUT))
        render_live_paper_room(paper_root)

html('<div class="terminal-footer"><span>INDIAN MARKET RESEARCH LAKE</span><span>Historical simulation · Local workspace · IST</span></div>')
