"""TradeMind selector safety, frozen form controls and rejected-result state."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from quant_trademind import STRATEGIES, Unavailable
from trademind_backtest import LABELS


class TradeMindUiTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.root=Path(self.temp.name)/'output'
        self.root.mkdir()
        self.retained=Path(self.temp.name)/'retained'
        (self.retained/'manifests/EXPIRED_OPTION_1M').mkdir(parents=True)
        (self.retained/'research-underlying-certifications').mkdir()
        # Exercise real availability using temporary prerequisites. No dependency
        # on this machine's C:/TSA tree, and rejection assertions remain active.
        self.source_root=patch('trademind_data.DEFAULT_ROOT',self.retained)
        self.source_root.start()

    def tearDown(self):
        self.source_root.stop()
        self.temp.cleanup()

    def component(self,strategy):
        code='from trademind_ui import render_form\n'+f'render_form({strategy!r},"NIFTY 50",{str(self.root)!r})'
        return AppTest.from_string(code,default_timeout=45).run()

    def test_all_six_default_dhan_choices_block_run_and_parameter_tuning(self):
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy):
                app=self.component(strategy)
                self.assertFalse(app.exception,[x.message for x in app.exception])
                self.assertTrue(app.button(key='run_backtest').disabled)
                self.assertFalse(app.session_state['active_result'])
                self.assertTrue(any('DATA_UNAVAILABLE' in x.value for x in app.warning))
                self.assertFalse(any(w.label in ('Take profit','Stop loss','Minimum premium') for w in app.selectbox))
                self.assertEqual(len(app.metric),0)

    def test_source_rejection_clears_old_result_and_does_not_save_success(self):
        app=self.component(STRATEGIES[0])
        app.selectbox(key='tm_provider').set_value('trademind_retained').run()
        self.assertFalse(app.exception)
        app.session_state['active_result']={'old':'successful result'}
        with patch('quant_backtester.run_backtest',side_effect=Unavailable('synthetic missing dated candle')) as run:
            app.button(key='run_backtest').click().run()
        self.assertEqual(run.call_count,1)
        self.assertFalse(app.exception)
        self.assertIsNone(app.session_state['active_result'])
        self.assertTrue(any('missing dated candle' in e.value for e in app.error))
        self.assertFalse(app.success)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_missing_retained_directories_disable_run_without_creating_them(self):
        missing=Path(self.temp.name)/'absent_retained'
        with patch('trademind_data.DEFAULT_ROOT',missing),patch('quant_backtester.run_backtest') as run:
            app=self.component(STRATEGIES[0])
            app.selectbox(key='tm_provider').set_value('trademind_retained').run()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key='run_backtest').disabled)
        self.assertTrue(any('Retained manifest-bound data are absent' in w.value for w in app.warning))
        self.assertIsNone(app.session_state['active_result'])
        self.assertFalse(missing.exists())
        run.assert_not_called()

    def test_full_selector_integrates_new_strategy_and_clears_previous_view(self):
        with patch('quant_backtester.OUT',self.root/'backtests'),patch('quant_lake.ROOT',self.root/'lake'):
            app=AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')),default_timeout=45).run()
            app.selectbox(key='bt_strategy').set_value(LABELS[STRATEGIES[2]]).run()
            self.assertFalse(app.exception,[x.message for x in app.exception])
            self.assertTrue(app.button(key='run_backtest').disabled)
            self.assertEqual(len(app.get('plotly_chart')),0)
            self.assertIsNone(app.session_state['active_result'])
            self.assertTrue(any('2m native rules' in c.value for c in app.caption))

    def test_paper_selector_explicitly_disables_trademind_without_account_mutation(self):
        from quant_backtester import Config
        from quant_terminal import save_strategy,saved_strategy_path
        from trademind_backtest import frozen_config
        output=self.root/'backtests'
        save_strategy(output,frozen_config(Config(strategy=STRATEGIES[3],capital=100000,start='2026-01-01',end='2027-01-01')),'T3 research only')
        code='from quant_deployment import render_strategy_deployment\n'+f'render_strategy_deployment({str(self.root/"accounts")!r},{str(saved_strategy_path(output))!r})'
        app=AppTest.from_string(code,default_timeout=45).run()
        self.assertFalse(app.exception)
        self.assertTrue(any('backtest-only' in w.value for w in app.warning))
        self.assertFalse(any(b.key=='deploy_paper' for b in app.button))
        self.assertFalse((self.root/'accounts').exists())


if __name__=='__main__':unittest.main()
