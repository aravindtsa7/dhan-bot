"""Positive-path signal fixtures prevent a silent always-false strategy."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
import pandas as pd
from quant_backtester import Config, HIGH_STRATEGIES, run_backtest
from quant_high_edge import high_edge_events


class SignalTests(unittest.TestCase):
    def fixture(self):
        date='2026-10-22'
        day=dict(timestamp=pd.Timestamp(date,tz='Asia/Kolkata'),contract='NIFTY',
            prices={t:(100,105,95,100,0) for t in range(555,921)},options={},option_meta={},
            rolling={(side,m):{t:100 for t in range(555,921)} for side in ('CE','PE') for m in ('ATM','ATM-1','ATM+1')})
        for side in ('CE','PE'):
            for t in range(555,650):
                close=120 if t==589 and side=='CE' else 100
                day['options'][t,side,100]=(close,close+1,close-1,close,300 if t==589 else 100)
                day['option_meta'][t,side,100]=(1000+10*t,20,100)
        bars=pd.DataFrame([dict(timestamp=day['timestamp']+pd.Timedelta(minutes=555+5*i),open=105,
             high=111,low=106 if i==5 else 104,close=110,volume=100,adx=30,atr=10,
             ema9=109,ema21=100,ema50=100,trend=1,vwap=100) for i in range(12)])
        data=SimpleNamespace(days={date:day},mcx=False,features=lambda tf:{date:bars})
        return date,data,bars

    def test_h1_h2_h3_generate_causal_positive_signals(self):
        date,data,bars=self.fixture()
        for strategy in HIGH_STRATEGIES[:3]:
            c=replace(Config(),strategy=strategy,anchor='09:30',breakout_pct=18)
            events=high_edge_events(data,c,date)
            self.assertTrue(any(e[0]==590 and e[1]=='CE' for e in events),strategy)
            self.assertTrue(all(e[0]>=590 for e in events))

    def test_low_volume_or_unchanged_oi_reject_signal(self):
        date,data,_=self.fixture(); day=data.days[date]
        p=day['options'][589,'CE',100]
        day['options'][589,'CE',100]=(*p[:4],100)
        self.assertFalse(high_edge_events(data,Config(),date))
        day['options'][589,'CE',100]=p; day['option_context_cache']={}
        day['option_meta'][589,'CE',100]=day['option_meta'][584,'CE',100]
        self.assertFalse(high_edge_events(data,Config(),date))

    def test_h4_compressed_range_breakout(self):
        date,data,bars=self.fixture(); data.mcx=True
        day=data.days[date]
        day['prices']={t:(100,110 if t<1020 else 105,90 if t<1020 else 95,100,100) for t in range(990,1396)}
        bars['timestamp']=[day['timestamp']+pd.Timedelta(minutes=990+5*i) for i in range(len(bars))]
        c=Config(instrument='CRUDEOIL',strategy=HIGH_STRATEGIES[3],entry_start='17:30',entry_end='21:30')
        events=high_edge_events(data,c,date)
        self.assertEqual(events[0][:2],(1050,'LONG'))
        self.assertTrue(all(1050<=e[0]<=1290 for e in events))

    def test_sniper_daily_cap_and_chop_zone_enforced_in_execution(self):
        date='2026-10-22'
        c=Config(strategy=HIGH_STRATEGIES[2],max_trades_day=2)
        day=dict(timestamp=pd.Timestamp(date,tz='Asia/Kolkata'),contract='NIFTY')
        data=SimpleNamespace(days={date:day},mcx=False,instrument='NIFTY',start=c.start,end=c.end,
            fingerprint='fixture',available_days=[date],exclusions=[],has_calendar=False,expiry_calendar=set())
        data.events=lambda c,date:[(600,'CE',24000,None,1),(610,'CE',24000,None,1),(700,'CE',24000,None,1)]
        data.price=lambda day,at,side,strike:(100,110,1,50,100)
        result=run_backtest(c,data,persist=False)
        self.assertEqual(result['metrics']['total_trades'],1)
        self.assertEqual(result['metrics']['unresolved_trades'],0)
        c=replace(c,strategy=HIGH_STRATEGIES[0])
        data.events=lambda c,date:[(700,'CE',24000,None,1)]
        result=run_backtest(c,data,persist=False)
        self.assertEqual(result['metrics']['total_trades'],0)


if __name__=='__main__': unittest.main()
