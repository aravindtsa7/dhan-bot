"""Offline real-lake strategy/interval and risk sensitivity matrix.

Writes verification evidence only. Never registers results in Reports.
In-sample comparison is descriptive, not evidence of future expectancy.
"""
from dataclasses import asdict, replace
from pathlib import Path
import argparse
import json
import socket
import pandas as pd
from quant_backtester import ResearchData, run_backtest, RESEARCH_STRATEGIES, BOOK_STRATEGIES, atomic_json
from quant_option_model import MissingObservedPrice
from quant_terminal import STRATEGY_LABELS, build_config, instrument_defaults


def verify(start='2025-01-01', end='2026-10-31', output=Path('data/strategy_verification'), instruments=('NIFTY','SENSEX','NATURALGAS')):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    rows=[]
    for instrument in instruments:
        data=ResearchData(instrument,start,end)
        defaults=instrument_defaults(instrument)
        print(f'{instrument}: {len(data.days)} eligible, {len(data.exclusions)} excluded sessions',flush=True)
        atomic_json(output/f'{instrument}_eligibility.json',data.eligibility)
        for interval in ('5 minutes','15 minutes'):
            for label,(key,_) in STRATEGY_LABELS.items():
                if instrument=='NATURALGAS' and key in BOOK_STRATEGIES[1:3]:
                    rows.append(dict(instrument=instrument,strategy=key,timeframe='5m' if interval=='5 minutes' else '15m',variant='default',status='Not applicable: requires index options'))
                    continue
                if key=='mcx_evening_breakout' and instrument!='NATURALGAS':
                    rows.append(dict(instrument=instrument,strategy=key,timeframe='5m' if interval=='5 minutes' else '15m',variant='default',status='Not applicable: MCX only'))
                    continue
                c=build_config('NIFTY 50' if instrument=='NIFTY' else instrument,label,
                    (pd.Timestamp(start).date(),(pd.Timestamp(end)-pd.Timedelta(days=1)).date()),
                    defaults['capital'],defaults['allocation'],interval,defaults['take_profit'],defaults['stop_loss'],
                    defaults['minimum_premium'],'Trailing SL' if key=='supertrend_adx_trend' else 'Target / EOD')
                for variant,config in [('default',c),('risk_sensitivity',replace(c,
                    sl_pct=2 if instrument=='NATURALGAS' else 10,
                    target_pct=5 if instrument=='NATURALGAS' else 50,
                    trail_pct=2 if instrument=='NATURALGAS' and c.exit_mode=='trailing_sl' else c.trail_pct))]:
                    try:
                        result=run_backtest(config,data,persist=False)
                    except MissingObservedPrice as exc:
                        rows.append(dict(instrument=instrument,strategy=key,timeframe=config.timeframe,
                            variant=variant,status='FAILED: incomplete execution prices',error=str(exc)))
                        print(f'{instrument} {config.timeframe} {key} {variant}: FAILED {exc}',flush=True)
                        pd.DataFrame(rows).to_csv(output/'summary.csv',index=False)
                        continue
                    f=result['_frames']['trades']
                    if len(f):
                        assert (f.quantity>0).all() and (f.total_cost>0).all()
                        assert ((f.gross_pnl-f.total_cost-f.net_pnl).abs()<1e-6).all()
                        assert (f.exit_time>=f.entry_time).all()
                        assert f.groupby('date').size().max()<=2
                    row=dict(instrument=instrument,strategy=key,timeframe=config.timeframe,variant=variant,
                        status=result['status'],eligible_sessions=result['eligible_sessions'],excluded_sessions=result['excluded_sessions'],
                        first_session=result['first_session'],last_session=result['last_session'],
                        data_fingerprint=result['data_fingerprint'],**result['metrics'])
                    rows.append(row)
                    document={k:v for k,v in result.items() if k!='_frames'}
                    atomic_json(output/f'{instrument}_{key}_{config.timeframe}_{variant}.json',document)
                    print(f'{instrument} {config.timeframe} {key} {variant}: net={row["net_pnl"]:.2f} win={row["win_rate"]:.2f}% trades={row["total_trades"]} PF={row["profit_factor"]}',flush=True)
                    # Incremental evidence survives an interrupted long run.
                    pd.DataFrame(rows).to_csv(output/'summary.csv',index=False)
    return pd.DataFrame(rows)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',default='2025-01-01');parser.add_argument('--end',default='2026-10-31')
    parser.add_argument('--output',type=Path,default=Path('data/strategy_verification'))
    parser.add_argument('--instruments',nargs='+',default=['NIFTY','SENSEX','NATURALGAS'])
    args=parser.parse_args()
    def blocked(*args,**kwargs): raise AssertionError('Verification is offline: network blocked')
    socket.socket.connect=blocked;socket.socket.connect_ex=blocked
    verify(args.start,args.end,args.output,args.instruments)
