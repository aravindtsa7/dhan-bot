"""All ten strategies, two windows/intervals; offline, no saved user runs."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import socket
import pandas as pd
from quant_backtester import Config, ResearchData, STRATEGIES, run_backtest, atomic_json
from quant_terminal import strategy_defaults


def verify(instrument, output=Path('data/strategy_audit'), strategies=STRATEGIES):
    output=Path(output);output.mkdir(parents=True,exist_ok=True);rows=[]
    summary=output/f'{instrument}_summary.csv'
    if list(strategies)!=STRATEGIES and summary.exists():
        old=pd.read_csv(summary);rows=old[~old.strategy.isin(strategies)].to_dict('records')
    for window,start,end in [('recent','2026-08-01','2026-10-22'),('full','2025-01-01','2026-11-01')]:
        data=ResearchData(instrument,start,end);expiry_data=None
        print(f'{instrument} {window}: {len(data.days)} eligible, {len(data.exclusions)} excluded',flush=True)
        for tf in ('5m','15m'):
            for strategy in strategies:
                defaults=strategy_defaults(instrument,strategy)
                c=Config(instrument=instrument,strategy=strategy,timeframe=tf,start=start,end=end,
                    capital=100000,capital_usage_pct=50 if strategy in ('expiry_day','expiry_intraday') else 100,sizing_mode='fixed_lots',lots=1,
                    futures_margin_pct=20,minimum_premium=(70 if strategy=='expiry_day' else 40 if strategy=='expiry_intraday' else 50 if not data.mcx and strategy in ('vwap_ema_pullback','orb_volume_breakout','supertrend_adx_trend','book_vol_oi_momentum') else 0),
                    target_pct=float(defaults['take_profit'].rstrip('%')),sl_pct=float(defaults['stop_loss'].rstrip('%')),
                    trail_pct=0,exit_mode='target_eod',risk_basis='percentage',volume_multiple=1.3,
                    entry_start='17:00' if data.mcx and strategy!='vwap_ema_pullback' else '09:30',
                    entry_end='22:00' if data.mcx and strategy!='vwap_ema_pullback' else '14:30',
                    price_policy='strict_observed' if data.mcx else 'model_continuity')
                row=dict(window=window,instrument=instrument,timeframe=tf,strategy=strategy,start=start,end=end)
                try:c.validate()
                except ValueError as exc:
                    row.update(status='unsupported',reason=str(exc))
                else:
                    if strategy=='expiry_day' and expiry_data is None:expiry_data=ResearchData(instrument,start,end,expiry_config=c)
                    selected=expiry_data if strategy=='expiry_day' else data
                    row.update(eligible_sessions=len(selected.days),excluded_sessions=len(selected.exclusions),data_fingerprint=selected.fingerprint)
                    try:
                        result=run_backtest(c,selected,persist=False)
                        trades=result['_frames']['trades'];paths=result['_frames']['stop_paths']
                        if len(trades):
                            assert (trades.quantity>0).all() and (trades.total_cost>0).all()
                            assert (abs(trades.gross_pnl-trades.total_cost-trades.net_pnl)<1e-6).all()
                            assert (abs(trades.gross_before_slippage-trades.slippage_cost-trades.gross_pnl)<1e-6).all()
                            assert (trades.capital_reserved<=trades.allocation_budget+1e-6).all()
                            assert trades.groupby('date').size().max()<=(1 if strategy in ('book_strap_strip','mcx_evening_breakout','expiry_day','expiry_intraday') else 2)
                            if 'legs' in trades:
                                for trade in trades.itertuples():
                                    legs=json.loads(trade.legs)
                                    assert abs(sum(l['total_cost'] for l in legs)-trade.total_cost)<1e-6
                                    if trade.side in ('BULL_CALL_SPREAD','BEAR_PUT_SPREAD','IRON_BUTTERFLY'):
                                        width=abs(legs[0]['strike']-legs[2 if trade.side=='IRON_BUTTERFLY' else 1]['strike'])
                                        low,high=(-width,0) if trade.side=='IRON_BUTTERFLY' else (.05,width-.05)
                                        modeled=paths[paths.trade_id.eq(trade.trade_id) & paths.spread_bounds_applied]
                                        for field in ('open','high','low','close','mark'):assert modeled[field].between(low-1e-7,high+1e-7).all()
                        assert result['metrics']['unresolved_trades']==0
                        row.update(status=result['status'],first_session=result['first_session'],last_session=result['last_session'],**result['metrics'])
                        name=f'{instrument}_{window}_{tf}_{strategy}'
                        atomic_json(output/f'{name}.json',{k:v for k,v in result.items() if k!='_frames'})
                        (output/f'{name}_failure.json').unlink(missing_ok=True)
                        trades.to_parquet(output/f'{name}_trades.parquet',index=False)
                    except Exception as exc:
                        row.update(status='FAILED',reason=f'{type(exc).__name__}: {exc}')
                        atomic_json(output/f'{instrument}_{window}_{tf}_{strategy}_failure.json',dict(row,config=asdict(c)))
                rows.append(row)
                pd.DataFrame(rows).to_csv(output/f'{instrument}_summary.csv',index=False)
                print(json.dumps(row,default=str),flush=True)
    return rows


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('instrument',choices=('NIFTY','SENSEX','NATURALGAS'))
    parser.add_argument('--strategies',nargs='+',choices=STRATEGIES,default=STRATEGIES)
    args=parser.parse_args()
    def blocked(*args,**kwargs):raise AssertionError('External sockets blocked during matrix validation')
    socket.socket.connect=blocked;socket.socket.connect_ex=blocked
    rows=verify(args.instrument,strategies=args.strategies)
    raise SystemExit(int(any(r['status']=='FAILED' for r in rows)))
