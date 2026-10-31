"""Book structures: causal signals, atomic costs, fixed strikes and paper parity."""
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
from streamlit.testing.v1 import AppTest
import numpy as np
import pandas as pd

from quant_backtester import Config, BOOK_STRATEGIES, STRATEGIES, simulate_trade, run_backtest, research_events
from quant_book import leg_specs, activity
from quant_option_model import wing_fallback, MissingObservedPrice, intrinsic
from quant_terminal import STRATEGY_LABELS, build_config, save_strategy, saved_strategy_path, save_result, load_saved_strategies
from quant_results import trade_ledger, trade_chart
from quant_deployment import launch_strategy, account_path
from step6_paper_trader import configuration, book_activity_confirmed
from paper_basket import BasketPaperAccount
from paper_engine import PaperAccount
from paper_feed import Asset, Tick, IST, option_structure_assets

DATE='2026-10-22'


def fixture(strategy='book_vertical_spread',side='BULL_CALL_SPREAD',at=600):
    c=Config(strategy=strategy,start=DATE,end='2026-10-23',slippage=0,sl_pct=15,target_pct=25,
             trail_pct=0,exit_mode='target_eod',lot_size=10,volume_multiple=1.3)
    stamp=pd.Timestamp(DATE,tz='Asia/Kolkata')
    day=dict(timestamp=stamp,contract='FIXTURE',prices={t:(100,101,99,100,0) for t in range(555,921)},
        options={},option_meta={},rolling={(s,m):{t:k for t in range(555,921)} for s in ('CE','PE')
        for m,k in [('ATM',100),('ATM+1',110),('ATM-1',90)]})
    for t in range(555,921):
        for option in ('CE','PE'):
            for k in (90,100,110):
                p=6 if k==100 else (2 if (option=='CE' and k==110) or (option=='PE' and k==90) else 12)
                day['options'][t,option,k]=(p,p,p,p,100)
                day['option_meta'][t,option,k]=(1000,20,100)
    d=SimpleNamespace(instrument='NIFTY',mcx=False,start=c.start,end=c.end,days={DATE:day},
        available_days=[DATE],exclusions=[],fingerprint='book-fixture',has_calendar=False,expiry_calendar=set())
    d.price=lambda day,t,s,k:day['options'].get((t,s,k))
    d.events=lambda c,date:[(at,side,100,None,1)]
    return c,d,day


