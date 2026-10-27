"""Exercise dark-terminal navigation and real bounded option / MCX simulations."""
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import pandas as pd
from streamlit.testing.v1 import AppTest
from quant_backtester import run_backtest
from quant_terminal import PAGES, load_registry


def main():
    saved=[]
    with TemporaryDirectory() as directory:
        output=Path(directory)
        def execute(config, output):
            assert config.capital==2000000 and config.capital_usage_pct==75
            assert config.start=='2026-10-01' and config.end=='2026-10-23'
            assert config.minimum_premium==100 and config.risk_basis=='percentage'
            result=run_backtest(config,output=output)
            saved.append(result)
            return result
        with patch('quant_backtester.OUT',output), patch('quant_backtester.run_backtest',side_effect=execute):
            app=AppTest.from_file('quant_ui.py',default_timeout=180).run()
            def clean():
                assert not app.exception,[e.message for e in app.exception]
                assert not app.error,[e.value for e in app.error]
            clean(); assert not app.tabs
            for page in PAGES:
                app.radio(key='page').set_value(page).run(); clean()
            app.radio(key='page').set_value('Backtests').run()
            for instrument,strategy in [('NIFTY 50','Buy CE / Buy PE'),('CRUDEOIL','MCX Breakout')]:
                app.selectbox(key='bt_instrument').select(instrument)
                app.selectbox(key='bt_strategy').select(strategy)
                app.date_input(key='bt_period').set_value((date(2026,10,1),date(2026,10,22)))
                app.number_input(key='bt_capital').set_value(2000000)
                app.selectbox(key='bt_allocation').select('75%')
                app.button(key='run_backtest').click().run(timeout=180); clean()
                result=saved[-1]
                assert result['metrics']['unresolved_trades']==0
                trades=pd.read_parquet(Path(result['path'])/'trades.parquet')
                assert len(trades)>0, 'Real smoke window must exercise actual fills'
                assert trades.capital_used_pct.le(75).all()
                if instrument=='NIFTY 50': assert trades.raw_entry_price.ge(100).all()
                assert app.get('plotly_chart')
                app.button(key='save_strategy').click().run(); clean()
                print(f"PASS: {instrument}, {len(trades)} trades, zero unresolved, saved configuration and chart.")
            registry=load_registry(output)
            assert len(registry['runs'])==2 and len(registry['strategies'])==2
            assert [r['display_id'] for r in registry['runs']]==['BT-001','BT-002']
            app.radio(key='page').set_value('Reports').run(); clean()
            assert len(app.get('plotly_chart'))==2
            print('PASS: seven sidebar pages, real option and MCX backtests, premium filter, allocation, persistence and Reports replay.')


if __name__=='__main__': main()
