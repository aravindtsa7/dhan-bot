"""Offline book-strategy lake matrix; outputs stay under ignored local data/."""
from dataclasses import replace
import argparse
import json
from pathlib import Path
import socket
import pandas as pd
from quant_backtester import Config, ResearchData, BOOK_STRATEGIES, run_backtest, atomic_json
from quant_option_model import MissingObservedPrice


def verify(start='2025-01-01',end='2026-10-31',output=Path('data/book_verification'),
           instruments=('NIFTY','SENSEX','NATURALGAS')):
    output=Path(output); output.mkdir(parents=True,exist_ok=True); rows=[]
    for instrument in instruments:
        data=ResearchData(instrument,start,end)
        print(f'{instrument}: {len(data.days)} eligible / {len(data.exclusions)} excluded',flush=True)
        atomic_json(output/f'{instrument}_eligibility.json',data.eligibility)
        for tf in ('5m','15m'):
            for strategy in BOOK_STRATEGIES:
                row=dict(instrument=instrument,timeframe=tf,strategy=strategy,
                         eligible_sessions=len(data.days),excluded_sessions=len(data.exclusions),data_fingerprint=data.fingerprint)
                c=Config(instrument=instrument,strategy=strategy,start=start,end=end,timeframe=tf,
                         capital=1_000_000,lots=1,capital_usage_pct=100,minimum_premium=0,
                         target_pct=25,sl_pct=15,trail_pct=0,exit_mode='target_eod',volume_multiple=1.3,
                         entry_start='17:00' if data.mcx else '09:30',entry_end='22:45' if data.mcx else '14:30',
                         price_policy='strict_observed' if data.mcx else 'model_continuity')
                try: c.validate()
                except ValueError as exc:
                    if not (data.mcx and strategy in BOOK_STRATEGIES[1:3]): raise
                    row.update(status='unsupported: requires options',reason=str(exc)); rows.append(row)
                    pd.DataFrame(rows).to_csv(output/'summary.csv',index=False); continue
                try:
                    result=run_backtest(c,data,persist=False)
                except MissingObservedPrice as exc:
                    row.update(status='FAILED: incomplete execution prices',reason=str(exc))
                    rows.append(row)
                    atomic_json(output/f'{instrument}_{tf}_{strategy}.json',row)
                    pd.DataFrame(rows).to_csv(output/'summary.csv',index=False)
                    print(f'{instrument} {tf} {strategy}: FAILED {exc}',flush=True)
                    continue  # No partial P&L or zero-unresolved claim for failed runs.
                f=result['_frames']['trades']
                paths=result['_frames']['stop_paths']
                if len(f):
                    assert (f.quantity>0).all() and (f.total_cost>0).all()
                    assert (abs(f.gross_pnl-f.total_cost-f.net_pnl)<1e-6).all()
                    assert (abs(f.gross_before_slippage-f.slippage_cost-f.gross_pnl)<1e-6).all()
                    assert f.groupby('date').size().max()<=2
                    assert (f.entry_time<=f.exit_time).all()
                    if 'legs' in f:
                        for trade in f.itertuples():
                            legs=json.loads(trade.legs)
                            assert abs(sum(l['total_cost'] for l in legs)-trade.total_cost)<1e-6
                            gross=sum(l['ratio']*(l['exit_price']-l['entry_price'])*trade.quantity for l in legs)
                            assert abs(gross-trade.gross_pnl)<1e-6
                            if trade.side in ('BULL_CALL_SPREAD','BEAR_PUT_SPREAD','IRON_BUTTERFLY'):
                                width=abs(legs[0]['strike']-legs[2 if trade.side=='IRON_BUTTERFLY' else 1]['strike'])
                                low,high=(-width,0) if trade.side=='IRON_BUTTERFLY' else (.05,width-.05)
                                marked=paths[paths.trade_id.eq(trade.trade_id) & paths.spread_bounds_applied]
                                for field in ('open','high','low','close','mark'):
                                    assert marked[field].between(low-1e-8,high+1e-8).all(), (instrument,trade.trade_id,field)
                                if len(marked) and marked.iloc[-1].timestamp==trade.exit_time:
                                    assert low-1e-8<=trade.raw_exit_price<=high+1e-8
                assert result['metrics']['unresolved_trades']==0
                row.update(status=result['status'],first_session=result['first_session'],last_session=result['last_session'],**result['metrics'])
                rows.append(row)
                atomic_json(output/f'{instrument}_{tf}_{strategy}.json',{k:v for k,v in result.items() if k!='_frames'})
                print(f'{instrument} {tf} {strategy}: net={row["net_pnl"]:.2f}, win={row["win_rate"]:.2f}%, trades={row["total_trades"]}, PF={row["profit_factor"]}, wing={row["modeled_wing_fallback_trades"]}',flush=True)
                pd.DataFrame(rows).to_csv(output/'summary.csv',index=False)
    return pd.DataFrame(rows)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',default='2025-01-01');parser.add_argument('--end',default='2026-10-31')
    parser.add_argument('--output',type=Path,default=Path('data/book_verification'))
    parser.add_argument('--instruments',nargs='+',choices=('NIFTY','SENSEX','NATURALGAS'),default=('NIFTY','SENSEX','NATURALGAS'))
    args=parser.parse_args()
    def blocked(*args,**kwargs): raise AssertionError('External network forbidden during lake verification')
    socket.socket.connect=blocked; socket.socket.connect_ex=blocked
    result=verify(args.start,args.end,args.output,args.instruments)
    raise SystemExit(int(result.status.str.startswith('FAILED:').any()))
