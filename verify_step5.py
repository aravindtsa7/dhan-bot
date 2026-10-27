"""Representative full-coverage Step 5 runs and persisted ledger reconciliation."""
from dataclasses import replace
import json
import shutil
import pandas as pd
from quant_backtester import Config, ResearchData, HIGH_STRATEGIES, OUT, ENGINE_VERSION, run_backtest, refresh_leaderboard, atomic_json


def main():
    # Preserve prior research rankings; run artifacts themselves remain immutable.
    for name in ('leaderboard','verified_leaderboard','top10','top15_verified','top15_research'):
        source=OUT/f'{name}.csv'; backup=OUT/f'{name}_step4.csv'
        if source.exists() and not backup.exists(): shutil.copy2(source,backup)
    rows=[]
    for instrument in ('NIFTY','SENSEX','CRUDEOIL','NATURALGAS','GOLDM'):
        print(f'Loading full available history: {instrument}',flush=True)
        data=ResearchData(instrument,'2025-01-01','2026-10-23')
        mcx=data.mcx
        strategy=HIGH_STRATEGIES[3] if instrument=='GOLDM' else HIGH_STRATEGIES[5] if mcx else HIGH_STRATEGIES[4]
        base=Config(instrument=instrument,strategy=strategy,capital=1000000 if mcx else 100000,
            capital_usage_pct=75,sizing_mode='allocation',max_lots=10,compound=True,strategy_mode='growth',
            futures_margin_pct=100,daily_profit_pct=10,daily_loss_pct=3,
            entry_start='17:30' if mcx else '13:00',entry_end='21:30' if mcx else '14:30',
            target_pct=150,trail_pct=20,atr_expansion=1.5 if mcx and instrument!='GOLDM' else 1)
        configs=[replace(base,timeframe=tf) for tf in ('3m','5m')]
        if instrument=='CRUDEOIL':
            configs.append(replace(base,strategy=HIGH_STRATEGIES[3],strategy_mode='steady',timeframe='5m',atr_expansion=1,
                daily_profit_pct=0,daily_profit_target=1500,daily_loss_limit=2000,daily_loss_pct=0,compound=False))
        for c in configs:
            r=run_backtest(c,data)
            path=OUT/'runs'/r['run_id']
            t=pd.read_parquet(path/'trades.parquet')
            e=pd.read_parquet(path/'equity.parquet')
            monthly=pd.read_parquet(path/'monthly.parquet')
            assert r['metrics']['unresolved_trades']==0
            assert abs(t.net_pnl.sum()-r['metrics']['net_pnl'])<1e-6
            assert abs(e.net_pnl.sum()-monthly.net_pnl.sum())<1e-6
            if len(t):
                assert t.status.eq('closed').all()
                assert ((t.exit_time.dt.hour*60+t.exit_time.dt.minute)<=(1395 if mcx else 920)).all()
                assert (t.entry_time.dt.date==t.exit_time.dt.date).all()
                assert (t.capital_reserved<=t.allocation_budget+1e-6).all()
                assert (t.capital_reserved<=t.entry_equity*c.capital_usage_pct/100+1e-6).all()
                assert ((t.gross_pnl-t.total_cost-t.net_pnl).abs()<1e-6).all()
                assert ((100*t.net_pnl/t.capital_allocated-t.roi_pct).abs()<1e-6).all()
                assert ((100*t.capital_allocated/t.entry_equity-t.capital_used_pct).abs()<1e-6).all()
            row=dict(run_id=r['run_id'],instrument=instrument,strategy=c.strategy,timeframe=c.timeframe,
                trades=len(t),net_pnl=r['metrics']['net_pnl'],unresolved=r['metrics']['unresolved_trades'],
                modeled=r['metrics']['modeled_trades'],skipped=r['metrics']['skipped_entries'])
            rows.append(row)
            print(json.dumps(row),flush=True)
        del data
    leaderboard=refresh_leaderboard()
    from run_strategy_research import report
    report(leaderboard,[r['run_id'] for r in rows],OUT)
    atomic_json(OUT/'step5_verification.json',dict(engine=ENGINE_VERSION,runs=rows,
        trades=sum(r['trades'] for r in rows),unresolved=sum(r['unresolved'] for r in rows),
        scope='11 representative configurations over all available history; not a new comprehensive sweep'))
    print('PASS: persisted ledgers, capital allocations, costs, ROI, equity, and square-offs reconciled.',flush=True)


if __name__=='__main__': main()
