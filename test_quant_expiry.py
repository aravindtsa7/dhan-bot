"""Expiry Day BTST, combined-only risk, late-window audits and virtual recovery."""
from dataclasses import asdict, replace
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from quant_backtester import Config, ResearchData, run_backtest, simulate_trade
from quant_expiry import select_legs, expiry_pairs, previous_session, observed_expiries, live_event, paper_candidates, paper_selection
from quant_option_model import MissingObservedPrice
from quant_terminal import build_config, save_strategy, save_result, saved_strategy_path, load_saved_strategies, form_values
from quant_results import trade_ledger, trade_chart
from paper_feed import Asset, Tick, IST
from paper_basket import BasketPaperAccount
from paper_engine import MinuteBook
from step6_paper_trader import configuration

PRE='2026-10-26';EXP='2026-10-27'


def fixture(**changes):
    values=dict(strategy='expiry_day',start=PRE,end='2026-10-28',sl_pct=25,target_pct=25,trail_pct=0,
                exit_mode='target_eod',lot_size=1,slippage=0)
    values.update(changes);c=Config(**values)
    days={}
    for date in (PRE,EXP):
        day=dict(timestamp=pd.Timestamp(date,tz='Asia/Kolkata'),contract='NIFTY',
            prices={t:(100,101,99,100,0) for t in range(555,930)},options={},option_meta={},
            rolling={(side,offset):{t:k for t in range(555,930)} for side in ('CE','PE')
                     for offset,k in [('ATM',100),('ATM-1',90),('ATM+1',110)]})
        for t in range(555,930):
            for side in ('CE','PE'):
                for k in (90,100,110):
                    p=100 if k==100 else 80 if (side,k) in [('CE',110),('PE',90)] else 120
                    day['options'][t,side,k]=(p,p,p,p,100)
                    day['option_meta'][t,side,k]=(1000,20,100)
        days[date]=day
    data=SimpleNamespace(instrument='NIFTY',start=c.start,end=c.end,mcx=False,days=days,
        available_days=[d for d in days if c.start<=d<c.end],exclusions=[],eligibility=[],has_calendar=True,
        expiry_calendar={EXP},fingerprint='expiry-fixture')
    data.price=lambda day,t,s,k:day['options'].get((t,s,k))
    return c,data


def prices(data,date,at,ce,pe):
    for side,p in [('CE',ce),('PE',pe)]: data.days[date]['options'][at,side,100]=(p,p,p,p,100)


