"""Regression for oversized spread marks from independently modeled wings."""
from dataclasses import replace
import json
import unittest

from quant_backtester import simulate_trade
from quant_book import bounded_spread_rows
from quant_option_model import MissingObservedPrice, intrinsic
from test_quant_book import fixture, DATE


class SpreadBoundTests(unittest.TestCase):
    def moved_fixture(self, side, jump, observed=None):
        strategy='book_iron_butterfly' if side=='IRON_BUTTERFLY' else 'book_vertical_spread'
        c,data,day=fixture(strategy,side)
        base=24550 if side=='BEAR_PUT_SPREAD' else 24650
        mapping={90:base-50,100:base,110:base+50}
        day['rolling']={key:{t:mapping[k] for t,k in stream.items()} for key,stream in day['rolling'].items()}
        day['options']={(t,s,mapping[k]):(30 if k==100 else 10,)*4+(100,)
                        for t,s,k in day['options']}
        for t in range(555,921):
            spot=base+(jump if t>=601 else 0)
            day['prices'][t]=(spot,spot+10,spot-10,spot,0)
            if t<601: continue
            for option in ('CE','PE'):
                for strike in mapping.values():
                    if (option,strike)==observed: continue
                    day['options'].pop((t,option,strike),None)
                for offset,k in [('ATM-1',spot-50),('ATM',spot),('ATM+1',spot+50)]:
                    day['rolling'][option,offset][t]=k
                    p=intrinsic(spot,k,option)+25
                    day['options'][t,option,k]=(p,)*4+(100,)
        return replace(c,sl_pct=99,target_pct=10000),data,day,base

    def check_trade(self,side,jump,observed=None,slippage=0):
        c,data,day,base=self.moved_fixture(side,jump,observed)
        c=replace(c,slippage=slippage)
        trade,path=simulate_trade(data,c,DATE,(600,side,base,None,1),'bounded')
        self.assertTrue(trade['modeled_wing_fallback'])
        self.assertTrue(trade['spread_bounds_applied'])
        low,high=(-50,0) if side=='IRON_BUTTERFLY' else (.05,49.95)
        for row in path:
            if not row['spread_bounds_applied']: continue
            for field in ('open','high','low','close','mark'):
                self.assertGreaterEqual(row[field],low-1e-8)
                self.assertLessEqual(row[field],high+1e-8)
        self.assertGreaterEqual(trade['raw_exit_price'],low-1e-8)
        self.assertLessEqual(trade['raw_exit_price'],high+1e-8)
        legs=json.loads(trade['legs'])
        self.assertAlmostEqual(trade['raw_exit_price'],sum(l['ratio']*l['raw_exit_price'] for l in legs))
        self.assertAlmostEqual(trade['gross_pnl'],sum(l['ratio']*(l['exit_price']-l['entry_price'])*trade['quantity'] for l in legs))
        self.assertAlmostEqual(trade['net_pnl'],trade['gross_before_slippage']-trade['slippage_cost']-trade['total_cost'])
        if observed:
            fixed=next(l for l in legs if (l['side'],l['strike'])==observed)
            self.assertFalse(fixed['spread_bounds_applied'])
            self.assertEqual(fixed['raw_exit_price'],day['options'][trade['exit_time'].hour*60+trade['exit_time'].minute,*observed][0])
        return c,data,base

    def test_bull_call_24650_24700_both_missing_jump_500(self):
        self.check_trade('BULL_CALL_SPREAD',500)

    def test_bear_put_24550_24500_both_missing_moves_500(self):
        for jump in (-500,500):
            with self.subTest(jump=jump): self.check_trade('BEAR_PUT_SPREAD',jump)

    def test_one_observed_vertical_leg_is_immutable(self):
        for side,observed,jump in [('BULL_CALL_SPREAD',('CE',24700),500),
                                    ('BULL_CALL_SPREAD',('CE',24650),-500),
                                    ('BEAR_PUT_SPREAD',('PE',24500),-500),
                                    ('BEAR_PUT_SPREAD',('PE',24550),500)]:
            with self.subTest(side=side,observed=observed): self.check_trade(side,jump,observed)

    def test_butterfly_one_width_budget_for_rally_and_crash(self):
        for jump in (-500,-50,50,500):
            with self.subTest(jump=jump): self.check_trade('IRON_BUTTERFLY',jump)

    def test_bounded_leg_prices_drive_slippage_costs_and_net_pnl(self):
        for side in ('BULL_CALL_SPREAD','BEAR_PUT_SPREAD','IRON_BUTTERFLY'):
            with self.subTest(side=side): self.check_trade(side,500,slippage=.5)

    def test_strict_observed_still_rejects_missing_leg(self):
        c,data,_,base=self.moved_fixture('BULL_CALL_SPREAD',500)
        with self.assertRaises(MissingObservedPrice):
            simulate_trade(data,replace(c,price_policy='strict_observed'),DATE,(600,'BULL_CALL_SPREAD',base,None,1),'strict')

    def test_observed_prices_and_non_spread_baskets_are_unchanged(self):
        rows=[(123,125,120,122,1),(1,2,.5,1,1)]
        legs=[dict(side='CE',strike=24650,ratio=1),dict(side='CE',strike=24700,ratio=-1)]
        for side,sources in [('BULL_CALL_SPREAD',['observed']*2),('STRAP',['model_intrinsic_wing_fallback']*2),
                             ('EXPIRY_DAY',['model_intrinsic_wing_fallback']*2)]:
            actual,changed=bounded_spread_rows({},600,side,legs,rows,sources)
            self.assertFalse(changed);self.assertEqual(actual,rows)

    def test_intrinsic_floor_and_saturated_tick_ceiling_at_open_and_close(self):
        legs=[dict(side='CE',strike=24650,ratio=1),dict(side='CE',strike=24700,ratio=-1)]
        rows=[(500,500,500,500,0),(20,20,20,20,1)]
        day={'prices':{600:(24675,25200,24600,25150,0)}}
        actual,changed=bounded_spread_rows(day,600,'BULL_CALL_SPREAD',legs,rows,
                                          ['model_intrinsic_wing_fallback','observed'])
        self.assertTrue(changed);self.assertEqual(actual[1],rows[1])
        self.assertGreaterEqual(actual[0][0]-actual[1][0],25)
        self.assertAlmostEqual(actual[0][3]-actual[1][3],49.95)


if __name__=='__main__': unittest.main()
