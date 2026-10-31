"""Observed-to-modeled spread transitions must not manufacture profits."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import pandas as pd

from quant_backtester import Config, simulate_trade
from quant_book import bounded_spread_rows
from quant_option_model import MissingObservedPrice
from spread_regression_fixtures import CASES


def sample(number):
    case=CASES[number];option=case['side'].split('_')[1]
    option='PE' if option=='PUT' else 'CE'
    day=dict(timestamp=pd.Timestamp(case['date'],tz='Asia/Kolkata'),prices={},options={},rolling={},option_meta={})
    for at,op,cl,atm,quotes in case['bars']:
        day['prices'][at]=(op,max(op,cl),min(op,cl),cl,0)
        for mon,k in [('ATM-1',atm-50),('ATM',atm),('ATM+1',atm+50)]:
            day['rolling'].setdefault((option,mon),{})[at]=k
        for k,a,b in quotes:day['options'][at,option,k]=(a,max(a,b),min(a,b),b,100)
    return case,day,[dict(side=option,strike=k,ratio=r) for k,r in zip(case['strikes'],(1,-1))]


class SpreadContinuityTests(unittest.TestCase):
    def test_all_six_saved_trade_transitions_are_continuous(self):
        for number in CASES:
            with self.subTest(number=number):
                case,day,legs=sample(number);state={};bounded_count=0
                for at,_,_,_,_ in case['bars']:
                    rows=[day['options'].get((at,l['side'],l['strike'])) for l in legs]
                    sources=['observed' if r else 'model_spread_wing_continuity' for r in rows]
                    previous=state.get((0,1))
                    actual,bounded=bounded_spread_rows(day,at,case['side'],legs,
                        [r or (.05,)*4+(0,) for r in rows],sources,state)
                    if bounded:
                        bounded_count+=1
                        value=actual[0][0]-actual[1][0];close=actual[0][3]-actual[1][3]
                        self.assertLessEqual(abs(value-previous[0]),.25*abs(day['prices'][at][0]-previous[1])+1e-8)
                        self.assertLessEqual(abs(close-value),.25*abs(day['prices'][at][3]-day['prices'][at][0])+1e-8)
                        self.assertLess(value,40,number)
                        for r,a in zip(rows,actual):
                            if r is not None:self.assertEqual(r,a)
                self.assertGreater(bounded_count,0)

    def test_trade_70_stop_at_0941_fills_continuously_at_0942(self):
        case,day,legs=sample(70)
        c=Config(strategy='book_vertical_spread',start=case['date'],end='2026-09-30',timeframe='3m',
                 capital=100000,capital_usage_pct=25,sizing_mode='allocation',sl_pct=15,target_pct=25,
                 exit_mode='target_eod',slippage=.5,minimum_premium=50)
        data=SimpleNamespace(days={case['date']:day},mcx=False,instrument='NIFTY',has_calendar=False,expiry_calendar=set())
        data.price=lambda d,t,s,k:d['options'].get((t,s,k))
        with patch('quant_book.continuation_candle',side_effect=AssertionError('Independent leg fallback is forbidden')):
            trade,path=simulate_trade(data,c,case['date'],(579,case['side'],22600,None,1),'70')
        self.assertEqual(trade['exit_reason'],'stop_loss')
        self.assertEqual(trade['exit_time'].strftime('%H:%M'),'09:42')
        self.assertAlmostEqual(trade['entry_price'],22.75)
        self.assertAlmostEqual(path[-2]['close'],18.2)
        self.assertAlmostEqual(trade['raw_exit_price'],18.0)
        self.assertAlmostEqual(trade['exit_price'],17.0)
        self.assertLess(trade['net_pnl'],0)
        self.assertTrue(trade['modeled_wing_fallback'])
        # Fill uses previous close/current open, never current close or future extrema.
        day['prices'][582]=(22619.3,99999,1,23000,0)
        for key,row in list(day['options'].items()):
            if key[0]==582: day['options'][key]=(row[0],99999,.05,200,100)
        again,_=simulate_trade(data,c,case['date'],(579,case['side'],22600,None,1),'70')
        self.assertEqual(again['raw_exit_price'],trade['raw_exit_price'])

    def test_trade_73_two_minute_move_does_not_become_cap_win(self):
        case,day,legs=sample(73);state={}
        for at,_,_,_,_ in case['bars']:
            rows=[day['options'].get((at,l['side'],l['strike'])) for l in legs]
            result,_=bounded_spread_rows(day,at,case['side'],legs,[r or (.05,)*4+(0,) for r in rows],
                ['observed' if r else 'model_spread_wing_continuity' for r in rows],state)
            if at==569:
                self.assertLess(result[0][0]-result[1][0],19)
                self.assertGreater(result[0][0]-result[1][0],14)

    def test_calls_puts_and_both_missing_use_bounded_adjacent_blend(self):
        for option,direction in [('CE',1),('PE',-1)]:
            for move in (0,20,50,-20,-50):
                with self.subTest(option=option,move=move):
                    legs=[dict(side=option,strike=100,ratio=1),dict(side=option,strike=100+direction*50,ratio=-1)]
                    day={'prices':{600:(100+move,)*4+(0,)},'rolling':{(option,'ATM'):{600:200},
                        (option,'ATM+1' if option=='CE' else 'ATM-1'):{600:200+direction*50}},
                        'options':{(600,option,200):(90,)*4+(1,),(600,option,200+direction*50):(40,)*4+(1,)}}
                    rows,_=bounded_spread_rows(day,600,'BULL_CALL_SPREAD' if option=='CE' else 'BEAR_PUT_SPREAD',legs,
                        [(500,)*4+(0,),(.05,)*4+(0,)],['model_spread_wing_continuity']*2,{(0,1):(20,100,50)})
                    value=rows[0][0]-rows[1][0]
                    self.assertLessEqual(abs(value-20),.25*abs(move)+1e-8)
                    self.assertTrue(all(r[0]>=.05 for r in rows))
                    if move:self.assertGreaterEqual((value-20)*direction*(1 if move>0 else -1),.15*abs(move)-1e-8)

    def test_adjacent_quotes_are_required_and_synthetic_is_not_observed(self):
        case,day,legs=sample(70)
        for synthetic in (False,True):
            d={**day,'options':dict(day['options'])}
            if synthetic:d['price_sources']={(582,'PE',22650):'synthetic_ffill'}
            else:del d['options'][582,'PE',22650]
            with self.assertRaises(MissingObservedPrice):
                bounded_spread_rows(d,582,case['side'],legs,[day['options'][582,'PE',22600],(.05,)*4+(0,)],
                    ['observed','model_spread_wing_continuity'],{(0,1):(18.2,22618.5,36.9)})

    def test_butterfly_uses_continuous_call_and_put_pairs(self):
        legs=[dict(side=s,strike=k,ratio=r) for s,k,r in [('CE',100,-1),('PE',100,-1),('CE',150,1),('PE',50,1)]]
        day={'prices':{600:(120,)*4+(0,)},'rolling':{},'options':{}}
        for s,far in [('CE',250),('PE',150)]:
            day['rolling'][s,'ATM']={600:200}
            day['rolling'][s,'ATM+1' if s=='CE' else 'ATM-1']={600:far}
            day['options'][600,s,200]=(80,)*4+(1,)
            day['options'][600,s,far]=(60,)*4+(1,)
        state={(0,2):(20,100,60),(1,3):(20,100,60)}
        rows,_=bounded_spread_rows(day,600,'IRON_BUTTERFLY',legs,[(.05,)*4+(0,)]*4,
                                  ['model_spread_wing_continuity']*4,state)
        call=rows[0][0]-rows[2][0];put=rows[1][0]-rows[3][0]
        self.assertTrue(23<=call<=25);self.assertTrue(15<=put<=17)
        self.assertLessEqual(call+put,50)

    def test_sensex_misplaced_offset_uses_observed_same_width_pair(self):
        # 2025-11-12 10:58: ATM+1 unexpectedly labels 85000, 600 above ATM.
        # The 84300/84400 pair is an observed 100-point vertical; 84400/85000 is not.
        legs=[dict(side='CE',strike=84500,ratio=1),dict(side='CE',strike=84600,ratio=-1)]
        day={'prices':{658:(84410,)*4+(0,)},'rolling':{},'options':{}}
        for mon,k,p in [('ATM-1',84300,335.75),('ATM',84400,275.35),('ATM+1',85000,169.95)]:
            day['rolling']['CE',mon]={658:k};day['options'][658,'CE',k]=(p,)*4+(1,)
        rows,_=bounded_spread_rows(day,658,'BULL_CALL_SPREAD',legs,[(.05,)*4+(0,)]*2,
            ['model_spread_continuity']*2,{(0,1):(50,84420,182)})
        self.assertAlmostEqual(rows[0][0]-rows[1][0],47.5)
        self.assertGreater(rows[0][0]-rows[1][0],0)


if __name__=='__main__':unittest.main()
