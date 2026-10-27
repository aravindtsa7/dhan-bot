"""Targeted risk-profile sweeps. Local candles only; end date is exclusive."""
from __future__ import annotations
import argparse
from dataclasses import asdict, replace
from itertools import product
import hashlib
import io
from contextlib import redirect_stdout
import json
from pathlib import Path
import time
import numpy as np
import pandas as pd

from quant_backtester import (Config, ResearchData, HIGH_STRATEGIES, OUT, ENGINE_VERSION,
                              run_backtest, atomic_json, adverse_tick, fees)

TUNING_VERSION='1.0'
DEFINITIONS={
    'daily_average':'Net after all modeled trading costs / all quality-eligible underlying sessions; no-trade days included.',
    'active_daily_average':'Net / sessions with at least one executed trade; shown separately.',
    'hit_rates':'Final realized session net >=1000, >=2000, and inclusive [1000,2000], each divided by all eligible sessions.',
    'max_single_day_drawdown':'Largest within-session peak-to-trough fall in net liquidation equity sampled at minute closes and actual exits; session peak starts at zero. Intrabar excursions are not measured.',
    'worst_day':'Minimum final realized daily P&L; different from intraday drawdown.',
    'explosive_options':'Fraction of closed option trades capturing at least 20/50/100% entry-to-exit premium gain after fill slippage, with positive net P&L. Not maximum favorable excursion.',
    'explosive_futures':'Fraction of closed futures trades earning at least 20/50/100% net return on assumed margin committed. This is not an underlying futures price return.',
    'daily_locks':'Net realized + open liquidation P&L at each minute close; latch until session end and exit at next open. Gaps and fees can overshoot cutoffs; profits can fade before execution.',
    'selection':'Select on dates before the declared split. Later-period results are descriptive checks, not untouched out-of-sample evidence: this historical lake was inspected in earlier steps.',
    'risk':'Allocation caps committed capital, not worst-case loss. Futures margin percentage is an explicit scenario assumption, not a verified broker margin schedule.',
    'prices':'Modeled option prices are tagged. Rolling option expiry identity remains unverified even when all prices are observed.',
}


def targeted_grid(start,end,option_capital=100000,futures_capital=500000,margin_pct=20,include_high_beta=True):
    """224 predeclared configs: 64 steady H4, 96 H5, optional 64 H6."""
    common=dict(start=start,end=end,sizing_mode='allocation',max_lots=10,compound=False,max_trades_day=1,
                daily_profit_pct=0,daily_loss_pct=0)
    base=Config(instrument='CRUDEOIL',strategy=HIGH_STRATEGIES[3],timeframe='5m',capital=futures_capital,
        futures_margin_pct=margin_pct,entry_start='17:30',entry_end='21:30',slippage=1,compression_ratio=1,**common)
    for sl,rr,jump,loss,usage,goal in product((1.5,2.),(2.,3.),(.25,.5),(1000,1500),(25,50),(1000,2000)):
        yield 'steady',replace(base,atr_sl=sl,reward_risk=rr,atr_trail_jump=jump,daily_loss_limit=loss,
                              capital_usage_pct=usage,daily_profit_target=goal,trail_pct=20)
    for instrument,tf in (('NIFTY','3m'),('SENSEX','5m')):
        base=Config(instrument=instrument,strategy=HIGH_STRATEGIES[4],timeframe=tf,capital=option_capital,
            strategy_mode='growth',entry_start='13:00',entry_end='14:30',trail_pct=20,daily_profit_target=0,**common)
        for sl,target,jump,loss,usage in product((20,25),(50,100,150),(5,10),(1500,2500),(50,75)):
            yield 'explosive',replace(base,sl_pct=sl,target_pct=target,trail_jump_pct=jump,
                                     daily_loss_limit=loss,capital_usage_pct=usage)
    if include_high_beta:
        for instrument in ('CRUDEOIL','NATURALGAS'):
            base=Config(instrument=instrument,strategy=HIGH_STRATEGIES[5],timeframe='3m',capital=futures_capital,
                strategy_mode='growth',futures_margin_pct=margin_pct,entry_start='17:30',entry_end='21:30',
                slippage=.5 if instrument=='NATURALGAS' else 1,atr_expansion=1.5,trail_pct=20,**common)
            for sl,rr,jump,loss,usage in product((1.5,2.),(2.,3.),(.25,.5),(1500,2500),(50,75)):
                yield 'explosive',replace(base,atr_sl=sl,reward_risk=rr,atr_trail_jump=jump,
                                         daily_loss_limit=loss,capital_usage_pct=usage)


