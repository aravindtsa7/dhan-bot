"""Daily-lock latching, risk accounting and profile summary definitions."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
import pandas as pd
from quant_backtester import Config, HIGH_STRATEGIES, simulate_trade, fees
from quant_portfolio import execute_portfolio
from fine_tune_strategy import summarize, targeted_grid, rank_candidates


class FineTuneTests(unittest.TestCase):
    def fixture(self,prices,events=None):
        day=dict(timestamp=pd.Timestamp('2026-10-22',tz='Asia/Kolkata'),contract='FIXED')
        return SimpleNamespace(days={'2026-10-22':day},instrument='NIFTY',mcx=False,has_calendar=False,expiry_calendar=set(),
            events=lambda c,d:events or [(600,'CE',24000,None,1),(610,'PE',24000,None,1)],
            price=lambda day,t,s,k:prices.get(t))

    def config(self,**kw):
        return replace(Config(),capital=50000,lot_size=100,slippage=0,sl_pct=80,target_pct=200,trail_pct=0,**kw)

    def test_loss_latch_survives_rebound_and_reports_overshoot(self):
        c=self.config(daily_loss_limit=1500)
        data=self.fixture({600:(100,101,79,80,1),601:(75,200,70,190,1)})
        audit=[]; trades,_,_=execute_portfolio(data,c,'loss',audit)
        self.assertEqual(len(trades),1)
        a=audit[0]
        self.assertEqual(a['lock_minute'],600)
        self.assertEqual(a['lock_reason'],'daily_loss_limit')
        self.assertEqual(a['blocked_entries'],1)
        self.assertEqual(trades[0]['exit_price'],75)
        self.assertAlmostEqual(a['loss_limit_excess'],-trades[0]['net_pnl']-1500)
        self.assertAlmostEqual(a['max_drawdown_1m_close'],-trades[0]['net_pnl'])

    def test_profit_latch_does_not_reopen_when_profit_fades(self):
        c=self.config(daily_profit_target=2000)
        data=self.fixture({600:(100,126,99,125,1),601:(110,115,100,105,1)})
        audit=[]; trades,_,_=execute_portfolio(data,c,'profit',audit)
        a=audit[0]
        self.assertEqual(len(trades),1)
        self.assertEqual(a['lock_reason'],'daily_profit_target')
        self.assertGreater(a['lock_trigger_pnl'],2000)
        self.assertLess(trades[0]['net_pnl'],2000)
        self.assertEqual(a['blocked_entries'],1)
        # Drawdown measures the decline from the profitable peak, not loss from open.
        self.assertAlmostEqual(a['max_drawdown_1m_close'],a['lock_trigger_pnl']-trades[0]['net_pnl'])

    def test_locks_reset_next_session_and_empty_day_is_included(self):
        c=self.config(daily_profit_target=2000)
        data=self.fixture({600:(100,126,99,125,1),601:(125,126,124,125,1)})
        data.days['2026-10-23']=dict(timestamp=pd.Timestamp('2026-10-23',tz='Asia/Kolkata'),contract='FIXED')
        data.days['2026-10-26']=dict(timestamp=pd.Timestamp('2026-10-26',tz='Asia/Kolkata'),contract='FIXED')
        data.events=lambda c,d:[] if d=='2026-10-26' else [(600,'CE',24000,None,1)]
        audit=[]; trades,_,_=execute_portfolio(data,c,'reset',audit)
        self.assertEqual(len(trades),2)
        self.assertEqual(len(audit),3)
        self.assertEqual(audit[2]['net_pnl'],0)
        self.assertEqual(audit[2]['trades'],0)

    def test_net_costs_determine_lock_not_gross_gain(self):
        c=self.config(daily_profit_target=2000)
        data=self.fixture({600:(100,121,99,120,1),601:(120,126,119,125,1),602:(125,126,124,125,1)})
        audit=[]; trades,_,_=execute_portfolio(data,c,'net',audit)
        self.assertEqual(audit[0]['lock_minute'],601)
        self.assertEqual(trades[0]['exit_time'].minute,2)

    def test_mcx_trailing_jump_changes_actual_stop(self):
        c=Config(instrument='CRUDEOIL',strategy=HIGH_STRATEGIES[3],atr_sl=1,slippage=0,trail_pct=20,reward_risk=10)
        data=self.fixture({600:(100,109,99,108,1),601:(99,100,98,99,1)})
        data.mcx=True
        # Quarter-distance moves stop to 97.5; half-distance moves it to 95.
        # Use a later adverse gap so both positions eventually close.
        data.price=lambda day,t,s,k:{600:(100,109,99,108,1),601:(96,97,94,95,1)}.get(t)
        a,pa=simulate_trade(data,c,'2026-10-22',(600,'LONG',0,None,10),'a')
        b,pb=simulate_trade(data,replace(c,atr_trail_jump=.5),'2026-10-22',(600,'LONG',0,None,10),'b')
        self.assertEqual(pa[-1]['stop'],97.5)
        self.assertEqual(pb[-1]['stop'],95)
        self.assertNotEqual(a['exit_price'],b['exit_price'])

    def test_summary_denominators_and_captured_explosions(self):
        daily=pd.DataFrame(dict(date=['a','b','c','d'],trades=[1,1,1,0],net_pnl=[1500,2500,-1000,0],
            max_drawdown_1m_close=[100,700,1200,0],lock_reason=['','','daily_loss_limit',''],blocked_entries=[0,0,2,0],loss_limit_excess=[0,0,0,0]))
        trades=pd.DataFrame(dict(entry_price=[100,100,100],exit_price=[125,200,90],net_pnl=[1500,2500,-1000],roi_pct=[15,25,-10],used_modeled_prices=[False]*3))
        m=summarize(trades,daily)
        self.assertEqual(m['avg_daily_pnl'],750)
        self.assertEqual(m['avg_active_daily_pnl'],1000)
        self.assertEqual(m['hit_1000_pct'],50)
        self.assertEqual(m['hit_2000_pct'],25)
        self.assertEqual(m['hit_band_1000_2000_pct'],25)
        self.assertEqual(m['max_single_day_drawdown'],1200)
        self.assertEqual(m['worst_day_pnl'],-1000)
        self.assertAlmostEqual(m['explosive_20_pct'],200/3)
        self.assertAlmostEqual(summarize(trades,daily,True)['explosive_20_pct'],100/3)
        self.assertIsNone(summarize(trades.iloc[:0],daily.iloc[:0])['hit_1000_pct'])
        trades.loc[0,'exit_price']=120  # binary floating point must not miss exactly +20%
        self.assertAlmostEqual(summarize(trades,daily)['explosive_20_pct'],200/3)

    def test_grid_unique_and_all_requested_axes_vary(self):
        grid=list(targeted_grid('2025-01-01','2026-10-23'))
        self.assertEqual(len(grid),224)
        self.assertEqual(len(set(c for _,c in grid)),224)
        for _,c in grid: c.validate()
        steady=[c for p,c in grid if p=='steady']
        self.assertEqual({c.capital_usage_pct for c in steady},{25,50})
        self.assertEqual({c.daily_loss_limit for c in steady},{1000,1500})
        self.assertEqual({c.daily_profit_target for c in steady},{1000,2000})

    def test_ranking_ignores_later_outcomes(self):
        common=dict(profile='steady',instrument='CRUDEOIL',development_trading_days=20,
            development_net_pnl=1000,development_hit_1000_pct=25,development_max_single_day_drawdown=1000)
        table=pd.DataFrame([dict(**common,run_id='a',development_avg_daily_pnl=100,later_avg_daily_pnl=-10000),
                            dict(**common,run_id='b',development_avg_daily_pnl=50,later_avg_daily_pnl=10000)])
        ranked=rank_candidates(table)
        self.assertEqual(ranked.iloc[0].run_id,'a')
        self.assertTrue(rank_candidates(pd.DataFrame()).empty)


if __name__=='__main__': unittest.main()
