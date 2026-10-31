"""New strategies, exact chart fills, named deployment and causal risk fixtures."""
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
import numpy as np
import pandas as pd

from quant_backtester import (Config, RESEARCH_STRATEGIES, ResearchData, research_events,
                              research_indicators, wilder, run_backtest, make_bars, STRATEGIES)
from quant_option_model import MissingObservedPrice
from quant_terminal import (STRATEGY_LABELS, build_config, save_strategy, saved_strategy_path,
                            load_saved_strategies, instrument_defaults)
from quant_results import trade_chart, trade_ledger
from quant_deployment import launch_strategy, account_path
from paper_feed import Asset, Tick, IST, option_asset, option_volume_confirmed
from paper_engine import PaperAccount, MinuteBook
from step6_paper_trader import configuration


DATE='2026-10-22'


def config(instrument='NIFTY', strategy='vwap_ema_pullback', timeframe='5m'):
    return Config(instrument=instrument,strategy=strategy,timeframe=timeframe,start=DATE,end='2026-10-23',
        capital=1_000_000,entry_start='17:00' if instrument=='NATURALGAS' and strategy!='vwap_ema_pullback' else '09:25',
        entry_end='22:45' if instrument=='NATURALGAS' and strategy!='vwap_ema_pullback' else '14:30',volume_multiple=1.3,
        risk_basis='percentage',exit_mode='target_eod',trail_pct=0,sl_pct=10,target_pct=20,slippage=.5)


def signal_fixture(c, bearish=False):
    mcx=c.instrument=='NATURALGAS'
    n=int(c.timeframe[:-1]); opening=540 if mcx else 555
    at=(750 if c.strategy=='vwap_ema_pullback' else 1080) if mcx else 750
    stamp=pd.Timestamp(DATE,tz='Asia/Kolkata')
    rows=[]
    for minute in range(opening,at,n):
        rows.append(dict(timestamp=stamp+pd.Timedelta(minutes=minute),open=100.,high=105.,low=95.,close=100.,
            volume=100.,ema9=100.,ema21=98.,ema50=98.,adx=30.,atr=2.,atr14=2.,
            rsi=60.,trend=1,supertrend=90.,vwap=99.,vwap_proxy=False))
    bars=pd.DataFrame(rows)
    bars.loc[len(bars)-1,['open','high','low','close','volume']]=[100,112,99,110,200]
    if bearish:
        bars.loc[len(bars)-1,['high','low','close','ema9','ema21','ema50','vwap','rsi','trend']]=[101,88,90,98,100,100,99,30,-1]
        bars.loc[len(bars)-2,'trend']=-1
    day=dict(timestamp=stamp,contract='FIXED_TEST',
        prices={t:(100,105,95,100,100) for t in range(opening,1396 if mcx else 921)},
        rolling={(s,'ATM'):{t:24000 for t in range(opening,921)} for s in ('CE','PE')})
    data=SimpleNamespace(mcx=mcx,instrument=c.instrument,start=c.start,end=c.end,fingerprint='signal-fixture',
        days={DATE:day},available_days=[DATE],exclusions=[],has_calendar=False,expiry_calendar=set(),
        features=lambda tf:{DATE:bars},research_features=lambda tf:{DATE:bars})
    def price(day,t,side,strike):
        # Fixed contract, real costs and next-open fills exercised below.
        return (100,125,99,110,200 if at-n<=t<at else 100)
    data.price=price
    data.events=lambda c,date:research_events(data,c,date)
    return data,bars,at


