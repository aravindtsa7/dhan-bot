"""Stateful option boundary, futures risk and observed quote-noise regressions."""
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
import pandas as pd
from quant_backtester import Config, simulate_trade
from quant_option_model import continuation_candle, option_mark_state, intrinsic
from quant_book import bounded_spread_rows
from quant_strategy_models import futures_risk, profit_lock
from quant_terminal import strategy_defaults, build_config


def wing_day(side='CE',spot=200,at=600):
    sign=1 if side=='CE' else -1;strike=1000;spot=strike+sign*(spot-100)
    day=dict(timestamp=pd.Timestamp('2026-10-22',tz='Asia/Kolkata'),prices={at:(spot,)*4+(0,)},rolling={},options={})
    for mon,offset in [('ATM-1',-50),('ATM',0),('ATM+1',50)]:
        k=spot+offset
        day['rolling'][side,mon]={at:k}
        premium=intrinsic(spot,k,side)+60
        day['options'][at,side,k]=(premium,)*4+(100,)
    return SimpleNamespace(instrument='NIFTY'),day,strike,sign


class ExecutionV5Tests(unittest.TestCase):
    def test_favorable_two_strike_move_retains_time_value_calls_and_puts(self):
        for side in ('CE','PE'):
            data,day,k,sign=wing_day(side)
            state=dict(price=120,spot=k,time=day['timestamp']+pd.Timedelta(minutes=600))
            row,meta=continuation_candle(data,day,600,side,k,(120,)*4+(1,),state=state,expiry_decay=False,open_only=True)
            self.assertGreater(row[0],180);self.assertLess(row[0],210)
            self.assertGreater(row[0],intrinsic(day['prices'][600][0],k,side))
            self.assertTrue(meta['modeled_wing_fallback'])

    def test_one_tick_band_transition_never_collapses_a_winning_option(self):
        for side in ('CE','PE'):
            data,day,k,sign=wing_day(side)
            current=day['prices'][600][0]
            state=dict(price=190,spot=current-sign*.10,time=day['timestamp']+pd.Timedelta(minutes=600))
            row,_=continuation_candle(data,day,600,side,k,(190,)*4+(1,),state=state,expiry_decay=False,open_only=True)
            self.assertTrue(190<row[0]<190.10)

    def test_zero_spot_boundary_change_only_has_small_intraday_theta(self):
        data,day,k,_=wing_day()
        state=dict(price=190,spot=1100,time=day['timestamp']+pd.Timedelta(minutes=600))
        row,_=continuation_candle(data,day,600,'CE',k,(190,)*4+(1,),state=state,expiry_decay=False)
        self.assertEqual(row[0],190)
        self.assertTrue(189.97<row[3]<190)

    def test_observed_quote_recalibrates_next_missing_mark(self):
        data,day,k,_=wing_day()
        state=dict(price=20,spot=1000,time=day['timestamp'])
        option_mark_state(day,600,(210,)*4+(1,),state,open_only=True)
        row,_=continuation_candle(data,day,600,'CE',k,None,state=state,expiry_decay=False,open_only=True)
        self.assertEqual(row[0],210)

    def test_expiry_theta_uses_remaining_time_once_and_keeps_intrinsic(self):
        data,day,k,_=wing_day(at=919)
        state=dict(price=111,spot=1100,time=day['timestamp']+pd.Timedelta(minutes=919))
        row,_=continuation_candle(data,day,919,'CE',k,None,state=state,expiry_decay=True)
        self.assertEqual(row[0],111)
        self.assertTrue(109.9<=row[3]<=110.1)
        self.assertGreaterEqual(row[3],100)

    def test_overnight_state_does_not_apply_overnight_minutes_as_intraday_theta(self):
        data,day,k,_=wing_day(at=555)
        state=dict(price=120,spot=1000,time=day['timestamp']-pd.Timedelta(hours=8,minutes=30))
        row,_=continuation_candle(data,day,555,'CE',k,None,state=state,expiry_decay=True,open_only=True)
        self.assertGreater(row[0],180)

    def test_missing_fill_open_does_not_use_future_close(self):
        data,day,k,_=wing_day()
        state=dict(price=190,spot=1100,time=day['timestamp']+pd.Timedelta(minutes=600))
        first,_=continuation_candle(data,day,600,'CE',k,None,state=dict(state),open_only=True)
        day['prices'][600]=(1100,9999,1,2000,0)
        again,_=continuation_candle(data,day,600,'CE',k,None,state=dict(state),open_only=True)
        self.assertEqual(first,again)

    def test_futures_five_x_percentage_distances_and_atr_caps(self):
        for instrument in ('NATURALGAS','CRUDEOIL','GOLDM'):
            c=Config(instrument=instrument,sl_pct=15,target_pct=25)
            self.assertEqual(futures_risk(c,220,2),(3,5))
            sl,tp=futures_risk(c,220,100)
            self.assertAlmostEqual(sl,6.6);self.assertAlmostEqual(tp,11)

    def test_futures_trailing_activates_at_12_atr_in_both_directions(self):
        c=Config(instrument='NATURALGAS',strategy='book_vertical_spread')
        for d in (1,-1):
            stop=220-d*3
            self.assertEqual(profit_lock(c,220,220+d*2.39,stop,d,2),stop)
            self.assertAlmostEqual(profit_lock(c,220,220+d*2.4,stop,d,2),220+d*.4)
            self.assertAlmostEqual(profit_lock(c,220,220+d*2.9,stop,d,2),220+d*.9)

    def test_directional_profit_lock_requires_18_percent_then_steps(self):
        c=Config(strategy='book_vertical_spread',exit_mode='target_eod')
        self.assertEqual(profit_lock(c,100,117.99,80),80)
        self.assertEqual(profit_lock(c,100,118,80),110)
        self.assertEqual(profit_lock(c,100,123,110),115)
        self.assertEqual(profit_lock(replace(c,exit_mode='end_of_day'),100,123,80),80)

    def test_four_observed_width_spikes_are_tagged_and_bounded(self):
        for width,noise,option in [(50,.30,'PE'),(50,.05,'PE'),(100,.75,'PE'),(100,.80,'CE'),(100,1.30,'PE')]:
            legs=[dict(side=s,strike=k) for s,k in [('CE',1000),('PE',1000),('CE',1000+width),('PE',1000-width)]]
            rows=[(10,)*4+(1,)]*4
            h=0 if option=='CE' else 1
            rows[h]=(10+width+noise,)*4+(1,)
            sources=['observed']*4
            corrected,bounded=bounded_spread_rows(dict(prices={915:(1000,)*4+(0,)}),915,'IRON_BUTTERFLY',legs,rows,sources,{})
            self.assertTrue(bounded)
            self.assertAlmostEqual(corrected[h][0]-corrected[h+2][0],width-.05)
            self.assertEqual(sources[h+2],'model_observed_spread_projection')
            self.assertEqual(rows[h+2][0],10)  # stored quotes remain untouched

    def test_strategy_defaults_and_explicit_ui_overrides(self):
        exp=strategy_defaults('NIFTY','expiry_day')
        self.assertEqual((exp['take_profit'],exp['stop_loss'],exp['minimum_premium'],exp['allocation']),('25%','60%',70.,'50%'))
        for key in ('supertrend_adx_trend','vwap_ema_pullback','orb_volume_breakout','book_vertical_spread','book_vol_oi_momentum'):
            values=strategy_defaults('NIFTY',key)
            self.assertEqual((values['take_profit'],values['stop_loss']),('30%','20%'))
        c=build_config('NIFTY 50','Expiry Day',(pd.Timestamp('2026-10-22').date(),)*2,100000,'75%','5 minutes','50%','30%',80,'Target + SL')
        self.assertEqual((c.target_pct,c.sl_pct,c.minimum_premium,c.capital_usage_pct),(50,30,80,75))

    def test_mcx_simulator_and_paper_use_identical_atr_profit_lock(self):
        from test_paper_trader import config, tick, NOW, ASSET
        from paper_engine import PaperAccount
        for direction,side in [(1,'LONG'),(-1,'SHORT')]:
            c=config(strategy='book_vol_oi_momentum',slippage=0,sl_pct=15,target_pct=25,exit_mode='target_eod')
            date='2026-10-23'
            quotes={1055:(220,max(220,220+direction*2.4),min(220,220+direction*2.4),220+direction*2.4,100),
                    1056:(220+direction*.3,)*4+(100,)}
            data=SimpleNamespace(mcx=True,instrument='CRUDEOIL',has_calendar=False,
                days={date:dict(timestamp=pd.Timestamp(date,tz='Asia/Kolkata'),contract='TEST')},
                price=lambda day,at,*args:quotes.get(at))
            trade,path=simulate_trade(data,c,date,(1055,side,0,None,2),'risk')
            self.assertAlmostEqual(trade['initial_stop'],220-direction*3)
            self.assertAlmostEqual(trade['target'],220+direction*5)
            self.assertEqual(trade['exit_reason'],'trailing_stop')
            self.assertAlmostEqual(path[1]['stop'],220+direction*.4)
            with TemporaryDirectory() as root:
                account=PaperAccount(Path(root),c,ASSET,lambda _:None)
                self.assertTrue(account.enter((1055,side,0,None,2),tick(220,bid=None,ask=None),NOW))
                account.on_tick(tick(220+direction*2.4,NOW+1,bid=None,ask=None),NOW+1)
                self.assertAlmostEqual(account.position['stop'],path[1]['stop'])
                self.assertEqual(account.position['entry_atr'],2)

    def test_quote_projection_is_disclosed_and_strict_policy_rejects_it(self):
        from test_quant_book import fixture, DATE
        from quant_option_model import MissingObservedPrice
        c,data,day=fixture(at=919)
        day['options'][920,'CE',100]=(12.05,)*4+(100,)
        trade,path=simulate_trade(data,c,DATE,(919,'BULL_CALL_SPREAD',100,None,1),'noise')
        self.assertAlmostEqual(trade['raw_exit_price'],9.95)
        self.assertTrue(trade['spread_bounds_applied'])
        self.assertTrue(path[-1]['spread_bounds_applied'])
        leg=json.loads(trade['legs'])[1]
        self.assertEqual(leg['exit_price_source'],'model_observed_spread_projection')
        self.assertEqual(leg['last_projection_observed_open'],2)
        with self.assertRaisesRegex(MissingObservedPrice,'quote-noise projection'):
            simulate_trade(data,replace(c,price_policy='strict_observed'),DATE,(919,'BULL_CALL_SPREAD',100,None,1),'strict')

    def test_entire_repaired_band_retains_prior_curve_without_calibrating_to_repairs(self):
        from test_quant_spread_continuity import sample
        case,day,legs=sample(70)
        day['price_sources']={}
        for mon in ('ATM-1','ATM','ATM+1'):
            k=day['rolling']['PE',mon][582]
            day['price_sources'][582,'PE',k]='synthetic_ffill'
        state={(0,1):(18.2,22618.5,36.9),('curve',0,1):(22618.5,18.2,50)}
        rows=[day['options'][582,'PE',22600],(.05,)*4+(0,)]
        marked,_=bounded_spread_rows(day,582,case['side'],legs,rows,['synthetic_ffill','model_spread_wing_continuity'],state,open_only=True)
        self.assertTrue(state['retained_repair_calibration'])
        self.assertLessEqual(abs(marked[0][0]-marked[1][0]-18.2),.42*.8+1e-7)
        self.assertEqual(state['curve',0,1],(22618.5,18.2,50))

    def test_ui_strategy_change_sets_defaults_then_retains_user_edits(self):
        from streamlit.testing.v1 import AppTest
        app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=60).run()
        app.selectbox(key='bt_strategy').select('Expiry Day').run()
        self.assertFalse(app.exception)
        self.assertEqual(app.number_input(key='bt_expiry_tp').value,25)
        self.assertEqual(app.number_input(key='bt_expiry_sl').value,60)
        self.assertEqual(app.number_input(key='bt_minimum_premium').value,70)
        self.assertEqual(app.selectbox(key='bt_allocation').value,'50%')
        app.number_input(key='bt_expiry_sl').set_value(35).run()
        self.assertEqual(app.number_input(key='bt_expiry_sl').value,35)


if __name__=='__main__':unittest.main()
