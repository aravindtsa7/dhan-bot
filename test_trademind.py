"""Source-executed parity fixtures, strict evidence guards and independent accounting.

Expected fixture values were emitted by isolated original TypeScript modules,
not by the Python functions under test. See fixture authority and review scripts.
"""
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from quant_backtester import Config, STRATEGIES as PUBLIC, run_backtest
from quant_trademind import (STRATEGIES, Contract, Unavailable, V2State, V4State, V8State,
    atm_lower_half, close_then_open_exit, floor_gate, indicator_series, orb_signal,
    phase_b_exit, rise5m, select_contract, size_lots, source_fees, strategy_fingerprint,
    t3_structure, t3_exit)
from trademind_backtest import availability, capital_ledger, dated_lot, frozen_config
from trademind_signals import IST, generate_signals, stamp
from trademind_ticks import simulate_tick_day, quote_ineligibility, attempt_exit, build_candles

FIXTURE = json.loads((Path(__file__).parent / 'tests/fixtures/trademind/source_parity.json').read_text(encoding='utf-8'))


def sessions(case):
    days = defaultdict(dict)
    for b in case['input']:
        time = datetime.fromisoformat(b['timestamp']).astimezone(IST)
        days[str(time.date())][time.hour * 60 + time.minute] = {k:v for k,v in b.items() if k != 'timestamp'}
    return [dict(date=d,bars=b) for d,b in sorted(days.items())]


def case(name):
    return next(c for c in FIXTURE['cases'] if c['id'] == name)


