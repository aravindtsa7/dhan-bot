"""Account invariants, causal daily exits, and aggressive strategy fixtures."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
import pandas as pd
from quant_backtester import Config, LEGACY_STRATEGIES as STRATEGIES, HIGH_STRATEGIES, order_allocation
from quant_portfolio import execute_portfolio
from quant_high_edge import high_edge_events


class CapitalTests(unittest.TestCase):
    def data(self,c,events,prices,date='2026-10-22'):
        day=dict(timestamp=pd.Timestamp(date,tz='Asia/Kolkata'),contract='FIXED')
        return SimpleNamespace(days={date:day},instrument=c.instrument,mcx=c.instrument in ('CRUDEOIL','NATURALGAS','GOLDM'),
            has_calendar=False,expiry_calendar=set(),events=lambda c,d:events,
            price=lambda day,t,s,k:prices(t,s))

    def config(self,**kw):
        return replace(Config(),slippage=0,trail_pct=0,sl_pct=80,target_pct=200,capital=20000,**kw)

    def test_whole_lots_and_fee_reserve(self):
        c=self.config(sizing_mode='allocation',capital_usage_pct=50)
        qty,principal,reserve=order_allocation(c,'2026-10-22',100,10000)
        self.assertEqual(qty,65)
        self.assertLessEqual(principal+reserve,10000)
        self.assertEqual(order_allocation(c,'2026-10-22',100,6500)[0],0)
        self.assertEqual(order_allocation(replace(c,sizing_mode='fixed_lots',lots=2),'2026-10-22',100,10000)[0],0)

    def test_mcx_margin_is_not_option_premium(self):
        c=replace(Config(),instrument='CRUDEOIL',strategy=HIGH_STRATEGIES[5],futures_margin_pct=20)
        qty,allocated,reserve=order_allocation(c,'2026-10-22',6000,130000)
        self.assertEqual(qty,100)
        self.assertEqual(allocated,120000)
        self.assertGreater(reserve,40)
        self.assertEqual(order_allocation(replace(c,futures_margin_pct=100),'2026-10-22',6000,130000)[0],0)

    def test_profit_and_loss_cutoff_next_open_no_reentry(self):
        for mark,limit,reason in [(120,1000,'daily_profit_target'),(80,1000,'daily_loss_limit')]:
            c=self.config(daily_profit_target=limit if mark>100 else 0,daily_loss_limit=limit if mark<100 else 0)
            prices=lambda t,s:(100,121,79,mark,100) if t==600 else (110,115,105,110,100)
            d=self.data(c,[(600,'CE',100,None,1),(610,'PE',100,None,1)],prices)
            records,_,_=execute_portfolio(d,c,'test')
            self.assertEqual(len(records),1)
            self.assertEqual(records[0]['exit_reason'],reason)
            self.assertEqual(records[0]['exit_time'].hour*60+records[0]['exit_time'].minute,601)
            self.assertEqual(records[0]['exit_price'],110)
            self.assertEqual(records[0]['status'],'closed')

    def test_two_legs_share_capital_and_joint_daily_limit(self):
        c=self.config(strategy=STRATEGIES[2],mode='B',sizing_mode='allocation',capital_usage_pct=75,daily_profit_target=1800)
        def prices(t,s):
            return (100,120,99,120,100) if t==600 else (120,121,119,120,100)
        d=self.data(c,[(600,'CE',100,None,1),(600,'PE',100,None,1)],prices)
        trades,_,_=execute_portfolio(d,c,'pair')
        self.assertEqual(len(trades),2)
        self.assertLessEqual(sum(t['capital_reserved'] for t in trades),c.capital*.75)
        self.assertTrue(all(t['exit_reason']=='daily_profit_target' for t in trades))
        self.assertTrue(all(t['exit_time'].minute==1 for t in trades))

    def test_compounding_only_uses_already_realized_profits(self):
        c=replace(self.config(sizing_mode='allocation'),capital=14000,target_pct=100)
        prices=lambda t,s:(100,201,99,200,100)
        d=self.data(c,[(600,'CE',100,None,1),(610,'CE',100,None,1)],prices)
        fixed,_,_=execute_portfolio(d,c,'fixed')
        grown,_,_=execute_portfolio(d,replace(c,compound=True),'grown')
        self.assertEqual([t['quantity'] for t in fixed],[130,130])
        self.assertEqual([t['quantity'] for t in grown],[130,260])
        for t in grown:
            self.assertLessEqual(t['capital_reserved'],t['entry_equity'])
            self.assertAlmostEqual(t['roi_pct'],100*t['net_pnl']/t['capital_allocated'])

    def test_insufficient_capital_audited(self):
        c=replace(self.config(),capital=5000)
        d=self.data(c,[(600,'CE',100,None,1)],lambda t,s:(100,101,99,100,100))
        trades,_,skips=execute_portfolio(d,c,'poor')
        self.assertFalse(trades)
        self.assertEqual(len(skips),1)

    def test_straddle_requires_both_affordable_legs(self):
        c=self.config(strategy=STRATEGIES[2],mode='B')
        d=self.data(c,[(600,'CE',100,None,1),(600,'PE',100,None,1)],
                    lambda t,s: (100,101,99,100,100) if s=='CE' else (300,301,299,300,100))
        trades,_,skips=execute_portfolio(d,c,'pair_reject')
        self.assertEqual(trades,[])
        self.assertTrue(any(s['reason']=='straddle_pair_unaffordable_or_missing' for s in skips))

    def test_percentage_goal_uses_session_opening_equity(self):
        c=self.config(daily_profit_pct=5)
        d=self.data(c,[(600,'CE',100,None,1)],lambda t,s:(100,121,99,120,100))
        trades,_,_=execute_portfolio(d,c,'percent')
        self.assertEqual(trades[0]['exit_reason'],'daily_profit_target')
        self.assertEqual(trades[0]['exit_time'].minute,1)

    def test_config_rejects_invalid_risk_settings(self):
        for changes in [dict(capital_usage_pct=101),dict(daily_loss_limit=-1),dict(capital=float('nan')),
                        dict(lots=1.5),dict(instrument='GOLDM',strategy=HIGH_STRATEGIES[5])]:
            with self.assertRaises(ValueError): replace(Config(),**changes).validate()


class GrowthSignalTests(unittest.TestCase):
    def fixture(self,mcx=False):
        day=dict(timestamp=pd.Timestamp('2026-10-22',tz='Asia/Kolkata'),prices={},options={},option_meta={},rolling={})
        at=1100 if mcx else 785
        day['prices']={t:(100,101,99,100,100) for t in range(555,1396)}
        bars=pd.DataFrame([dict(timestamp=day['timestamp']+pd.Timedelta(minutes=at-105+5*i),
            open=100,high=101,low=99,close=100,volume=100,adx=30,atr=5,atr_baseline=5) for i in range(21)])
        bars.loc[20,['close','high','volume','atr']]=[110,111,300,10]
        if not mcx:
            for side in ('CE','PE'):
                for m in ('ATM','ATM-1','ATM+1'): day['rolling'][side,m]={t:100 for t in range(555,921)}
                for t in range(555,at):
                    cl=120 if t==at-1 else 100
                    day['options'][t,side,100]=(cl,cl+1,cl-1,cl,300 if t==at-1 else 100)
                    day['option_meta'][t,side,100]=(1000+10*t,20,100)
        data=SimpleNamespace(days={'2026-10-22':day},mcx=mcx,features=lambda tf:{'2026-10-22':bars})
        return data,bars

    def test_h5_expiry_and_afternoon_chop_exception(self):
        d,b=self.fixture()
        c=Config(instrument='SENSEX',strategy=HIGH_STRATEGIES[4],entry_start='13:00')
        events=high_edge_events(d,c,'2026-10-22')
        self.assertEqual(events[0][:2],(785,'CE'))
        # Thursday in October 2026 is Sensex expiry, not Nifty expiry.
        self.assertEqual(high_edge_events(d,replace(c,instrument='NIFTY'),'2026-10-22'),[])
        b.loc[20,'close']=100
        self.assertEqual(high_edge_events(d,c,'2026-10-22'),[])

    def test_h6_requires_atr_and_volume_surge(self):
        d,b=self.fixture(True)
        c=Config(instrument='NATURALGAS',strategy=HIGH_STRATEGIES[5],entry_start='17:30',entry_end='21:30')
        self.assertEqual(high_edge_events(d,c,'2026-10-22')[0][:2],(1100,'LONG'))
        b.loc[20,'volume']=100
        self.assertEqual(high_edge_events(d,c,'2026-10-22'),[])
        b.loc[20,'volume']=300; b.loc[20,'atr']=5
        self.assertEqual(high_edge_events(d,c,'2026-10-22'),[])


if __name__=='__main__': unittest.main()