def summarize(trades,daily,mcx=False):
    count=len(daily); n=len(trades)
    active=daily[daily.trades.gt(0)]
    pnl=daily.net_pnl
    pct=lambda numerator,denominator:100*int(numerator)/denominator if denominator else None
    result=dict(eligible_days=count,trading_days=len(active),trades=n,net_pnl=float(pnl.sum()),
        avg_daily_pnl=float(pnl.mean()) if count else None,
        avg_active_daily_pnl=float(active.net_pnl.mean()) if len(active) else None,
        hit_1000_pct=pct(pnl.ge(1000-1e-8).sum(),count),hit_2000_pct=pct(pnl.ge(2000-1e-8).sum(),count),
        hit_band_1000_2000_pct=pct(pnl.between(1000-1e-8,2000+1e-8).sum(),count),
        max_single_day_drawdown=float(daily.max_drawdown_1m_close.max()) if count else None,
        worst_day_pnl=float(pnl.min()) if count else None,
        profit_lock_days=int(daily.lock_reason.eq('daily_profit_target').sum()),
        loss_lock_days=int(daily.lock_reason.eq('daily_loss_limit').sum()),
        blocked_entries=int(daily.blocked_entries.sum()),
        max_loss_cutoff_excess=float(daily.loss_limit_excess.max()) if count else None,
        modeled_trades=int(trades.used_modeled_prices.sum()) if n else 0,
        explosive_basis='net ROI on assumed futures margin' if mcx else 'captured option premium move',
        insufficient_active_sample=len(active)<10)
    move=trades.roi_pct if n and mcx else (100*(trades.exit_price/trades.entry_price-1) if n else pd.Series(dtype=float))
    for threshold in (20,50,100):
        hits=int(((move>=threshold-1e-9)&trades.net_pnl.gt(0)).sum()) if n else 0
        result[f'explosive_{threshold}_trades']=hits
        result[f'explosive_{threshold}_pct']=pct(hits,n)
    return result


def audit_run(result):
    folder=Path(result['path'])
    trades=pd.read_parquet(folder/'trades.parquet')
    daily=pd.read_parquet(folder/'daily_risk.parquet')
    assert not daily.date.duplicated().any()
    assert len(daily)==result['eligible_sessions']
    assert result['metrics']['unresolved_trades']==0
    assert trades.status.eq('closed').all()
    assert np.isclose(daily.net_pnl.sum(),trades.net_pnl.sum(),atol=1e-6,rtol=0)
    assert daily.trades.sum()==len(trades)
    for row in daily[daily.lock_reason.ne('')].itertuples():
        dt=trades[trades.date.eq(row.date)]
        entries=dt.entry_time.dt.hour*60+dt.entry_time.dt.minute
        exits=dt.exit_time.dt.hour*60+dt.exit_time.dt.minute
        assert (entries<=row.lock_minute).all(),'Entry after daily lock'
        assert (exits<=row.lock_minute+1).all(),'Open position survived lock exit minute'
        if row.lock_reason=='daily_profit_target': assert row.lock_trigger_pnl>=row.profit_limit-1e-7
        else: assert row.lock_trigger_pnl<=-row.loss_limit+1e-7
    return trades,daily


