"""Adversarial fill/identity tests using small hand-constructed candle paths."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
import pandas as pd

from quant_backtester import Config, LEGACY_STRATEGIES as STRATEGIES, fees, quantity, simulate_trade, performance, indicators
from quant_option_model import MissingObservedPrice, continuation_candle, intrinsic
from quant_high_edge import HIGH_STRATEGIES, option_statistics, option_confirmation
from quant_calendar import is_expiry_day, next_expiry


class EngineTests(unittest.TestCase):
    def fake(self, rows, mcx=False):
        day=dict(timestamp=pd.Timestamp('2026-10-22',tz='Asia/Kolkata'),contract='TEST_FIXED_FUTURE')
        data=SimpleNamespace(days={'2026-10-22':day},mcx=mcx,instrument='NIFTY',has_calendar=False,expiry_calendar=set())
        data.price=lambda day,at,side,strike: rows.get((at,strike))
        return data

    def config(self, **kwargs):
        return replace(Config(),slippage=0,trail_pct=0,sl_pct=20,target_pct=40,**kwargs)

    def test_stop_wins_collision_and_gap(self):
        d=self.fake({(600,24000):(100,150,70,110,1)})
        t,_=simulate_trade(d,self.config(),'2026-10-22',(600,'CE',24000,None,0),'t')
        self.assertEqual(t['exit_price'],80)
        self.assertEqual(t['exit_reason'],'stop_loss')
        d=self.fake({(600,24000):(100,105,95,100,1),(601,24000):(65,90,60,85,1)})
        t,_=simulate_trade(d,self.config(),'2026-10-22',(600,'CE',24000,None,0),'t')
        self.assertEqual(t['exit_price'],65)

    def test_trailing_update_only_next_bar(self):
        d=self.fake({(600,24000):(100,130,90,125,1),(601,24000):(120,125,105,110,1)})
        c=replace(self.config(),trail_pct=20,trail_jump_pct=5)
        t,path=simulate_trade(d,c,'2026-10-22',(600,'CE',24000,None,0),'t')
        self.assertEqual(t['exit_time'].minute,1)
        self.assertEqual(t['exit_price'],110)
        self.assertEqual([r['stop'] for r in path],[80,110])

    def test_trailing_overshoot_moves_only_whole_jumps(self):
        d=self.fake({(600,24000):(100,132,90,125,1),(601,24000):(120,124,109,112,1)})
        c=replace(self.config(),trail_pct=20,trail_jump_pct=5)
        t,path=simulate_trade(d,c,'2026-10-22',(600,'CE',24000,None,0),'t')
        self.assertEqual([r['stop'] for r in path],[80,110])
        self.assertEqual(t['exit_price'],110)

    def test_strict_strike_roll_fails_without_fabricating_exit(self):
        d=self.fake({(600,24000):(100,105,95,100,1),(601,24100):(200,250,195,240,1)})
        with self.assertRaises(MissingObservedPrice):
            simulate_trade(d,replace(self.config(),price_policy='strict_observed'),'2026-10-22',(600,'CE',24000,None,0),'t')

    def test_squareoff_at_open_and_costs(self):
        d=self.fake({(919,24000):(100,105,95,100,1),(920,24000):(110,200,1,150,1)})
        t,_=simulate_trade(d,self.config(),'2026-10-22',(919,'CE',24000,None,0),'t')
        self.assertEqual(t['exit_reason'],'square_off')
        self.assertEqual(t['exit_price'],110)
        self.assertEqual(t['brokerage'],40)
        self.assertLess(t['net_pnl'],t['gross_pnl'])

    def test_stt_date_and_short_futures_tax(self):
        c=Config()
        self.assertEqual(fees(c,100,200,100,'2026-03-31')['stt_ctt'],20)
        self.assertEqual(fees(c,100,200,100,'2026-04-01')['stt_ctt'],30)
        c=replace(c,instrument='CRUDEOIL',strategy=STRATEGIES[4])
        self.assertEqual(fees(c,100,80,100,'2026-10-22',-1)['stt_ctt'],1)
        self.assertEqual(quantity(c,'2026-10-22'),100)
        self.assertEqual(quantity(replace(c,instrument='GOLDM'),'2026-10-22'),10)

    def test_metrics_include_initial_loss_and_zero_days(self):
        trades=pd.DataFrame([dict(date='2026-01-01',status='closed',net_pnl=-100,total_cost=10),
                             dict(date='2026-01-03',status='censored',net_pnl=float('nan'),total_cost=float('nan'))])
        metrics,daily,_=performance(trades,['2026-01-01','2026-01-02','2026-01-03'],1000)
        self.assertEqual(metrics['max_drawdown'],100)
        self.assertEqual(metrics['censored_trades'],1)
        self.assertEqual(daily.net_pnl.tolist(),[-100,0,0])

    def test_indicators_do_not_see_future(self):
        f=pd.DataFrame(dict(open=range(100,150),high=range(102,152),low=range(98,148),close=range(101,151),volume=[0]*50))
        before=indicators(f.iloc[:30])
        after=indicators(f)
        pd.testing.assert_frame_equal(before,after.iloc[:30])
        self.assertTrue(before.vwap_proxy.all())

    def test_dated_expiries_and_holiday_adjustment(self):
        self.assertTrue(is_expiry_day('SENSEX','2025-01-03'))
        self.assertTrue(is_expiry_day('SENSEX','2025-01-07'))
        self.assertFalse(is_expiry_day('SENSEX','2025-01-10'))
        self.assertTrue(is_expiry_day('NIFTY','2025-08-28'))
        self.assertTrue(is_expiry_day('NIFTY','2025-09-02'))
        self.assertTrue(is_expiry_day('SENSEX','2025-09-04'))
        self.assertTrue(is_expiry_day('SENSEX','2026-01-14'))
        self.assertTrue(is_expiry_day('NIFTY','2026-10-19'))
        self.assertEqual(next_expiry('NIFTY','2026-10-19'),'2026-10-19')

    def test_lots_follow_inferred_expiry_not_just_trade_year(self):
        self.assertEqual(quantity(Config(),'2025-01-27'),25)
        self.assertEqual(quantity(Config(),'2025-12-31'),65)
        self.assertEqual(quantity(Config(instrument='SENSEX'),'2025-01-02'),10)

    def test_fixed_strike_true_vwap_and_gap_flag(self):
        day=dict(options={(555,'CE',100):(10,12,8,10,100),(556,'CE',100):(20,22,18,20,300),
                          (555,'CE',150):(100,110,90,100,9999),(558,'CE',100):(30,32,28,30,100)})
        s=option_statistics(day)
        self.assertEqual(s[556,'CE',100]['option_vwap'],17.5)
        self.assertTrue(s[556,'CE',100]['vwap_full_session'])
        self.assertFalse(s[558,'CE',100]['vwap_full_session'])
        self.assertEqual(s[555,'CE',150]['option_vwap'],100)

    def test_option_confirmation_uses_no_entry_or_future_bar(self):
        day=dict(options={},option_meta={})
        for at in range(555,600):
            day['options'][at,'CE',24000]=(100,101,99,100,100 if at<599 else 200)
            day['option_meta'][at,'CE',24000]=(1000+at,20,24000)
        day['option_stats']=option_statistics(day)
        c=replace(Config(),oi_min_change_pct=.1)
        before=option_confirmation(day,600,'CE',24000,c)
        self.assertIsNotNone(before)
        day['options'][600,'CE',24000]=(1,100000,0,1,1e20)
        day['option_meta'][600,'CE',24000]=(1e20,900,100000)
        day['option_context_cache']={}
        self.assertEqual(before,option_confirmation(day,600,'CE',24000,c))

    def model_data(self):
        d=self.fake({(918,24000):(100,101,99,100,100)})
        day=d.days['2026-10-22']
        day.update(prices={t:(24010,24015,24005,24010,0) for t in (917,918,919,920)},
                   rolling={(s,k):{t:24100 for t in (917,918,919,920)} for s in ('CE','PE') for k in ('ATM','ATM-1','ATM+1')},
                   options={(t,'CE',24100):(60,65,55,60,100) for t in (917,918,919,920)},
                   option_meta={(t,'CE',24100):(1000,20,24010) for t in (917,918,919,920)})
        return d

    def test_model_closes_and_never_claims_observed_prices(self):
        d=self.model_data()
        c=replace(self.config(),target_pct=100)
        t,path=simulate_trade(d,c,'2026-10-22',(918,'CE',24000,None,0),'t')
        self.assertEqual(t['status'],'closed')
        self.assertTrue(t['used_modeled_prices'])
        self.assertEqual(t['modeled_bars'],2)
        self.assertEqual(t['exit_reason'],'square_off')
        self.assertEqual(t['entry_strike'],24000)
        self.assertEqual(t['verification_status'],'model_dependent')
        self.assertEqual(path[-1]['price_source'],'model_delta_gamma_continuity')
        self.assertAlmostEqual(t['net_pnl'],t['gross_pnl']-t['total_cost'])

    def test_model_uses_lagged_iv_and_no_future_prices(self):
        d=self.model_data(); day=d.days['2026-10-22']; previous=(100,101,99,100,100)
        p,_=continuation_candle(d,day,919,'CE',24000,previous)
        day['option_meta'][919,'CE',24100]=(1000,900,24010)
        day['options'][920,'CE',24100]=(9000,9999,1,9000,1)
        other,_=continuation_candle(d,day,919,'CE',24000,previous)
        self.assertEqual(p,other)
        self.assertGreaterEqual(p[3],intrinsic(24010,24000,'CE'))

    def test_sniper_breakeven_activates_next_minute(self):
        d=self.fake({(600,24000):(100,116,90,115,1),(601,24000):(104,106,99,101,1)})
        t,path=simulate_trade(d,replace(self.config(),strategy=HIGH_STRATEGIES[2]),'2026-10-22',(600,'CE',24000,None,0),'t')
        self.assertEqual(t['exit_price'],100)
        self.assertEqual([p['stop'] for p in path],[80,100])


if __name__=='__main__': unittest.main()
