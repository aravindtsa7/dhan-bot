"""Terminal controls, durable history, and adversarial execution regressions."""
from dataclasses import asdict, replace
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from quant_backtester import Config, LEGACY_STRATEGIES as STRATEGIES, simulate_trade, run_backtest
from quant_portfolio import execute_portfolio
from quant_terminal import (PAGES, STRATEGY_LABELS, build_config, register_run, load_registry,
                            save_strategy, form_values, lake_snapshot, save_result, load_saved_strategies, saved_strategy_path)


def prepared(config, price=150., events=None):
    day = config.start
    stamp = pd.Timestamp(day, tz='Asia/Kolkata')
    mcx = config.instrument in ('CRUDEOIL', 'NATURALGAS')
    side = 'LONG' if mcx else config.option_side if config.option_side != 'both' else 'CE'
    event = (1394 if mcx else 919, side, 24000, None, 1)
    return SimpleNamespace(instrument=config.instrument, start=config.start, end=config.end,
        fingerprint='terminal-test-fixture', days={day:dict(timestamp=stamp, contract='TEST_CONTRACT')},
        available_days=[day], exclusions=[], mcx=mcx, has_calendar=False, expiry_calendar=set(),
        price=lambda d,t,s,k:(price, price*1.4, price*.99, price*1.2, 100),
        events=lambda c,d:events if events is not None else [event])


