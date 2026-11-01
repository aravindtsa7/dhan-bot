"""Cross-stage cache invalidation and explicit failed-simulation evidence."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from quant_backtester import run_backtest, ENGINE_VERSION
from quant_book import InvalidBasketObservation
from quant_option_model import MissingObservedPrice
from test_quant_book import fixture, DATE


class ReviewIntegrationTests(unittest.TestCase):
    def test_invalid_open_position_is_persisted_as_invalid_not_zero_profit(self):
        c,d,day=fixture();c=replace(c,capital=100000,price_policy='strict_observed')
        for at in range(601,921):day['options'][at,'CE',100]=(25,25,25,25,100)
        with TemporaryDirectory() as directory:
            with self.assertRaises(InvalidBasketObservation) as caught:
                run_backtest(c,d,Path(directory),persist=True)
            exc=caught.exception
            self.assertTrue(exc.evidence['simulation_invalidated'])
            self.assertFalse(exc.evidence['contract_identity_verified'])
            report=Path(exc.invalidation_path)
            saved=json.loads(report.read_text())
            self.assertIsNone(saved['metrics']);self.assertIsNone(saved['net_pnl'])
            self.assertEqual(saved['config']['price_policy'],'strict_observed')
            self.assertEqual(saved['evidence']['field'],'close' if saved['evidence']['minute']==600 else 'open')
            self.assertFalse((report.parent/'result.json').exists())
            self.assertFalse((report.parent/'trades.parquet').exists())

    def test_warmup_synthetic_evidence_rejects_strict_and_is_disclosed_in_model(self):
        c,d,_=fixture();c=replace(c,capital=100000,price_policy='strict_observed')
        d.warmup_eligibility=[dict(date='2026-10-21',status='eligible',repaired_inputs=[dict(method='synthetic_ffill',timestamp='2026-10-21T10:00:00+05:30')])]
        with self.assertRaisesRegex(MissingObservedPrice,'synthetic repaired'):
            run_backtest(c,d,persist=False)
        result=run_backtest(replace(c,price_policy='model_continuity'),d,persist=False)
        self.assertEqual(result['status'],'repaired_inputs')
        self.assertEqual(len(result['warmup_repaired_inputs']),1)
        self.assertEqual(result['engine_version'],ENGINE_VERSION)
        self.assertEqual(result['pricing_revision'],'validated-window-baskets-v8')

    def test_disconnected_repaired_history_does_not_veto_strict_execution(self):
        c,d,_=fixture();c=replace(c,capital=100000,price_policy='strict_observed')
        d.warmup_eligibility=[dict(date='2026-10-01',status='eligible',repaired_inputs=[dict(method='synthetic_ffill')])]
        d.indicator_initialization=dict(sessions={'2026-10-01':dict(origin='2026-10-01',contract='NIFTY'),DATE:dict(origin=DATE,contract='NIFTY')})
        result=run_backtest(c,d,persist=False)
        self.assertEqual(result['warmup_repaired_inputs'],[])
        self.assertEqual(result['indicator_warmup_dates_used'],[])

if __name__=='__main__':unittest.main()