class TradeMindSourceParityTests(unittest.TestCase):
    def assert_signals(self, strategy, fixture_name):
        source = case(fixture_name)
        actual, _ = generate_signals(strategy, sessions(source))
        self.assertEqual(len(actual), len(source['expected']))
        for got, expected in zip(actual, source['expected']):
            self.assertEqual(got['timestamp'], int(datetime.fromisoformat(expected['decisionTimestamp']).timestamp() * 1000))
            self.assertEqual('BUY_' + got['side'], expected['direction'])
            self.assertEqual(got['spot'], expected['underlyingReference']['price'])

    def test_v2_retained_source_signal(self):
        self.assert_signals(STRATEGIES[0], 'v2_source_retained_fixture')

    def test_v4_retained_source_signal_and_regime_completion_order(self):
        self.assert_signals(STRATEGIES[1], 'v4_source_retained_fixture')

    def test_v8_coherent_separate_fixture_reproduces_source_signal(self):
        self.assert_signals(STRATEGIES[2], 'v8_coherent_harness_fixture')

    def test_retained_v8_fixture_ohlc_conflict_remains_an_explicit_discrepancy(self):
        original = case('v8_source_retained_fixture')
        self.assertEqual(len(original['expected']), 1)  # legacy source tolerates malformed OHLC
        with self.assertRaisesRegex(Unavailable, 'INVALID_UNDERLYING_BUCKET'):
            generate_signals(STRATEGIES[2], sessions(original))

    def test_indicator_seed_and_recurrences_match_executed_source(self):
        source = FIXTURE['indicators']
        actual = indicator_series(source['input'])
        checked = 0
        for a,e in zip(actual, source['expected']):
            if e is None:
                continue
            for key,value in e.items():
                self.assertAlmostEqual(a[key], value, places=10)
                checked += 1
        self.assertGreater(checked, 50)

    def test_all_three_frozen_fingerprint_preimages(self):
        preimages = [
            dict(strategyId='V2_TREND_DOWN_PE',timeframe='5m',regime='TREND_DOWN',ema=['EMA15','EMA35'],proximityPercent=.2,rsi='RSI14<35',cooldownMinutes=10,targetPercent=5,stopPercent=5,holdMinutes=15),
            dict(strategyId='V4_NIFTY_MOMENTUM_PE_SHADOW',timeframe='3m',compressionBars=3,compressionRangeAtr=2,bodyAtr=1,breakoutAtr=.1,regime='TREND_DOWN',cooldownMinutes=5,targetPercent=5,stopPercent=5,holdMinutes=15,shadowOnly=True),
            dict(strategyId='V8_NIFTY_BULLISH_RECLAIM_CE_SHADOW',sourceStrategyId='V8_NIFTY_BULLISH_RECLAIM_CE',timeframe=2,structuralLevelType='PDH',reclaimBufferAtr=.1,bullishBodyAtr=.25,rsiGate=50,regimeGate='NO_REGIME_FILTER',cooldownMinutes=5,
                 episodeRearmSemantics='PRIOR_COMPLETED_INTERACTION_ARMS; SUBSEQUENT_COMPLETED_BULLISH_RECLAIM_EMITS; COOLDOWN_BLOCK_CONSUMES_EPISODE; SESSION_RESET',optionSelectionSemantics='EXISTING_DETERMINISTIC_NIFTY_HISTORICAL_NEAR_ATM_CE_SELECTOR',targetPercent=6,stopPercent=5,maximumHoldMinutes=15,shadowOnly=True)]
        self.assertEqual([strategy_fingerprint(p) for p in preimages],
                         ['f8a0ee53d6fdeb8a','549aacaca3149ffd','c815f819acd71e98'])

    def test_independent_source_fee_components_without_dhan_rounding(self):
        for c in FIXTURE['fees']:
            i = c['input']
            actual = source_fees(i['date'],i['buy'],i['sell'],i['q'])
            for k,v in actual.items():
                self.assertAlmostEqual(v,c['expected'][k],places=10)
        with self.assertRaisesRegex(Unavailable,'NOT_COMPUTABLE'):
            source_fees('2025-06-01',100,110,75)

    def test_h30_and_orb_source_exit_paths_not_shared_threshold_fills(self):
        for source in FIXTURE['exits']:
            ce, pe = dict(source['ce']), dict(source['pe'])
            h30 = source['kind'] == 'H30'
            actual = close_then_open_exit(source['entry'],source['session'],[ce,pe] if h30 else [ce],kind=STRATEGIES[4] if h30 else STRATEGIES[5])
            expected = source['expected']['unit'] if h30 else source['expected']
            self.assertEqual(actual['status'],expected['status'])
            if actual['status'] == 'RESOLVED':
                self.assertEqual(actual['exitMinute'],expected['exitMinute'])
                self.assertAlmostEqual(actual['points'],expected['points'])
                self.assertEqual(actual['trigger'],'TIMEOUT' if expected['trigger'] == 'EOD' else expected['trigger'])

    def test_phase_b_original_source_fill_semantics(self):
        for source in FIXTURE['phaseB']:
            bars = {datetime.fromisoformat(b['timestamp']).astimezone(IST).hour*60+datetime.fromisoformat(b['timestamp']).astimezone(IST).minute:b for b in source['input']['candles']}
            actual = phase_b_exit(601,bars,STRATEGIES[0],require_contiguous=False)
            expected = source['expected']['outcome']
            if expected['status'] == 'EXITED':
                self.assertEqual(actual['status'],'RESOLVED')
                self.assertAlmostEqual(actual['exitPrices'][0],expected['exitPrice'])
                self.assertEqual(actual['trigger'],expected['exitReason'])
            else:
                self.assertEqual(actual['status'],'PNL_UNRESOLVED')
                self.assertIsNone(actual['points'])

    def test_phase_b_strict_gap_guard_is_a_declared_legacy_discrepancy(self):
        source = next(x for x in FIXTURE['phaseB'] if x['scenario'] == 'missing')
        bars = {datetime.fromisoformat(b['timestamp']).astimezone(IST).hour*60+datetime.fromisoformat(b['timestamp']).astimezone(IST).minute:b for b in source['input']['candles']}
        self.assertEqual(source['expected']['outcome']['status'],'EXITED')
        actual = phase_b_exit(601,bars,STRATEGIES[0])
        self.assertEqual(actual['status'],'PNL_UNRESOLVED')
        self.assertIn('MISSING_OR_INVALID',actual['reason'])

    def test_t3_retained_four_leg_prices_reproduce_source_pnl(self):
        for source in FIXTURE['t3']:
            r,e = source['row'],source['expected']
            structure = t3_structure(r['niftyReference'],r['vix0929Close'],r['sessionsToExpiry'],r['straddle']['ce'],r['straddle']['pe'])
            self.assertEqual(structure['wingDistance'],e['wing']['wingDistance'])
            self.assertEqual([x['ratio'] for x in structure['legs']],[-1,-1,1,1])
            names = ('shortCe','shortPe','longCe','longPe')
            legs = [{571:dict(open=r['entry'][k],close=r['entry'][k]),901:dict(open=r['exit'][k],close=r['exit'][k])} for k in names]
            actual = t3_exit(structure['wingDistance'],legs)
            self.assertEqual(actual['status'],'RESOLVED')
            self.assertAlmostEqual(actual['credit'],e['credit'])
            self.assertAlmostEqual(actual['points'],e['points'])

    def test_tick_variant_matches_independent_source_entry_exit_and_exposure(self):
        for source in FIXTURE['tick']:
            actual = simulate_tick_day(source['input'],lambda *args:dict(entryCharges=10,exitCharges=10,total=20))
            e = source['expected']
            self.assertEqual(actual['status'],e['status'])
            self.assertEqual(actual['entry']['qty'],e['entry']['quantity'])
            self.assertEqual(actual['entry']['askCe'],e['entry']['askCe'])
            if actual['status'] == 'ENTERED_EXITED':
                self.assertAlmostEqual(actual['gross'],e['pnl']['grossRealized'],places=6)
                self.assertAlmostEqual(actual['net'],e['pnl']['netRealized'],places=6)
            else:
                self.assertIsNone(actual['net'])


