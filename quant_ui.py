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
from quant_terminal import (PAGES, INSTRUMENT_LABELS, STRATEGY_LABELS, STRATEGY_NOTES,
                            INTERVALS, EXITS, build_config, lake_snapshot, load_registry,
                            register_run, save_strategy, form_values, read_json)

st.set_page_config(page_title='Indian Market Research Lake', page_icon='📊', layout='wide', initial_sidebar_state='expanded')
st.markdown('<style>'+Path(__file__).with_name('quant_terminal.css').read_text(encoding='utf-8')+'</style>', unsafe_allow_html=True)


@st.cache_data(ttl=120, show_spinner=False)
def snapshot(root):
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
            state = 'positive' if row['status'] == 'Ready' else 'amber'
            rows += (f'<tr><td class="strong">{escape(row["dataset"])}</td><td>{row["interval"]}</td>'
                     f'<td>{row["coverage"]}</td><td><span class="{state} dot">{row["status"]}</span></td></tr>')
        html('<div class="table-scroll"><table class="terminal-table"><thead><tr><th>Dataset</th><th>Interval</th>'
             '<th>Coverage</th><th>Status</th></tr></thead><tbody>'+rows+'</tbody></table></div>')
        st.caption(f'{data["rows"]:,} candles · {data["partitions"]:,} partitions · Historical lake snapshot')


def quality_card(data):
    report = data['quality']
    checked = pd.Timestamp(report['checked_at']).tz_convert(IST).strftime('%d %b %Y, %H:%M IST') if report.get('checked_at') else 'Not audited'
    counts = report.get('quality_counts', {})
    with st.container(border=True, key='quality'):
        card_title('🛡️ Data Quality', f'Last checked: {checked}')
        missing = f'{counts["missing_internal_minutes"]:,}' if 'missing_internal_minutes' in counts else '—'
        duplicates = f'{counts["exact_duplicates"]:,}' if 'exact_duplicates' in counts else '—'
        html(f'<div class="quality-grid"><div><label>Missing candles</label><strong>{missing}</strong></div>'
             f'<div><label>Duplicates</label><strong>{duplicates}</strong></div>'
             f'<div><label>Expiry mapping</label><strong class="amber quality-text">{data["expiry_mapping"]}</strong></div></div>')
        st.caption(f'Exact duplicates removed · {counts.get("conflicting_duplicate_rows", 0):,} conflicting rows quarantined. Rolling option expiry identities are unverified.')


