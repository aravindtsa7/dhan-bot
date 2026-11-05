"""Retirement boundaries and old-record compatibility; no removed strategy rules."""
from dataclasses import asdict, fields
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import ast
import hashlib
import importlib.util
import io
import json
import unittest
import pandas as pd

from quant_backtester import (Config, STRATEGIES, REMOVED_STRATEGIES, OBSOLETE_CONFIG_FIELDS,
    config_from_saved, run_backtest)
from quant_terminal import (STRATEGY_LABELS, build_config, load_saved_strategies,
    saved_strategy_path, form_values, strategy_label)


def current_config():
    return build_config('NIFTY 50',next(iter(STRATEGY_LABELS)),(date(2026,10,19),date(2026,10,21)),
        100000,'25%','5 minutes','25%','15%',50,'Target / EOD')


def record(strategy, index=0):
    values=asdict(current_config())
    values.update(strategy=strategy,**dict(zip(sorted(OBSOLETE_CONFIG_FIELDS),('native','dhan_native','source_baseline'))))
    return dict(strategy_id='saved_'+str(index),name='Saved '+str(index),config=values,last_run=None)


class RemovedStrategyTests(unittest.TestCase):
    def test_paper_main_reports_removal_without_logs_account_or_feed(self):
        from contextlib import redirect_stderr
        from step6_paper_trader import main
        with TemporaryDirectory() as folder:
            root=Path(folder);path=root/'saved.json';output=root/'never-created'
            path.write_text(json.dumps([record(next(iter(REMOVED_STRATEGIES)))]))
            args=['step6_paper_trader.py','--saved-strategy','saved_0','--strategies-file',str(path),'--output',str(output)]
            message=io.StringIO()
            with patch('sys.argv',args),patch('step6_paper_trader.logger') as logger,patch('step6_paper_trader.live') as feed,redirect_stderr(message):
                self.assertEqual(main(),2)
                logger.assert_not_called();feed.assert_not_called()
            self.assertIn('Strategy removed:',message.getvalue())
            self.assertFalse(output.exists())

    def test_registry_contains_only_remaining_ten_and_no_mode_fields(self):
        self.assertEqual(len(STRATEGIES),10)
        self.assertEqual({key for key,_ in STRATEGY_LABELS.values()},set(STRATEGIES))
        self.assertTrue(set(STRATEGIES).isdisjoint(REMOVED_STRATEGIES))
        self.assertTrue({f.name for f in fields(Config)}.isdisjoint(OBSOLETE_CONFIG_FIELDS))

    def test_removed_api_rejects_before_reading_data_or_creating_output(self):
        with TemporaryDirectory() as folder,patch('quant_backtester.ResearchData') as read:
            output=Path(folder)/'no-output'
            for key in REMOVED_STRATEGIES:
                with self.subTest(strategy=key),self.assertRaisesRegex(ValueError,'Strategy removed:'):
                    run_backtest(Config(strategy=key),output=output)
            read.assert_not_called()
            self.assertFalse(output.exists())

    def test_obsolete_fields_load_for_remaining_records_but_unknown_fields_fail(self):
        old=record(STRATEGIES[0])['config']
        restored=config_from_saved(old)
        restored.validate()
        self.assertEqual(asdict(restored),asdict(current_config()))
        with self.assertRaises(TypeError):config_from_saved(dict(old,unrecognized_rule=True))

    def test_saved_removed_and_remaining_records_are_read_without_mutation(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'saved.json'
            records=[record(STRATEGIES[0]),*[record(k,i+1) for i,k in enumerate(sorted(REMOVED_STRATEGIES))]]
            path.write_text(json.dumps(records))
            before=path.read_bytes()
            self.assertEqual(load_saved_strategies(path),records)
            self.assertEqual(path.read_bytes(),before)
            for r in records[1:]:
                self.assertIn('Strategy removed',strategy_label(r['config']))
                with self.assertRaisesRegex(ValueError,'Strategy removed'):
                    form_values(r['config'],r['name'])

    def test_paper_account_and_launcher_reject_before_any_writes(self):
        from paper_engine import PaperAccount
        from quant_deployment import launch_strategy
        with TemporaryDirectory() as folder,patch('quant_deployment.subprocess.Popen') as start:
            root=Path(folder)/'account'
            for key in REMOVED_STRATEGIES:
                with self.subTest(strategy=key):
                    with self.assertRaisesRegex(ValueError,'Strategy removed'):
                        PaperAccount(root,Config(strategy=key),None)
                    with self.assertRaisesRegex(ValueError,'Strategy removed'):
                        launch_strategy(record(key),root,Path(folder)/'saved.json')
            start.assert_not_called()
            self.assertFalse(root.exists())

    def test_paper_cli_saved_configuration_rejects_removed_before_feed(self):
        from step6_paper_trader import configuration
        with TemporaryDirectory() as folder:
            path=Path(folder)/'saved.json'
            for key in REMOVED_STRATEGIES:
                path.write_text(json.dumps([record(key)]))
                with self.subTest(strategy=key),self.assertRaisesRegex(ValueError,'Strategy removed'):
                    configuration(SimpleNamespace(saved_strategy='saved_0',strategies_file=path))

    def test_modules_are_absent_and_no_active_import_depends_on_them(self):
        names={'quant_trademind','trademind_backtest','trademind_data','trademind_signals','trademind_ticks',
            'trademind_ui','trademind_native','trademind_native_data','trademind_native_ui','sync_trademind_to_dhan_lake'}
        root=Path(__file__).parent
        for name in names:
            self.assertFalse((root/(name+'.py')).exists())
            self.assertIsNone(importlib.util.find_spec(name))
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                imported=[x.name.split('.')[0] for x in node.names] if isinstance(node,ast.Import) else [node.module.split('.')[0]] if isinstance(node,ast.ImportFrom) and node.module else []
                self.assertFalse(names.intersection(imported),str(path))

    def test_supported_old_ledger_uses_saved_values_with_obsolete_fields(self):
        from quant_results import trade_ledger
        from test_quant_book import fixture
        c,data,_=fixture()
        result=run_backtest(c,data,persist=False)
        old=dict(asdict(c),**{key:'obsolete' for key in OBSOLETE_CONFIG_FIELDS})
        ledger=trade_ledger(result['_frames']['trades'],old)
        self.assertGreater(len(ledger),0)
        self.assertAlmostEqual(ledger['Net P&L (₹)'].sum(),result['metrics']['net_pnl'])

    def test_ui_removed_records_disabled_and_paper_selector_excludes_them(self):
        from streamlit.testing.v1 import AppTest
        with TemporaryDirectory() as folder:
            root=Path(folder);output=root/'backtests';path=saved_strategy_path(output)
            path.parent.mkdir(parents=True)
            records=[record(STRATEGIES[0]),*[record(k,i+1) for i,k in enumerate(sorted(REMOVED_STRATEGIES))]]
            path.write_text(json.dumps(records));before=hashlib.sha256(path.read_bytes()).hexdigest()
            with patch('quant_backtester.OUT',output),patch('quant_lake.ROOT',root/'lake'):
                app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=45).run()
                self.assertFalse(app.exception)
                self.assertEqual(app.selectbox(key='bt_strategy').options,list(STRATEGY_LABELS))
                self.assertFalse(any(x.label in ('Execution mode','Native pricing policy','Price source') for x in app.selectbox))
                for r in records[1:]:self.assertTrue(app.button(key='load_'+r['strategy_id']).disabled)
                self.assertTrue(any('Strategy removed' in x.value for x in app.warning))
                app.session_state.pending_strategy=records[1]
                app.run()
                self.assertFalse(app.exception)
                self.assertTrue(any('Strategy removed' in x.value for x in app.warning))
                app.radio(key='page').set_value('Live Paper Trading').run()
                self.assertFalse(app.exception)
                self.assertEqual(app.selectbox(key='paper_saved_strategy').options,[records[0]['name']])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_removed_historical_snapshot_renders_without_any_implementation(self):
        from streamlit.testing.v1 import AppTest
        with TemporaryDirectory() as folder:
            root=Path(folder)
            with patch('quant_backtester.OUT',root/'backtests'),patch('quant_lake.ROOT',root/'lake'):
                app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=45).run()
                app.session_state.active_result=dict(run_id='old_saved',config=record(next(iter(REMOVED_STRATEGIES)))['config'],
                    metrics=dict(net_pnl=None,total_trades=1),_frames=dict(trades=pd.DataFrame([dict(status='invalidated',net_pnl=None)])))
                app.run()
                self.assertFalse(app.exception)
                self.assertTrue(any('Historical snapshot only' in x.value for x in app.warning))
                self.assertTrue(len(app.dataframe))
                self.assertFalse(any(x.key=='save_result' for x in app.button))


if __name__=='__main__':unittest.main()