class TradeMindEvidenceAndStateTests(unittest.TestCase):
    def test_registry_adds_six_without_replacing_expiry_strategies(self):
        self.assertEqual(PUBLIC[8:10],['expiry_day','expiry_intraday'])
        self.assertEqual(PUBLIC[10:],list(STRATEGIES))

    def test_all_frozen_configs_are_strict_and_native(self):
        for s in STRATEGIES:
            c = frozen_config(Config(strategy=s,capital=100000,start='2026-01-01',end='2027-01-01'))
            c.validate()
            with self.assertRaisesRegex(ValueError,'strict_observed'):
                replace(c,price_policy='model_continuity').validate()
            with self.assertRaisesRegex(ValueError,'cannot be tuned'):
                replace(c,target_pct=99).validate()
            with self.assertRaisesRegex(ValueError,'NIFTY only'):
                replace(c,instrument='SENSEX').validate()

    def test_dhan_rolling_identity_is_blocked_not_zero_profit(self):
        c = frozen_config(Config(strategy=STRATEGIES[0],capital=100000,start='2026-01-01',end='2027-01-01'))
        self.assertFalse(availability(c.strategy)['runnable'])
        with self.assertRaisesRegex(Unavailable,'dated expiry'):
            run_backtest(c,persist=False)

    def test_paper_cli_launcher_and_account_reject_before_creating_files(self):
        from paper_engine import PaperAccount
        from quant_deployment import launch_strategy
        c = frozen_config(Config(strategy=STRATEGIES[0],capital=100000,start='2026-01-01',end='2027-01-01'))
        with TemporaryDirectory() as root:
            path=Path(root)/'account'
            with self.assertRaisesRegex(ValueError,'backtest-only'):
                PaperAccount(path,c,Mock())
            self.assertFalse(path.exists())
            with self.assertRaisesRegex(ValueError,'backtest-only'):
                launch_strategy(dict(config=__import__('dataclasses').asdict(c)),path,Path(root)/'strategies.json')
            self.assertFalse(path.exists())

    def test_zero_volume_flat_indicator_seed_is_rsi50_adx0(self):
        values = indicator_series([dict(open=100,high=100,low=100,close=100,volume=0) for _ in range(40)])
        self.assertEqual((values[-1]['rsi14'],values[-1]['adx14']),(50,0))
        self.assertIsNone(values[12]['atr14'])
        self.assertEqual(values[13]['atr14'],0)
        self.assertIsNone(values[33]['ema35'])
        self.assertEqual(values[34]['ema35'],100)

    def test_v2_cooldown_boundary_and_no_h30_floor(self):
        state=V2State()
        self.assertTrue(state.evaluate(0,'TREND_DOWN',99,100,100,34.99)['entry'])
        self.assertEqual(state.evaluate(599999,'TREND_DOWN',99,100,100,34.99)['reason'],'BLOCKED_COOLDOWN')
        self.assertTrue(state.evaluate(600000,'TREND_DOWN',99,100,100,34.99)['entry'])
        bars={m:dict(open=10,high=10.1,low=9.9,close=10) for m in range(602,616)}
        self.assertEqual(phase_b_exit(601,bars,STRATEGIES[0])['status'],'RESOLVED')

    def test_v4_compression_body_breakout_equalities(self):
        prior=[dict(high=102,low=100)]*3
        state=V4State()
        r=state.evaluate(0,dict(open=101,close=99),prior,1,'TREND_DOWN')
        self.assertTrue(r['signal'])
        self.assertEqual(r['compressionRangeAtr'],2)
        self.assertFalse(state.evaluate(300000,dict(open=101,close=99),prior,1,'TREND_UP')['signal'])

    def test_v8_interaction_reclaim_and_cooldown_consumes_episode(self):
        state=V8State()
        low=dict(open=101,high=102,low=99,close=100)
        high=dict(open=100,high=103,low=100,close=102)
        self.assertFalse(state.evaluate(0,low,100,1,50)['signal'])  # strict RSI>50
        self.assertFalse(state.armed)
        state.evaluate(120000,low,100,1,51)
        self.assertTrue(state.evaluate(240000,high,100,1,51)['signal'])
        state.evaluate(360000,low,100,1,51)
        blocked=state.evaluate(480000,high,100,1,51)
        self.assertEqual(blocked['reason'],'COOLDOWN')
        self.assertFalse(state.armed)

    def test_v8_incomplete_nearest_prior_session_is_not_replaced(self):
        data=sessions(case('v8_coherent_harness_fixture'))
        earlier=dict(date='2025-01-07',bars=data[0]['bars'])
        truncated=dict(date=data[0]['date'],bars={m:b for m,b in data[0]['bars'].items() if m!=900})
        signals,diagnostics=generate_signals(STRATEGIES[2],[earlier,truncated,data[1]])
        self.assertFalse(any(s['date']==data[1]['date'] for s in signals))
        self.assertTrue(any(d['status']=='REQUIRED_PRIOR_SESSION_INCOMPLETE' for d in diagnostics))

    def test_future_append_does_not_change_earlier_signals(self):
        original=sessions(case('v2_source_retained_fixture'))
        first,_=generate_signals(STRATEGIES[0],original)
        extension=[dict(date=x['date'],bars=dict(x['bars'])) for x in original]
        last=max(extension[0]['bars'])
        extension[0]['bars'][last+1]=dict(open=24000,high=25000,low=23000,close=24000,volume=0)
        second,_=generate_signals(STRATEGIES[0],extension)
        self.assertEqual(first,[x for x in second if x['timestamp']<=first[-1]['timestamp']])

    def test_exact_atm_tie_and_provider_contract_selection(self):
        self.assertEqual(atm_lower_half(24025),24000)
        self.assertEqual(atm_lower_half(24025.01),24050)
        contracts=[Contract('GROWW','P1','2026-05-12',24000,'PE',0,evidence='PIT'),Contract('OTHER','P2','2026-05-12',24025,'PE',0,evidence='PIT')]
        chosen=select_contract(contracts,provider='GROWW',expiry='2026-05-12',side='PE',spot=24025,decision=1)
        self.assertEqual(chosen.contract_id,'P1')
        self.assertIsNone(select_contract(contracts,provider='GROWW',expiry='2026-05-19',side='PE',spot=24025,decision=1))
        self.assertIsNone(select_contract(contracts,provider='GROWW',expiry='2026-05-12',side='PE',spot=24050,decision=1,exact=True))

    def test_h30_rise_uses_same_pair_all_six_closes_and_strict_operator(self):
        a={m:dict(open=120,close=120) for m in range(655,661)}
        b={m:dict(open=120,close=120) for m in range(655,661)}
        self.assertFalse(rise5m(661,set(range(555,930)),a,b)['qualifies'])
        b[660]['close']=120.01
        self.assertTrue(rise5m(661,set(range(555,930)),a,b)['qualifies'])
        del a[657]
        self.assertEqual(rise5m(661,set(range(555,930)),a,b)['status'],'FEATURE_UNAVAILABLE')

    def test_orb_first_close_only_and_missing_scan_fails_closed(self):
        bars={m:dict(open=95,high=100,low=90,close=95) for m in range(555,660)}
        bars[570]['close']=100
        bars[571]['close']=101
        bars[580]['close']=89
        self.assertEqual(orb_signal(bars),dict(status='SIGNAL',minute=571,side='CE',close=101))
        del bars[570]
        self.assertEqual(orb_signal(bars)['status'],'FAIL_CLOSED_MISSING_SCAN_CANDLE')

    def test_orb_both_reference_and_entry_floors_are_strict(self):
        bars={570:dict(open=110,close=100),571:dict(open=110,close=110)}
        self.assertEqual(floor_gate(bars,570),'SKIP_PREMIUM_FLOOR_SIGNAL_CLOSE')
        bars[570]['close']=110;bars[571]['open']=100
        self.assertEqual(floor_gate(bars,570),'SKIP_PREMIUM_FLOOR_ENTRY_OPEN')

    def test_t3_vix_inclusive_exclusive_and_no_expiry_day(self):
        args=(24025,15,1,100,100)
        self.assertEqual(t3_structure(*args)['status'],'SELECTED')
        self.assertNotEqual(t3_structure(24025,20,1,100,100)['status'],'SELECTED')
        self.assertEqual(t3_structure(24025,15,0,100,100)['status'],'NO_TRADE_EXPIRY_DAY')
        self.assertEqual(t3_structure(24025,None,1,100,100)['status'],'NO_TRADE_VIX_DATA')

    def test_t3_invalid_exit_keeps_raw_leg_evidence_and_no_pnl(self):
        legs=[{571:dict(open=p,close=p),901:dict(open=x,close=x)} for p,x in zip([150,150,50,50],[300,300,1,1])]
        actual=t3_exit(250,legs)
        self.assertEqual(actual['status'],'DATA_INTEGRITY_UNRESOLVED')
        self.assertEqual(actual['rawExitDebit'],598)
        self.assertIsNone(actual['points'])
        del legs[0][901]
        self.assertEqual(t3_exit(250,legs)['status'],'POST_ENTRY_UNRESOLVED_DATA')

    def test_tick_zero_bid_and_generation_never_use_ltp(self):
        o=dict(recv=1000,src=999,gen=1,bid=0,ask=110,bidQty=100,askQty=100,ltp=110)
        self.assertEqual(quote_ineligibility(o,'SELL',900,1,1000),'MISSING_OR_ZERO_BID')
        self.assertEqual(quote_ineligibility(o,'BUY',900,2,1000),'GENERATION_MISMATCH')

    def test_tick_unchanged_bid_is_not_counted_as_new_liquidity(self):
        o=dict(key='CE',recv=1300,src=1299,gen=1,bid=100,ask=101,bidQty=65,askQty=65,seq=1)
        r=attempt_exit([o,{**o,'recv':1400,'src':1399,'seq':2}],'CE','PE',1000,1,130)
        self.assertEqual(r['remaining'],dict(CE=65,PE=130))
        self.assertEqual(len(r['fills']),1)

    def test_source_lot_authority_is_bounded_and_no_current_master_guess(self):
        self.assertEqual(dated_lot('2025-01-28','2025-01-30'),25)
        self.assertEqual(dated_lot('2026-05-06','2026-05-12'),65)
        self.assertIsNone(dated_lot('2026-10-07','2026-10-13'))
        self.assertEqual(size_lots(98000,150,65,lambda q:q*3),9)

    def test_capital_unresolved_open_freezes_following_equity(self):
        trades=[dict(date='2026-05-06',expiry='2026-05-12',trade_id='1',status='PNL_UNRESOLVED',entryMinute=601,entryPrices=[100],points=None),
                dict(date='2026-05-07',expiry='2026-05-12',trade_id='2',status='RESOLVED',entryMinute=601,exitMinute=603,entryPrices=[100],exitPrices=[105],points=5)]
        rows,metrics=capital_ledger(trades,STRATEGIES[0],'allocation_100')
        self.assertEqual([r['status'] for r in rows],['EXIT_DATA_UNRESOLVED','EQUITY_UNRESOLVED'])
        self.assertIsNone(metrics['final_equity'])

    def test_capital_same_timestamp_exit_cannot_fund_entry_open(self):
        trade=dict(date='2026-05-06',expiry='2026-05-12',status='RESOLVED',entryPrices=[1000],exitPrices=[1100],points=100)
        trades=[dict(trade,trade_id='1',entryMinute=601,exitMinute=602),dict(trade,trade_id='2',entryMinute=602,exitMinute=603)]
        rows,metrics=capital_ledger(trades,STRATEGIES[0],'allocation_100')
        self.assertEqual(rows[1]['status'],'CAPITAL_NO_TRADE')
        self.assertEqual(metrics['final_equity'],106500)

    def test_full_2025_net_is_uncomputable_not_zero(self):
        trade=dict(date='2025-05-06',expiry='2025-05-08',status='RESOLVED',entryMinute=601,exitMinute=603,entryPrices=[100],exitPrices=[105],points=5)
        _,metrics=capital_ledger([trade],STRATEGIES[0],'allocation_100',net=True)
        self.assertEqual(metrics['status'],'NOT_COMPUTABLE')
        self.assertIsNone(metrics['net_rupees'])

    def test_option_adapter_preserves_bad_future_row_without_rejecting_early_trade(self):
        import hashlib
        import pandas as pd
        from trademind_data import RetainedTradeMindLake
        with TemporaryDirectory() as root:
            directory=Path(root)/'parquet/EXPIRED_OPTION_1M/test/sessions'
            directory.mkdir(parents=True)
            path=directory/'2026-05-06.parquet'
            pd.DataFrame([
                dict(candleTime=pd.Timestamp('2026-05-06T10:01:00+05:30'),open=120,high=127,low=119,close=125,volume=0),
                dict(candleTime=pd.Timestamp('2026-05-06T15:00:00+05:30'),open=120,high=110,low=109,close=125,volume=0),
            ]).to_parquet(path,index=False)
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            lake=RetainedTradeMindLake(root)
            bars=lake._read_frame('EXPIRED_OPTION_1M','test','2026-05-06',digest,defer_option_validity=True)
            self.assertFalse(bars[900]['input_valid'])
            self.assertEqual(bars[900]['high'],110)  # evidence retained, no repair
            result=phase_b_exit(600,bars,STRATEGIES[0])
            self.assertEqual(result['status'],'RESOLVED')
            self.assertEqual(result['exitPrices'],[126])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)

    def test_invalid_required_candle_freezes_h30_but_post_exit_invalidity_is_irrelevant(self):
        a={m:dict(open=120,close=120,input_valid=True) for m in range(661,692)}
        b={m:dict(open=120,close=120,input_valid=True) for m in range(661,692)}
        a[663]['input_valid']=False
        result=close_then_open_exit(661,set(range(555,930)),[a,b],kind=STRATEGIES[4])
        self.assertEqual(result['status'],'UNRESOLVED')
        self.assertEqual(result['entryPrices'],[120,120])
        self.assertIsNone(result['points'])
        a[661]['close']=140  # target at661 -> exact662 open, before bad663
        resolved=close_then_open_exit(661,set(range(555,930)),[a,b],kind=STRATEGIES[4])
        self.assertEqual(resolved['status'],'RESOLVED')
        self.assertEqual(resolved['exitMinute'],662)

    def test_new_config_roundtrip_and_v8_native_frame(self):
        from quant_terminal import form_values, build_config
        c=frozen_config(Config(strategy=STRATEGIES[2],capital=100000,start='2026-01-01',end='2027-01-01'))
        f=form_values(__import__('dataclasses').asdict(c),'saved')
        self.assertEqual(f['interval'],'2 minutes')
        self.assertEqual(f['trademind_provider'],'dhan')

    def test_nonfinite_optional_evidence_is_tagged_without_repair_or_price_change(self):
        import math
        from trademind_backtest import evidence_json_safe
        raw=dict(open=120.,close=125.,volume=float('nan'),volume_valid=False)
        safe=evidence_json_safe(raw)
        self.assertEqual(safe['open'],raw['open'])
        self.assertEqual(safe['close'],raw['close'])
        self.assertEqual(safe['volume'],{'nonfinite_float':'NaN'})
        self.assertTrue(math.isnan(raw['volume']))
        self.assertIn('nonfinite_float',json.dumps(safe,allow_nan=False))


if __name__ == '__main__':
    unittest.main()
