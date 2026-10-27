"""Exercise live Streamlit widgets and a real, bounded custom backtest."""
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from streamlit.testing.v1 import AppTest
from quant_backtester import run_backtest, OUT, HIGH_STRATEGIES


def main():
    saved=[]
    def execute(config):
        assert config.capital==500000 and config.capital_usage_pct==50
        assert config.futures_margin_pct==20 and config.daily_profit_target==1500
        assert config.start=='2026-10-01' and config.end=='2026-10-23'
        result=run_backtest(config,output=OUT/'ui_step5_smoke')
        saved.append(result)
        return result
    with patch('quant_backtester.run_backtest',side_effect=execute), patch('quant_backtester.refresh_leaderboard',return_value=pd.DataFrame()):
        app=AppTest.from_file('quant_ui.py',default_timeout=120).run()
        def clean():
            assert not app.exception,[e.message for e in app.exception]
        def widget(kind,label): return next(w for w in getattr(app,kind) if w.label==label)
        clean(); assert len(app.tabs)==4
        for instrument in ('NIFTY','SENSEX','CRUDEOIL','NATURALGAS','GOLDM'):
            widget('selectbox','Instrument').select(instrument).run(); clean()
            widget('selectbox','Strategy mode').select('High-Risk Explosive Growth').run(); clean()
            expected=HIGH_STRATEGIES[3] if instrument=='GOLDM' else HIGH_STRATEGIES[5] if instrument in ('CRUDEOIL','NATURALGAS') else HIGH_STRATEGIES[4]
            assert widget('selectbox','Test strategy').value==expected
        widget('selectbox','Instrument').select('CRUDEOIL').run()
        widget('selectbox','Strategy mode').select('Steady Income').run()
        widget('selectbox','Test strategy').select(HIGH_STRATEGIES[3]).run()
        widget('date_input','From Date').set_value('2026-10-01')
        widget('number_input','Total trading capital (₹)').set_value(500000)
        widget('slider','Capital usage / maximum concurrent exposure (%)').set_value(50)
        widget('slider','Futures margin assumption (% of notional)').set_value(20)
        widget('button','Run & save backtest').click().run(timeout=120)
        clean()
        assert not app.error,[e.value for e in app.error]
        assert saved and saved[0]['metrics']['unresolved_trades']==0
        trades=pd.read_parquet(Path(saved[0]['path'])/'trades.parquet')
        assert len(trades)>0,'Smoke window must exercise actual fills'
        assert (trades.capital_used_pct<=50).all()
        print(f'PASS: all instruments/modes/four tabs; real custom run {saved[0]["run_id"]}, {len(trades)} trades, zero unresolved.')


if __name__=='__main__': main()
