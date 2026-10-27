"""Offline fixtures: packet layout, causality, virtual risk and restart recovery."""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
import csv
import json
import struct
import unittest

import pandas as pd
from paper_feed import Asset, Tick, IST, FeedError, FEED_IST_OFFSET, decode_packets, warmup
from paper_engine import PaperAccount, AccountLock, MinuteBook
from quant_backtester import Config, HIGH_STRATEGIES


NOW=datetime(2026,10,23,17,35,tzinfo=IST).timestamp()
ASSET=Asset('CRUDEOIL',5,573422,'2026-11-19',100)


def config(**changes):
    return replace(Config(instrument='CRUDEOIL',strategy=HIGH_STRATEGIES[3],timeframe='5m',
        capital=500000,capital_usage_pct=50,futures_margin_pct=20,sizing_mode='fixed_lots',
        lots=1,slippage=1,atr_sl=2,reward_risk=3,atr_trail_jump=.5,
        daily_profit_target=0,daily_loss_limit=0,entry_start='17:30',entry_end='21:30'),**changes)


def tick(price=100,at=NOW,**changes):
    return replace(Tick(ASSET.key,price,at,at,1000,10000,price-1,price+1,10,10),**changes)


def packet(code=8,price=100,at=NOW):
    size={2:16,4:50,8:162}[code]; p=bytearray(size)
    struct.pack_into('<BHBI',p,0,code,size,5,ASSET.security_id)
    struct.pack_into('<f',p,8,price)
    struct.pack_into('<I',p,12 if code==2 else 14,int(at)+FEED_IST_OFFSET)
    if code!=2: struct.pack_into('<I',p,22,12345)
    if code==8:
        struct.pack_into('<I',p,34,98765)
        struct.pack_into('<IIHHff',p,62,10,20,2,3,price-1,price+1)
    return bytes(p)


class FeedTests(unittest.TestCase):
    def test_all_quote_layouts_and_coalesced_frames(self):
        result=decode_packets(b''.join(packet(c) for c in (2,4,8)),NOW)
        self.assertEqual(len(result),3)
        for t in result:
            self.assertEqual(t.exchange_time,NOW); self.assertEqual(t.price,100)
            self.assertTrue(t.fresh(NOW)); self.assertEqual(t.wire_epoch,NOW+19800)
        self.assertEqual(result[2].volume,12345); self.assertEqual(result[2].oi,98765)
        self.assertEqual((result[2].bid,result[2].ask,result[2].bid_size,result[2].ask_size),(99,101,10,20))
        self.assertIsNone(result[0].volume)

    def test_bad_frames_and_disconnect_are_credential_free(self):
        for value in ('secret-bearing upstream error',b'bad',packet()[:-1]):
            with self.assertRaises(FeedError) as ctx: decode_packets(value,NOW)
            self.assertNotIn('secret',str(ctx.exception))
        with self.assertRaisesRegex(FeedError,'FEED_DISCONNECT_807'):
            decode_packets(struct.pack('<BHBIH',50,10,0,0,807),NOW)
        self.assertEqual(decode_packets(struct.pack('<BHBIfI',6,16,5,1,100,1000),NOW),[])

    def test_invalid_depth_nan_and_freshness(self):
        p=bytearray(packet()); struct.pack_into('<ff',p,74,103,101)
        t=decode_packets(bytes(p),NOW)[0]
        self.assertIsNone(t.bid); self.assertIsNone(t.ask)
        self.assertEqual(decode_packets(packet(price=float('nan')),NOW),[])
        self.assertFalse(t.fresh(NOW+16)); self.assertFalse(t.fresh(NOW-3))
        self.assertFalse(tick(at=NOW-86400,received_time=NOW).fresh(NOW))

    def test_ist_feed_conversion_across_midnight(self):
        at=datetime(2026,10,23,23,14,tzinfo=IST).timestamp()
        t=decode_packets(packet(at=at),at)[0]
        self.assertEqual(datetime.fromtimestamp(t.exchange_time,IST).strftime('%F %R'),'2026-10-23 23:14')

    @patch('paper_feed.requests.post')
    def test_warmup_only_completed_rest_minutes_and_readonly_route(self,post):
        body=dict(timestamp=[NOW-60,NOW,NOW+60],open=[100]*3,high=[101]*3,low=[99]*3,close=[100]*3,volume=[10]*3)
        post.return_value=Mock(status_code=200,json=lambda:body)
        rows=warmup('test-client','test-token',ASSET,NOW+3)
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['timestamp'],NOW-60)
        self.assertEqual(post.call_args.args[0],'https://api.dhan.co/v2/charts/intraday')
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertEqual(post.call_args.kwargs['json']['securityId'],str(ASSET.security_id))
        post.return_value=Mock(status_code=401)
        with self.assertRaisesRegex(FeedError,'WARMUP_HTTP_401'): warmup('a','b',ASSET,NOW)