class BookTests(unittest.TestCase):
    def test_all_eight_save_result_named_strategy_and_launch(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            for i,(label,(key,_)) in enumerate(STRATEGY_LABELS.items()):
                c=build_config('NATURALGAS' if key=='mcx_evening_breakout' else 'NIFTY 50',label,
                    (pd.Timestamp(DATE).date(),pd.Timestamp(DATE).date()),1_000_000,'100%','5 minutes','25%','15%',0,'Target / EOD')
                record=save_strategy(root,c,f'custom_{i}')
                actual=configuration(SimpleNamespace(saved_strategy=record['strategy_id'],strategies_file=saved_strategy_path(root)))
                self.assertEqual(actual.strategy,key)
                with patch('quant_deployment.subprocess.Popen',return_value=Mock(pid=123)):
                    self.assertEqual(launch_strategy(record,root/'paper',saved_strategy_path(root)),123)
            self.assertEqual(len(load_saved_strategies(saved_strategy_path(root))),9)
            c,d,_=fixture(); result=run_backtest(c,d,persist=False)
            save_result(root,result,'spread saved')
            self.assertTrue((root/'runs'/result['run_id']/'trades.parquet').exists())

    def test_four_book_ui_runs_charts_saves_and_paper_selector(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            def execute(c,output,persist):
                side={'book_vertical_spread':'BULL_CALL_SPREAD','book_iron_butterfly':'IRON_BUTTERFLY',
                      'book_strap_strip':'STRAP'}.get(c.strategy,'CE')
                _,data,_=fixture(c.strategy,side,914 if side=='IRON_BUTTERFLY' else 919)
                data.start=c.start;data.end=c.end
                return run_backtest(c,data,output,persist=False)
            with patch('quant_backtester.OUT',root),patch('quant_backtester.run_backtest',side_effect=execute):
                app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=60).run()
                for label,(key,_) in STRATEGY_LABELS.items():
                    if key not in BOOK_STRATEGIES: continue
                    app.selectbox(key='bt_strategy').select(label)
                    app.date_input(key='bt_period').set_value((pd.Timestamp(DATE).date(),pd.Timestamp(DATE).date()))
                    app.number_input(key='bt_minimum_premium').set_value(0.)
                    app.button(key='run_backtest').click().run()
                    self.assertFalse(app.exception);self.assertFalse(app.error)
                    self.assertEqual(len(app.get('plotly_chart')),3)
                    app.text_input(key='strategy_name').set_value('UI_'+key)
                    app.button(key='save_strategy').click().run()
                    app.button(key='save_result').click().run()
                    self.assertFalse(app.exception);self.assertFalse(app.error)
                app.radio(key='page').set_value('Live Paper Trading').run()
                self.assertFalse(app.exception)
                self.assertEqual(set(app.selectbox(key='paper_saved_strategy').options),{'UI_'+key for key in BOOK_STRATEGIES})

    def test_each_structure_costs_quantities_and_combined_pnl(self):
        for strategy,side in [('book_vertical_spread','BULL_CALL_SPREAD'),('book_vertical_spread','BEAR_PUT_SPREAD'),
                ('book_iron_butterfly','IRON_BUTTERFLY'),('book_strap_strip','STRAP'),('book_strap_strip','STRIP')]:
            with self.subTest(side=side):
                c,d,_=fixture(strategy,side)
                trade,path=simulate_trade(d,c,DATE,(600,side,100,None,1),'t')
                legs=json.loads(trade['legs'])
                self.assertEqual(trade['brokerage'],40*len(legs))
                self.assertEqual(trade['total_cost'],sum(l['total_cost'] for l in legs))
                self.assertEqual(trade['gross_pnl'],sum(l['ratio']*(l['exit_price']-l['entry_price'])*10 for l in legs))
                self.assertAlmostEqual(trade['net_pnl'],trade['gross_pnl']-trade['total_cost'])
                self.assertEqual([l['quantity'] for l in legs],[abs(l['ratio'])*10 for l in legs])
                self.assertEqual(trade['exit_time'].hour*60+trade['exit_time'].minute,915 if side=='IRON_BUTTERFLY' else 920)
                self.assertTrue(all(p['high']==max(p['open'],p['close']) for p in path))

    def test_eight_strategies_simulate_and_save_results(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            for key in STRATEGIES[:8]:
                side={'book_vertical_spread':'BULL_CALL_SPREAD','book_iron_butterfly':'IRON_BUTTERFLY',
                      'book_strap_strip':'STRAP','mcx_evening_breakout':'LONG'}.get(key,'CE')
                c,d,day=fixture(key,side,at=919 if side!='IRON_BUTTERFLY' else 914)
                if key=='mcx_evening_breakout':
                    c=replace(c,instrument='NATURALGAS',entry_start='17:00',entry_end='22:45')
                    d.instrument='NATURALGAS';d.mcx=True;d.price=lambda *args:(100,100,100,100,100)
                    d.events=lambda c,date:[(1394,'LONG',0,None,1)]
                result=run_backtest(c,d,persist=False)
                self.assertEqual(result['metrics']['total_trades'],1,key)
                save_result(root,result,key)
                self.assertTrue((root/'runs'/result['run_id']/'trades.parquet').exists())

    def test_short_leg_stt_is_on_entry_sale_and_slippage_on_every_leg(self):
        c,d,_=fixture();c=replace(c,lot_size=1000,slippage=.5)
        t,_=simulate_trade(d,c,DATE,(600,'BULL_CALL_SPREAD',100,None,1),'t')
        # Buy 6.50, sell 5.50; short 1.50, cover 2.50. Two contracts,
        # 4 orders; STT after 2026-04-01 is rounded per sell at 0.15%.
        self.assertEqual(t['gross_before_slippage'],0)
        self.assertEqual(t['gross_pnl'],-2000)
        self.assertEqual(t['slippage_cost'],2000)
        self.assertEqual(t['brokerage'],80)
        self.assertEqual(t['stt_ctt'],10)

    def test_basket_out_of_band_leg_fallback_and_strict_policy(self):
        c,d,day=fixture()
        for at in range(601,921):
            day['prices'][at]=(150,151,149,150,0)
            for mon,k in [('ATM-1',140),('ATM',150),('ATM+1',160)]:
                day['rolling']['CE',mon][at]=k; day['options'][at,'CE',k]=(12,13,11,12,100)
            del day['options'][at,'CE',100]
            # The short entry wing still has an exact observed quote.
        t,_=simulate_trade(d,c,DATE,(600,'BULL_CALL_SPREAD',100,None,1),'t')
        legs=json.loads(t['legs'])
        self.assertTrue(t['modeled_wing_fallback']);self.assertTrue(legs[0]['modeled_wing_fallback'])
        self.assertFalse(legs[1]['modeled_wing_fallback'])
        with self.assertRaises(MissingObservedPrice): simulate_trade(d,replace(c,price_policy='strict_observed'),DATE,(600,'BULL_CALL_SPREAD',100,None,1),'t')

    def test_credit_reserves_max_loss_and_debit_reserves_net_premium(self):
        for strategy,side,expected in [('book_vertical_spread','BULL_CALL_SPREAD',40),('book_iron_butterfly','IRON_BUTTERFLY',20)]:
            c,d,_=fixture(strategy,side)
            t,_=simulate_trade(d,c,DATE,(600,side,100,None,1),'t')
            self.assertEqual(t['capital_allocated'],expected)
            self.assertGreater(t['capital_reserved'],expected)
            self.assertEqual(t['entry_price'],-8 if side=='IRON_BUTTERFLY' else 4)

    def test_defined_risk_expiry_payoffs_are_bounded(self):
        for strategy,side in [('book_vertical_spread','BULL_CALL_SPREAD'),('book_vertical_spread','BEAR_PUT_SPREAD'),
                              ('book_iron_butterfly','IRON_BUTTERFLY')]:
            c,d,_=fixture(strategy,side)
            t,_=simulate_trade(d,c,DATE,(600,side,100,None,1),'t')
            legs=json.loads(t['legs'])
            pnls=[(sum(l['ratio']*intrinsic(spot,l['strike'],l['side']) for l in legs)-t['entry_price'])*t['quantity'] for spot in np.linspace(0,300,301)]
            self.assertAlmostEqual(min(pnls),-t['theoretical_max_loss'])
            if side=='IRON_BUTTERFLY': self.assertAlmostEqual(max(pnls),t['entry_credit']*t['quantity'])

    def test_all_legs_atomic_missing_or_unaffordable_has_no_partial_trade(self):
        c,d,day=fixture();del day['options'][600,'CE',110]
        self.assertEqual(simulate_trade(d,c,DATE,(600,'BULL_CALL_SPREAD',100,None,1),'t'),(None,[]))
        c,d,_=fixture()
        self.assertEqual(simulate_trade(d,c,DATE,(600,'BULL_CALL_SPREAD',100,None,1),'t',budget=50),(None,[]))

    def test_stop_target_use_combined_close_then_next_open(self):
        for premium,reason in [(8,'target'),(5,'stop_loss')]:
            c,d,day=fixture();day['options'][600,'CE',100]=(6,100,1,premium,100)
            day['options'][601,'CE',100]=(premium,premium,premium,premium,100)
            t,_=simulate_trade(d,c,DATE,(600,'BULL_CALL_SPREAD',100,None,1),'t')
            self.assertEqual(t['exit_reason'],reason)
            self.assertEqual(t['exit_time'].minute,1)
            self.assertEqual(t['exit_price'],premium-2)

    def test_credit_decay_and_expansion_exits(self):
        for premium,reason in [(4,'target'),(8,'stop_loss')]:
            c,d,day=fixture('book_iron_butterfly','IRON_BUTTERFLY')
            day['options'][600,'CE',100]=(6,max(6,premium),min(6,premium),premium,100)
            day['options'][601,'CE',100]=(premium,)*4+(100,)
            t,_=simulate_trade(d,c,DATE,(600,'IRON_BUTTERFLY',100,None,1),'t')
            self.assertEqual(t['exit_reason'],reason)
            self.assertEqual(t['exit_time'].minute,1)

    def test_basket_trailing_and_daily_lock_execute_next_minute(self):
        c,d,day=fixture('book_strap_strip','STRAP')
        c=replace(c,target_pct=500,trail_pct=10,trail_jump_pct=5,exit_mode='trailing_sl')
        day['options'][600,'CE',100]=(6,100,1,7,100)
        day['options'][601,'CE',100]=(6,6,6,6,100)
        t,path=simulate_trade(d,c,DATE,(600,'STRAP',100,None,1),'t')
        self.assertEqual(t['exit_reason'],'trailing_stop')
        self.assertEqual(t['exit_time'].minute,2)
        self.assertGreater(path[1]['stop'],path[0]['stop'])
        c=replace(c,daily_loss_limit=1)
        result=run_backtest(c,d,persist=False)
        self.assertEqual(result['_frames']['trades'].iloc[0].exit_reason,'daily_loss_limit')

    def test_no_reselecting_strikes_and_per_leg_slippage(self):
        c,d,day=fixture();c=replace(c,slippage=.5)
        for mapping in day['rolling'].values(): mapping.update({k:999 for k in range(600,921)})
        t,_=simulate_trade(d,c,DATE,(600,'BULL_CALL_SPREAD',100,None,1),'t')
        self.assertEqual([l['strike'] for l in json.loads(t['legs'])],[100,110])
        self.assertAlmostEqual(t['slippage_cost'],20)

    def test_invalid_width_or_credit_rejected(self):
        c,d,day=fixture('book_iron_butterfly','IRON_BUTTERFLY')
        day['rolling']['PE','ATM-1'][599]=80
        self.assertFalse(leg_specs(day,600,'IRON_BUTTERFLY',100))
        c,d,day=fixture();day['options'][600,'CE',110]=(7,7,7,7,100)
        self.assertEqual(simulate_trade(d,c,DATE,(600,'BULL_CALL_SPREAD',100,None,1),'t'),(None,[]))

    def test_outside_band_fallback_both_sides_no_iv_or_prior_needed(self):
        c,d,day=fixture()
        day['option_meta']={};day['prices'][700]=(150,155,145,151,0)
        for side in ('CE','PE'):
            day['rolling'][side,'ATM-1'][700]=140;day['rolling'][side,'ATM'][700]=150;day['rolling'][side,'ATM+1'][700]=160
            for k in (140,150,160): day['options'][700,side,k]=(12,13,11,12,100)
            del day['options'][700,side,100]
            row,meta=wing_fallback(day,700,side,100)
            self.assertTrue(meta['modeled_wing_fallback'])
            self.assertGreaterEqual(row[3],intrinsic(151,100,side))
            self.assertLessEqual(row[3]-intrinsic(151,100,side),10)
            self.assertEqual(wing_fallback(day,700,side,100),(row,meta))

    def test_exact_quotes_win_and_in_band_holes_do_not_use_wing_model(self):
        c,d,day=fixture()
        self.assertIsNone(wing_fallback(day,700,'CE',100))
        del day['options'][700,'CE',100]
        self.assertIsNone(wing_fallback(day,700,'CE',100))
        del day['options'][700,'CE',110]
        self.assertIsNone(wing_fallback(day,700,'CE',110))

    def test_large_sensex_move_closes_fixed_strike_and_discloses_fallback(self):
        c,d,day=fixture();c=replace(c,instrument='SENSEX',strategy='vwap_ema_pullback',exit_mode='end_of_day')
        d.instrument='SENSEX'
        for at in range(601,921):
            day['prices'][at]=(200,201,199,200,0)
            for mon,k in [('ATM-1',190),('ATM',200),('ATM+1',210)]:
                day['rolling']['CE',mon][at]=k;day['options'][at,'CE',k]=(12,13,11,12,100)
            del day['options'][at,'CE',100]
        t,path=simulate_trade(d,c,DATE,(600,'CE',100,None,1),'t')
        self.assertTrue(t['modeled_wing_fallback']);self.assertEqual(t['exit_reason'],'square_off')
        self.assertEqual(t['entry_strike'],100)
        self.assertEqual(path[-1]['price_source'],'model_intrinsic_wing_fallback')
        with self.assertRaises(MissingObservedPrice): simulate_trade(d,replace(c,price_policy='strict_observed'),DATE,(600,'CE',100,None,1),'t')

    def test_ledger_chart_structure_and_credit_are_explicit(self):
        c,d,_=fixture('book_iron_butterfly','IRON_BUTTERFLY');result=run_backtest(c,d,persist=False)
        trades=result['_frames']['trades']; ledger=trade_ledger(trades,asdict(c))
        self.assertEqual(ledger.Side.iloc[0],'IRON BUTTERFLY');self.assertEqual(ledger['Entry basis'].iloc[0],'credit')
        self.assertIn('-1x100CE',ledger['Contract / Strike'].iloc[0])
        chart=trade_chart(trades.iloc[0],pd.DataFrame(),result['_frames']['stop_paths'],asdict(c))
        self.assertIn('IRON BUTTERFLY',chart.layout.title.text)

    def test_vertical_and_sideways_signals_and_causal_next_open(self):
        c,d,day=fixture()
        bars=pd.DataFrame([dict(timestamp=day['timestamp']+pd.Timedelta(minutes=595),close=101,vwap=100,
                               ema9=101,ema21=100,adx=18,atr=1,atr14=1)])
        d.features=lambda tf:{DATE:bars}
        self.assertEqual(research_events(d,c,DATE)[0][:2],(600,'BULL_CALL_SPREAD'))
        bars.loc[0,['close','ema9']]=[99,99]
        self.assertEqual(research_events(d,c,DATE)[0][1],'BEAR_PUT_SPREAD')
        bars.loc[0,'adx']=17;self.assertFalse(research_events(d,c,DATE))
        c=replace(c,strategy='book_iron_butterfly');bars.loc[0,['close','adx']]=[100,21]
        self.assertEqual(research_events(d,c,DATE)[0][1],'IRON_BUTTERFLY')
        bars.loc[0,'timestamp']=day['timestamp']+pd.Timedelta(minutes=575)
        self.assertFalse(research_events(d,c,DATE))

    def test_strap_strip_volume_breakout_and_momentum_both_directions(self):
        for bearish in (False,True):
            c,d,day=fixture('book_strap_strip');at=750
            returns=np.array([(.002 if j%2 else .001) * (-1 if bearish else 1) for j in range(39)])
            close=np.r_[100,100*np.exp(np.cumsum(returns))]
            bars=pd.DataFrame(dict(timestamp=[day['timestamp']+pd.Timedelta(minutes=550+5*j) for j in range(40)],
                close=close,high=close+.01,low=close-.01,volume=100,ema9=100,ema21=99,vwap=100,adx=30,atr=1,atr14=1))
            d.features=lambda tf:{DATE:bars}
            for side in ('CE','PE'):
                for k in range(745,750):
                    day['options'][k,side,100]=(6,6,6,6,300);day['option_meta'][k,side,100]=(2000,20,100)
            events=research_events(d,c,DATE)
            self.assertIn((750,'STRIP' if bearish else 'STRAP',100,None,1.),events)
            c=replace(c,strategy='book_vol_oi_momentum')
            self.assertIn((750,'PE' if bearish else 'CE',100,None,1.),research_events(d,c,DATE))
            for side in ('CE','PE'):day['option_meta'][749,side,100]=(500,20,100)
            self.assertFalse(any(e[0]==750 for e in research_events(d,c,DATE)))

    def test_activity_missing_oi_disclosed_and_missing_volume_rejected(self):
        c,d,day=fixture();bars=pd.DataFrame({'volume':[0]*11})
        for k in range(645,650): day['options'][k,'CE',100]=(6,6,6,6,300)
        day['option_meta']={}
        a=activity(d,day,bars,10,650,'CE',100,c)
        self.assertFalse(a['signal_oi_available']);self.assertIsNone(a['signal_log_oi'])
        del day['options'][600,'CE',100]
        self.assertIsNone(activity(d,day,bars,10,650,'CE',100,c))

    def test_mcx_support_and_atr_capped_directional_exits(self):
        for key in BOOK_STRATEGIES:
            c=replace(Config(),instrument='NATURALGAS',strategy=key)
            if key in BOOK_STRATEGIES[1:3]:
                with self.assertRaisesRegex(ValueError,'require index option'): c.validate()
            else: c.validate()
        c,d,day=fixture();c=replace(c,instrument='NATURALGAS',risk_basis='percentage')
        d.mcx=True;d.price=lambda *args:(100,100,100,100,100)
        t,_=simulate_trade(d,c,DATE,(1394,'LONG',0,None,2),'t')
        self.assertEqual(t['initial_stop'],97);self.assertEqual(t['target'],106)

    def test_paper_atomic_fresh_quotes_recovery_and_per_leg_fees(self):
        c,d,day=fixture();now=datetime(2026,10,22,10,tzinfo=IST).timestamp()
        specs=leg_specs(day,600,'BULL_CALL_SPREAD',100)
        assets=[Asset('NIFTY',2,100+i,'2026-10-27',10,l['strike'],l['side']) for i,l in enumerate(specs)]
        resolved=list(zip(specs,assets));quotes={a.key:Tick(a.key,p,now,now) for a,p in zip(assets,[6,2])}
        with TemporaryDirectory() as directory:
            account=BasketPaperAccount(directory,c,assets[0],lambda _:None)
            self.assertFalse(account.enter_basket((600,'BULL_CALL_SPREAD',100,None,1),resolved,{},now))
            self.assertTrue(account.enter_basket((600,'BULL_CALL_SPREAD',100,None,1),resolved,quotes,now))
            account=BasketPaperAccount(directory,c,assets[0],lambda _:None)
            account.close(quotes[assets[0].key],now,'target');self.assertIsNotNone(account.position)
            account.on_tick(Tick(assets[0].key,8,now+1,now+1),now+1)
            self.assertIsNotNone(account.position)
            account.on_tick(Tick(assets[1].key,2,now+1,now+1),now+1)
            self.assertIsNone(account.position)
            trade=account.state['trades'][0]
            self.assertEqual(trade['brokerage'],80);self.assertEqual(trade['gross_pnl'],20)

    def test_existing_paper_policy_hash_remains_backward_compatible(self):
        c=replace(Config(),strategy='vwap_ema_pullback')
        asset=Asset('NIFTY',2,123,'2026-10-27',65,24000,'CE')
        original=asdict(c)
        for key in ('start','end','momentum_lookback','momentum_threshold'): original.pop(key)
        for key in list(original):
            if key.startswith('expiry_'): original.pop(key)
        digest=hashlib.sha256(json.dumps(original,sort_keys=True).encode()).hexdigest()
        with TemporaryDirectory() as directory:
            account=PaperAccount(directory,c,asset,lambda _:None)
            self.assertEqual(account.state['policy_hash'],digest)
            recovered=PaperAccount(directory,replace(c,momentum_lookback=15),asset,lambda _:None)
            self.assertEqual(recovered.state['policy_hash'],digest)
            legacy={k:v for k,v in asdict(c).items() if not k.startswith('expiry_')}
            self.assertEqual(account_path(directory,dict(strategy_id='old',config=legacy)),
                             account_path(directory,dict(strategy_id='old',config=asdict(c))))

    def test_paper_contract_resolver_common_expiry_and_book_activity(self):
        now=datetime(2026,10,22,10,tzinfo=IST).timestamp()
        frame=pd.DataFrame([dict(EXCH_ID='NSE',INSTRUMENT='OPTIDX',SYMBOL_NAME='NIFTY',OPTION_TYPE=s,
            SM_EXPIRY_DATE='2026-10-27',STRIKE_PRICE=k,SECURITY_ID=100+i*10+k,LOT_SIZE=10)
            for i,s in enumerate(('CE','PE')) for k in (90,100,110)])
        resolved=option_structure_assets(frame,'NIFTY','IRON_BUTTERFLY',101,now)
        self.assertEqual(len(resolved),4);self.assertEqual(len({a.expiry for _,a in resolved}),1)
        c=Config(strategy='book_vol_oi_momentum',volume_multiple=1.3)
        rows=[dict(timestamp=now-(55-i)*60,open=6,high=6,low=6,close=6,volume=300 if i>=50 else 100,oi=2000 if i>=50 else 1000) for i in range(55)]
        self.assertTrue(book_activity_confirmed(rows,c,(600,'CE',100,None,1),now))
        rows[-1]['oi']=500
        self.assertFalse(book_activity_confirmed(rows,c,(600,'CE',100,None,1),now))


if __name__=='__main__': unittest.main()
