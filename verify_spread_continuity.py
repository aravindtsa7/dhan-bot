"""Offline reproduction of the reported 3m window and six original entry events."""
from dataclasses import replace
from pathlib import Path
import socket
import pandas as pd
from quant_backtester import Config, ResearchData, run_backtest, simulate_trade, atomic_json
from spread_regression_fixtures import CASES


def verify(output=Path('data/spread_continuity_verification')):
    output.mkdir(parents=True,exist_ok=True);summaries=[];replays=[]
    base=Config(strategy='book_vertical_spread',start='2026-08-01',end='2026-10-22',timeframe='3m',
        capital=100000,capital_usage_pct=25,sizing_mode='allocation',sl_pct=15,target_pct=25,trail_pct=0,
        entry_start='09:25',entry_end='14:30',exit_mode='target_eod',risk_basis='percentage',
        slippage=.5,minimum_premium=50,volume_multiple=1.3)
    for instrument in ('NIFTY','SENSEX'):
        c=replace(base,instrument=instrument);data=ResearchData(instrument,c.start,c.end)
        result=run_backtest(c,data,persist=False)
        assert result['metrics']['unresolved_trades']==0
        summaries.append(dict(instrument=instrument,**result['metrics']))
        atomic_json(output/f'{instrument}_3m.json',{k:v for k,v in result.items() if k!='_frames'})
        result['_frames']['trades'].to_parquet(output/f'{instrument}_3m_trades.parquet',index=False)
        print(instrument,result['metrics'],flush=True)
        if instrument=='NIFTY':
            for number,case in CASES.items():
                trade,path=simulate_trade(data,c,case['date'],(case['entry'],case['side'],case['strikes'][0],None,1),f'original_{number}',budget=25000,entry_equity=100000)
                assert trade is not None
                if number==70:
                    assert trade['exit_reason']=='stop_loss' and trade['net_pnl']<0
                    assert abs(trade['raw_exit_price']-18)<1e-8
                replays.append(dict(original_trade_number=number,**trade))
                pd.DataFrame(path).to_parquet(output/f'trade_{number}_path.parquet',index=False)
                print('Original entry replay',number,trade['exit_time'],trade['raw_exit_price'],trade['exit_price'],trade['net_pnl'],trade['exit_reason'],flush=True)
    pd.DataFrame(summaries).to_csv(output/'summary.csv',index=False)
    pd.DataFrame(replays).to_csv(output/'original_entry_replays.csv',index=False)


if __name__=='__main__':
    def blocked(*args,**kwargs):raise AssertionError('External network forbidden during verification')
    socket.socket.connect=blocked;socket.socket.connect_ex=blocked
    verify()
