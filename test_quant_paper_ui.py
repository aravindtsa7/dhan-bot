"""Paper-room accounting, freshness and Streamlit integration, without broker access."""
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd
from streamlit.testing.v1 import AppTest
from quant_paper_ui import load_room, pnl_row, TRADE_COLUMNS


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


class PaperRoomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc).timestamp()

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, name, data):
        (self.root / name).write_text(json.dumps(data), encoding='utf-8')

    def fixture(self):
        rows = [dict(trade_id=f'p{i}', status='CLOSED', net_pnl=pnl, instrument='CRUDEOIL', security_id=573422,
                     strategy='H4', entry_time=iso(self.now-3600), exit_time=iso(self.now-1800))
                for i, pnl in enumerate([1200, -400, 0])]
        rows.append(dict(trade_id='open1', status='OPEN', net_pnl=None, instrument='CRUDEOIL',
            security_id=573422, strategy='H4', side='LONG', entry_price=9700, quantity=100,
            entry_time=iso(self.now-600), exit_time='', stop=9680, target=9760, pending_exit=''))
        self.save('state.json', {'trades': rows})
        pd.DataFrame(rows).to_csv(self.root / 'ledger.csv', index=False)
        self.save('process.json', {'started_at': iso(self.now-700), 'pid': 123})
        self.save('status.json', {'timestamp': iso(self.now), 'running': True, 'connected': True,
            'positions': [dict(trade_id='open1', security_id=573422, ltp=9710, net_pnl=750,
                               quote_time=iso(self.now-2))]})
        return rows

    def test_reconciles_closed_metrics_and_open_net_pnl(self):
        self.fixture()
        room = load_room(self.root, self.now)
        self.assertEqual(room['metrics'], dict(total=4, opened=1, closed=3, net=800., profit=1200., loss=-400., win_rate=100/3))
        self.assertEqual(room['unrealized'], 750)
        self.assertEqual(room['active'].iloc[0].ltp, 9710)
        self.assertEqual(room['health'], 'Running')
        self.assertFalse(room['warnings'])

    def test_expired_snapshot_is_not_live_even_when_saved_fresh(self):
        self.fixture()
        room = load_room(self.root, self.now+40)
        self.assertEqual(room['health'], 'Starting / heartbeat stale')
        self.assertIsNone(room['unrealized'])
        self.assertIsNone(room['active'].iloc[0].ltp)

    def test_stale_quote_or_wrong_contract_does_not_value_position(self):
        self.fixture()
        status = json.loads((self.root/'status.json').read_text())
        status['positions'][0]['security_id'] = 999
        self.save('status.json', status)
        self.assertIsNone(load_room(self.root, self.now)['unrealized'])
        status['positions'][0]['security_id'] = 573422
        status['positions'][0]['quote_time'] = iso(self.now-16)
        self.save('status.json', status)
        self.assertIsNone(load_room(self.root, self.now)['unrealized'])

    def test_stop_and_completed_shutdown_are_distinct(self):
        self.fixture()
        (self.root/'STOP').touch()
        self.assertEqual(load_room(self.root,self.now)['health'], 'Stop requested')
        self.save('last_run.json', {'stopped_at':iso(self.now+1)})
        room = load_room(self.root,self.now+2)
        self.assertEqual(room['health'], 'Stopped')
        self.assertTrue(room['stop_requested'])
        self.assertIsNone(room['unrealized'])

    def test_previous_shutdown_does_not_override_new_start(self):
        self.fixture()
        self.save('last_run.json', {'stopped_at':iso(self.now-1000)})
        self.assertEqual(load_room(self.root,self.now)['health'], 'Running')
        self.save('process.json', {'started_at':iso(self.now+1)})
        self.assertEqual(load_room(self.root,self.now+2)['health'], 'Starting / heartbeat stale')

    def test_csv_lag_uses_authoritative_state(self):
        rows = self.fixture()
        rows[-1].update(status='CLOSED', net_pnl=300, exit_time=iso(self.now))
        self.save('state.json', {'trades':rows})
        room = load_room(self.root,self.now)
        self.assertEqual(room['metrics']['net'], 1100)
        self.assertEqual(room['metrics']['opened'], 0)
        self.assertEqual(room['source'], 'state.json')
        self.assertTrue(room['warnings'])

    def test_empty_missing_and_invalid_sources(self):
        self.assertFalse(load_room(self.root,self.now)['available'])
        self.save('state.json', {'trades':[]})
        room = load_room(self.root,self.now)
        self.assertTrue(room['available']); self.assertEqual(room['metrics']['total'], 0)
        self.assertIsNone(room['metrics']['win_rate'])
        self.assertEqual(room['metrics']['net'], 0)
        (self.root/'state.json').write_text('{partial', encoding='utf-8')
        self.assertFalse(load_room(self.root,self.now)['available'])

    def test_duplicate_ledger_rejected_and_missing_net_not_zero(self):
        rows = self.fixture()
        rows[0]['net_pnl'] = None
        self.save('state.json', {'trades':rows})
        room = load_room(self.root,self.now)
        self.assertIsNone(room['metrics']['net']); self.assertIsNone(room['metrics']['win_rate'])
        (self.root/'state.json').unlink()
        pd.DataFrame([rows[0],rows[0]]).to_csv(self.root/'ledger.csv',index=False)
        self.assertFalse(load_room(self.root,self.now)['available'])

    def test_legacy_status_without_position_identity_not_guessed(self):
        self.fixture()
        self.save('status.json', {'timestamp':iso(self.now), 'connected':True, 'net_open_pnl':123,
                                  'open_positions':1, 'positions':None})
        self.assertIsNone(load_room(self.root,self.now)['unrealized'])

    def test_positive_and_negative_rows_have_distinct_colors(self):
        positive = pnl_row(pd.Series({'Net P&L (₹)':1, 'Status':'CLOSED'}))
        negative = pnl_row(pd.Series({'Net P&L (₹)':-1, 'Status':'CLOSED'}))
        self.assertTrue(all('#153f32' in v for v in positive))
        self.assertTrue(all('#51252e' in v for v in negative))
        self.assertEqual(pnl_row(pd.Series({'Net P&L (₹)':None})), [''])

    def test_fragment_populated_refresh_and_readonly_render(self):
        rows = self.fixture()
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        app = AppTest.from_string('from quant_paper_ui import render_live_paper_room\n'
                                  f'render_live_paper_room({str(self.root)!r})', default_timeout=20).run()
        self.assertFalse(app.exception)
        metrics = {m.label:m.value for m in app.metric}
        self.assertEqual(metrics['Paper trades executed'], '4')
        self.assertEqual(metrics['Closed net P&L'], '₹800.00')
        self.assertEqual(len(app.dataframe), 2)
        self.assertEqual({p.name:p.read_bytes() for p in self.root.iterdir()}, before)
        rows[0]['net_pnl'] = 1500
        self.save('state.json', {'trades':rows})
        app.button(key='paper_refresh').click().run()
        self.assertFalse(app.exception)
        self.assertEqual({m.label:m.value for m in app.metric}['Closed net P&L'], '₹1,100.00')

    def test_fragment_empty_and_corrupt_files_render(self):
        app = AppTest.from_string('from quant_paper_ui import render_live_paper_room\n'
                                  f'render_live_paper_room({str(self.root)!r})', default_timeout=20).run()
        self.assertFalse(app.exception)
        self.assertTrue(any('No readable paper account' in x.value for x in app.info))
        self.save('state.json', {'trades':[]})
        self.save('status.json', {'positions':None})
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual({m.label:m.value for m in app.metric}['Closed win rate'], 'N/A')

    def test_full_quant_ui_has_sidebar_and_paper_page(self):
        app = AppTest.from_file(str(Path(__file__).with_name('quant_ui.py')), default_timeout=60).run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.tabs), 0)
        self.assertEqual(app.radio(key='page').value, 'Backtests')
        app.radio(key='page').set_value('Live Paper Trading').run()
        self.assertFalse(app.exception)
        self.assertIn('Paper trades executed', [m.label for m in app.metric])


if __name__ == '__main__':
    unittest.main()