class AccountTests(unittest.TestCase):
    def setUp(self): self.tmp=TemporaryDirectory(); self.root=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def account(self,**changes): return PaperAccount(self.root,config(**changes),ASSET,lambda _:None)
    def enter(self,a,side='LONG',at=NOW):
        local=datetime.fromtimestamp(at,IST)
        return a.enter((local.hour*60+local.minute,side,0,None,10),tick(at=at),at)

    def test_target_and_all_costs_reconcile_with_ledger(self):
        a=self.account(); self.assertTrue(self.enter(a))
        self.assertEqual(a.position['entry_price'],102)
        a.on_tick(tick(165,NOW+1),NOW+1)
        p=a.state['trades'][0]; self.assertEqual(p['exit_reason'],'target')
        self.assertEqual(p['exit_price'],163); self.assertEqual(p['gross_pnl'],6100)
        self.assertEqual(p['net_pnl'],p['gross_pnl']-p['total_cost'])
        self.assertEqual(p['brokerage'],40); self.assertGreater(p['total_cost'],40)
        self.assertAlmostEqual(a.equity,500000+p['net_pnl'])
        with (self.root/'ledger.csv').open() as f: ledger=list(csv.DictReader(f))
        self.assertEqual(ledger[0]['status'],'CLOSED'); self.assertEqual(float(ledger[0]['net_pnl']),p['net_pnl'])

    def test_stop_gap_fills_actual_quote_not_stop_price(self):
        a=self.account(); self.enter(a); self.assertEqual(a.position['stop'],82)
        a.on_tick(tick(70,NOW+1),NOW+1)
        p=a.state['trades'][0]; self.assertEqual(p['exit_reason'],'stop_loss')
        self.assertEqual(p['exit_price'],68)

    def test_short_stop_and_target(self):
        a=self.account(); self.enter(a,'SHORT'); self.assertEqual(a.position['entry_price'],98)
        a.on_tick(tick(120,NOW+1),NOW+1)
        self.assertEqual(a.state['trades'][0]['exit_reason'],'stop_loss')
        self.assertEqual(a.state['trades'][0]['exit_price'],122)
        with TemporaryDirectory() as d:
            b=PaperAccount(d,config(),ASSET,lambda _:None); self.enter(b,'SHORT')
            b.on_tick(tick(35,NOW+1),NOW+1)
            self.assertEqual(b.state['trades'][0]['exit_reason'],'target')
            self.assertGreater(b.state['trades'][0]['net_pnl'],0)

    def test_step_trailing_is_monotonic_and_persists(self):
        a=self.account(); self.enter(a)
        a.on_tick(tick(111,NOW+1),NOW+1); self.assertEqual(a.position['stop'],82)
        a.on_tick(tick(122,NOW+2),NOW+2); self.assertEqual(a.position['stop'],102)
        b=self.account(); self.assertEqual(b.position['stop'],102)
        b.on_tick(tick(115,NOW+3),NOW+3); self.assertEqual(b.position['stop'],102)
        b.on_tick(tick(100,NOW+4),NOW+4)
        self.assertEqual(b.state['trades'][0]['exit_reason'],'trailing_stop')

    def test_daily_net_profit_lock_exits_and_blocks_reentry(self):
        a=self.account(daily_profit_target=1000,max_trades_day=2,strategy=HIGH_STRATEGIES[5])
        self.enter(a); a.on_tick(tick(115,NOW+1),NOW+1)
        self.assertIsNone(a.position)
        self.assertEqual(a.state['trades'][0]['exit_reason'],'daily_profit_target')
        self.assertGreaterEqual(a.realized_today(NOW),1000)
        self.assertFalse(self.enter(a,at=NOW+60))
        b=self.account(daily_profit_target=1000,max_trades_day=2,strategy=HIGH_STRATEGIES[5])
        self.assertEqual(b.session(NOW)['lock'],'daily_profit_target')
        self.assertFalse(self.enter(b,at=NOW+120))

    def test_daily_loss_cutoff_uses_unrealized_net_costs(self):
        a=self.account(daily_loss_limit=500); self.enter(a)
        a.on_tick(tick(99,NOW+1),NOW+1)
        p=a.state['trades'][0]
        self.assertEqual(p['exit_reason'],'daily_loss_limit')
        self.assertLessEqual(p['net_pnl'],-500)
        self.assertFalse(self.enter(a,at=NOW+60))

    def test_squareoff_without_quote_stays_pending_until_fresh(self):
        a=self.account(); self.enter(a)
        end=datetime(2026,10,23,23,15,tzinfo=IST).timestamp()
        a.heartbeat(end); self.assertEqual(a.position['pending_exit'],'square_off')
        a.on_tick(tick(100,NOW),end); self.assertIsNotNone(a.position)
        b=self.account(); b.on_tick(tick(110,end),end)
        self.assertIsNone(b.position); self.assertEqual(b.state['trades'][0]['exit_reason'],'square_off')

    def test_wrong_contract_stale_out_of_order_and_old_signal_rejected(self):
        a=self.account(); self.enter(a)
        a.on_tick(tick(70,NOW+1,key='5:999'),NOW+1); self.assertIsNotNone(a.position)
        a.on_tick(tick(70,NOW-1),NOW+1); self.assertIsNotNone(a.position)
        a.on_tick(tick(70,NOW+1),NOW+30); self.assertIsNotNone(a.position)
        with TemporaryDirectory() as d:
            b=PaperAccount(d,config(),ASSET,lambda _:None)
            self.assertFalse(b.enter((1050,'LONG',0,None,10),tick(),NOW))

    def test_contract_identity_survives_roll_and_policy_change_rejected(self):
        a=self.account(); self.enter(a)
        future=replace(ASSET,security_id=999999,expiry='2026-12-18')
        b=PaperAccount(self.root,config(),future,lambda _:None)
        self.assertEqual(b.position['security_id'],ASSET.security_id)
        b.on_tick(tick(165,NOW+1,key=future.key),NOW+1); self.assertIsNotNone(b.position)
        b.on_tick(tick(165,NOW+2),NOW+2); self.assertIsNone(b.position)
        with self.assertRaises(ValueError): self.account(capital=600000)

    def test_allocation_cap_and_h4_daily_cap(self):
        a=self.account(capital=1000,capital_usage_pct=25)
        self.assertFalse(self.enter(a)); self.assertFalse(a.state['trades'])
        with TemporaryDirectory() as d:
            b=PaperAccount(d,config(max_trades_day=2),ASSET,lambda _:None)
            self.enter(b); b.on_tick(tick(165,NOW+1),NOW+1)
            self.assertFalse(self.enter(b,at=NOW+60))

    def test_account_lock_and_idempotent_recovery(self):
        with AccountLock(self.root):
            with self.assertRaises(ValueError):
                with AccountLock(self.root): pass
        with AccountLock(self.root): pass
        a=self.account(); self.enter(a)
        b=self.account(); self.assertFalse(self.enter(b))
        self.assertEqual(len(b.state['trades']),1)