def run_picker(runs, key, default=False):
    ids = [r['run_id'] for r in runs]
    labels = {r['run_id']: f'{r["display_id"]} · {r["strategy_label"]} · {r["config"]["instrument"]}' for r in runs}
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
    defaults = st.session_state.get('form_defaults', dict(instrument='NIFTY 50', strategy='Buy CE / Buy PE',
        period=(date(2025, 1, 1), date(2026, 10, 22)), capital=100000, allocation='100%', interval='5 minutes',
        take_profit='25%', stop_loss='25%', minimum_premium=100., exit_label='Target / EOD'))
    pending = st.session_state.pop('pending_strategy', None)
    if pending:
        defaults = form_values(pending['config'], pending['name'])
        st.session_state.form_defaults = defaults
        for key in defaults:
            st.session_state.pop('bt_'+key, None)
    with st.container(border=True, key='configure'):
        card_title('⚙️ Configure backtest', 'SIMULATION')
        a, b = st.columns(2)
        with a: instrument = st.selectbox('Instrument', list(INSTRUMENT_LABELS), index=list(INSTRUMENT_LABELS).index(defaults['instrument']), key='bt_instrument')
        with b: strategy = st.selectbox('Strategy', list(STRATEGY_LABELS), index=list(STRATEGY_LABELS).index(defaults['strategy']), key='bt_strategy')
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
        with a: take_profit = st.selectbox('Take profit', ['5%', '10%', '15%', '25%', '50%', '100%'], index=['5%', '10%', '15%', '25%', '50%', '100%'].index(defaults['take_profit']), key='bt_take_profit')
        with b: stop_loss = st.selectbox('Stop loss', ['25%', '15%', '10%', '5%'], index=['25%', '15%', '10%', '5%'].index(defaults['stop_loss']), key='bt_stop_loss')
        a, b = st.columns(2)
        with a:
            premium = st.number_input('Minimum premium', min_value=0., value=defaults['minimum_premium'], step=10., format='%.0f', key='bt_minimum_premium',
                                       disabled=instrument in ('CRUDEOIL', 'NATURALGAS'),
                                       help='₹ per option unit. Observed entry prices below this value are skipped before slippage. Options only.')
        with b:
            exit_label = st.selectbox('Exit', list(EXITS), index=list(EXITS).index(defaults['exit_label']), key='bt_exit_label',
                help='End of day: protective stop, no target. Trailing SL: target plus a trail at the selected stop percentage. Target / EOD: fixed stop and target. Every mode closes by session end.')
        st.session_state.form_defaults = dict(instrument=instrument, strategy=strategy, period=period, capital=capital,
            allocation=allocation, interval=interval, take_profit=take_profit, stop_loss=stop_loss,
            minimum_premium=premium, exit_label=exit_label)
        html('<div class="cost-bar">ⓘ &nbsp; Costs: Brokerage + taxes + slippage</div>')
        a, b = st.columns(2)
        with a: submitted = st.button('▶ Run Backtest', type='primary', width='stretch', key='run_backtest')
        with b: saved = st.button('💾 Save Strategy', width='stretch', key='save_strategy')
        st.caption(STRATEGY_NOTES[strategy])
        if exit_label == 'End of day': st.caption('End of day keeps the protective stop active; take profit is inactive.')
        if instrument in ('CRUDEOIL', 'NATURALGAS'):
            st.caption('MCX: stops / targets use price percentages; allocation reserves 100% of contract notional plus fees.')
        if instrument == 'BANKNIFTY':
            st.info('Spot data is available. BANKNIFTY option history and contract schedules must be added before simulation.')
        if submitted or saved:
            try:
                config = build_config(instrument, strategy, period, capital, allocation, interval, take_profit, stop_loss, premium, exit_label)
                if submitted:
                    with st.spinner('Simulating eligible historical candles…'):
                        result = run_backtest(config, output=OUT)
                        entry = register_run(OUT, result, strategy)
                    history.clear(); artifact.clear()
                    st.session_state.selected_run = result['run_id']
                    st.session_state.notice = f'{entry["display_id"]} complete · Net P&L {money(result["metrics"]["net_pnl"], True)}'
                    st.session_state.reset_run_picker = True
                    st.rerun()
                else:
                    record = save_strategy(OUT, config, strategy)
                    history.clear()
                    linked = f' · Linked to {record["last_run"]}' if record['last_run'] else ' · Ready to run'
                    st.session_state.notice = 'Strategy saved'+linked
                    st.rerun()
            except (ValueError, OSError, RuntimeError) as exc: st.error(str(exc))


def trade_ledger(trades):
    columns = {'trade_id': 'Trade', 'side': 'Side', 'entry_time': 'Entry time', 'exit_time': 'Exit time', 'contract': 'Contract / strike',
               'quantity': 'Quantity', 'entry_price': 'Entry ₹', 'exit_price': 'Exit ₹', 'capital_allocated': 'Allocated ₹',
               'total_cost': 'Costs ₹', 'net_pnl': 'Net P&L ₹', 'roi_pct': 'ROI %', 'exit_reason': 'Exit reason', 'used_modeled_prices': 'Modeled prices'}
    return trades.reindex(columns=list(columns)).rename(columns=columns)


