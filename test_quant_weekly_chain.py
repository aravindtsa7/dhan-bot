"""Step 9: full strike ladders, expiry isolation, changes and multi-day replay."""
from datetime import date, time
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

import pandas as pd
from streamlit.testing.v1 import AppTest

from quant_lake import IST, QuantDataLake
from quant_option_chain_ui import (MATRIX_COLUMNS, weekly_chain_matrix, strike_replay,
                                    replay_figure, selected_strike, ladder_style)
from step2_build_data_lake import Builder


class WeeklyChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.chunks = {}
        self.expiry = '2025-01-09'
        self.days = ['2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08', '2025-01-09']
        times = [pd.Timestamp(f'{d} {t}', tz=IST) for d in self.days
                 for t in ['09:15', '09:16', '09:17', '09:18', '09:19', '15:29']]
        for side in ('CE', 'PE'):
            for strike in range(24000, 25001, 50):
                frame = pd.DataFrame(dict(timestamp=times, strike=float(strike), option_type=side,
                    expiry_flag='WEEK', contract_expiry=self.expiry,
                    open=[100. + i for i in range(len(times))],
                    high=[103. + i for i in range(len(times))], low=[99. + i for i in range(len(times))],
                    close=[102. + i for i in range(len(times))], volume=10.,
                    oi=[1000. + 10*i for i in range(len(times))], iv=20., spot=24500.))
                self.commit(f'NIFTY_{side}_{strike}_{self.expiry}', frame, 'options_contract_1m')
        spot = pd.DataFrame(dict(timestamp=times, open=24500., high=24510., low=24490., close=24500., volume=0.))
        self.commit('NIFTY', spot, 'spot_1m')

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, name, frame, dataset='options_rolling_1m'):
        path = f'{name}.parquet'
        frame.to_parquet(self.root / path, index=False)
        self.chunks[name] = dict(dataset=dataset, instrument=name, status='complete', path=path,
                                 start='2025-01-01', end='2025-01-20')
        (self.root / 'ingestion_manifest.json').write_text(json.dumps({'chunks': self.chunks}), encoding='utf-8')

    def load(self):
        with QuantDataLake(self.root) as lake:
            return (lake.option_history('NIFTY', self.expiry, '2025-01-03', '2025-01-10'),
                    lake.query('NIFTY', '2025-01-03', '2025-01-10'))

    def test_all_21_strikes_and_both_sides_from_fixed_contract_partitions(self):
        with QuantDataLake(self.root) as lake:
            catalog = lake.option_expiries('NIFTY')
            self.assertEqual(catalog.expiry_date.tolist(), [self.expiry])
            self.assertEqual(catalog.expiry_basis.tolist(), ['recorded'])
            quotes = lake.option_history('NIFTY', self.expiry, '2025-01-06', '2025-01-10')
            self.assertEqual(quotes.strike.nunique(), 21)
            self.assertEqual(set(quotes.option_type), {'CE', 'PE'})
            self.assertEqual(quotes.timestamp.dt.date.nunique(), 4)
            self.assertEqual(len(quotes), 21 * 2 * 6 * 4)
            fixed = lake.option_history('NIFTY', self.expiry, '2025-01-06 09:15', '2025-01-06 09:20',
                                        strike=24000, option_type='PE')
            self.assertEqual(len(fixed), 5)
            self.assertEqual(set(fixed.strike), {24000})
            self.assertEqual(set(fixed.option_type), {'PE'})

    def test_recorded_expiry_not_calendar_code_is_the_contract_identity(self):
        frame = pd.read_parquet(self.root / f'NIFTY_CE_24500_{self.expiry}.parquet')
        frame['contract_expiry'], frame['close'] = '2025-01-16', 999.
        self.commit('NIFTY_CE_24500_2025-01-16', frame, 'options_contract_1m')
        with QuantDataLake(self.root) as lake:
            first = lake.option_history('NIFTY', self.expiry, '2025-01-03', '2025-01-10')
            second = lake.option_history('NIFTY', '2025-01-16', '2025-01-03', '2025-01-10')
            self.assertFalse(first.close.eq(999).any())
            self.assertTrue(second.close.eq(999).all())
            self.assertTrue(lake.option_history('SENSEX', self.expiry, '2025-01-03', '2025-01-10').empty)
            self.assertTrue(lake.option_history('NIFTY', self.expiry, '2025-01-03', '2025-01-10', 'calendar-derived').empty)

    def test_calendar_mapping_stops_at_expiry_and_never_merges_with_recorded(self):
        frame = pd.read_parquet(self.root / f'NIFTY_CE_24500_{self.expiry}.parquet').drop(columns='contract_expiry')
        frame['expiry_code'], frame['moneyness'], frame['close'] = 1, 'ATM+10', 900.
        after = frame.tail(1).copy()
        after['timestamp'] = pd.Timestamp('2025-01-10 09:15', tz=IST)
        self.commit('NIFTY_CE_ATM+10_WEEK1', pd.concat([frame, after], ignore_index=True))
        with QuantDataLake(self.root) as lake:
            near = lake.option_history('NIFTY', self.expiry, '2025-01-03', '2025-01-11', 'calendar-derived')
            following = lake.option_history('NIFTY', '2025-01-16', '2025-01-03', '2025-01-11', 'calendar-derived')
            self.assertEqual(near.timestamp.max().date(), date(2025, 1, 9))
            self.assertEqual(len(following), 1)
            self.assertTrue(near.close.eq(900).all())
            self.assertFalse(near.quote_conflict.any())
            # Same expiry can be observed under WEEK2 before it becomes WEEK1.
            far = frame.iloc[:1].copy()
            far['expiry_code'] = 2
            self.commit('NIFTY_CE_ATM+10_WEEK2', far)
            lake.refresh()
            following = lake.option_history('NIFTY', '2025-01-16', '2025-01-03', '2025-01-11', 'calendar-derived')
            self.assertEqual(len(following), 2)

    def test_unclassified_fixed_contracts_are_not_assumed_weekly(self):
        frame = pd.read_parquet(self.root / f'NIFTY_CE_24500_{self.expiry}.parquet').drop(columns='expiry_flag')
        frame['contract_expiry'] = '2025-01-30'
        self.commit('NIFTY_CE_24500_UNKNOWN', frame, 'options_contract_1m')
        with QuantDataLake(self.root) as lake:
            self.assertNotIn('2025-01-30', lake.option_expiries('NIFTY').expiry_date.tolist())

    def test_matrix_layout_atm_changes_and_observed_session_volume(self):
        options, spot = self.load()
        at = pd.Timestamp('2025-01-06 09:19', tz=IST)
        chain = weekly_chain_matrix(options, spot, at, 'NIFTY')
        self.assertEqual(chain.columns.tolist(), MATRIX_COLUMNS)
        self.assertEqual(len(chain), 21)
        self.assertEqual(chain.attrs['atm'], 24500)
        row = chain[chain['Strike Price'].eq(24500)].iloc[0]
        self.assertEqual(row['LTP (CE)'], 112.)
        self.assertEqual(row['CE Volume'], 50.)
        self.assertEqual(row['PE OI'], 1100.)
        self.assertAlmostEqual(row['CE LTP Chg (%)'], (112 / 107 - 1) * 100)
        self.assertAlmostEqual(row['PE OI Chg (%)'], (1100 / 1050 - 1) * 100)
        self.assertEqual(chain.attrs['baseline_date'], '2025-01-03')
        style = ladder_style(chain)._compute()
        self.assertIn(('background-color', '#384023'), style.ctx[(10, 6)])
        self.assertEqual(selected_strike(chain, [0], 24500), 24000)
        self.assertEqual(selected_strike(chain, [], 24500), 24500)

    def test_wide_universe_not_limited_to_current_three_quotes(self):
        options, spot = self.load()
        at = pd.Timestamp('2025-01-06 09:19', tz=IST)
        options = options[~options.timestamp.eq(at) | options.strike.isin([24450, 24500, 24550])]
        chain = weekly_chain_matrix(options, spot, at, 'NIFTY', 'All available strikes')
        self.assertEqual(len(chain), 21)
        self.assertEqual(chain['LTP (CE)'].notna().sum(), 3)
        missing = chain[chain['Strike Price'].eq(24000)].iloc[0]
        self.assertTrue(pd.isna(missing['LTP (CE)']))
        self.assertTrue(pd.isna(missing['CE Volume']))

    def test_missing_zero_or_stale_baselines_do_not_create_percent_changes(self):
        options, spot = self.load()
        at = pd.Timestamp('2025-01-06 09:19', tz=IST)
        prior_close = options.timestamp.eq(pd.Timestamp('2025-01-03 15:29', tz=IST))
        options.loc[prior_close & options.strike.eq(24500), ['close', 'oi']] = 0.
        options = options[~(prior_close & options.strike.eq(24550))]
        chain = weekly_chain_matrix(options, spot, at, 'NIFTY')
        for strike in (24500, 24550):
            row = chain[chain['Strike Price'].eq(strike)].iloc[0]
            self.assertTrue(pd.isna(row['CE LTP Chg (%)']))
            self.assertTrue(pd.isna(row['PE OI Chg (%)']))
        self.assertFalse(chain.loc[chain['Strike Price'].eq(24450), 'CE LTP Chg (%)'].isna().any())

    def test_multiday_candles_have_no_overnight_buckets_or_future_leak(self):
        options, _ = self.load()
        start = pd.Timestamp('2025-01-06 09:15', tz=IST)
        end = pd.Timestamp('2025-01-09 15:30', tz=IST)
        bars = strike_replay(options, 24000, 'CE', start, end, '5m')
        self.assertEqual(bars.timestamp.dt.date.nunique(), 4)
        self.assertEqual(int(bars.bar_complete.sum()), 4)
        observed = bars[bars.bar_complete]
        self.assertEqual(observed.volume.tolist(), [50.] * 4)
        self.assertEqual(observed.oi.tolist(), [1100., 1160., 1220., 1280.])
        self.assertTrue(bars.timestamp.dt.hour.between(9, 15).all())
        cutoff = pd.Timestamp('2025-01-07 09:18', tz=IST)
        replay = strike_replay(options, 24000, 'CE', start, cutoff, '5m')
        self.assertTrue(replay.timestamp.le(cutoff).all())
        self.assertEqual(int(replay.bar_complete.sum()), 1)
        figure = replay_figure(bars, 'weekly')
        self.assertEqual([t.type for t in figure.data], ['candlestick', 'bar', 'scatter'])
        self.assertIn(None, list(figure.data[2].x))

    def test_missing_contract_still_has_inspectable_empty_week(self):
        options, _ = self.load()
        bars = strike_replay(options, 25500, 'PE', pd.Timestamp('2025-01-06 09:15', tz=IST),
                             pd.Timestamp('2025-01-09 15:30', tz=IST))
        self.assertFalse(bars.bar_complete.any())
        self.assertTrue(bars.close.isna().all())

    def test_wider_ingestion_requests_all_42_index_streams(self):
        builder = Builder.__new__(Builder)
        builder.start, builder.end = date(2025, 1, 1), date(2025, 1, 2)
        builder.chunk = Mock()
        builder.options(dict(instrument='NIFTY', security_id='13'), wing=10)
        self.assertEqual(builder.chunk.call_count, 42)
        strikes = {call.args[-1]['strike'] for call in builder.chunk.call_args_list}
        self.assertEqual(len(strikes), 21)
        self.assertIn('ATM-10', strikes)
        self.assertIn('ATM+10', strikes)
        with self.assertRaises(ValueError):
            builder.options(dict(instrument='NIFTY', security_id='13'), wing=11)

    def test_ui_full_week_side_selection_and_chronological_replay(self):
        app = AppTest.from_string('from quant_option_chain_ui import render_option_chain\n'
                                  f'render_option_chain({str(self.root)!r})', default_timeout=45).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        self.assertEqual(app.selectbox(key='chain_weekly_expiry_NIFTY').value, (self.expiry, 'recorded'))
        self.assertEqual(app.dataframe[0].value.columns.tolist(), MATRIX_COLUMNS)
        self.assertEqual(len(app.dataframe[0].value), 21)
        self.assertEqual(app.dataframe[-1].value.timestamp.dt.date.nunique(), 4)
        # AppTest has no public dataframe-selection helper. Send the same
        # serialized selection event as the frontend to exercise the callback.
        state = app._tree.get_widget_states()
        selection = state.widgets.add()
        selection.id = app.dataframe[0].proto.id
        selection.string_value = json.dumps({'selection': {'rows': [0], 'columns': [], 'cells': []}})
        app._run(state)
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key=f'chain_selected_strike_NIFTY_{self.expiry}_recorded').value, 24000)
        app.radio(key='chain_side').set_value('PE').run()
        app.selectbox(key='chain_timeframe').select('5m').run()
        self.assertFalse(app.exception)
        bars = app.dataframe[-1].value
        self.assertEqual(int(bars.bar_complete.sum()), 4)
        self.assertEqual(set(bars.strike), {24000})
        self.assertEqual(set(bars.option_type), {'PE'})
        app.date_input[0].set_value(date(2025, 1, 7)).run()
        app.time_input[0].set_value(time(9, 19)).run()
        app.radio(key='chain_chart_range').set_value('Through selected date/time').run()
        self.assertFalse(app.exception)
        self.assertEqual(app.dataframe[-1].value.timestamp.max(), pd.Timestamp('2025-01-07 09:15', tz=IST))


if __name__ == '__main__':
    unittest.main()
