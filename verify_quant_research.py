"""Verify completed sweep artifacts and exercise all Streamlit tabs offline."""
import json
from pathlib import Path
import pandas as pd
from quant_backtester import OUT, ENGINE_VERSION, refresh_leaderboard


def verify():
    step5=OUT/'step5_verification.json'
    if step5.exists() and json.loads(step5.read_text()).get('engine')==ENGINE_VERSION:
        current=json.loads(step5.read_text())
        summary=dict(completed=len(current['runs']),planned=len(current['runs']),failed=0,run_ids=[r['run_id'] for r in current['runs']])
    else:
        summary=json.loads((OUT/'research_summary.json').read_text())
    assert summary['completed']==summary['planned'] and not summary['failed'], 'Sweep incomplete'
    totals={'runs':0,'closed_trades':0,'unresolved_trades':0,'modeled_trades':0,'modeled_bars':0,'stored_stop_observations':0}
    for run_id in summary['run_ids']:
        folder=OUT/'runs'/run_id
        result=json.loads((folder/'result.json').read_text())
        assert result['engine_version']==ENGINE_VERSION
        trades=pd.read_parquet(folder/'trades.parquet')
        equity=pd.read_parquet(folder/'equity.parquet')
        paths=pd.read_parquet(folder/'stop_paths.parquet')
        monthly=pd.read_parquet(folder/'monthly.parquet')
        pd.read_parquet(folder/'excluded_sessions.parquet')
        closed=trades[trades.status.eq('closed')]
        censored=trades[trades.status.eq('censored')]
        assert len(censored)==0 and len(closed)==len(trades)
        assert result['metrics']['unresolved_trades']==0
        assert abs(closed.net_pnl.sum()-result['metrics']['net_pnl'])<1e-6
        assert abs(monthly.net_pnl.sum()-closed.net_pnl.sum())<1e-6
        assert abs(equity.net_pnl.sum()-closed.net_pnl.sum())<1e-6
        assert censored.net_pnl.isna().all() and censored.exit_price.isna().all()
        assert not trades.trade_id.duplicated().any()
        if not trades.empty:
            assert trades.entry_strike.eq(trades.strike).all()
            assert closed[['entry_price','exit_price','net_pnl','total_cost']].notna().all().all()
            assert (trades.exit_time>=trades.entry_time).all()
            assert (trades.exit_time.dt.date==trades.entry_time.dt.date).all()
            squareoff=23*60+15 if result['config']['instrument'] in ('CRUDEOIL','NATURALGAS','GOLDM') else 15*60+20
            assert (trades.exit_time.dt.hour*60+trades.exit_time.dt.minute<=squareoff).all()
            assert abs((closed.gross_pnl-closed.total_cost-closed.net_pnl)).max()<1e-6 if len(closed) else True
            assert closed.brokerage.eq(2*result['config']['brokerage']).all()
            assert abs(closed[['brokerage','exchange','sebi','ipft','stt_ctt','stamp','gst']].sum(axis=1)-closed.total_cost).max()<1e-6
            assert abs(closed.gross_before_slippage-closed.slippage_cost-closed.gross_pnl).max()<1e-6
            assert (trades.groupby('date').size()<=result['config']['max_trades_day']).all()
            from quant_high_edge import HIGH_STRATEGIES
            if result['config']['strategy'] in (HIGH_STRATEGIES[2],HIGH_STRATEGIES[3]):
                assert trades.groupby('date').size().max()<=1
            entry_min=trades.entry_time.dt.hour*60+trades.entry_time.dt.minute
            if result['config']['instrument'] in ('NIFTY','SENSEX'):
                if result['config']['strategy']!=HIGH_STRATEGIES[4]: assert not entry_min.between(690,794).any()
                assert not result['verified_price_eligible']
                assert trades.volume_ratio.ge(result['config']['volume_multiple']).all()
                if result['config']['strategy']==HIGH_STRATEGIES[0]:
                    assert trades.vwap_full_session.all()
                    assert (trades.close>trades.option_vwap).all()
            else:
                assert entry_min.between(1050,1290).all()
                assert not trades.used_modeled_prices.any()
                assert result['verified_price_eligible']
            counts=paths[paths.price_source.ne('observed')].groupby('trade_id').size()
            assert trades.modeled_bars.eq(trades.trade_id.map(counts).fillna(0)).all()
            assert int(trades.used_modeled_prices.sum())==result['metrics']['modeled_trades']
            assert (paths.high>=paths[['open','close','low']].max(axis=1)).all()
            assert (paths.low<=paths[['open','close','high']].min(axis=1)).all()
        totals['runs']+=1
        totals['closed_trades']+=len(closed)
        totals['unresolved_trades']+=len(censored)
        totals['modeled_trades']+=result['metrics']['modeled_trades']
        totals['modeled_bars']+=result['metrics']['modeled_bars']
        totals['stored_stop_observations']+=len(paths)
    board=refresh_leaderboard()
    assert set(summary['run_ids']).issubset(set(board.run_id))
    top15=pd.read_csv(OUT/'top15_verified.csv')
    assert len(top15)<=15
    if len(top15):
        assert top15.verified_price_eligible.all() and top15.net_pnl.gt(0).all()
        assert top15.modeled_trades.eq(0).all() and top15.unresolved_trades.eq(0).all()
    print('Artifact verification:',totals)
    from streamlit.testing.v1 import AppTest
    app=AppTest.from_file('quant_ui.py',default_timeout=90).run()
    assert not app.exception, [e.message for e in app.exception]
    assert len(app.tabs)==4 and len(app.get('plotly_chart'))==4
    for name in ('NIFTY','SENSEX','CRUDEOIL','NATURALGAS','GOLDM'):
        next(w for w in app.sidebar.selectbox if w.label=='Instrument').select(name).run()
        assert not app.exception, [e.message for e in app.exception]
    next(w for w in app.sidebar.selectbox if w.label=='Instrument').select('All').run()
    next(w for w in app.sidebar.selectbox if w.label=='Price evidence').select('Observed contract prices + accounting checked').run()
    assert not app.exception, [e.message for e in app.exception]
    print('Streamlit: all 4 tabs, 5 instrument filters, charts and replay passed')
    (OUT/'verification.json').write_text(json.dumps(dict(**totals,ui_tabs=4,instrument_filters=5,status='passed'),indent=2))


if __name__=='__main__': verify()