class CandleTests(unittest.TestCase):
    def test_partial_gap_and_cumulative_volume_handling(self):
        b=MinuteBook(); start=NOW
        for sec in range(0,121,10): b.ingest(tick(100+sec/100,start+sec,volume=1000+sec))
        self.assertNotIn(int(start),b.rows)  # first partially observed minute
        self.assertEqual(b.rows[int(start)+60]['volume'],60)
        b.disconnect()
        b.ingest(tick(110,start+130,volume=1130))
        b.ingest(tick(110,start+180,volume=1180))
        self.assertNotIn(int(start)+120,b.rows)

    def test_seed_does_not_include_current_bar_or_other_date(self):
        b=MinuteBook()
        rows=[dict(timestamp=t,open=100,high=101,low=99,close=100,volume=10) for t in (NOW-86400,NOW-60,NOW,NOW+60)]
        b.seed(rows,NOW+1)
        self.assertEqual(list(b.rows),[NOW-60])
        self.assertIsNone(b.signal(config(),NOW+1))

    def test_h4_adapter_positive_completed_bar_and_gap_rejection(self):
        at=datetime(2026,10,23,17,30,tzinfo=IST).timestamp()
        start=at-3600
        rows=[dict(timestamp=start+i*60,open=100,high=110 if i<30 else 105,
            low=90 if i<30 else 95,close=100,volume=100) for i in range(60)]
        bars=pd.DataFrame([dict(timestamp=pd.Timestamp(start+i*300,unit='s',tz='UTC').tz_convert(IST),
            open=105,close=110,high=111,low=104,volume=100,adx=30,atr=10) for i in range(12)])
        b=MinuteBook(); b.seed(rows,at)
        with patch('paper_engine.make_bars',return_value=bars):
            self.assertEqual(b.signal(config(),at)[:2],(1050,'LONG'))
            self.assertIsNone(b.signal(config(),at+1))  # one signal decision/bar
            b=MinuteBook(); b.seed(rows[:-1],at)
            self.assertIsNone(b.signal(config(),at))


if __name__=='__main__': unittest.main()