def independent_drawdown_check(result):
    """Rebuild selected-run liquidation paths from raw stop paths and trade fills."""
    folder=Path(result['path']); c=Config(**result['config'])
    trades=pd.read_parquet(folder/'trades.parquet'); paths=pd.read_parquet(folder/'stop_paths.parquet')
    daily=pd.read_parquet(folder/'daily_risk.parquet').set_index('date')
    for date,group in trades.groupby('date'):
        streams=[]
        for trade in group.itertuples():
            trace=paths[paths.trade_id.eq(trade.trade_id)]
            values={}
            direction=-1 if trade.side=='SHORT' else 1
            for point in trace.itertuples():
                if point.timestamp>=trade.exit_time: continue
                mark=adverse_tick(point.close-direction*c.slippage,c,direction==-1)
                values[point.timestamp]=direction*(mark-trade.entry_price)*trade.quantity-fees(c,trade.entry_price,mark,trade.quantity,date,direction)['total_cost']
            values[trade.exit_time]=trade.net_pnl
            streams.append(pd.Series(values))
        frame=pd.concat(streams,axis=1).sort_index().ffill().fillna(0)
        marks=np.r_[0,frame.sum(axis=1).to_numpy()]
        expected=float((np.maximum.accumulate(marks)-marks).max())
        assert np.isclose(expected,daily.loc[date,'max_drawdown_1m_close'],atol=1e-6,rtol=0)


def summary_row(profile,result,split):
    trades,daily=audit_run(result)
    c=result['config']; mcx=c['instrument'] in ('CRUDEOIL','NATURALGAS','GOLDM')
    row=dict(profile=profile,run_id=result['run_id'],**c,**summarize(trades,daily,mcx),
        unresolved_trades=result['metrics']['unresolved_trades'],skipped_entries=result['metrics']['skipped_entries'],
        excluded_sessions=result['excluded_sessions'],first_session=result['first_session'],last_session=result['last_session'],
        data_fingerprint=result['data_fingerprint'],price_status=result['status'])
    for name,mask in [('development',daily.date<split),('later',daily.date>=split)]:
        metrics=summarize(trades[trades.date.isin(daily.loc[mask,'date'])],daily[mask],mcx)
        row.update({f'{name}_{k}':v for k,v in metrics.items()})
    return row


def rank_candidates(table):
    if table.empty: return table
    pieces=[]
    for (profile,instrument),group in table.groupby(['profile','instrument'],sort=True):
        group=group.copy()
        group['development_sample_ok']=group.development_trading_days.ge(10)
        group['development_profitable']=group.development_net_pnl.gt(0)
        keys=['development_sample_ok','development_profitable']
        keys+=['development_hit_1000_pct','development_avg_daily_pnl','development_max_single_day_drawdown'] if profile=='steady' else ['development_avg_daily_pnl','development_explosive_20_pct','development_max_single_day_drawdown']
        group=group.sort_values(keys+['run_id'],ascending=[False]*(len(keys)-1)+[True,True],na_position='last')
        group['profile_rank']=range(1,len(group)+1)
        pieces.append(group)
    return pd.concat(pieces,ignore_index=True) if pieces else table


