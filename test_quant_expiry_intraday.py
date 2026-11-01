"""Separate morning expiry baskets and explicit-only trailing regression tests."""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import pandas as pd
from streamlit.testing.v1 import AppTest
from quant_backtester import Config, run_backtest, simulate_trade
from quant_expiry_intraday import intraday_events, live_event
from quant_terminal import strategy_defaults, form_values
from quant_strategy_models import profit_lock, futures_risk
from test_quant_expiry import fixture, prices, PRE, EXP
from paper_feed import IST, Asset, Tick


def morning(**changes):
    values=dict(strategy='expiry_intraday',target_pct=35,sl_pct=40,minimum_premium=40,capital_usage_pct=50)
    values.update(changes)
    c,data=fixture(**values)
    data.events=lambda config,date:intraday_events(data,config,date)
    return c,data


class ExpiryIntradayTests(unittest.TestCase):
    def test_morning_entry_and_same_day_eod_with_distinct_badge(self):
        c,d=morning();t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertEqual(t.signal_time.strftime('%F %R'),EXP+' 09:20')
        self.assertEqual(t.entry_time.strftime('%F %R'),EXP+' 09:21')
        self.assertEqual(t.exit_time.strftime('%F %R'),EXP+' 15:20')
        self.assertFalse(t.overnight);self.assertEqual(t.side,'EXPIRY_INTRADAY')
        self.assertEqual(t.structure_label,'EXPIRY INTRADAY (CE+PE)')
        self.assertEqual(t.leg_count,2)

    def test_losing_call_does_not_stop_basket_combined_35_percent_target(self):
        c,d=morning()
        prices(d,EXP,562,20,220)  # CE -80%, basket +20%; no leg stop
        prices(d,EXP,563,60,210);prices(d,EXP,564,60,210)
        t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertEqual(t.exit_time.strftime('%R'),'09:24')
        self.assertEqual(t.exit_reason,'target');self.assertAlmostEqual(t.combined_return_pct,35)

    def test_combined_loss_only_and_configurable_cutoff(self):
        c,d=morning()
        prices(d,EXP,562,10,200);prices(d,EXP,563,60,60);prices(d,EXP,564,60,60)
        t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertEqual(t.exit_reason,'stop_loss');self.assertEqual(t.exit_time.strftime('%R'),'09:24')
        c,d=morning(intraday_entry_signal='10:30',intraday_exit_cutoff='15:15',exit_mode='end_of_day')
        t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertEqual(t.entry_time.strftime('%R'),'10:31');self.assertEqual(t.exit_time.strftime('%R'),'15:15')

    def test_combined_trailing_requires_selected_mode(self):
        for mode,reason in [('target_eod','square_off'),('trailing_sl','trailing_stop')]:
            c,d=morning(exit_mode=mode,trail_pct=20,target_pct=100)
            prices(d,EXP,562,60,200);prices(d,EXP,563,110,110);prices(d,EXP,564,110,110)
            t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
            self.assertEqual(t.exit_reason,reason)

    def test_calendar_holiday_entry_gate_and_mcx_rejection(self):
        c,d=morning()
        self.assertFalse(intraday_events(d,c,PRE))
        self.assertTrue(intraday_events(d,c,'2026-10-19'))  # holiday-adjusted NIFTY expiry
        now=datetime(2026,10,27,9,21,tzinfo=IST).timestamp()
        self.assertEqual(live_event(c,now),(561,'EXPIRY_INTRADAY',0,None,0))
        self.assertIsNone(live_event(c,now+60))
        with self.assertRaisesRegex(ValueError,'MCX'):replace(c,instrument='NATURALGAS').validate()
        for signal in ('09:14','11:01'):
            with self.assertRaises(ValueError):replace(c,intraday_entry_signal=signal).validate()

    def test_old_btst_remains_separate_with_unchanged_defaults(self):
        c,d=fixture();t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertEqual(t.entry_time.strftime('%F %R'),PRE+' 15:26')
        self.assertEqual(t.exit_time.strftime('%F %R'),EXP+' 15:20');self.assertTrue(t.overnight)
        defaults=strategy_defaults('NIFTY','expiry_day')
        self.assertEqual([defaults[k] for k in ('take_profit','stop_loss','minimum_premium','allocation')],['25%','60%',70.,'50%'])

    def test_ui_import_fallback_and_new_strategy_save_workflow(self):
        import quant_terminal
        original=quant_terminal.strategy_defaults
        del quant_terminal.strategy_defaults
        try:
            app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=60).run()
            self.assertFalse(app.exception)
        finally:quant_terminal.strategy_defaults=original
        with TemporaryDirectory() as directory:
            def execute(c,*args,**kwargs):
                _,d=morning();d.start=c.start;d.end=c.end
                return run_backtest(c,d,persist=False)
            with patch('quant_backtester.OUT',Path(directory)),patch('quant_backtester.run_backtest',side_effect=execute):
                app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=60).run()
                app.selectbox(key='bt_strategy').select('Expiry Intraday').run()
                self.assertEqual(app.number_input(key='bt_expiry_tp').value,35)
                self.assertEqual(app.number_input(key='bt_expiry_sl').value,40)
                self.assertEqual(app.number_input(key='bt_minimum_premium').value,40)
                self.assertEqual(app.selectbox(key='bt_intraday_entry_signal').value,'09:20')
                app.date_input(key='bt_period').set_value((pd.Timestamp(EXP).date(),)*2)
                app.button(key='run_backtest').click().run()
                self.assertFalse(app.exception);self.assertFalse(app.error)
                self.assertEqual(len(app.get('plotly_chart')),3)
                app.text_input(key='strategy_name').set_value('Morning expiry')
                app.button(key='save_strategy').click().run();app.button(key='save_result').click().run()
                self.assertFalse(app.exception);self.assertFalse(app.error)
                app.radio(key='page').set_value('Live Paper Trading').run()
                self.assertIn('Morning expiry',app.selectbox(key='paper_saved_strategy').options)

    def test_live_paper_morning_basket_exits_atomically(self):
        from paper_basket import BasketPaperAccount
        from paper_engine import MinuteBook
        from quant_expiry import select_legs
        c,d=morning();now=datetime(2026,10,27,9,21,tzinfo=IST).timestamp()
        book=MinuteBook();book.rows[int(now)-60]=dict(timestamp=now-60,open=100,high=100,low=100,close=100,volume=100)
        event=book.signal(c,now);self.assertEqual(event[1],'EXPIRY_INTRADAY')
        specs=select_legs(d.days[EXP],560,40)
        assets=[Asset('NIFTY',2,100+i,EXP,1,l['strike'],l['side']) for i,l in enumerate(specs)]
        quotes={a.key:Tick(a.key,100,now,now,100,100,None,None,0,0) for a in assets}
        with TemporaryDirectory() as root:
            account=BasketPaperAccount(Path(root),c,assets[0],lambda _:None)
            self.assertTrue(account.enter_basket(event,list(zip(specs,assets)),quotes,now))
            account.on_tick(Tick(assets[0].key,60,now+1,now+1,100,100,None,None,0,0),now+1)
            self.assertIsNotNone(account.position)
            account.on_tick(Tick(assets[1].key,210,now+1,now+1,100,100,None,None,0,0),now+1)
            self.assertIsNone(account.position)
            trade=account.state['trades'][0]
            self.assertEqual(trade['exit_reason'],'target');self.assertFalse(trade['overnight'])
            self.assertAlmostEqual(trade['combined_return_pct'],35)