def render_result(run_id, full=False):
    path = Path(OUT) / 'runs' / run_id / 'result.json'
    if not path.exists():
        st.info('This run’s result files are no longer available locally.'); return
    result = read_json(path)
    config, metrics = result['config'], result['metrics']
    entry = next((r for r in history(str(OUT))['runs'] if r['run_id'] == run_id), None)
    with st.container(border=True, key='results'):
        card_title(f'{entry["display_id"] if entry else run_id} / Backtest results', f'{config["instrument"]} · {config["timeframe"]} · Engine {result["engine_version"]}')
        pnl_color = '#10b981' if metrics['net_pnl'] >= 0 else '#ef4444'
        html(f'<style>.st-key-results [data-testid="stColumn"]:nth-child(-n+2) [data-testid="stMetricValue"]{{color:{pnl_color};}}'
             '.st-key-results [data-testid="stColumn"]:nth-child(3) [data-testid="stMetricValue"]{color:#ef4444;}</style>')
        cols = st.columns(4)
        for col, label, value in zip(cols, ['Net P&L', 'Return on capital', 'Max drawdown', 'Closed trades'],
            [money(metrics['net_pnl'], True), f'{metrics["roi_pct"]:.2f}%', f'{metrics.get("max_drawdown_pct", 0):.2f}%', str(metrics['total_trades'])]):
            col.metric(label, value)
        end = (pd.Timestamp(config['end'])-pd.Timedelta(days=1)).strftime('%d %b %Y')
        st.caption(f'{pd.Timestamp(config["start"]):%d %b %Y} – {end} · Capital {money(config["capital"])} · Allocation {config.get("capital_usage_pct", 100):g}% · Costs {money(metrics["total_cost"], decimals=2)}')
        modeled = metrics.get('modeled_trades', 0)
        evidence = f'{modeled} trades use modeled prices. ' if modeled else ''
        if config['instrument'] not in ('CRUDEOIL', 'NATURALGAS', 'GOLDM'): evidence += 'Rolling option expiry identity is unverified.'
        if evidence: html(f'<div class="evidence-note">{escape(evidence)} Realized equity includes all closed trades.</div>')
        equity = artifact(str(OUT), run_id, 'equity')
        if not equity.empty:
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[.76, .24], vertical_spacing=.09)
            fig.add_trace(go.Scatter(x=equity.date, y=equity.equity, name='Equity', line=dict(color='#22d3ee', width=2), fill='tozeroy', fillcolor='rgba(34,211,238,.05)'), row=1, col=1)
            fig.add_trace(go.Scatter(x=equity.date, y=equity.drawdown_pct, name='Drawdown %', line=dict(color='#ef4444', width=1.5), fill='tozeroy'), row=2, col=1)
            fig.update_yaxes(tickprefix='₹', row=1, col=1); fig.update_yaxes(ticksuffix='%', row=2, col=1)
            st.plotly_chart(theme(fig), width='stretch', key='equity_'+run_id)
        trades = artifact(str(OUT), run_id, 'trades')
        card_title('Trade ledger', f'{len(trades):,} entries')
        if trades.empty: st.info('No trades met the signal, premium, and capital requirements for this configuration.')
        else:
            ledger = trade_ledger(trades)
            st.dataframe(ledger, hide_index=True, width='stretch', height=min(360, 40+len(ledger)*35),
                          column_config={'Net P&L ₹': st.column_config.NumberColumn(format='₹ %.2f'), 'ROI %': st.column_config.NumberColumn(format='%.2f%%')})
            st.download_button('Download trade ledger', ledger.to_csv(index=False), f'{run_id}_trades.csv', 'text/csv')
        if full and not trades.empty:
            selected = st.selectbox('Trade replay', trades.trade_id.tolist(), key='trade_replay_'+run_id,
                                    format_func=lambda v: f'{v.rsplit("_", 1)[-1]} · {trades.loc[trades.trade_id.eq(v), "side"].iloc[0]}')
            paths = artifact(str(OUT), run_id, 'stop_paths')
            if not paths.empty:
                trace = paths[paths.trade_id.eq(selected)]
                fig = go.Figure()
                fig.add_trace(go.Candlestick(x=trace.timestamp, open=trace.open, high=trace.high, low=trace.low, close=trace.close,
                                            increasing_line_color='#10b981', decreasing_line_color='#ef4444', name='Execution price'))
                fig.add_trace(go.Scatter(x=trace.timestamp, y=trace.stop, name='Stop', line=dict(color='#ef4444', shape='hv')))
                if config.get('exit_mode') != 'end_of_day':
                    fig.add_trace(go.Scatter(x=trace.timestamp, y=trace.target, name='Target', line=dict(color='#22d3ee', dash='dot')))
                if 'price_source' in trace:
                    modeled_trace = trace[trace.price_source.ne('observed')]
                    if not modeled_trace.empty:
                        fig.add_trace(go.Scatter(x=modeled_trace.timestamp, y=modeled_trace.close, mode='markers', name='Modeled price', marker=dict(color='#fbbf24', symbol='diamond', size=7)))
                fig.update_xaxes(rangeslider_visible=False)
                st.plotly_chart(theme(fig), width='stretch')
                st.caption('Prices in Asia/Kolkata. Amber diamonds mark modeled candles; stop updates apply on the next minute.')
        st.download_button('Download run configuration', json.dumps(result['config'], indent=2), f'{run_id}_config.json', 'application/json')