def print_report(table,top=2):
    chosen=table[table.profile_rank.le(top)].copy()
    chosen['ID']=chosen.run_id.str[:8]
    chosen['Active/Days']=chosen.trading_days.astype(str)+'/'+chosen.eligible_days.astype(str)
    chosen['Expl20/50/100%']=chosen.apply(lambda r:'/'.join('N/A' if pd.isna(r[f'explosive_{v}_pct']) else f'{r[f"explosive_{v}_pct"]:.1f}' for v in (20,50,100)),axis=1)
    chosen['Risk']=chosen.apply(lambda r:f'ATR{r.atr_sl:g}/RR{r.reward_risk:g}/J{r.atr_trail_jump:g}' if r.instrument in ('CRUDEOIL','NATURALGAS') else f'SL{r.sl_pct:g}/T{r.target_pct:g}/J{r.trail_jump_pct:g}',axis=1)
    cols={'ID':'ID','instrument':'Market','timeframe':'TF','capital_usage_pct':'Alloc%','Risk':'SL/Target/Jump',
        'daily_loss_limit':'Loss cap','daily_profit_target':'Profit cap','Active/Days':'Active/Days',
        'avg_daily_pnl':'Avg/day','hit_1000_pct':'>=1k%','hit_2000_pct':'>=2k%',
        'hit_band_1000_2000_pct':'1-2k%','max_single_day_drawdown':'Max day DD','worst_day_pnl':'Worst day','Expl20/50/100%':'Expl20/50/100%'}
    for profile,group in chosen.groupby('profile',sort=False):
        print(f'\n{profile.upper()} | development-ranked candidates, FULL-WINDOW descriptive results',flush=True)
        print(group[list(cols)].rename(columns=cols).to_string(index=False,float_format=lambda x:f'{x:,.2f}'),flush=True)
    print('\nLATER-PERIOD CHECK OF THE SAME CANDIDATES (previously inspected history, not untouched holdout)',flush=True)
    cols={'ID':'ID','instrument':'Market','development_trading_days':'Dev active','development_avg_daily_pnl':'Dev avg/day',
        'later_trading_days':'Later active','later_avg_daily_pnl':'Later avg/day','later_hit_1000_pct':'Later >=1k%',
        'later_max_single_day_drawdown':'Later max day DD','insufficient_active_sample':'Full sample <10','modeled_trades':'Modeled'}
    print(chosen[list(cols)].rename(columns=cols).to_string(index=False,float_format=lambda x:f'{x:,.2f}'),flush=True)
    print('\nDenominator: all quality-eligible underlying sessions, including zero-trade days. Excluded sessions are not treated as valid zero returns.',flush=True)
    print('Max day DD: within-session peak-to-trough NET liquidation P&L at 1-minute closes/exits; not worst final daily loss or intrabar worst case.',flush=True)
    print('Currency columns are INR. Expl20/50/100%: frequencies capturing those option premium gains with positive net profit; futures instead use net ROI on assumed margin. No-trade rates are N/A.',flush=True)
    print('Profit cap 0 disables the daily upper lock for explosive profiles; stop/target and mandatory square-off remain active. Lock fills occur next open and can overshoot.',flush=True)
    print('Low samples and earlier full-window strategy selection limit any claim of consistency. All figures are simulated after configured costs.',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',default='2025-01-01')
    parser.add_argument('--end',default='2026-10-23')
    parser.add_argument('--split',default='2026-09-01')
    parser.add_argument('--option-capital',type=float,default=100000)
    parser.add_argument('--futures-capital',type=float,default=500000)
    parser.add_argument('--futures-margin-pct',type=float,default=20,help='Explicit assumed margin; 100 means fully funded notional')
    parser.add_argument('--core-only',action='store_true',help='Omit exploratory H6 sweeps')
    parser.add_argument('--instruments',nargs='+',choices=['CRUDEOIL','NIFTY','SENSEX','NATURALGAS'])
    parser.add_argument('--limit',type=int)
    parser.add_argument('--output',type=Path,default=OUT/'fine_tuning')
    parser.add_argument('--report-only',action='store_true')
    parser.add_argument('--rebuild-report',action='store_true',help='Reaudit existing fills and rebuild metrics without reloading market history')
    args=parser.parse_args()
    if args.report_only or args.rebuild_report:
        plan=json.loads((args.output/'plan.json').read_text())
        completion=json.loads((args.output/'completion.json').read_text())
        if completion['plan_hash']!=plan['plan_hash']:
            parser.error('Saved report belongs to a different/incomplete plan; finish the current run or choose its output directory')
        board=pd.read_csv(args.output/'summary.csv')
        if args.rebuild_report:
            board=rank_candidates(pd.DataFrame([summary_row(row.profile,json.loads((args.output/'runs'/row.run_id/'result.json').read_text()),plan['split']) for row in board.itertuples()]))
            board.to_csv(args.output/'summary.csv',index=False)
            selected=board[board.profile_rank.eq(1)]
            selected.to_csv(args.output/'selected_profiles.csv',index=False)
            for row in selected.itertuples(): independent_drawdown_check(json.loads((args.output/'runs'/row.run_id/'result.json').read_text()))
        buffer=io.StringIO()
        with redirect_stdout(buffer): print_report(board)
        (args.output/'summary.txt').write_text(buffer.getvalue(),encoding='utf-8')
        print(buffer.getvalue()); return 0
    if not pd.Timestamp(args.start)<pd.Timestamp(args.split)<pd.Timestamp(args.end):
        parser.error('split must be strictly inside the requested interval')
    if args.limit is not None and args.limit<1: parser.error('limit must be positive')
    configs=list(targeted_grid(args.start,args.end,args.option_capital,args.futures_capital,args.futures_margin_pct,not args.core_only))
    if args.instruments: configs=[(p,c) for p,c in configs if c.instrument in args.instruments]
    configs.sort(key=lambda pc: (pc[1].instrument,pc[0]))
    if args.limit: configs=configs[:args.limit]
    if not configs: parser.error('No configurations selected')
    for _,c in configs: c.validate()
    args.output.mkdir(parents=True,exist_ok=True)
    plan=dict(engine=ENGINE_VERSION,tuning_version=TUNING_VERSION,start=args.start,end=args.end,split=args.split,
              definitions=DEFINITIONS,configs=[dict(profile=p,**asdict(c)) for p,c in configs])
    plan['plan_hash']=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()[:16]
    atomic_json(args.output/'plan.json',plan)
    print(f'Fine tuning: {len(configs)} configurations | engine {ENGINE_VERSION} | development < {args.split}',flush=True)
    print(f'Options capital INR {args.option_capital:,.0f}; futures INR {args.futures_capital:,.0f}; futures margin ASSUMPTION {args.futures_margin_pct:g}%.',flush=True)
    print('Steady allocation 25/50%; daily profit locks INR 1000/2000, loss cutoffs INR 1000/1500. Explosive allocation 50/75%, loss cutoffs INR 1500/2500.',flush=True)
    rows=[]; failures=[]; data=None; began=time.perf_counter()
    for number,(profile,c) in enumerate(configs,1):
        try:
            if data is None or data.instrument!=c.instrument:
                data=None
                print(f'Loading {c.instrument}...',flush=True)
                data=ResearchData(c.instrument,c.start,c.end)
                print(f'  {len(data.days)} eligible / {len(data.exclusions)} excluded sessions; {min(data.days)} to {max(data.days)}',flush=True)
            result=run_backtest(c,data,output=args.output)
            row=summary_row(profile,result,args.split)
            rows.append(row)
            if number%16==0 or number==len(configs):
                print(f'[{number:03}/{len(configs)}] {c.instrument} {profile}: net {row["net_pnl"]:,.2f}, avg/day {row["avg_daily_pnl"]:,.2f}, >=1k {row["hit_1000_pct"]:.1f}%, trades {row["trades"]}, unresolved 0',flush=True)
        except Exception as exc:
            failures.append(dict(profile=profile,config=asdict(c),error=f'{type(exc).__name__}: {exc}'))
            print(f'FAILED {number}: {type(exc).__name__}: {exc}',flush=True)
    board=rank_candidates(pd.DataFrame(rows))
    if not board.empty:
        board.to_csv(args.output/'summary.csv',index=False)
        selected=board[board.profile_rank.eq(1)]
        selected.to_csv(args.output/'selected_profiles.csv',index=False)
        for row in selected.itertuples():
            result=json.loads((args.output/'runs'/row.run_id/'result.json').read_text())
            independent_drawdown_check(result)
        buffer=io.StringIO()
        with redirect_stdout(buffer): print_report(board)
        (args.output/'summary.txt').write_text(buffer.getvalue(),encoding='utf-8')
        print(buffer.getvalue(),flush=True)
    atomic_json(args.output/'completion.json',dict(plan_hash=plan['plan_hash'],engine=ENGINE_VERSION,
        planned=len(configs),completed=len(rows),failures=failures,unresolved_trades=sum(r['unresolved_trades'] for r in rows),
        seconds=round(time.perf_counter()-began,2),selected_drawdowns_independently_rebuilt=not board.empty))
    print(f'\nCompleted {len(rows)}/{len(configs)}; failed {len(failures)}; unresolved {sum(r["unresolved_trades"] for r in rows)}. Saved to {args.output.resolve()}',flush=True)
    return int(bool(failures))


if __name__=='__main__': raise SystemExit(main())
