"""Backtest-only TradeMind form/results; source rules are read-only controls."""
from datetime import date
from dataclasses import asdict
from pathlib import Path
import json
import pandas as pd

from quant_trademind import STRATEGIES
from trademind_backtest import LABELS, NATIVE_FRAMES, availability, frozen_config


def render_form(strategy, instrument, output):
    import streamlit as st
    from quant_backtester import Config, run_backtest
    from quant_terminal import form_values, save_strategy
    st.caption('SPEC_COMPLETE · IMPLEMENTED · research only. Parity scope and source conflicts: README_TRADEMIND_STRATEGIES.md.')
    provider = st.selectbox('Price source', ['dhan', 'trademind_retained'], key='tm_provider',
        format_func=lambda v: 'Dhan local lake (dated-identity evidence required)' if v == 'dhan' else 'Retained TradeMind: Upstox underlying / Groww dated options')
    year = st.selectbox('Independent baseline year', [2025, 2026], key='tm_year')
    scenario = st.selectbox('Sizing scenario', ['source_baseline', 'allocation_100'], key='tm_scenario',
        format_func=lambda v: 'Source baseline sizing (explicit source assumptions)' if v == 'source_baseline' else '100% available cash / bounded dated lots')
    st.caption(f'Fresh ₹100,000 account · {NATIVE_FRAMES[strategy]} native rules · strict observed prices · no parameter tuning.')
    if strategy == STRATEGIES[3]:
        st.warning('T3 includes two short options. Broker margin NOT_COMPUTABLE. Any loss-capacity sizing shown is theoretical and does not establish affordability at a broker.')
    ready = availability(strategy, provider)
    compatible = instrument == 'NIFTY 50'
    if not compatible:
        st.error('Unsupported combination: these frozen strategies support NIFTY only.')
    elif not ready['runnable']:
        st.warning(ready['reason'])
    else:
        st.info(ready['reason'])
    signature = (strategy, provider, year, scenario, instrument)
    if st.session_state.get('tm_form_signature') != signature:
        st.session_state.active_result = None
        st.session_state.selected_run = None
        st.session_state.pop('notice', None)
        st.session_state.tm_form_signature = signature
    config = frozen_config(Config(strategy=strategy, instrument='NIFTY', capital=100_000,
                                  start=f'{year}-01-01', end=f'{year+1}-01-01'),
                           trademind_provider=provider, trademind_scenario=scenario)
    st.session_state.form_defaults = form_values(asdict(config), LABELS[strategy])
    with st.expander('Frozen rules and execution assumptions', expanded=True):
        summary = {
            STRATEGIES[0]: '5m TREND_DOWN, RSI14<35, close<EMA35, high within0.20%; 10m cooldown. 5% target/stop; decision+15m deadline.',
            STRATEGIES[1]: '3m compression≤2ATR, bearish body≥1ATR, close≤priorLow−0.1ATR, completed5m TREND_DOWN; 5m cooldown. 5% target/stop; decision+15m deadline.',
            STRATEGIES[2]: '2m PDH interaction then bullish reclaim≥PDH+0.1ATR, body≥0.25ATR, RSI14>50; 5m cooldown consumes episode. 6% target/5% stop; decision+15m deadline.',
            STRATEGIES[3]: '15≤09:29 VIX<20, non-expiry. Short ATM CE+PE; long wings=ceil50(09:29 straddle). 09:31 opens→15:01 opens. No target, stop or trailing.',
            STRATEGIES[4]: 'A/11:01/P5/H30: exact ATM pair; each premium>100; all six10:55–11:00 closes; combined11:00>10:55. Combined+5%/−25% close trigger→next open, timeout11:31.',
            STRATEGIES[5]: '09:15–09:29 HIGH/LOW range; first completed close outside it during09:30–10:59. Buy only signalled exactATM option; reference close and entry open>100. +5%/−25% close trigger→next open; 30m timeout.'}
        st.write(summary[strategy])
        st.caption('Paper/Live disabled. No modeled options, basket projection, neighboring-contract rescue or post-entry unresolved-trade deletion. Independent market matching NOT VERIFIED.')
    submitted = st.button('▶ Run Backtest', type='primary', width='stretch', key='run_backtest', disabled=not compatible or not ready['runnable'])
    name = st.text_input('Strategy Name', key='tm_strategy_name')
    saved = st.button('📌 Save backtest configuration', key='tm_save', disabled=not compatible or not ready['runnable'])
    if submitted:
        st.session_state.active_result = None
        st.session_state.selected_run = None
        st.session_state.pop('notice', None)
        try:
            with st.spinner('Reading fingerprint-bound retained inputs; simulating observed paths…'):
                result = run_backtest(config, output=output, persist=False)
            st.session_state.active_result = result
            st.rerun()
        except (ValueError, OSError, RuntimeError) as exc:
            st.error(str(exc))
    if saved:
        try:
            record = save_strategy(output, config, name)
            st.info('Backtest configuration saved: ' + record['name'] + '. Paper/Live remains disabled.')
        except (ValueError, OSError) as exc:
            st.error(str(exc))


def render_result(result, output):
    import streamlit as st
    from quant_terminal import save_result
    config = result['config']
    st.subheader(LABELS[config['strategy']])
    if result.get('status') != 'BACKTEST_COMPLETED':
        st.warning('UNRESOLVED simulation: current full-period P&L/equity is unavailable. The tables retain affected entries and explicitly labelled computable prefixes.')
    st.caption(f"{config.get('trademind_provider')} · {config.get('trademind_scenario')} · {result['pricing_revision']}")
    st.write(result['metrics'])
    st.caption('Gross and supported-net curves are separate fresh accounts. Source-assumed lots and theoretical T3 loss capacity are not certified broker affordability. Market matching NOT VERIFIED.')
    frames = result.get('_frames')
    if frames is None:
        path = Path(result['path'])
        frames = {name:pd.read_parquet(path / (name + '.parquet')) for name in ('trades', 'equity', 'gross_equity')}
    for name, title in [('trades', 'Observed leg and outcome evidence'), ('gross_equity', 'Gross cash ledger'), ('equity', 'Cost-supported fresh net ledger')]:
        st.write(title)
        frame = frames.get(name, pd.DataFrame())
        st.dataframe(frame, hide_index=True)
        st.download_button('Download ' + title, frame.to_csv(index=False), result['run_id'] + '_' + name + '.csv', key='tm_download_' + name)
    with st.expander('Eligibility, source fingerprints and limitations'):
        st.dataframe(pd.DataFrame(result['eligibility']), hide_index=True)
        st.write(result['limitations'])
        st.json(result['source_fingerprints'])
    if st.button('Save TradeMind evidence result', key='tm_save_result', disabled=result.get('_frames') is None):
        save_result(output, result, 'TradeMind observed research; ' + result['status'])
        st.info('Evidence saved; unresolved states retain their explicit status.')
    st.download_button('Download frozen configuration', json.dumps(config, indent=2), result['run_id'] + '_config.json')
