"""Audit regressions: economics, causal activity, warm-up and execution limits."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from quant_backtester import Config, ResearchData, research_indicators, research_events, run_backtest
from quant_book import basket_allocation, activity, bounded_spread_rows
from quant_strategy_models import basket_slippage, spread_curve, activity_frame, fresh_pullback
from test_quant_book import fixture, DATE
from test_quant_expiry import fixture as expiry_fixture, PRE, EXP
from test_quant_strategy_workflow import signal_fixture, config


class StrategyAuditTests(unittest.TestCase):
    def test_inconsistent_observed_butterfly_pair_fails_without_fabricating_quotes(self):
        from quant_option_model import MissingObservedPrice
        legs=[dict(side=s,strike=k) for s,k in [('CE',100),('PE',100),('CE',150),('PE',50)]]
        # A 51-point liability exceeds both the wing and the small-noise tolerance.
        rows=[(.05,)*4+(0,),(61,)*4+(100,),(.05,)*4+(0,),(10,)*4+(100,)]
        day=dict(timestamp='2025-10-28',prices={914:(90,)*4+(0,)},
            rolling={('CE','ATM'):{914:50},('CE','ATM+1'):{914:100}},
            options={(914,'CE',50):(60,)*4+(100,),(914,'CE',100):(10,)*4+(100,)})
        with self.assertRaisesRegex(MissingObservedPrice,'2025-10-28 minute 914.*wing bound'):
            bounded_spread_rows(day,914,'IRON_BUTTERFLY',legs,rows,
                ['model_spread_wing_continuity','observed','model_spread_wing_continuity','observed'],
                {(0,2):(0,90,.05),(1,3):(49,90,10)})
        self.assertEqual(rows[1][0],61);self.assertEqual(rows[3][0],10)

    def test_butterfly_open_exit_does_not_inspect_later_incompatible_close(self):
        # NIFTY 2026-09-08 15:15: observed central CE later falls to 6,
        # below the last 6.244 pair mark, despite unchanged recorded spot.
        legs=[dict(side=s,strike=k) for s,k in [('CE',100),('PE',100),('CE',150),('PE',50)]]
        rows=[(14.65,14.65,5.45,6,100),(60,60,60,60,100),(.05,)*4+(0,),(30,)*4+(100,)]
        day=dict(prices={915:(90,90,90,90,0)},rolling={('CE','ATM'):{915:50},('CE','ATM+1'):{915:100}},
                 options={(915,'CE',50):(60,)*4+(100,),(915,'CE',100):rows[0]})
        state={(0,2):(6.244,90,8.406),(1,3):(30,90,30)}
        marked,_=bounded_spread_rows(day,915,'IRON_BUTTERFLY',legs,rows,
            ['observed','observed','model_spread_wing_continuity','observed'],state,open_only=True)
        self.assertAlmostEqual(marked[0][0]-marked[2][0],6.244)
        self.assertEqual(marked[0][0],14.65)
        self.assertTrue(all(r[0]==r[3] for r in marked))

    def test_inconsistent_observed_calibration_rejected_without_projecting_wing(self):
        legs=[dict(side=s,strike=k) for s,k in [('CE',100),('PE',100),('CE',150),('PE',50)]]
        rows=[(6,)*4+(100,),(60,)*4+(100,),(.05,)*4+(0,),(30,)*4+(100,)]
        day=dict(prices={900:(90,)*4+(0,)},rolling={('CE','ATM'):{900:50},('CE','ATM+1'):{900:100}},
                 options={(900,'CE',50):(60,)*4+(100,),(900,'CE',100):rows[0]})
        state={(0,2):(6.244,90,8.406),(1,3):(30,90,30)}
        from quant_book import InvalidBasketObservation
        with self.assertRaises(InvalidBasketObservation) as caught:
            bounded_spread_rows(day,900,'IRON_BUTTERFLY',legs,rows,
                ['observed','observed','model_spread_wing_continuity','observed'],state)
        self.assertEqual(caught.exception.evidence['phase'],'continuity_calibration')
        self.assertEqual(caught.exception.evidence['raw_basket_value'],54)
        self.assertEqual(day['options'][900,'CE',50][0],60)
        self.assertEqual(rows[0][0],6)

    def test_sigmoid_anchor_intrinsic_floor_direction_and_width(self):
        for direction in (-1,1):
            value=lambda move:spread_curve(100+direction*move,100,20,100,50,direction,20)
            self.assertEqual(value(0),20)
            prior=.05
            for move in np.linspace(-500,500,1001):
                v=value(move)
                self.assertTrue(.05<=v<=49.95)
                self.assertGreaterEqual(v+1e-8,prior);prior=v
                if move>0:self.assertGreaterEqual(v+1e-8,min(49.95,20+.32*move,.85*min(move,50)))
            self.assertTrue(25.6<value(20)<28.4)
            self.assertGreater(value(50),36)
            self.assertLess(value(50),49.95)
            # The local slope increases smoothly from ATM into the money.
            slope=lambda move:(value(move+.001)-value(move))/.001
            self.assertAlmostEqual(slope(0),.28,places=4)
            self.assertTrue(.40<slope(40)<.42)

    def test_proportional_slippage_rounding_and_user_zero(self):
        c=Config(slippage=.5)
        for price,expected in [(1,.05),(6,.10),(10,.15),(20,.25),(40,.5),(200,.5)]:
            self.assertAlmostEqual(basket_slippage(c,price),expected)
            self.assertEqual(basket_slippage(replace(c,slippage=0),price),0)
        self.assertEqual(basket_slippage(replace(c,slippage=.1),100),.1)
        self.assertEqual(basket_slippage(replace(c,slippage=2),100),.5)

    def test_butterfly_25000_reserves_full_100_point_wing_not_375_loss(self):
        c=Config(instrument='SENSEX',strategy='book_iron_butterfly',sizing_mode='allocation',lot_size=20)
        legs=[dict(strike=k,ratio=r,entry_price=p) for k,r,p in [(80000,-1,60),(80000,-1,60),(80100,1,11.875),(79900,1,11.875)]]
        qty,allocated,reserve,debit=basket_allocation(c,DATE,legs,25000)
        self.assertEqual(debit,-96.25);self.assertLessEqual(qty,240)
        self.assertEqual(allocated,100*qty)
        self.assertGreater(reserve,160);self.assertLessEqual(allocated+reserve,25000)
        self.assertGreater(allocated,(100+debit)*qty)

    def test_option_activity_weights_index_vwap_without_mutating_lake(self):
        _,_,day=fixture()
        day['spot']=pd.DataFrame([dict(timestamp=day['timestamp']+pd.Timedelta(minutes=t),open=p,high=p,low=p,close=p,volume=0) for t,p in [(600,100),(601,110)]])
        original=day['spot'].copy(deep=True)
        for side in ('CE','PE'):day['options'][601,side,100]=(6,6,6,6,300)
        result=research_indicators(activity_frame(day))
        self.assertEqual(result.volume.tolist(),[200,600])
        self.assertAlmostEqual(result.vwap.iloc[-1],107.5)
        self.assertTrue(result.activity_proxy.all());self.assertFalse(result.vwap_proxy.any())
        pd.testing.assert_frame_equal(original,day['spot'])
        # Future activity cannot change prior VWAP.
        day['options'][601,'CE',100]=(6,6,6,6,999999)
        self.assertEqual(research_indicators(activity_frame(day)).vwap.iloc[0],100)

    def test_activity_missing_or_synthetic_leg_is_not_invented(self):
        _,_,day=fixture()
        day['spot']=pd.DataFrame([dict(timestamp=day['timestamp']+pd.Timedelta(minutes=600),open=100,high=100,low=100,close=100,volume=0)])
        day['price_sources']={(600,'PE',100):'synthetic_ffill'}
        result=activity_frame(day)
        self.assertEqual(result.volume.iloc[0],0);self.assertFalse(result.activity_proxy.iloc[0])

    def test_momentum_t_plus_two_warmup_and_changing_atm_strikes(self):
        for tf in ('5m','15m'):
            c,d,day=fixture('book_vol_oi_momentum');c=replace(c,timeframe=tf)
            n=int(tf[:-1]);count=c.momentum_lookback+2;at=555+count*n
            close=100*np.exp(np.cumsum([0]+[.001 if i%2 else .002 for i in range(1,count)]))
            bars=pd.DataFrame(dict(timestamp=pd.date_range(day['timestamp']+pd.Timedelta(minutes=555),periods=count,freq=f'{n}min'),
                close=close,volume=0,ema9=100,ema21=99,vwap=100,adx=np.nan,atr=1,atr14=1))
            d.features=lambda tf:{DATE:bars}
            day['options']={};day['option_meta']={}
            for minute in range(555,at):
                k=100+minute  # No fixed strike survives even two minutes.
                day['rolling']['CE','ATM'][minute]=k
                last=minute>=at-n
                day['options'][minute,'CE',k]=(6,6,6,6,300 if last else 100)
                day['option_meta'][minute,'CE',k]=(2000 if last else 1000,)
            events=research_events(d,c,DATE)
            self.assertEqual(len(events),1)
            self.assertEqual(events[0][:3],(at,'CE',100+at-1))
            a=activity(d,day,bars,count-1,at,'CE',100+at-1,c)
            self.assertEqual(a['signal_volume_basis'],'rolling_atm_option')
            self.assertAlmostEqual(a['signal_log_volume'],np.log(3))
            self.assertAlmostEqual(a['signal_log_oi'],np.log(2))

    def test_warmup_carries_only_across_consecutive_eligible_sessions(self):
        for dates,carry in [(['2026-10-19','2026-10-20'],True),(['2026-10-19','2026-10-21'],False)]:
            data=ResearchData.__new__(ResearchData)
            data.mcx=False;data.instrument='NIFTY';data._features={};data.days={}
            data.available_days=['2026-10-19','2026-10-20','2026-10-21']
            for date in dates:
                timestamps=pd.date_range(date+' 09:15',periods=375,freq='min',tz='Asia/Kolkata')
                price=np.arange(375)*.01+100
                data.days[date]=dict(contract='NIFTY',spot=pd.DataFrame(dict(timestamp=timestamps,open=price,high=price+1,low=price-1,close=price,volume=100)))
            second=data.research_features('5m')[dates[-1]]
            self.assertEqual(bool(pd.notna(second.ema21.iloc[0])),carry)
            self.assertAlmostEqual(second.vwap.iloc[0],(second.high.iloc[0]+second.low.iloc[0]+second.close.iloc[0])/3)

    def test_fresh_pullback_rejects_flat_adx_wrong_body_and_same_wave(self):
        prev=pd.Series(dict(low=99,high=101,close=100,ema9=100,adx=20))
        row=pd.Series(dict(open=100,close=102,ema9=101,adx=21))
        self.assertTrue(fresh_pullback(prev,row,True))
        row.adx=20;self.assertFalse(fresh_pullback(prev,row,True))
        row.adx=21;row.open=103;self.assertFalse(fresh_pullback(prev,row,True))
        row.open=100;prev.low=101;prev.close=102
        self.assertFalse(fresh_pullback(prev,row,True))

    def test_three_bar_stop_cooldown_and_max_two_entries(self):
        for strategy,side in [('book_vertical_spread','BULL_CALL_SPREAD'),('vwap_ema_pullback','CE')]:
            c,d,day=fixture(strategy,side)
            day['options'][600,'CE',100]=(6,6,5,5,100)
            d.events=lambda c,date:[(t,side,100,None,1) for t in (600,602,615,616,700)]
            trades=run_backtest(c,d,persist=False)['_frames']['trades']
            expected=616 if strategy=='book_vertical_spread' else 615
            self.assertEqual([t.hour*60+t.minute for t in trades.entry_time],[600,expected])
            self.assertGreaterEqual((trades.iloc[1].entry_time-trades.iloc[0].exit_time).total_seconds(),15*60)
            self.assertEqual(trades.iloc[0].exit_reason,'stop_loss')

    def test_strap_max_one_entry_even_after_immediate_loss(self):
        c,d,day=fixture('book_strap_strip','STRAP')
        day['options'][600,'CE',100]=(6,6,1,1,100)
        d.events=lambda c,date:[(t,'STRAP',100,None,1) for t in (600,610,620)]
        trades=run_backtest(c,d,persist=False)['_frames']['trades']
        self.assertEqual(len(trades),1);self.assertEqual(trades.iloc[0].exit_reason,'stop_loss')

    def test_mcx_body_alignment_and_evening_end(self):
        c=config('NATURALGAS','mcx_evening_breakout')
        d,bars,at=signal_fixture(c)
        self.assertTrue(research_events(d,c,DATE))
        bars.loc[len(bars)-1,'ema21']=96
        self.assertFalse(research_events(d,c,DATE))
        bars.loc[len(bars)-1,'ema21']=98
        bars['timestamp']+=pd.Timedelta(hours=5)
        self.assertFalse(research_events(d,c,DATE))

    def test_expiry_wing_time_value_decays_only_modeled_leg(self):
        c,d=expiry_fixture(exit_mode='end_of_day',sl_pct=99)
        del d.days[EXP]['options'][920,'PE',100]
        with patch('quant_expiry.continuation_candle',return_value=((10,10,10,10,0),dict(price_source='model_stateful_wing_continuity',modeled_wing_fallback=True))) as model:
            result=run_backtest(c,d,persist=False)
        trade=result['_frames']['trades'].iloc[0];legs=json.loads(trade.legs)
        self.assertAlmostEqual(trade.raw_exit_price,110)  # theta is applied once, inside the model
        self.assertTrue(model.call_args.kwargs['expiry_decay'])
        self.assertIn('state',model.call_args.kwargs)
        self.assertEqual(legs[0]['exit_price'],100)
        self.assertEqual(legs[1]['exit_price_source'],'model_stateful_wing_continuity')
        self.assertEqual(trade.entry_time.strftime('%Y-%m-%d %H:%M'),PRE+' 15:26')


if __name__=='__main__':unittest.main()