def render_catalog(data, search):
    coverage_card(data)
    with st.container(border=True, key='catalog'):
        card_title('Dataset catalog', 'LOCAL PARQUET')
        frame = pd.DataFrame(data['catalog'])
        if frame.empty:
            st.info('No committed datasets. Build the data lake to begin.'); return
        mask = frame.instrument.str.contains(search, case=False, regex=False) | frame.dataset.str.contains(search, case=False, regex=False)
        st.dataframe(frame[mask].rename(columns=lambda c: c.replace('_', ' ').title()), hide_index=True, width='stretch')
        st.download_button('Download catalog', frame[mask].to_csv(index=False), 'lake_catalog.csv', 'text/csv')
    with st.container(border=True, key='explorer'):
        card_title('Explore candles')
        with QuantDataLake(ROOT) as lake: catalog = lake.instruments()
        a, b = st.columns([2, 1])
        selected = a.selectbox('Dataset', catalog, format_func=lambda v: f'{v[1]} / {v[0]}')
        day = b.date_input('Session', date(2026, 10, 22))
        if selected:
            with QuantDataLake(ROOT) as lake:
                rows = lake.query(selected[1], str(day), str(day+pd.Timedelta(days=1)), dataset=selected[0], timeframe='1d' if selected[0] == 'daily' else '1m')
            if rows.empty: st.info('No candles for this dataset and session.')
            else: st.dataframe(rows, hide_index=True, width='stretch', height=320)


def load_saved(record):
    st.session_state.pending_strategy = record
    st.session_state.page = 'Strategy Builder'


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
    data = snapshot(str(ROOT))
    registry = history(str(OUT))
except (ValueError, OSError) as exc:
    st.error(str(exc)); st.stop()
runs = sorted(registry['runs'], key=lambda r: r.get('last_opened', r['created_at']), reverse=True)
st.session_state.setdefault('page', 'Backtests')
if st.session_state.pop('reset_run_picker', False):
    for key in ('recent_run', 'report_run', 'search_run'): st.session_state.pop(key, None)

with st.sidebar:
    html('<div class="brand"><div class="brand-icon">▥</div><div>INDIAN MARKET<br>RESEARCH LAKE</div></div>')
    html('<div class="nav-label">WORKSPACE</div>')
    icons = {'Overview': '◫', 'Data Lake': '▤', 'Data Quality': '◇', 'Strategy Builder': '⌘', 'Backtests': '▥', 'Reports': '▧', 'Live Paper Trading': '◉'}
    st.radio('Navigation', PAGES, key='page', format_func=lambda v: f'{icons[v]}   {v}', label_visibility='collapsed')
    html('<div class="sidebar-footer"><span class="positive dot">LOCAL WORKSPACE</span><p>Historical research<br>Asia/Kolkata · IST</p></div>')

with st.container(key='topbar'):
    a, b, c = st.columns([6, 1.35, 1.55], vertical_alignment='center')
    with a: search = st.text_input('Search', placeholder='Search datasets, strategies, or runs...', label_visibility='collapsed', key='search')
    with b: html(f'<div class="lake-badge">● &nbsp; {"LIVE LAKE" if data["available"] else "DEMO DATA"}</div>')
    with c: html('<div class="profile"><span>AR</span><div>Researcher<small>Local workspace</small></div></div>')

page = st.session_state.page
titles = {'Overview': ('Workspace Overview', 'Indian markets • Research at a glance'),
          'Data Lake': ('Data Lake', 'Indian market datasets • Committed Parquet partitions'),
          'Data Quality': ('Data Quality', 'Coverage, gaps & provenance • Audit evidence'),
          'Strategy Builder': ('Strategy Builder', 'Build, save & reuse • Historical simulation'),
          'Backtests': ('Research & Backtest', 'Indian index options • Historical simulation'),
          'Reports': ('Research Reports', 'Saved runs • Trade ledgers & equity'),
          'Live Paper Trading': ('Live Paper Trading', 'Paper account • Read-only monitoring')}
title, subtitle = titles[page]
html(f'<div class="page-heading"><div class="eyebrow">01 / DARK TERMINAL</div><h1>{title}</h1><p>{subtitle}</p></div>')

