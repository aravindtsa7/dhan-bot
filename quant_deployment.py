"""Explicit, virtual-only saved-strategy deployment; no broker order endpoints."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from quant_backtester import Config, STRATEGIES


def account_path(root, record):
    policy = {k:v for k,v in record['config'].items() if k not in ('start','end')}
    if policy.get('strategy')!='expiry_day':
        policy={k:v for k,v in policy.items() if not k.startswith('expiry_')}
    digest = hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest()[:10]
    return Path(root)/(record['strategy_id']+'_'+digest)


def launch_strategy(record, root, strategies_file):
    config = Config(**record['config'])
    config.validate()
    if config.strategy not in STRATEGIES: raise ValueError('Unsupported saved strategy')
    output = account_path(root,record)
    output.mkdir(parents=True,exist_ok=True)
    from paper_engine import AccountLock
    # A running account owns this OS lock, so a second click cannot replace it.
    with AccountLock(output): pass
    if (output/'STOP').exists(): (output/'STOP').unlink()
    command = [sys.executable,str(Path(__file__).with_name('step6_paper_trader.py')),
               '--mode','live','--saved-strategy',record['strategy_id'],
               '--strategies-file',str(Path(strategies_file).resolve()),'--output',str(output.resolve())]
    log = output/'launcher.log'
    with log.open('ab') as stream:
        process = subprocess.Popen(command,cwd=Path(__file__).parent,stdout=stream,stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return process.pid


def render_strategy_deployment(root, strategies_file):
    import streamlit as st
    from quant_terminal import load_saved_strategies
    records = load_saved_strategies(strategies_file)
    if not records:
        st.info('Save a named strategy on Backtests to deploy it here.')
        return Path(root)
    by_id = {r['strategy_id']:r for r in records}
    selected = st.selectbox('Saved strategy',list(by_id),key='paper_saved_strategy',
                            format_func=lambda key:by_id[key]['name'])
    record = by_id[selected]
    c = record['config']
    st.caption(f'{c["instrument"]} · {c["timeframe"]} · Capital ₹{c["capital"]:,.0f} · Allocation {c["capital_usage_pct"]:g}% · {c["exit_mode"]}')
    a,b = st.columns(2)
    if a.button('▶ Deploy to Paper',key='deploy_paper',type='primary',width='stretch'):
        try:
            pid = launch_strategy(record,root,strategies_file)
            st.success(f'{record["name"]}: paper runner launched (PID {pid}). Connection status appears below.')
        except (ValueError,OSError) as exc: st.error(str(exc))
    output = account_path(root,record)
    if b.button('Stop paper strategy',key='stop_paper',width='stretch'):
        output.mkdir(parents=True,exist_ok=True)
        (output/'STOP').touch()
        st.info('Stop requested. Open virtual positions wait for a fresh quote to close.')
    log = output/'paper_trader.log'
    if log.exists():
        with st.expander('Paper runner log'): st.code(log.read_text(encoding='utf-8')[-6000:])
    return output
