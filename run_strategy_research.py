"""Deterministic resumable research grid. All end dates are exclusive."""
import argparse
from dataclasses import replace, asdict
from itertools import product
import json
from pathlib import Path
import time

from quant_backtester import Config, ResearchData, STRATEGIES, HIGH_STRATEGIES, INSTRUMENTS, POINT_VALUE, OUT, run_backtest, refresh_leaderboard, atomic_json, LIMITATIONS, ROOT
from quant_calendar import export_calendar


def grid(start, end, instruments):
    """252 causal H1-H4 configurations; declared before seeing the results."""
    for instrument in instruments:
        base=Config(instrument=instrument,start=start,end=end)
        if instrument in POINT_VALUE:
            for tf,sl,compression in product(('3m','5m','15m'),(1.5,2.0),(0.8,1.0)):
                yield replace(base,strategy=HIGH_STRATEGIES[3],timeframe=tf,atr_sl=sl,reward_risk=2.5,trail_pct=20,
                              compression_ratio=compression,entry_start='17:30',entry_end='21:30',slippage=1.0,max_trades_day=1)
        else:
            for strategy,tf,strike,(sl,target,trail),oi_mode in product((HIGH_STRATEGIES[0],HIGH_STRATEGIES[2]),
                    ('3m','5m','15m'),('ATM','ATM-1','ATM+1'),((20,50,15),(25,75,20)),('rising','either')):
                yield replace(base,strategy=strategy,timeframe=tf,strike=strike,sl_pct=sl,target_pct=target,trail_pct=trail,
                              oi_mode=oi_mode,max_trades_day=1 if strategy==HIGH_STRATEGIES[2] else 2,
                              entry_end='11:15' if strategy==HIGH_STRATEGIES[2] else '14:30')
            for tf,anchor,threshold,(sl,target,trail) in product(('3m','5m','15m'),('09:25','09:30','13:30'),(18,25),((20,50,15),(25,75,20))):
                yield replace(base,strategy=HIGH_STRATEGIES[1],timeframe=tf,anchor=anchor,
                              breakout_pct=threshold,sl_pct=sl,target_pct=target,trail_pct=trail)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',default='2025-01-01')
    parser.add_argument('--end',default='2026-10-23',help='Exclusive end date')
    parser.add_argument('--instruments',nargs='+',choices=INSTRUMENTS,default=INSTRUMENTS)
    parser.add_argument('--limit',type=int,help='Optional smoke-test configuration cap')
    parser.add_argument('--include-growth',action='store_true',help='Add H5/H6 allocation-sized configurations to the H1-H4 grid')
    parser.add_argument('--growth-only',action='store_true',help='Run only H5/H6 allocation-sized configurations')
    parser.add_argument('--output',type=Path,default=OUT)
    parser.add_argument('--report-only',action='store_true',help='Reprint and export the completed sweep without loading candles')
    args=parser.parse_args()
    if args.report_only:
        saved=json.loads((args.output/'research_summary.json').read_text())
        board=refresh_leaderboard(args.output)
        run_ids=saved['run_ids']
        if not board.empty and not board.run_id.isin(run_ids).any():
            print('Saved sweep belongs to an older engine; reporting available current-engine runs.')
            run_ids=board.run_id.tolist()
        report(board,run_ids,args.output)
        return 0
    export_calendar(ROOT)
    configs=list(grid(args.start,args.end,args.instruments))
    if args.growth_only: configs=[]
    if args.include_growth or args.growth_only:
        for instrument in args.instruments:
            if instrument=='GOLDM': continue
            mcx=instrument in POINT_VALUE
            for tf in ('3m','5m','15m'):
                for target in (100,200) if not mcx else (100,):
                    configs.append(Config(instrument=instrument,strategy=HIGH_STRATEGIES[5 if mcx else 4],timeframe=tf,
                        start=args.start,end=args.end,capital=1000000 if mcx else 100000,capital_usage_pct=75,
                        sizing_mode='allocation',compound=True,strategy_mode='growth',daily_profit_pct=10,daily_loss_pct=3,
                        target_pct=target,entry_start='17:30' if mcx else '13:00',entry_end='21:30' if mcx else '14:30',
                        atr_expansion=1.5 if mcx else 1))
        configs.sort(key=lambda c:args.instruments.index(c.instrument))
    if args.limit: configs=configs[:args.limit]
    args.output.mkdir(parents=True,exist_ok=True)
    print(f'Research grid: {len(configs)} configurations | {args.start} <= date < {args.end}',flush=True)
    for note in LIMITATIONS: print('NOTE:',note,flush=True)
    plan=dict(configurations=len(configs),start=args.start,end_exclusive=args.end,instruments=args.instruments,
              config_grid=[asdict(c) for c in configs],limitations=LIMITATIONS)
    atomic_json(args.output/'research_plan.json',plan)
    data=None; successes=[]; failures=[]; audits=[]; began=time.perf_counter()
    for index,c in enumerate(configs,1):
        try:
            if data is None or data.instrument!=c.instrument:
                data=None  # free the previous option snapshot before allocating next
                print(f'Loading {c.instrument} and applying quality exclusions ...',flush=True)
                data=ResearchData(c.instrument,c.start,c.end)
                print(f'  {len(data.days)} eligible sessions; {len(data.exclusions)} excluded; {min(data.days)} .. {max(data.days)}',flush=True)
                if not data.mcx:
                    from audit_option_research import audit
                    audits.append(audit(data,args.output))
                    atomic_json(args.output/'option_data_audit.json',audits)
                    print('  Diagnostic model MAE:',round(audits[-1]['model_mae'],3),'price points; not verified prices',flush=True)
            r=run_backtest(c,data,args.output)
            successes.append(r['run_id']); m=r['metrics']
            assert m['unresolved_trades']==0
            print(f'[{index:03}/{len(configs)}] {c.instrument:10} H{HIGH_STRATEGIES.index(c.strategy)+1} {c.timeframe:3} {c.strike:5} '
                  f'net={m["net_pnl"]:12,.2f} trades={m["total_trades"]:4} modeled={m["modeled_trades"]:4} unresolved={m["unresolved_trades"]} id={r["run_id"]}',flush=True)
            if index%25==0: refresh_leaderboard(args.output)
        except Exception as exc:
            failures.append(dict(config=asdict(c),error=f'{type(exc).__name__}: {exc}'))
            print(f'FAILED {index}: {type(exc).__name__}: {exc}',flush=True)
            if data is None: break
    table=refresh_leaderboard(args.output)
    summary=dict(planned=len(configs),completed=len(successes),failed=len(failures),run_ids=successes,failures=failures,
                 seconds=round(time.perf_counter()-began,2),limitations=LIMITATIONS)
    atomic_json(args.output/'research_summary.json',summary)
    report(table,successes,args.output)
    print(f'Completed {len(successes)}/{len(configs)}; failures={len(failures)}; elapsed={summary["seconds"]}s',flush=True)
    return 1 if failures or len(successes)!=len(configs) else 0