cards = ''
for code, label in [('NIFTY', 'NIFTY 50 • NSE'), ('BANKNIFTY', 'BANKNIFTY • NSE'), ('SENSEX', 'SENSEX • BSE')]:
    market = data['markets'][code]
    if market:
        change, pct = market['change'], market['change_pct']
        color = 'positive' if change is not None and change >= 0 else 'negative'
        delta = f'{change:+,.2f} ({pct:+.2f}%)' if change is not None else 'Previous session unavailable'
        timestamp = pd.Timestamp(market['asof']).strftime('%d %b %Y · %H:%M IST')
        cards += f'<div class="market-card"><label>{label}</label><strong>{market["price"]:,.2f}</strong><div class="{color}">{delta}</div><small>As of {timestamp}</small></div>'
    else:
        cards += f'<div class="market-card"><label>{label}</label><strong>—</strong><div class="muted">Not ingested</div><small>No sample prices substituted</small></div>'
cards += '<div class="market-card metadata"><label>RESEARCH INFRASTRUCTURE</label><div class="meta-grid"><div>Source<strong>DhanHQ</strong></div><div>Storage<strong>Parquet</strong></div><div>Timezone<strong>Asia/Kolkata</strong></div></div><small>Local historical lake · No live quote feed</small></div>'
html('<div class="market-strip">'+cards+'</div>')

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
    left, right = st.columns([1.15, 1], gap='medium')
    with left:
        coverage_card(data); quality_card(data); recent_card(runs, search)
    with right: configure_card()
    if st.session_state.get('selected_run'): render_result(st.session_state.selected_run)
elif page == 'Overview':
    cols = st.columns(3)
    for col, label, value in zip(cols, ['Stored candles', 'Saved runs', 'Saved strategies'], [f'{data["rows"]:,}', f'{len(runs):,}', str(len(registry['strategies']))]): col.metric(label, value)
    a, b = st.columns([1.3, 1])
    with a: coverage_card(data); recent_card(runs, search)
    with b:
        quality_card(data)
        st.button('Configure a backtest →', type='primary', on_click=go_page, args=('Backtests',))
elif page == 'Data Lake': render_catalog(data, search)
elif page == 'Data Quality':
    quality_card(data); coverage_card(data)
    with st.container(border=True, key='audit'):
        card_title('Audit findings')
        report = data['quality']
        if not report: st.info('No audit report yet. Run audit_quant_lake.py to check the local lake.')
        else:
            for issue in report.get('issues', []): st.error(str(issue))
            if not report.get('issues'): st.success('Partition integrity checks passed. Historical coverage still requires the reviews below.')
            st.dataframe(pd.DataFrame([{'Check': k.replace('_', ' ').title(), 'Count': v} for k, v in report.get('quality_counts', {}).items()]), hide_index=True, width='stretch')
            st.caption(f'{report.get("short_sessions_for_review", 0)} short sessions need calendar review.')
            for note in report.get('notes', []): st.write('• '+note)
elif page == 'Strategy Builder':
    a, b = st.columns([1.15, 1], gap='large')
    with a: configure_card()
    with b: render_saved(registry['strategies'], search)
elif page == 'Reports':
    with st.container(border=True, key='library'):
        card_title('Run library', f'{len(runs)} completed runs')
        visible = [r for r in runs if search.lower() in f'{r["display_id"]} {r["run_id"]} {r["strategy_label"]} {r["config"]["instrument"]}'.lower()]
        if visible:
            selected = run_picker(visible, 'report_run', default=True)
            frame = pd.DataFrame([{'Run': r['display_id'], 'Strategy': r['strategy_label'], 'Instrument': r['config']['instrument'],
                                   'Net P&L ₹': r['metrics']['net_pnl'], 'Max drawdown %': r['metrics'].get('max_drawdown_pct'), 'Status': r['status'], 'Engine': r['engine_version']} for r in visible])
            st.dataframe(frame, hide_index=True, width='stretch', height=240)
            st.download_button('Export runs', frame.to_csv(index=False), 'backtest_runs.csv', 'text/csv')
        else:
            selected = None
            st.info('No saved runs match this search.')
    if selected: render_result(selected, full=True)
elif page == 'Live Paper Trading':
    with st.container(border=True, key='paper'): render_live_paper_room(ROOT / 'paper_trades')

html('<div class="terminal-footer"><span>INDIAN MARKET RESEARCH LAKE</span><span>Historical simulation · Local workspace · IST</span></div>')