class StrategyTests(unittest.TestCase):
    def test_public_strategy_catalog_preserves_four_research_keys_and_adds_four_book_keys(self):
        self.assertEqual(STRATEGIES[:4],RESEARCH_STRATEGIES)
        self.assertEqual(len(STRATEGIES),9)
        self.assertIn(Config().strategy,RESEARCH_STRATEGIES)

    def test_all_four_keys_instrument_matrix_and_both_intervals(self):
        for instrument in ('NIFTY','SENSEX','NATURALGAS'):
            for strategy in RESEARCH_STRATEGIES:
                for tf in ('5m','15m'):
                    c=config(instrument,strategy,tf)
                    if strategy=='mcx_evening_breakout' and instrument!='NATURALGAS':
                        with self.assertRaisesRegex(ValueError,'supports NATURALGAS'): c.validate()
                    else:
                        c.validate()
                        for bearish in (False,True):
                            with self.subTest(instrument=instrument,strategy=strategy,timeframe=tf,bearish=bearish):
                                data,bars,at=signal_fixture(c,bearish)
                                events=research_events(data,c,DATE)
                                side=('SHORT' if bearish else 'LONG') if data.mcx else ('PE' if bearish else 'CE')
                                self.assertIn((at,side,0 if data.mcx else 24000,None,2.),events)
                                result=run_backtest(c,data,persist=False)
                                trades=result['_frames']['trades']
                                self.assertGreater(len(trades),0)
                                self.assertLessEqual(len(trades),2)
                                self.assertTrue((trades.total_cost>40).all())
                                np.testing.assert_allclose(trades.gross_pnl-trades.total_cost,trades.net_pnl)

    def test_rules_reject_chop_weak_volume_unconfirmed_breaks_and_morning_mcx(self):
        for key in RESEARCH_STRATEGIES:
            c=config('NATURALGAS' if key=='mcx_evening_breakout' else 'NIFTY',key)
            data,bars,at=signal_fixture(c)
            if key=='vwap_ema_pullback': bars['adx']=17
            elif key=='orb_volume_breakout': bars['volume']=100
            elif key=='supertrend_adx_trend': bars['adx']=22
            else: bars.loc[len(bars)-1,'open']=109
            self.assertFalse(research_events(data,c,DATE),key)
        c=config('NATURALGAS','mcx_evening_breakout')
        data,bars,at=signal_fixture(c)
        bars['timestamp']-=pd.Timedelta(hours=4)
        self.assertFalse(research_events(data,c,DATE))

    def test_zero_spot_volume_requires_observed_fixed_strike_history(self):
        c=config(strategy='orb_volume_breakout')
        data,bars,at=signal_fixture(c)
        bars['volume']=0;bars['vwap_proxy']=True
        self.assertTrue(research_events(data,c,DATE))
        evidence=data.days[DATE]['signal_evidence'][c.strategy,c.timeframe,at,'CE',24000]
        self.assertEqual(evidence['signal_volume_basis'],'fixed_strike_option')
        original=data.price
        data.price=lambda d,t,s,k: None if t==at-11*5 else original(d,t,s,k)
        self.assertFalse(research_events(data,c,DATE))
        data.price=lambda d,t,s,k:(100,125,99,110,100)
        self.assertFalse(research_events(data,c,DATE))

    def test_causal_indicators_known_wilder_seed_and_no_future_leak(self):
        series=pd.Series([1.,2.,3.,4.,5.])
        np.testing.assert_allclose(wilder(series,3).iloc[2:],[2.,8/3,31/9])
        n=100;price=100+np.sin(np.arange(n)/3)*4+np.arange(n)*.2
        raw=pd.DataFrame(dict(timestamp=pd.date_range(DATE+' 09:15',periods=n,freq='5min',tz='Asia/Kolkata'),
            open=price,high=price+2,low=price-2,close=price+1,volume=100))
        full=research_indicators(raw)
        short=research_indicators(raw.iloc[:60])
        pd.testing.assert_frame_equal(full.iloc[:60],short)
        self.assertEqual(full.atr.first_valid_index(),9)
        self.assertEqual(full.adx.first_valid_index(),26)
        self.assertTrue(full.rsi.dropna().between(0,100).all())
        self.assertTrue((full.loc[full.trend.gt(0),'supertrend']<full.loc[full.trend.gt(0),'close']).all())
        self.assertTrue((full.loc[full.trend.lt(0),'supertrend']>=full.loc[full.trend.lt(0),'close']).all())

    def test_bar_close_execution_boundary_and_future_change_is_causal(self):
        c=config()
        data,bars,at=signal_fixture(c)
        events=research_events(data,c,DATE)
        self.assertEqual(events[-1][0],at)
        self.assertEqual(bars.iloc[-1].timestamp+pd.Timedelta(minutes=5), data.days[DATE]['timestamp']+pd.Timedelta(minutes=at))
        late=bars.iloc[-1:].copy();late['timestamp']+=pd.Timedelta(minutes=5);late['close']=999
        extended=pd.concat([bars,late],ignore_index=True)
        data.research_features=lambda tf:{DATE:extended}
        self.assertEqual([e for e in research_events(data,c,DATE) if e[0]<=at],events)

    def test_chart_marker_coordinates_levels_and_ledger_cost_reconciliation(self):
        c=config();data,bars,at=signal_fixture(c)
        r=run_backtest(c,data,persist=False);f=r['_frames'];trade=f['trades'].iloc[0]
        fig=trade_chart(trade,f['underlying'],f['stop_paths'],r['config'])
        for name,time,price in [('▲ ENTRY',trade.entry_time,trade.entry_price),('▼ EXIT',trade.exit_time,trade.exit_price)]:
            marker=next(t for t in fig.data if t.name==name)
            self.assertEqual(pd.Timestamp(marker.x[0]),time)
            self.assertEqual(marker.y[0],price)
            self.assertEqual(marker.yaxis,'y2')
        for name in ('Entry price','Stop loss price','Target price'):
            line=next(t for t in fig.data if t.name==name)
            self.assertEqual(list(line.x),[trade.entry_time,trade.exit_time])
        ledger=trade_ledger(f['trades'],r['config'])
        np.testing.assert_allclose(ledger['Gross P&L']-ledger['Costs'],ledger['Net P&L (₹)'])
        self.assertAlmostEqual(ledger['Balance (₹)'].iloc[-1],c.capital+r['metrics']['net_pnl'])
        self.assertIn('BUY CE',ledger.Side.tolist())

    def test_execution_model_failure_has_coordinates_and_never_saves_partial_result(self):
        c=config();data,bars,at=signal_fixture(c)
        data.price=lambda day,t,*args:(100,101,99,100,100) if t<=at else None
        with TemporaryDirectory() as directory:
            output=Path(directory)/'runs'
            with patch('quant_backtester.continuation_candle',side_effect=MissingObservedPrice('No priced neighbor')):
                with self.assertRaisesRegex(MissingObservedPrice,'2026-10-22 12:31 CE 24000'):
                    run_backtest(c,data,output=output,persist=False)
            self.assertFalse(output.exists())