def report(table,run_ids,output):
    if table.empty: return
    current=table[table.run_id.isin(run_ids) & table.total_trades.gt(0)]
    cols=['run_id','instrument','strategy','timeframe','strike','oi_mode','sl_pct','target_pct','atr_sl','compression_ratio','net_pnl','win_rate','profit_factor','max_drawdown','sharpe','total_trades','modeled_trades','unresolved_trades','train_2025_net','validation_2026_net']
    verified=current[current.verified_price_eligible & current.net_pnl.gt(0)]
    print('\nTOP 15 VERIFIED-PRICE PROFITABLE CONFIGURATIONS',flush=True)
    print('Verification scope: observed fixed-contract candles + reconciled simulated accounting; not proof of future profitability or broker fills.',flush=True)
    print(f'Qualifying configurations: {len(verified)}. No synthetic or expiry-unidentified option run is eligible.',flush=True)
    print(verified.head(15)[cols].to_string(index=False,float_format=lambda x:f'{x:,.2f}') if len(verified) else 'NONE - no verified-price profitable configuration found.',flush=True)
    verified.head(15).to_csv(Path(output)/'top15_verified.csv',index=False)
    verified.head(10).to_csv(Path(output)/'top10.csv',index=False)
    print('\nTOP 15 ALL-TRADE RESEARCH CONFIGURATIONS (MODELED/UNVERIFIED OPTIONS EXPLICITLY INCLUDED)',flush=True)
    print(current.head(15)[cols].to_string(index=False,float_format=lambda x:f'{x:,.2f}'),flush=True)
    current.head(15).to_csv(Path(output)/'top15_research.csv',index=False)
    print('\nBEST PER INSTRUMENT',flush=True)
    print(current.groupby('instrument',sort=False).head(1)[cols].to_string(index=False,float_format=lambda x:f'{x:,.2f}'),flush=True)


if __name__=='__main__':
    raise SystemExit(main())
