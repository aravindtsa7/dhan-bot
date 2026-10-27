"""DuckDB fixtures and interactive replay checks; no broker access."""
from datetime import date, time
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd
from streamlit.testing.v1 import AppTest

from quant_lake import IST, QuantDataLake
from quant_option_chain_ui import chain_snapshot, strike_replay, replay_figure


class OptionChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.chunks = {}
        self.times = pd.date_range('2025-01-02 09:15', periods=10, freq='min', tz=IST)
        for side in ('CE', 'PE'):
            for stream, strike in [('ATM-1', 24450), ('ATM', 24500), ('ATM+1', 24550)]:
                self.write_stream(side, stream, strike)
        spot = pd.DataFrame({'timestamp': self.times, 'open': 24500., 'high': 24510.,
                             'low': 24490., 'close': 24501., 'volume': 0})
        self.commit('NIFTY', spot, 'spot_1m')

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, name, frame, dataset='options_rolling_1m'):
        path = f'{name}.parquet'
        frame.to_parquet(self.root / path, index=False)
        self.chunks[name] = dict(dataset=dataset, instrument=name, status='complete', path=path,
                                 start='2025-01-01', end='2025-01-04')
        (self.root / 'ingestion_manifest.json').write_text(json.dumps({'chunks': self.chunks}), encoding='utf-8')

    def write_stream(self, side, stream, strike, expiry_code=1, expiry_date=None):
        name = f'NIFTY_{side}_{stream}_WEEK{expiry_code}'
        frame = pd.DataFrame(dict(timestamp=self.times, strike=float(strike), option_type=side,
            moneyness=stream, expiry_flag='WEEK', expiry_code=expiry_code,
            open=[100. + i for i in range(10)], high=[103. + i for i in range(10)],
            low=[99. + i for i in range(10)], close=[102. + i for i in range(10)],
            volume=[10 + i for i in range(10)], oi=[1000 + i for i in range(10)], iv=20., spot=24501.))
        if expiry_date:
            frame['contract_expiry'] = expiry_date
        self.commit(name, frame)
        return name, frame

    def load(self, **kwargs):
        with QuantDataLake(self.root) as lake:
            return lake.option_minutes('NIFTY', '2025-01-02', **kwargs)

    def app(self):
        app = AppTest.from_string('from quant_option_chain_ui import render_option_chain\n'
                                  f'render_option_chain({str(self.root)!r})', default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        return app

    def test_catalog_uses_observed_dates_and_real_expiry_metadata(self):
        self.write_stream('CE', 'ATM', 24500, 2, '2025-01-09')
        with QuantDataLake(self.root) as lake:
            sessions = lake.option_sessions('NIFTY')
            self.assertEqual(set(pd.to_datetime(sessions.trading_date).dt.date), {date(2025, 1, 2)})
            self.assertEqual(set(sessions.expiry_code), {1, 2})
            self.assertTrue(sessions.loc[sessions.expiry_code.eq(1), 'expiry_date'].isna().all())
            self.assertEqual(sessions.loc[sessions.expiry_code.eq(2), 'expiry_date'].iloc[0], '2025-01-09')
            self.assertTrue(lake.option_sessions('SENSEX').empty)
            self.assertTrue(lake.option_sessions('NIFTY', '2025-01-03', '2025-01-04').empty)

    def test_snapshot_pairs_numeric_strikes_and_exact_spot(self):
        with QuantDataLake(self.root) as lake:
            spot = lake.query('NIFTY', '2025-01-02', '2025-01-03')
        chain = chain_snapshot(self.load(), spot, self.times[0])
        self.assertEqual(chain['Strike Price'].tolist(), [24450, 24500, 24550])
        self.assertEqual(chain['CE LTP'].tolist(), [102.] * 3)
        self.assertEqual(chain['PE OI'].tolist(), [1000.] * 3)
        self.assertEqual(chain['Spot Index LTP'].tolist(), [24501.] * 3)
        self.assertTrue(chain_snapshot(self.load(), spot, self.times[-1] + pd.Timedelta(minutes=1)).empty)
        chain = chain_snapshot(self.load(), spot.iloc[1:], self.times[0])
        self.assertTrue(chain['Spot Index LTP'].isna().all())

    def test_missing_side_stays_blank_and_zero_volume_survives(self):
        name, frame = self.write_stream('PE', 'ATM', 24500)
        self.commit(name, frame.iloc[1:])
        name, frame = self.write_stream('CE', 'ATM', 24500)
        frame.loc[0, 'volume'] = 0
        self.commit(name, frame)
        chain = chain_snapshot(self.load(), pd.DataFrame(), self.times[0]).set_index('Strike Price')
        self.assertTrue(pd.isna(chain.loc[24500, 'PE LTP']))
        self.assertEqual(chain.loc[24500, 'CE Volume'], 0)

    def test_expiries_do_not_mix(self):
        name, frame = self.write_stream('CE', 'ATM', 24500, 2, '2025-01-09')
        frame['close'] = 999.
        self.commit(name, frame)
        near = self.load()
        far = self.load(expiry_code=2, expiry_date='2025-01-09')
        self.assertFalse(near.close.eq(999).any())
        self.assertEqual(len(far), 10)
        self.assertTrue(far.close.eq(999).all())
        self.assertTrue(self.load(expiry_code=2).empty)

    def test_overlaps_deduplicate_without_double_counting_volume(self):
        self.write_stream('CE', 'ATM+1', 24500)
        frame = self.load()
        quotes = frame[frame.strike.eq(24500) & frame.option_type.eq('CE')]
        self.assertEqual(len(quotes), 10)
        self.assertFalse(quotes.quote_conflict.any())
        bars = strike_replay(frame, 24500, 'CE', self.times[0], self.times[4], '5m')
        self.assertEqual(bars.volume.iloc[0], 60)

    def test_conflicting_overlaps_are_gaps(self):
        name, frame = self.write_stream('CE', 'ATM+1', 24500)
        frame.loc[0, 'close'] = 101.
        self.commit(name, frame)
        quotes = self.load()
        conflict = quotes[quotes.quote_conflict]
        self.assertEqual(len(conflict), 1)
        self.assertTrue(conflict.close.isna().all())
        bars = strike_replay(quotes, 24500, 'CE', self.times[0], self.times[4], '5m')
        self.assertEqual(bars.source_rows.iloc[0], 4)
        self.assertTrue(bars.close.isna().all())

    def test_fixed_strike_follows_stream_switch_before_resampling(self):
        name, frame = self.write_stream('CE', 'ATM', 24500)
        frame.loc[2:, 'strike'] = 24600.
        self.commit(name, frame)
        name, frame = self.write_stream('CE', 'ATM+1', 24550)
        frame.loc[2:, 'strike'] = 24500.
        self.commit(name, frame)
        bars = strike_replay(self.load(), 24500, 'CE', self.times[0], self.times[4], '5m')
        self.assertTrue(bars.bar_complete.all())
        self.assertEqual(bars[['open', 'high', 'low', 'close', 'volume', 'oi']].iloc[0].tolist(),
                         [100., 107., 99., 106., 60., 1004.])

    def test_cursor_gaps_and_partial_buckets(self):
        name, frame = self.write_stream('CE', 'ATM', 24500)
        self.commit(name, frame.drop(index=2))
        quotes = self.load()
        minute = strike_replay(quotes, 24500, 'CE', self.times[0], self.times[6])
        self.assertEqual(len(minute), 7)
        self.assertTrue(pd.isna(minute.close.iloc[2]))
        self.assertEqual(minute.timestamp.max(), self.times[6])
        bars = strike_replay(quotes, 24500, 'CE', self.times[0], self.times[6], '5m')
        self.assertEqual(bars.source_rows.tolist(), [4, 2])
        self.assertFalse(bars.bar_complete.any())
        self.assertTrue(bars.close.isna().all())
        figure = replay_figure(minute, 'test')
        self.assertEqual([t.type for t in figure.data], ['candlestick', 'bar', 'scatter'])
        self.assertFalse(figure.data[2].connectgaps)
        self.assertIsNone(pd.Timestamp(figure.data[0].x[0]).tzinfo)

    def test_oi_is_last_not_sum_and_missing_last_oi_is_not_filled(self):
        name, frame = self.write_stream('CE', 'ATM', 24500)
        frame['oi'] = frame.oi.astype(float)
        frame.loc[4, 'oi'] = float('nan')
        self.commit(name, frame)
        bars = strike_replay(self.load(), 24500, 'CE', self.times[0], self.times[-1], '5m')
        self.assertTrue(pd.isna(bars.oi.iloc[0]))
        self.assertEqual(bars.oi.iloc[1], 1009)

    def test_empty_date_and_invalid_instrument(self):
        with QuantDataLake(self.root) as lake:
            self.assertTrue(lake.option_minutes('NIFTY', '2025-01-03').empty)
            self.assertTrue(lake.option_minutes('NIFTY', '2026-01-03').empty)
            with self.assertRaises(ValueError):
                lake.option_sessions('BANKNIFTY')

    def test_ui_controls_replay_empty_dates_and_time_validation(self):
        app = self.app()
        self.assertEqual(app.date_input[0].value, date(2025, 1, 2))
        self.assertEqual(len(app.dataframe[0].value), 21)
        app.selectbox(key='chain_timeframe').select('5m').run()
        self.assertFalse(app.exception)
        app.selectbox(key=next(w.key for w in app.selectbox if w.label == 'Strike to replay')).select(24500.).run()
        app.radio(key='chain_side').set_value('PE').run()
        app.radio(key='chain_chart_range').set_value('Through selected date/time').run()
        app.time_input[0].set_value(time(9, 19)).run()
        self.assertFalse(app.exception)
        candles = app.dataframe[-1].value
        self.assertEqual(candles.timestamp.max(), self.times[0])
        self.assertEqual(candles.close.iloc[-1], 106.)
        app.date_input[0].set_value(date(2025, 1, 1)).run()
        self.assertTrue(any('No option quotes at this exact minute' in i.value for i in app.info))
        app.time_input[0].set_value(time(16)).run()
        self.assertTrue(any('Snapshot time must' in e.value for e in app.error))
        app.selectbox(key='chain_instrument').select('SENSEX').run()
        self.assertFalse(app.exception)
        self.assertTrue(any('No weekly option observations' in i.value for i in app.info))

    def test_single_minute_window_and_refresh(self):
        app = self.app()
        app.time_input[0].set_value(time(9, 15)).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        app.button(key='chain_refresh').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.dataframe[0].value), 21)


class LocalLakeOptionTests(unittest.TestCase):
    def test_real_duckdb_quotes_match_source_streams_for_both_indices(self):
        with QuantDataLake() as lake:
            for instrument in ('NIFTY', 'SENSEX'):
                for day in ('2025-01-01', '2026-10-22'):
                    with self.subTest(instrument=instrument, day=day):
                        options = lake.option_minutes(instrument, day)
                        self.assertFalse(options.empty)
                        lo = f'{day} 09:15'
                        source = lake.query(f'{instrument}_CE_ATM_WEEK1', lo, f'{day} 09:16',
                                            dataset='options_rolling_1m')
                        self.assertEqual(len(source), 1)
                        quote = options[options.timestamp.eq(source.timestamp.iloc[0])
                                        & options.strike.eq(source.strike.iloc[0])
                                        & options.option_type.eq('CE')].iloc[0]
                        for field in ('open', 'high', 'low', 'close', 'volume', 'oi', 'iv'):
                            self.assertEqual(quote[field], source[field].iloc[0])


if __name__ == '__main__':
    unittest.main()
