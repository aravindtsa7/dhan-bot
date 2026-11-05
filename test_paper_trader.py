"""Offline fixtures: packet layout, causality, virtual risk and restart recovery."""
from dataclasses import asdict, replace
from datetime import date, datetime
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
from paper_basket import BasketPaperAccount
from quant_backtester import Config, HIGH_STRATEGIES
from quant_terminal import STRATEGY_LABELS, build_config, form_values
from step6_paper_trader import (
    book_activity_confirmed,
    build_status_quotes,
    pending_deadline,
    quote_label,
    warmup_candidates,
)


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


class LivePaperGapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_deadline_and_parallel_warmup_candidates(self):
        minute_base = int(NOW) // 60 * 60
        self.assertEqual(pending_deadline(minute_base + 2), minute_base + 47)
        self.assertEqual(pending_deadline(minute_base + 14), minute_base + 55)
        candidates = [
            (f'CAND_{i}', Asset('NIFTY', 2, 41000 + i, '2026-10-29', 65, 24000 + i * 50, 'CE'))
            for i in range(6)
        ]
        with patch('step6_paper_trader.warmup', side_effect=lambda cid, tok, a, now: [{'security_id': a.security_id}]) as mocked:
            hist = warmup_candidates('cid', 'tok', candidates, NOW)
        self.assertEqual(mocked.call_count, 6)
        self.assertEqual(set(hist), {a.key for _, a in candidates})

    def test_book_activity_confirmed_maps_pe_and_strip_sides(self):
        captured = {}
        def fake_activity(data, day, bars, lookback, at, side, strike, c):
            captured['side'] = side
            return dict(signal_log_volume=1.0, signal_oi_available=False, signal_log_oi=0.0)
        rows = [
            dict(timestamp=NOW - 300 * (10 - i), open=100, high=102, low=99, close=101, volume=500, oi=1000 + i * 10)
            for i in range(10)
        ]
        c = config(instrument='NIFTY', strategy='book_vertical_spread', timeframe='5m')
        with patch('quant_book.activity', side_effect=fake_activity):
            self.assertTrue(book_activity_confirmed(rows, c, (615, 'PE', 0, None, 10), NOW))
            self.assertEqual(captured['side'], 'PE')
            self.assertTrue(book_activity_confirmed(rows, c, (615, 'STRIP', 0, None, 10), NOW))
            self.assertEqual(captured['side'], 'PE')
            self.assertTrue(book_activity_confirmed(rows, c, (615, 'CE', 0, None, 10), NOW))
            self.assertEqual(captured['side'], 'CE')

    def test_build_status_quotes_keeps_spot_and_option_legs_distinct(self):
        spot = Asset('NIFTY', 0, 13, '2099-12-31', 65)
        ce = Asset('NIFTY', 2, 51001, '2026-10-29', 65, 24000.0, 'CE')
        pe = Asset('NIFTY', 2, 51002, '2026-10-29', 65, 23800.0, 'PE')
        latest = {
            spot.key: Tick(spot.key, 24012.5, NOW, NOW),
            ce.key: Tick(ce.key, 145.25, NOW, NOW, 1000, 5000, 145.0, 145.5),
            pe.key: Tick(pe.key, 82.75, NOW, NOW, 900, 4200, 82.5, 83.0),
        }
        quotes, option_quotes, _ = build_status_quotes((spot, ce, pe), latest, NOW)
        self.assertEqual(quotes['NIFTY']['ltp'], 24012.5)
        self.assertEqual(quote_label(ce), 'NIFTY:51001')
        self.assertEqual(quotes['NIFTY:51001']['ltp'], 145.25)
        self.assertEqual(option_quotes['NIFTY:51002']['option_type'], 'PE')
        self.assertEqual(option_quotes['NIFTY:51002']['strike'], 23800.0)

    def test_async_leg_skew_skips_transient_violation_until_synchronized(self):
        legs = [
            Asset('NIFTY', 2, 61001, '2026-10-29', 65, 24000, 'CE'),
            Asset('NIFTY', 2, 61002, '2026-10-29', 65, 24100, 'CE'),
        ]
        c = replace(
            Config(
                instrument='NIFTY',
                strategy='book_vertical_spread',
                timeframe='5m',
                capital=500000,
                sizing_mode='fixed_lots',
                lots=1,
                slippage=0,
                entry_start='09:20',
                entry_end='15:10',
            )
        )
        at = datetime(2026, 10, 23, 10, 15, 28, tzinfo=IST).timestamp()
        local = datetime.fromtimestamp(at, IST)
        ev = (local.hour * 60 + local.minute, 'BULL_CALL_SPREAD', 0, None, 20)
        a = BasketPaperAccount(self.root, c, legs[0], lambda _: None, allow_async_skew=True)
        quotes = {
            legs[0].key: Tick(legs[0].key, 120.0, at, at, wire_epoch=at + FEED_IST_OFFSET),
            legs[1].key: Tick(legs[1].key, 60.0, at, at, wire_epoch=at + FEED_IST_OFFSET),
        }
        resolved = [
            (dict(side='CE', strike=24000, ratio=1), legs[0]),
            (dict(side='CE', strike=24100, ratio=-1), legs[1]),
        ]
        # Pending basket fill at :28s succeeds and enters cleanly
        self.assertTrue(a.enter_basket(ev, resolved, quotes, at))
        # Asynchronous tick where short leg briefly jumps above long leg across different timestamps does not freeze account
        a.on_tick(Tick(legs[1].key, 130.0, at + 1, at + 1, wire_epoch=at + 1 + FEED_IST_OFFSET), at + 1)
        self.assertEqual(a.state.get('account_status', 'ACTIVE'), 'ACTIVE')
        self.assertIsNotNone(a.position)
        # Once the long leg catches up at at + 1, MTM resumes cleanly
        a.on_tick(Tick(legs[0].key, 195.0, at + 1, at + 1, wire_epoch=at + 1 + FEED_IST_OFFSET), at + 1)
        self.assertEqual(a.state.get('account_status', 'ACTIVE'), 'ACTIVE')

    def test_quiet_minute_resilience_and_ui_daily_limits_wiring(self):
        at = datetime(2026, 10, 23, 17, 30, tzinfo=IST).timestamp()
        start = at - 3600
        # Drop one interior minute (i == 25) to simulate a quiet zero-tick minute
        rows = [
            dict(timestamp=start + i * 60, open=100, high=110 if i < 30 else 105, low=90 if i < 30 else 95, close=100, volume=100)
            for i in range(60)
            if i != 25
        ]
        bars = pd.DataFrame([
            dict(timestamp=pd.Timestamp(start + i * 300, unit='s', tz='UTC').tz_convert(IST), open=105, close=110, high=111, low=104, volume=100, adx=30, atr=10)
            for i in range(12)
        ])
        b = MinuteBook(allow_quiet_minutes=True)
        b.seed(rows, at)
        self.assertIn(int(start + 25 * 60), b.rows)
        self.assertEqual(b.rows[int(start + 25 * 60)]['volume'], 0.0)
        # Also drop a minute after seed to verify live signal backfill when allow_quiet_minutes=True
        del b.rows[int(start + 40 * 60)]
        with patch('paper_engine.make_bars', return_value=bars):
            self.assertEqual(b.signal(config(), at)[:2], (1050, 'LONG'))

        cfg = build_config(
            'NIFTY 50',
            next(iter(STRATEGY_LABELS)),
            (date(2026, 10, 19), date(2026, 10, 21)),
            100000,
            '25%',
            '5 minutes',
            '25%',
            '15%',
            50,
            'Target / EOD',
            daily_profit_target=4500.0,
            daily_loss_limit=2500.0,
        )
        self.assertEqual(cfg.daily_profit_target, 4500.0)
        self.assertEqual(cfg.daily_loss_limit, 2500.0)
        restored = form_values(asdict(cfg), 'Saved Test Strategy')
        self.assertEqual(restored['daily_profit_target'], 4500.0)
        self.assertEqual(restored['daily_loss_limit'], 2500.0)


if __name__=='__main__': unittest.main()