class TerminalParameterTests(unittest.TestCase):
    def config(self, **changes):
        return replace(Config(), start='2026-10-22', end='2026-10-23', slippage=0,
                       trail_pct=0, **changes)

    def test_all_form_parameters_and_inclusive_period(self):
        c = build_config('SENSEX', 'VWAP + EMA(9/21) Pullback', (date(2026,10,1),date(2026,10,22)),
                         250000, '50%', '1 minute', '10%', '15%', 125, 'Trailing SL')
        self.assertEqual((c.instrument,c.strategy,c.option_side), ('SENSEX','vwap_ema_pullback','both'))
        self.assertEqual((c.start,c.end,c.capital,c.capital_usage_pct), ('2026-10-01','2026-10-23',250000,50))
        self.assertEqual((c.timeframe,c.target_pct,c.sl_pct,c.minimum_premium,c.exit_mode,c.trail_pct),
                         ('1m',10,15,125,'trailing_sl',15))
        self.assertEqual(c.sizing_mode,'allocation')
        self.assertEqual(c.risk_basis,'percentage')
        self.assertEqual(form_values(asdict(c),'VWAP + EMA(9/21) Pullback')['period'],(date(2026,10,1),date(2026,10,22)))

    def test_strategy_choices_validate_without_hidden_overrides(self):
        for strategy in STRATEGY_LABELS:
            for exit_label in ('End of day','Trailing SL','Target / EOD'):
                with self.subTest(strategy=strategy,exit=exit_label):
                    c = build_config('CRUDEOIL' if strategy=='MCX Evening Momentum & Donchian Breakout' else 'NIFTY 50', strategy,
                        (date(2026,10,22),date(2026,10,22)),100000,'75%','3 minutes','50%','25%',100,exit_label)
                    c.validate()
                    self.assertEqual(c.end,'2026-10-23')

    def test_unavailable_instrument_and_incompatible_strategy_are_explicit(self):
        args=((date(2026,10,22),date(2026,10,22)),100000,'100%','5 minutes','25%','25%',100,'Target / EOD')
        with self.assertRaisesRegex(ValueError,'BANKNIFTY spot is available'):
            build_config('BANKNIFTY','VWAP + EMA(9/21) Pullback',*args)
        with self.assertRaisesRegex(ValueError,'supports NATURALGAS'):
            build_config('NIFTY 50','MCX Evening Momentum & Donchian Breakout',*args)

    def test_invalid_premium_exit_and_partial_period_rejected(self):
        for values in [dict(minimum_premium=-1),dict(minimum_premium=float('nan')),dict(exit_mode='guess'),
                       dict(option_side='invalid'),dict(risk_basis='invalid'),dict(exit_mode='trailing_sl',trail_pct=0)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(Config(),**values).validate()
        with self.assertRaisesRegex(ValueError,'both the start and end'):
            build_config('NIFTY 50','VWAP + EMA(9/21) Pullback',(date(2026,10,22),),100000,'100%','1 minute','25%','25%',100,'Target / EOD')

    def test_premium_threshold_is_observed_price_not_slipped_price(self):
        c = replace(self.config(minimum_premium=100), slippage=.5)
        data = prepared(c,99.75)
        trade, trace = simulate_trade(data,c,c.start,(919,'CE',24000,None,1),'low')
        self.assertIsNone(trade); self.assertEqual(trace,[])
        trades,_,skipped = execute_portfolio(data,c,'low')
        self.assertEqual(trades,[])
        self.assertEqual(skipped[0]['reason'],'below_minimum_premium')
        trade,_ = simulate_trade(prepared(c,100),c,c.start,(919,'CE',24000,None,1),'equal')
        self.assertIsNotNone(trade)

    def test_trigger_premium_and_futures_are_handled_correctly(self):
        c = self.config(minimum_premium=100)
        trade,_ = simulate_trade(prepared(c,90),c,c.start,(919,'CE',24000,105,1),'trigger')
        self.assertEqual(trade['raw_entry_price'],105)
        c = replace(c,instrument='CRUDEOIL',strategy=STRATEGIES[4],risk_basis='percentage',minimum_premium=10000)
        trade,_ = simulate_trade(prepared(c,100),c,c.start,(1394,'LONG',0,None,1),'future')
        self.assertIsNotNone(trade)
        self.assertEqual(trade['initial_stop'],75)
        self.assertEqual(trade['target'],150)

    def test_side_filter_rejects_opposite_leg(self):
        c = self.config(option_side='CE')
        data = prepared(c,events=[(919,'PE',24000,None,1)])
        trades,_,skips = execute_portfolio(data,c,'side')
        self.assertEqual(trades,[])
        self.assertEqual(skips[0]['reason'],'option_side_filter')

    def test_end_of_day_ignores_target_but_keeps_protective_stop(self):
        c = self.config(exit_mode='end_of_day',target_pct=5,sl_pct=25)
        trade,_ = simulate_trade(prepared(c),c,c.start,(919,'CE',24000,None,1),'eod')
        self.assertEqual(trade['exit_reason'],'square_off')
        trade,_ = simulate_trade(prepared(c),replace(c,exit_mode='target_eod'),c.start,(919,'CE',24000,None,1),'target')
        self.assertEqual(trade['exit_reason'],'target')
        data = prepared(c)
        data.price = lambda *args:(150,220,100,150,100)
        trade,_ = simulate_trade(data,c,c.start,(919,'CE',24000,None,1),'stop')
        self.assertEqual(trade['exit_reason'],'stop_loss')

    def test_trailing_exit_updates_next_minute_and_fixed_exit_does_not(self):
        c = replace(self.config(exit_mode='trailing_sl',target_pct=100),trail_pct=20,trail_jump_pct=5)
        data = prepared(c)
        data.price = lambda day,at,*args:(100,130,95,125,100) if at==600 else (115,120,105,110,100)
        trade, trace = simulate_trade(data,c,c.start,(600,'CE',24000,None,1),'trail')
        self.assertEqual(trade['exit_reason'],'trailing_stop')
        self.assertEqual([p['stop'] for p in trace],[75,110])
        trade,_ = simulate_trade(data,replace(c,exit_mode='target_eod'),c.start,(600,'CE',24000,None,1),'fixed')
        self.assertEqual(trade['exit_reason'],'square_off')

    def test_short_futures_percentage_risk_obeys_ui_stop(self):
        c = self.config(instrument='CRUDEOIL',strategy=STRATEGIES[4],risk_basis='percentage',exit_mode='target_eod',sl_pct=10,target_pct=5)
        data = prepared(c,100)
        trade,_ = simulate_trade(data,c,c.start,(1394,'SHORT',0,None,1),'short')
        self.assertEqual(trade['initial_stop'],110)
        self.assertEqual(trade['target'],95)
        self.assertEqual(trade['exit_reason'],'stop_loss')

    def test_runs_and_named_strategies_are_explicit_and_durable(self):
        c = replace(self.config(minimum_premium=100,exit_mode='target_eod'), strategy='vwap_ema_pullback')
        with TemporaryDirectory() as directory:
            output=Path(directory)/'backtests'
            with self.assertRaisesRegex(ValueError,'custom Strategy Name'): save_strategy(output,c,' ')
            record=save_strategy(output,c,'Nifty_5m_1L')
            self.assertEqual(record['strategy_key'],c.strategy)
            result=run_backtest(c,data=prepared(c),output=output,persist=False)
            self.assertFalse((output/'runs').exists())
            self.assertEqual(load_registry(output)['runs'],[])
            first=save_result(output,result,'Reviewed result')
            self.assertEqual(first['display_id'],'BT-001')
            self.assertEqual(save_result(output,result)['display_id'],'BT-001')
            self.assertEqual(len(load_registry(output)['runs']),1)
            self.assertEqual(load_saved_strategies(saved_strategy_path(output))[0]['config'],asdict(c))
            self.assertTrue((output/'runs'/result['run_id']/'trades.parquet').exists())

    def test_legacy_auto_saved_runs_are_hidden_and_files_preserved(self):
        with TemporaryDirectory() as directory:
            output=Path(directory)
            path=output/'terminal';path.mkdir()
            old={'version':1,'runs':[{'display_id':'BT-999'}],'strategies':[]}
            target=path/'registry.json';target.write_text(json.dumps(old))
            self.assertEqual(load_registry(output)['runs'],[])
            self.assertEqual(json.loads(target.read_text()),old)

    def test_corrupt_history_is_not_overwritten(self):
        with TemporaryDirectory() as directory:
            output=Path(directory); path=output/'terminal'; path.mkdir()
            (path/'registry.json').write_text('not json')
            result=run_backtest(self.config(),data=prepared(self.config()),persist=False)
            with self.assertRaisesRegex(ValueError,'registry.json'): save_result(output,result)
            self.assertEqual((path/'registry.json').read_text(),'not json')

    def test_empty_lake_does_not_fabricate_prices_or_quality(self):
        with TemporaryDirectory() as directory:
            snapshot=lake_snapshot(directory)
            self.assertFalse(snapshot['available'])
            self.assertTrue(all(v is None for v in snapshot['markets'].values()))
            self.assertEqual(snapshot['quality'],{})


class TerminalUITests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.output=self.root/'backtests'
        self.patches=[patch('quant_backtester.OUT',self.output),patch('quant_lake.ROOT',self.root)]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def app(self):
        return AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=45).run()

    def clean(self,app):
        self.assertFalse(app.exception,[e.message for e in app.exception])
        self.assertFalse(app.error,[e.value for e in app.error])

    def test_six_pages_empty_states_and_clean_strategy_choices(self):
        app=self.app();self.clean(app)
        self.assertEqual(app.radio(key='page').value,'Backtests')
        self.assertEqual(len(PAGES),6)
        self.assertNotIn('Strategy Builder',PAGES)
        self.assertEqual(app.selectbox(key='bt_strategy').options,list(STRATEGY_LABELS))
        self.assertIn('GOLDM',app.selectbox(key='bt_instrument').options)
        for page in PAGES:
            app.radio(key='page').set_value(page).run();self.clean(app)
            self.assertEqual(len(app.tabs),0)
        app.radio(key='page').set_value('Backtests').run()
        app.text_input(key='search').set_value('does-not-exist').run();self.clean(app)
        self.assertTrue(any('No matches' in i.value for i in app.info))

    def test_session_result_explicit_saves_reports_and_paper_selector(self):
        configs=[]
        def execute(c,output,persist):
            self.assertFalse(persist)
            configs.append(c)
            return run_backtest(c,data=prepared(c),output=output,persist=False)
        with patch('quant_backtester.run_backtest',side_effect=execute):
            app=self.app()
            app.selectbox(key='bt_instrument').select('SENSEX')
            app.date_input(key='bt_period').set_value((date(2026,10,22),date(2026,10,22)))
            app.number_input(key='bt_capital').set_value(250000)
            app.selectbox(key='bt_allocation').select('50%')
            app.number_input(key='bt_minimum_premium').set_value(125)
            app.selectbox(key='bt_exit_label').select('Trailing SL')
            app.button(key='run_backtest').click().run();self.clean(app)
            self.assertEqual(len(configs),1)
            self.assertEqual(load_registry(self.output)['runs'],[])
            self.assertFalse((self.output/'runs').exists())
            self.assertEqual(len(app.get('plotly_chart')),3)
            self.assertTrue(any('Net P&amp;L' in x.value or 'Net P&L' in x.value for x in app.markdown))
            app.button(key='save_strategy').click().run()
            self.assertTrue(any('custom Strategy Name' in x.value for x in app.error))
            app.text_input(key='strategy_name').set_value('Sensex_Custom_5m')
            app.button(key='save_strategy').click().run();self.clean(app)
            records=load_saved_strategies(saved_strategy_path(self.output))
            self.assertEqual(records[0]['config'],asdict(configs[0]))
            self.assertEqual(load_registry(self.output)['runs'],[])
            app.text_input(key='result_note_'+app.session_state['active_result']['run_id']).set_value('Reviewed')
            app.button(key='save_result').click().run();self.clean(app)
            registry=load_registry(self.output)
            self.assertEqual(len(registry['runs']),1)
            self.assertEqual(registry['runs'][0]['note'],'Reviewed')
            app.radio(key='page').set_value('Reports').run();self.clean(app)
            self.assertEqual(len(app.get('plotly_chart')),3)
            app.radio(key='page').set_value('Live Paper Trading').run();self.clean(app)
            self.assertEqual(app.selectbox(key='paper_saved_strategy').options,['Sensex_Custom_5m'])
            self.assertIn('deploy_paper',[x.key for x in app.button])
            fresh=self.app();self.clean(fresh)
            self.assertNotIn('active_result',fresh.session_state)
            self.assertEqual(len(load_registry(self.output)['runs']),1)

    def test_banknifty_and_mcx_only_strategy_validation(self):
        with patch('quant_backtester.run_backtest') as execute:
            app=self.app()
            app.selectbox(key='bt_instrument').select('BANKNIFTY')
            app.button(key='run_backtest').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any('BANKNIFTY spot is available' in e.value for e in app.error))
            execute.assert_not_called()
            app.selectbox(key='bt_instrument').select('NIFTY 50')
            app.selectbox(key='bt_strategy').select('MCX Evening Momentum & Donchian Breakout')
            app.button(key='run_backtest').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any('supports NATURALGAS' in e.value for e in app.error))
            execute.assert_not_called()


if __name__=='__main__': unittest.main()