class SavedDeploymentTests(unittest.TestCase):
    def test_live_mcx_requires_morning_warmup_and_uses_shared_strategy_rules(self):
        for tf,hour,minute in [('5m',17,35),('15m',17,45)]:
            c=config('NATURALGAS','orb_volume_breakout',tf)
            now=datetime(2026,10,22,hour,minute,tzinfo=IST).timestamp()
            start=datetime(2026,10,22,9,0,tzinfo=IST).timestamp()
            rows=[dict(timestamp=t,open=100,high=101,low=99,close=100,volume=10) for t in range(int(start),int(now),60)]
            book=MinuteBook(history_days=5);book.seed(rows,now)
            event=(hour*60+minute,'LONG',0,None,2)
            with patch('paper_engine.research_events',return_value=[event]) as shared:
                self.assertEqual(book.signal(c,now),event)
                self.assertTrue(shared.call_args.kwargs['confirm_volume'])
                self.assertEqual(shared.call_args.args[1],c)
            del book.rows[int(start)+5*60]
            book.last_signal_minute=None
            with patch('paper_engine.research_events') as shared:
                self.assertIsNone(book.signal(c,now));shared.assert_not_called()

    def test_named_strategy_roundtrip_cli_exact_policy_and_virtual_launcher(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);output=root/'backtests';c=config('SENSEX','supertrend_adx_trend')
            record=save_strategy(output,c,'Sensex_Custom_5m')
            path=saved_strategy_path(output)
            self.assertEqual(load_saved_strategies(path)[0]['name'],'Sensex_Custom_5m')
            args=SimpleNamespace(saved_strategy=record['strategy_id'],strategies_file=path)
            actual=asdict(configuration(args));expected=asdict(c)
            for key in ('start','end'): actual.pop(key);expected.pop(key)
            self.assertEqual(actual,expected)
            with patch('quant_deployment.subprocess.Popen',return_value=Mock(pid=1234)) as popen:
                self.assertEqual(launch_strategy(record,root/'paper',path),1234)
                command=popen.call_args.args[0]
                self.assertIn('--saved-strategy',command)
                self.assertIn(record['strategy_id'],command)
                self.assertNotIn('--mode demo',command)
            self.assertEqual(account_path(root/'paper',record).parent,root/'paper')
            changed=save_strategy(output,replace(c,minimum_premium=90),'Sensex_Custom_5m')
            self.assertEqual(len(load_saved_strategies(path)),1)
            self.assertNotEqual(account_path(root/'paper',record),account_path(root/'paper',changed))

    def test_current_master_contract_resolution_and_exact_option_lot_size(self):
        at=datetime(2026,10,22,10,0,tzinfo=IST).timestamp()
        for instrument,exchange,segment in [('NIFTY','NSE',2),('SENSEX','BSE',8)]:
            frame=pd.DataFrame([dict(EXCH_ID=exchange,INSTRUMENT='OPTIDX',SYMBOL_NAME=instrument,
                OPTION_TYPE=side,SM_EXPIRY_DATE=expiry,STRIKE_PRICE=strike,SECURITY_ID=100+i,LOT_SIZE=20)
                for i,(side,expiry,strike) in enumerate([('CE',DATE,24000),('CE',DATE,24100),('PE',DATE,24000),('CE','2026-10-29',24000)])])
            asset=option_asset(frame,instrument,'CE',24020,at)
            self.assertEqual((asset.segment,asset.strike,asset.expiry,asset.point_value),(segment,24000,DATE,20))

    def test_option_paper_execution_keeps_percentage_exits_and_premium_filter(self):
        now=datetime(2026,10,22,10,0,tzinfo=IST).timestamp()
        asset=Asset('NIFTY',2,12345,'2026-10-27',65,24000,'CE')
        c=replace(config(),minimum_premium=100,slippage=0)
        with TemporaryDirectory() as directory:
            a=PaperAccount(directory,c,asset,lambda _:None)
            quote=lambda p,t:Tick(asset.key,p,t,t,1000)
            event=(600,'CE',24000,None,2)
            self.assertFalse(a.enter(event,quote(99,now),now))
            # New minute: below-premium signal was consumed, as in live polling.
            event=(605,'CE',24000,None,2);now+=300
            self.assertTrue(a.enter(event,quote(100,now),now))
            self.assertEqual(a.position['stop'],90)
            self.assertEqual(a.position['target'],120)
            a.on_tick(quote(125,now+1),now+1)
            self.assertEqual(a.state['trades'][0]['exit_reason'],'target')

    def test_fixed_strike_live_volume_confirmation_rejects_holes_and_flat_volume(self):
        now=datetime(2026,10,22,11,0,tzinfo=IST).timestamp();c=config(strategy='orb_volume_breakout')
        rows=[dict(timestamp=now-55*60+i*60,volume=200 if i>=50 else 100) for i in range(55)]
        event=(660,'CE',24000,None,1)
        self.assertTrue(option_volume_confirmed(rows,c,event,now))
        self.assertFalse(option_volume_confirmed(rows[1:],c,event,now))
        for r in rows:r['volume']=100
        self.assertFalse(option_volume_confirmed(rows,c,event,now))


if __name__=='__main__': unittest.main()
