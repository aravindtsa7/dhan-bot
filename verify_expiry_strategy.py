"""Offline Expiry Day BTST matrix; research outputs remain in ignored data/."""
import argparse
from dataclasses import asdict, replace
import gc
import json
from pathlib import Path
import socket

import pandas as pd

from quant_backtester import Config, ResearchData, run_backtest, atomic_json
from quant_expiry import expiry_pairs


def verify(start='2025-01-01',end='2026-10-31',output=Path('data/expiry_verification')):
    output=Path(output);output.mkdir(parents=True,exist_ok=True);rows=[]
    for instrument in ('NIFTY','SENSEX'):
        base=Config(instrument=instrument,strategy='expiry_day',start=start,end=end,timeframe='5m',
            capital=1_000_000,lots=1,capital_usage_pct=100,target_pct=25,sl_pct=25,trail_pct=0,
            minimum_premium=0,exit_mode='target_eod',price_policy='model_continuity')
        data=ResearchData(instrument,start,end,expiry_config=base)
        atomic_json(output/f'{instrument}_eligibility.json',data.eligibility)
        print(f'{instrument}: {len(expiry_pairs(data,base))} scheduled expiry pairs; {len(data.days)} eligible dependency sessions / {len(data.exclusions)} excluded',flush=True)
        for tp,sl in ((25,25),(50,30)):
            c=replace(base,target_pct=tp,sl_pct=sl)
            result=run_backtest(c,data,persist=False)
            frames=result['_frames'];trades=frames['trades']
            assert result['metrics']['unresolved_trades']==0
            if len(trades):
                assert trades.overnight.all()
                assert trades.entry_time.dt.strftime('%H:%M').eq('15:26').all()
                assert trades.exit_time.dt.strftime('%Y-%m-%d').eq(trades.expiry_exit_session).all()
                assert trades.side.eq('EXPIRY_DAY').all()
                assert (abs(trades.net_pnl-(trades.gross_before_slippage-trades.slippage_cost-trades.total_cost))<1e-6).all()
                for trade in trades.itertuples():
                    legs=json.loads(trade.legs)
                    assert len(legs)==2 and {l['side'] for l in legs}=={'CE','PE'}
                    assert abs(sum(l['total_cost'] for l in legs)-trade.total_cost)<1e-6
                    assert abs(sum((l['exit_price']-l['entry_price'])*l['quantity'] for l in legs)-trade.gross_pnl)<1e-6
            row=dict(instrument=instrument,timeframe=c.timeframe,target_pct=tp,sl_pct=sl,
                scheduled_pairs=len(expiry_pairs(data,c)),eligible_sessions=len(data.days),excluded_sessions=len(data.exclusions),
                first_session=result['first_session'],last_session=result['last_session'],status=result['status'],**result['metrics'])
            rows.append(row)
            stem=f'{instrument}_TP{tp}_SL{sl}'
            atomic_json(output/f'{stem}.json',{k:v for k,v in result.items() if k!='_frames'})
            trades.to_parquet(output/f'{stem}_trades.parquet',index=False)
            frames['skipped_entries'].to_csv(output/f'{stem}_skipped.csv',index=False)
            pd.DataFrame(rows).to_csv(output/'summary.csv',index=False)
            print(f'{stem}: net={row["net_pnl"]:.2f}, win={row["win_rate"]:.2f}%, trades={row["total_trades"]}, PF={row["profit_factor"]}, wing={row["modeled_wing_fallback_trades"]}, skipped={row["skipped_entries"]}',flush=True)
            del result,frames,trades
        del data;gc.collect()
    return pd.DataFrame(rows)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',default='2025-01-01');parser.add_argument('--end',default='2026-10-31')
    parser.add_argument('--output',type=Path,default=Path('data/expiry_verification'))
    args=parser.parse_args()
    def blocked(*args,**kwargs): raise AssertionError('External network forbidden during Expiry Day verification')
    socket.socket.connect=blocked;socket.socket.connect_ex=blocked
    verify(args.start,args.end,args.output)
