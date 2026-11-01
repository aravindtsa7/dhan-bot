"""Explicit EOD suppresses targets, never protective stops; no broker/feed."""
from dataclasses import replace
from datetime import datetime
from tempfile import TemporaryDirectory
import unittest
from quant_backtester import simulate_trade
from quant_book import leg_specs
from paper_basket import BasketPaperAccount
from paper_feed import Asset, Tick, IST
from test_quant_book import fixture, DATE


class ButterflyEodTests(unittest.TestCase):
    def config(self):
        c,d,day=fixture('book_iron_butterfly','IRON_BUTTERFLY')
        return replace(c,capital=100000,exit_mode='end_of_day',sl_pct=99,target_pct=25,trail_pct=0,slippage=0),d,day

    def test_backtest_eod_suppresses_ordinary_target(self):
        c,d,day=self.config()
        for t in range(601,921):
            for side in ('CE','PE'):day['options'][t,side,100]=(4,4,4,4,100)
        trade,path=simulate_trade(d,c,DATE,(600,'IRON_BUTTERFLY',100,None,1),'eod')
        self.assertTrue(any(p['close']>=p['target'] for p in path))
        self.assertEqual(trade['exit_reason'],'square_off')
        self.assertEqual(trade['exit_time'].hour*60+trade['exit_time'].minute,915)

    def test_backtest_eod_retains_protective_stop(self):
        c,d,day=self.config();c=replace(c,sl_pct=10)
        for side in ('CE','PE'):day['options'][601,side,100]=(7,7,7,7,100)
        trade,_=simulate_trade(d,c,DATE,(600,'IRON_BUTTERFLY',100,None,1),'stop')
        self.assertEqual(trade['exit_reason'],'stop_loss')
        self.assertEqual(trade['exit_time'].minute,2)

    def test_paper_eod_suppresses_target_and_retains_stop(self):
        for stop in (False,True):
            with self.subTest(stop=stop),TemporaryDirectory() as directory:
                c,_,day=self.config();c=replace(c,sl_pct=10 if stop else 99)
                now=datetime(2026,10,22,10,tzinfo=IST).timestamp()
                specs=leg_specs(day,600,'IRON_BUTTERFLY',100)
                assets=[Asset('NIFTY',2,100+i,'2026-10-27',10,l['strike'],l['side']) for i,l in enumerate(specs)]
                quotes={a.key:Tick(a.key,p,now,now) for a,p in zip(assets,[6,6,2,2])}
                account=BasketPaperAccount(directory,c,assets[0],lambda message:None)
                self.assertTrue(account.enter_basket((600,'IRON_BUTTERFLY',100,None,1),list(zip(specs,assets)),quotes,now))
                for asset,price in zip(assets,[7,7,2,2] if stop else [4,4,2,2]):
                    account.on_tick(Tick(asset.key,price,now+1,now+1),now+1)
                p=account.state['trades'][0]
                self.assertEqual(p['status'],'CLOSED' if stop else 'OPEN')
                if stop:self.assertEqual(p['exit_reason'],'stop_loss')

if __name__=='__main__':unittest.main()