class ExpiryTests(unittest.TestCase):
    def test_entry_close_fixed_strikes_and_overnight_exit_date_accounting(self):
        c,d=fixture();r=run_backtest(c,d,persist=False);t=r['_frames']['trades'].iloc[0]
        self.assertEqual(t.signal_time,pd.Timestamp(PRE+' 15:25',tz=IST))
        self.assertEqual(t.entry_time,pd.Timestamp(PRE+' 15:26',tz=IST))
        self.assertEqual(t.exit_time,pd.Timestamp(EXP+' 15:20',tz=IST))
        self.assertTrue(t.overnight);self.assertEqual(t.date,EXP)
        self.assertEqual([l['strike'] for l in json.loads(t.legs)],[100,100])
        self.assertEqual(r['_frames']['equity'].set_index('date').loc[PRE,'net_pnl'],0)
        self.assertEqual(t.brokerage,80)

    def test_minus_40_plus_90_exits_both_at_combined_25_target(self):
        c,d=fixture();prices(d,EXP,555,60,190);prices(d,EXP,556,60,190)
        r=run_backtest(c,d,persist=False);t=r['_frames']['trades'].iloc[0]
        self.assertEqual(len(r['_frames']['trades']),1)
        self.assertEqual(t.exit_reason,'target');self.assertEqual(t.exit_time,pd.Timestamp(EXP+' 09:16',tz=IST))
        self.assertEqual(t.combined_return_pct,25)
        self.assertEqual([(l['entry_price'],l['exit_price']) for l in json.loads(t.legs)],[(100,60),(100,190)])

    def test_losing_leg_minus_80_does_not_stop_when_basket_up_20(self):
        c,d=fixture();prices(d,EXP,555,20,220);prices(d,EXP,556,60,190);prices(d,EXP,557,60,190)
        t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertEqual(t.exit_time,pd.Timestamp(EXP+' 09:17',tz=IST));self.assertEqual(t.exit_reason,'target')

    def test_stop_only_on_combined_sum_breach(self):
        c,d=fixture();prices(d,EXP,555,60,100);prices(d,EXP,556,60,80);prices(d,EXP,557,60,80)
        t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertEqual(t.exit_reason,'stop_loss');self.assertEqual(t.exit_time,pd.Timestamp(EXP+' 09:17',tz=IST))
        self.assertEqual(t.combined_return_pct,-30)

    def test_pre_expiry_value_does_not_trigger_tp_or_sl(self):
        c,d=fixture()
        for at in (927,928,929): prices(d,PRE,at,1,1)
        t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertEqual(t.exit_time.date().isoformat(),EXP);self.assertEqual(t.exit_reason,'square_off')

    def test_combined_trail_and_eod_mode(self):
        c,d=fixture(target_pct=100,sl_pct=50,trail_pct=25,exit_mode='trailing_sl')
        prices(d,EXP,555,20,220);prices(d,EXP,556,20,160);prices(d,EXP,557,20,160)
        t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertEqual(t.exit_reason,'trailing_stop');self.assertEqual(t.exit_time.minute,17)
        c,d=fixture(exit_mode='end_of_day',expiry_exit_cutoff='15:15')
        prices(d,EXP,555,60,190)
        t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertEqual(t.exit_reason,'square_off');self.assertEqual(t.exit_time,pd.Timestamp(EXP+' 15:15',tz=IST))

    def test_balanced_selection_prefers_atm_on_ties_and_uses_signal_only(self):
        c,d=fixture();day=d.days[PRE]
        self.assertEqual([l['strike'] for l in select_legs(day,925)],[100,100])
        day['options'][925,'PE',100]=(130,130,130,130,100)
        day['options'][925,'PE',90]=(101,101,101,101,100)
        day['options'][925,'CE',90]=(123,123,123,123,100)
        day['options'][925,'PE',110]=(150,150,150,150,100)
        selected=select_legs(day,925)
        self.assertEqual([l['strike'] for l in selected],[100,90])
        self.assertEqual(select_legs(day,925,minimum_premium=200),[])
        day['options'][926,'PE',90]=(300,300,300,300,100)
        self.assertEqual(select_legs(day,925),selected)

    def test_missing_or_unaffordable_leg_is_atomic(self):
        c,d=fixture();del d.days[PRE]['options'][926,'PE',100]
        self.assertEqual(run_backtest(c,d,persist=False)['metrics']['total_trades'],0)
        c,d=fixture(capital=250)
        self.assertEqual(run_backtest(c,d,persist=False)['metrics']['total_trades'],0)

    def test_fixed_strikes_never_roll_and_large_overnight_gap_fallback(self):
        c,d=fixture(sl_pct=90,target_pct=500)
        day=d.days[EXP]
        for at in range(555,921):
            day['prices'][at]=(200,201,199,200,0)
            for side in ('CE','PE'):
                del day['options'][at,side,100]
                for offset,k in [('ATM-1',190),('ATM',200),('ATM+1',210)]:
                    day['rolling'][side,offset][at]=k
                    day['options'][at,side,k]=(15,16,14,15,100)
        t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertTrue(t.modeled_wing_fallback);self.assertEqual(t.entry_strike,100)
        self.assertTrue(all(l['modeled_wing_fallback'] for l in json.loads(t.legs)))
        with self.assertRaises(MissingObservedPrice): run_backtest(replace(c,price_policy='strict_observed'),d,persist=False)

    def test_single_expiry_window_context_and_intraday_alternative(self):
        c,d=fixture(start=EXP)
        self.assertEqual(len(expiry_pairs(d,c)),1)
        self.assertEqual(run_backtest(c,d,persist=False)['metrics']['total_trades'],1)
        self.assertEqual(expiry_pairs(d,replace(c,expiry_include_prior_session=False)),[])
        c=replace(c,expiry_entry_mode='expiry_intraday')
        t=run_backtest(c,d,persist=False)['_frames']['trades'].iloc[0]
        self.assertEqual(t.entry_time,pd.Timestamp(EXP+' 09:16',tz=IST));self.assertFalse(t.overnight)

    def test_holiday_shift_and_no_hopping_over_missing_pre_session(self):
        c,d=fixture(start='2026-10-15',end='2026-10-21')
        d.expiry_calendar={'2026-10-19'}
        self.assertEqual(expiry_pairs(d,c),[('2026-10-16','2026-10-19')])
        self.assertEqual(previous_session('2026-10-19'),'2026-10-16')
        c,d=fixture();del d.days[PRE]
        r=run_backtest(c,d,persist=False)
        self.assertEqual(r['metrics']['total_trades'],0)
        self.assertIn('ineligible',r['_frames']['skipped_entries'].iloc[0].reason)

    def test_observed_mode_requires_actual_reset_or_dated_expiry(self):
        c,d=fixture(expiry_calendar_mode='observed_reset')
        d.observed_expiry_dates=set()
        self.assertEqual(expiry_pairs(d,c),[])
        frame=pd.DataFrame({'date':[PRE,EXP,'2026-10-28'],'strike':[100,200,1000],'weekly_reset':[False,False,True]})
        self.assertEqual(observed_expiries(frame),{EXP})
        frame=frame.drop(columns='weekly_reset').assign(expiry_date=EXP)
        self.assertEqual(observed_expiries(frame),{EXP})
        self.assertEqual(observed_expiries(frame.drop(columns='expiry_date')),set())

    def test_config_validation_and_all_chart_intervals(self):
        for tf in ('1m','3m','5m','15m'):
            c,d=fixture(timeframe=tf);c.validate()
            self.assertEqual(run_backtest(c,d,persist=False)['metrics']['total_trades'],1)
        for instrument in ('CRUDEOIL','NATURALGAS','GOLDM'):
            with self.assertRaisesRegex(ValueError,'MCX is unsupported'): replace(c,instrument=instrument).validate()
        for values in [dict(expiry_entry_signal='15:24'),dict(expiry_exit_cutoff='15:29'),dict(expiry_entry_mode='guess'),dict(option_side='CE')]:
            with self.assertRaises(ValueError): replace(c,**values).validate()

    def test_late_signal_time_and_saved_custom_config_roundtrip(self):
        c,d=fixture(expiry_entry_signal='15:28');r=run_backtest(c,d,persist=False)
        self.assertEqual(r['_frames']['trades'].iloc[0].entry_time,pd.Timestamp(PRE+' 15:29',tz=IST))
        with TemporaryDirectory() as directory:
            root=Path(directory);record=save_strategy(root,c,'My Expiry BTST')
            actual=configuration(SimpleNamespace(saved_strategy=record['strategy_id'],strategies_file=saved_strategy_path(root)))
            self.assertEqual(actual.expiry_entry_signal,'15:28')
            self.assertEqual(form_values(asdict(c),'test')['expiry_entry_signal'],'15:28')
            save_result(root,r,'BTST')
            self.assertTrue((root/'runs'/r['run_id']/'trades.parquet').exists())

    def test_ledger_and_chart_show_both_dates_leg_prices_and_combined_return(self):
        c,d=fixture();prices(d,EXP,555,60,190);prices(d,EXP,556,60,190)
        r=run_backtest(c,d,persist=False);t=r['_frames']['trades'].iloc[0]
        ledger=trade_ledger(r['_frames']['trades'],asdict(c))
        self.assertEqual(ledger.Side.iloc[0],'EXPIRY DAY (CE+PE)')
        self.assertIn(PRE,ledger['Entry Date & Time'].iloc[0]);self.assertIn(EXP,ledger['Exit Date & Time'].iloc[0])
        self.assertIn('100.00 → 60.00',ledger['CE Entry → Exit ₹'].iloc[0]);self.assertEqual(ledger['Combined Return %'].iloc[0],25)
        chart=trade_chart(t,pd.DataFrame(),r['_frames']['stop_paths'],asdict(c))
        self.assertIn('EXPIRY DAY (CE+PE)',chart.layout.title.text)
        self.assertTrue(any(PRE in (a.text or '') and EXP in (a.text or '') for a in chart.layout.annotations))

    def test_real_loader_single_expiry_context_extends_entry_audit_to_1529(self):
        from test_quant_run_data import RunDataTests
        helper=RunDataTests();helper.setUp()
        try:
            for date in (PRE,EXP):
                for name in ['NIFTY']+[f'NIFTY_{s}_{m}_WEEK1' for s in ('CE','PE') for m in ('ATM','ATM-1','ATM+1')]:
                    helper.store(name,date)
            helper.ensure_audit();c,_=fixture(start=EXP)
            data=ResearchData('NIFTY',c.start,c.end,helper.root,expiry_config=c)
            self.assertEqual(set(data.days),{PRE,EXP});self.assertEqual(data.available_days,[EXP])
            audit=next(a for a in data.eligibility if a['date']==PRE)
            self.assertEqual(audit['required_windows']['options'],['09:15','15:29'])
            self.assertEqual(run_backtest(c,data,persist=False)['metrics']['total_trades'],1)
            ordinary=ResearchData('NIFTY',c.start,c.end,helper.root)
            with self.assertRaisesRegex(ValueError,'late entry'): run_backtest(c,ordinary,persist=False)
            helper.store('NIFTY_CE_ATM+1_WEEK1',PRE,missing=('15:27',));helper.ensure_audit()
            data=ResearchData('NIFTY',c.start,c.end,helper.root,expiry_config=c)
            self.assertNotIn(PRE,data.days)
            self.assertEqual(run_backtest(c,data,persist=False)['metrics']['total_trades'],0)
        finally: helper.doCleanups()

    def test_paper_overnight_recovery_and_combined_only_target(self):
        c,_=fixture();now=datetime(2026,10,26,15,26,tzinfo=IST).timestamp()
        resolved=[(dict(side=s,strike=100,ratio=1),Asset('NIFTY',2,10+i,EXP,1,100,s)) for i,s in enumerate(('CE','PE'))]
        assets=[a for _,a in resolved]
        quotes={a.key:Tick(a.key,100,now,now) for a in assets}
        with TemporaryDirectory() as directory:
            account=BasketPaperAccount(directory,c,assets[0],lambda _:None)
            self.assertTrue(account.enter_basket((926,'EXPIRY_DAY',0,None,0),resolved,quotes,now))
            account.heartbeat(now+180);self.assertFalse(account.position['pending_exit'])
            account=BasketPaperAccount(directory,c,assets[0],lambda _:None)
            tomorrow=datetime(2026,10,27,9,15,tzinfo=IST).timestamp()
            account.heartbeat(tomorrow);self.assertFalse(account.position['pending_exit'])
            account.on_tick(Tick(assets[0].key,60,tomorrow,tomorrow),tomorrow)
            self.assertIsNotNone(account.position)
            account.on_tick(Tick(assets[1].key,190,tomorrow,tomorrow),tomorrow)
            self.assertIsNone(account.position);t=account.state['trades'][0]
            self.assertEqual(t['exit_reason'],'target');self.assertEqual(t['combined_return_pct'],25)

    def test_paper_daily_account_lock_overrides_hold_for_entire_basket(self):
        c,_=fixture(daily_loss_limit=1);now=datetime(2026,10,26,15,26,tzinfo=IST).timestamp()
        resolved=[(dict(side=s,strike=100,ratio=1),Asset('NIFTY',2,10+i,EXP,1,100,s)) for i,s in enumerate(('CE','PE'))]
        assets=[a for _,a in resolved];quotes={a.key:Tick(a.key,100,now,now) for a in assets}
        with TemporaryDirectory() as directory:
            account=BasketPaperAccount(directory,c,assets[0],lambda _:None)
            self.assertTrue(account.enter_basket((926,'EXPIRY_DAY',0,None,0),resolved,quotes,now))
            self.assertIsNone(account.position)
            trade=account.state['trades'][0]
            self.assertEqual(trade['exit_reason'],'daily_loss_limit')
            self.assertTrue(all('exit_price' in l for l in trade['legs']))

    def test_live_signal_ignores_chart_grid_and_resolves_balanced_real_contracts(self):
        c,_=fixture(timeframe='15m');now=datetime(2026,10,26,15,26,tzinfo=IST).timestamp()
        book=MinuteBook();book.seed([dict(timestamp=now-60,open=100,high=100,low=100,close=100,volume=0)],now)
        self.assertEqual(book.signal(c,now),(926,'EXPIRY_DAY',0,None,0))
        self.assertIsNone(book.signal(c,now))
        frame=pd.DataFrame([dict(EXCH_ID='NSE',INSTRUMENT='OPTIDX',SYMBOL_NAME='NIFTY',OPTION_TYPE=s,SM_EXPIRY_DATE=EXP,
                STRIKE_PRICE=k,SECURITY_ID=1000+i*100+k,LOT_SIZE=65) for i,s in enumerate(('CE','PE')) for k in (90,100,110)])
        candidates=paper_candidates(frame,'NIFTY',100,now)
        histories={a.key:[dict(timestamp=now-60,open=100,high=100,low=100,close=100,volume=100)] for _,a in candidates}
        selected=paper_selection(c,candidates,histories,(926,'EXPIRY_DAY',0,None,0),now)
        self.assertEqual([a.strike for _,a in selected],[100,100])
        self.assertEqual(len({a.expiry for _,a in selected}),1)

    def test_expiry_ui_controls_save_result_and_paper_selector(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            def execute(c,output,persist):
                _,data=fixture();data.start=c.start;data.end=c.end
                return run_backtest(c,data,output,persist=False)
            with patch('quant_backtester.OUT',root),patch('quant_backtester.run_backtest',side_effect=execute):
                app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=60).run()
                app.selectbox(key='bt_strategy').select('Expiry Day').run()
                self.assertFalse(app.exception)
                app.date_input(key='bt_period').set_value((pd.Timestamp(EXP).date(),pd.Timestamp(EXP).date()))
                app.number_input(key='bt_expiry_tp').set_value(50.)
                app.number_input(key='bt_expiry_sl').set_value(30.)
                app.number_input(key='bt_minimum_premium').set_value(0.)
                app.selectbox(key='bt_expiry_exit_label').select('Target + SL')
                app.button(key='run_backtest').click().run()
                self.assertFalse(app.exception);self.assertFalse(app.error)
                app.text_input(key='strategy_name').set_value('My_Expiry_Day')
                app.button(key='save_strategy').click().run();app.button(key='save_result').click().run()
                self.assertFalse(app.exception);self.assertFalse(app.error)
                record=load_saved_strategies(saved_strategy_path(root))[0]
                self.assertEqual((record['config']['target_pct'],record['config']['sl_pct']),(50,30))
                app.radio(key='page').set_value('Live Paper Trading').run()
                self.assertEqual(app.selectbox(key='paper_saved_strategy').options,['My_Expiry_Day'])


if __name__=='__main__': unittest.main()