class ExplicitExitTests(unittest.TestCase):
    def test_target_mode_never_applies_automatic_lock_options_or_futures(self):
        for strategy in ('vwap_ema_pullback','book_vertical_spread','book_vol_oi_momentum'):
            for mode in ('target_eod','end_of_day','legacy'):
                c=Config(strategy=strategy,exit_mode=mode)
                self.assertEqual(profit_lock(c,100,130,80),80)
                self.assertEqual(profit_lock(c,220,223,217,atr=2),217)

    def test_option_target_runs_through_former_auto_lock(self):
        from test_quant_backtester import EngineTests
        d=EngineTests().fake({(600,24000):(100,119,95,118,1),(601,24000):(105,110,104,105,1),(602,24000):(140,140,139,140,1)})
        c=Config(slippage=0,minimum_premium=0,sl_pct=20,target_pct=40,exit_mode='target_eod')
        trade,path=simulate_trade(d,c,'2026-10-22',(600,'CE',24000,None,2),'fixed')
        self.assertEqual(trade['exit_reason'],'target');self.assertEqual(trade['exit_price'],140)
        self.assertEqual([r['stop'] for r in path],[80]*3)

    def test_futures_default_atr_distances_and_fixed_target_paper_parity(self):
        from test_paper_trader import config, tick, NOW, ASSET
        from paper_engine import PaperAccount
        for sign,side in [(1,'LONG'),(-1,'SHORT')]:
            c=config(strategy='book_vertical_spread',slippage=0,sl_pct=15,target_pct=30,exit_mode='target_eod')
            self.assertEqual(futures_risk(c,220,2),(2.4,4.4))
            with TemporaryDirectory() as root:
                a=PaperAccount(Path(root),c,ASSET,lambda _:None)
                a.enter((1055,side,0,None,2),tick(220,bid=None,ask=None),NOW)
                for i,gain in enumerate((2.4,.2),1):
                    a.on_tick(tick(220+sign*gain,NOW+i,bid=None,ask=None),NOW+i)
                    self.assertIsNotNone(a.position);self.assertAlmostEqual(a.position['stop'],220-sign*2.4)
                a.on_tick(tick(220+sign*4.5,NOW+3,bid=None,ask=None),NOW+3)
                self.assertIsNone(a.position);self.assertEqual(a.state['trades'][0]['exit_reason'],'target')


if __name__=='__main__':unittest.main()
