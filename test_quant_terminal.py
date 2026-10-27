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

from quant_backtester import Config, STRATEGIES, simulate_trade, run_backtest
from quant_portfolio import execute_portfolio
from quant_terminal import (PAGES, STRATEGY_LABELS, build_config, register_run, load_registry,
                            save_strategy, form_values, lake_snapshot)


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
        c = build_config('SENSEX', 'Reversal PE', (date(2026,10,1),date(2026,10,22)),
                         250000, '50%', '1 minute', '10%', '15%', 125, 'Trailing SL')
        self.assertEqual((c.instrument,c.strategy,c.option_side), ('SENSEX',STRATEGIES[3],'PE'))
        self.assertEqual((c.start,c.end,c.capital,c.capital_usage_pct), ('2026-10-01','2026-10-23',250000,50))
        self.assertEqual((c.timeframe,c.target_pct,c.sl_pct,c.minimum_premium,c.exit_mode,c.trail_pct),
                         ('1m',10,15,125,'trailing_sl',15))
        self.assertEqual(c.sizing_mode,'allocation')
        self.assertEqual(c.risk_basis,'percentage')
        self.assertEqual(form_values(asdict(c),'Reversal PE')['period'],(date(2026,10,1),date(2026,10,22)))

    def test_strategy_choices_validate_without_hidden_overrides(self):
        for strategy in STRATEGY_LABELS:
            for exit_label in ('End of day','Trailing SL','Target / EOD'):
                with self.subTest(strategy=strategy,exit=exit_label):
                    c = build_config('CRUDEOIL' if strategy=='MCX Breakout' else 'NIFTY 50', strategy,
                        (date(2026,10,22),date(2026,10,22)),100000,'75%','3 minutes','50%','25%',100,exit_label)
                    c.validate()
                    self.assertEqual(c.end,'2026-10-23')

    def test_unavailable_instrument_and_incompatible_strategy_are_explicit(self):
        args=((date(2026,10,22),date(2026,10,22)),100000,'100%','5 minutes','25%','25%',100,'Target / EOD')
        with self.assertRaisesRegex(ValueError,'BANKNIFTY spot is available'):
            build_config('BANKNIFTY','Buy CE / Buy PE',*args)
        with self.assertRaisesRegex(ValueError,'MCX Breakout'):
            build_config('CRUDEOIL','Buy CE / Buy PE',*args)

    def test_invalid_premium_exit_and_partial_period_rejected(self):
        for values in [dict(minimum_premium=-1),dict(minimum_premium=float('nan')),dict(exit_mode='guess'),
                       dict(option_side='invalid'),dict(risk_basis='invalid'),dict(exit_mode='trailing_sl',trail_pct=0)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(Config(),**values).validate()
        with self.assertRaisesRegex(ValueError,'both the start and end'):
            build_config('NIFTY 50','Buy CE / Buy PE',(date(2026,10,22),),100000,'100%','1 minute','25%','25%',100,'Target / EOD')

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

    def test_runs_and_strategies_are_durable_and_deduplicated(self):
        c = self.config(minimum_premium=100,exit_mode='target_eod')
        with TemporaryDirectory() as directory:
            output=Path(directory)
            save_strategy(output,c,'Buy CE / Buy PE')
            result=run_backtest(c,data=prepared(c),output=output)
            first=register_run(output,result,'Buy CE / Buy PE')
            self.assertEqual(first['display_id'],'BT-001')
            self.assertEqual(register_run(output,result,'Buy CE / Buy PE')['display_id'],'BT-001')
            registry=load_registry(output)
            self.assertEqual(len(registry['runs']),1)
            self.assertEqual(registry['strategies'][0]['last_run'],'BT-001')
            changed=replace(c,minimum_premium=200)
            result2=run_backtest(changed,data=prepared(changed),output=output)
            self.assertNotEqual(result['run_id'],result2['run_id'])
            self.assertEqual(register_run(output,result2,'Buy CE / Buy PE')['display_id'],'BT-002')
            self.assertEqual(result2['metrics']['total_trades'],0)
            self.assertEqual(load_registry(output)['runs'][-1]['config']['minimum_premium'],200)

    def test_corrupt_history_is_not_overwritten(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'terminal'; path.mkdir()
            (path/'registry.json').write_text('not json')
            with self.assertRaisesRegex(ValueError,'registry.json'):
                save_strategy(directory,self.config(),'Buy CE / Buy PE')
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

    def test_seven_pages_empty_states_and_removed_clutter(self):
        app=self.app();self.clean(app)
        self.assertEqual(app.radio(key='page').value,'Backtests')
        self.assertEqual(len(app.tabs),0)
        self.assertNotIn('Result collection',[w.label for w in app.selectbox])
        self.assertEqual(app.selectbox(key='bt_instrument').options,['NIFTY 50','SENSEX','BANKNIFTY','CRUDEOIL','NATURALGAS'])
        for page in PAGES:
            app.radio(key='page').set_value(page).run();self.clean(app)
            self.assertEqual(len(app.tabs),0)
        app.radio(key='page').set_value('Backtests').run()
        app.text_input(key='search').set_value('does-not-exist').run();self.clean(app)
        self.assertTrue(any('No matches' in i.value for i in app.info))

    def test_real_execution_from_controls_saved_strategy_reports_and_reload(self):
        configs=[]
        def execute(c,output):
            configs.append(c)
            return run_backtest(c,data=prepared(c),output=output)
        with patch('quant_backtester.run_backtest',side_effect=execute):
            app=self.app()
            app.selectbox(key='bt_instrument').select('SENSEX')
            app.selectbox(key='bt_strategy').select('Reversal PE')
            app.date_input(key='bt_period').set_value((date(2026,10,22),date(2026,10,22)))
            app.number_input(key='bt_capital').set_value(250000)
            app.selectbox(key='bt_allocation').select('50%')
            app.selectbox(key='bt_interval').select('1 minute')
            app.selectbox(key='bt_take_profit').select('10%')
            app.selectbox(key='bt_stop_loss').select('15%')
            app.number_input(key='bt_minimum_premium').set_value(125)
            app.selectbox(key='bt_exit_label').select('Trailing SL')
            app.button(key='run_backtest').click().run();self.clean(app)
            expected=build_config('SENSEX','Reversal PE',(date(2026,10,22),date(2026,10,22)),250000,'50%','1 minute','10%','15%',125,'Trailing SL')
            self.assertEqual(configs,[expected])
            self.assertTrue(any('BT-001 complete' in i.value for i in app.success))
            self.assertEqual(len(app.get('plotly_chart')),1)
            self.assertIn('Closed trades',[m.label for m in app.metric])
            app.button(key='save_strategy').click().run();self.clean(app)
            registry=load_registry(self.output)
            self.assertEqual(registry['strategies'][0]['config'],asdict(expected))
            self.assertEqual(registry['strategies'][0]['last_run'],'BT-001')
            app.number_input(key='bt_minimum_premium').set_value(200)
            app.button(key='run_backtest').click().run();self.clean(app)
            self.assertTrue(any('BT-002 complete' in i.value for i in app.success))
            self.assertTrue(any('No trades met' in i.value for i in app.info))
            app.radio(key='page').set_value('Reports').run();self.clean(app)
            app.selectbox(key='report_run').select(registry['runs'][0]['run_id']).run();self.clean(app)
            self.assertEqual(len(app.get('plotly_chart')),2)
            app.radio(key='page').set_value('Strategy Builder').run();self.clean(app)
            app.button(key='load_'+registry['strategies'][0]['strategy_id']).click().run();self.clean(app)
            self.assertEqual(app.number_input(key='bt_minimum_premium').value,125)
            self.assertEqual(app.number_input(key='bt_capital').value,250000)
            self.assertEqual(app.selectbox(key='bt_strategy').value,'Reversal PE')
            app.radio(key='page').set_value('Overview').run();self.clean(app)
            app.radio(key='page').set_value('Backtests').run();self.clean(app)
            self.assertEqual(app.number_input(key='bt_capital').value,250000)
            self.assertEqual(app.number_input(key='bt_minimum_premium').value,125)
            app.text_input(key='search').set_value('BT-001').run();self.clean(app)
            app.selectbox(key='search_run').select(registry['runs'][0]['run_id']).run();self.clean(app)
            self.assertEqual(app.selectbox(key='recent_run').value,registry['runs'][0]['run_id'])
            fresh=self.app();self.clean(fresh)
            self.assertEqual(len(fresh.selectbox(key='recent_run').options),2)

    def test_banknifty_and_incompatible_configuration_do_not_execute(self):
        with patch('quant_backtester.run_backtest') as execute:
            app=self.app()
            app.selectbox(key='bt_instrument').select('BANKNIFTY')
            app.button(key='run_backtest').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any('BANKNIFTY spot is available' in e.value for e in app.error))
            execute.assert_not_called()
            app.selectbox(key='bt_instrument').select('CRUDEOIL')
            app.button(key='run_backtest').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any('MCX Breakout' in e.value for e in app.error))
            execute.assert_not_called()


if __name__=='__main__': unittest.main()
