"""Offline regression checks; no credentials loaded and no API calls made."""
import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch

import step1_health_check as health


class HealthCheckTests(unittest.TestCase):
    def test_nested_sdk_response(self):
        self.assertEqual(health.unwrap({'status': 'success', 'data': {
            'status': 'success', 'data': {'IDX_I': {'13': {'last_price': 1}}}}}),
            {'IDX_I': {'13': {'last_price': 1}}})

    def test_outer_success_does_not_hide_api_failure(self):
        with self.assertRaises(health.CheckError) as caught:
            health.unwrap({'status': 'success', 'data': {
                'status': 'failure', 'remarks': {'error_code': '807'}}})
        self.assertEqual(caught.exception.code, '807')

    def test_missing_balance_does_not_pass_as_zero(self):
        client = Mock()
        client.call.return_value = {}
        with self.assertRaises(health.CheckError):
            health.check_funds(client)
        client.call.return_value = {'availabelBalance': 0}
        self.assertEqual(health.check_funds(client), {'availabelBalance': 0})

    def test_missing_sensex_quote_fails(self):
        client = Mock()
        client.call.return_value = {'IDX_I': {'13': {'last_price': 22000}}}
        with self.assertRaises(health.CheckError):
            health.check_quotes(client)

    def test_legacy_constructor(self):
        def legacy(client_id, access_token):
            return (client_id, access_token)
        with patch.object(health.sdk, 'dhanhq', legacy):
            self.assertEqual(health.DhanClient('example', 'fake').client,
                             ('example', 'fake'))

    def test_context_constructor(self):
        def modern(dhan_context):
            return dhan_context
        with patch.object(health.sdk, 'dhanhq', modern), patch.object(
                health.sdk, 'DhanContext', return_value='context') as factory:
            self.assertEqual(health.DhanClient('example', 'fake').client, 'context')
            factory.assert_called_once_with('example', 'fake')

    def test_old_history_signature_uses_rest_with_dates_and_interval(self):
        client = health.DhanClient.__new__(health.DhanClient)
        client.client_id, client.token = 'example', 'fake'
        class OldSDK:
            def intraday_minute_data(self, security_id, exchange_segment, instrument_type):
                raise AssertionError('Old endpoint must not be used')
        client.client = OldSDK()
        with patch.object(health, 'http_json', return_value={'timestamp': [1]}) as http:
            result = client.call('intraday_minute_data', '/charts/intraday',
                                 kwargs={'from_date': '2026-10-01', 'interval': 1},
                                 payload={'fromDate': '2026-10-01', 'interval': '1'})
        self.assertEqual(result, {'timestamp': [1]})
        self.assertEqual(http.call_args.kwargs['json']['interval'], '1')

    def test_expired_token_refresh_is_verified_and_persisted_without_logging(self):
        env = {'DHAN_CLIENT_ID': 'example', 'DHAN_PIN': '654321',
               'DHAN_TOTP_SECRET': 'JBSWY3DPEHPK3PXP',
               'DHAN_ACCESS_TOKEN': 'fake-old-token'}
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch.object(health, 'load_dotenv'), \
                patch.object(health, 'DhanClient'), \
                patch.object(health, 'check_funds', side_effect=[
                    health.CheckError('DH-901'), {'availabelBalance': 0}]) as funds, \
                patch.object(health, 'new_access_token', return_value='fake-new-token') as refresh, \
                patch.object(health, 'set_key') as persist, \
                patch.object(health, 'check_quotes', return_value='quotes verified'), \
                patch.object(health, 'check_expiries', return_value='expiries verified'), \
                patch.object(health, 'check_history', side_effect=health.CheckError('HISTORY')), \
                contextlib.redirect_stdout(output):
            result = health.main()
        self.assertEqual(result, 1)
        self.assertEqual(funds.call_count, 2)
        refresh.assert_called_once()
        persist.assert_called_once_with(str(health.ENV_FILE), 'DHAN_ACCESS_TOKEN', 'fake-new-token')
        text = output.getvalue()
        self.assertIn('RESULT: 4/5 checks passed.', text)
        self.assertNotIn('ALL SYSTEMS READY', text)
        for credential in (*env.values(), 'fake-new-token'):
            self.assertNotIn(credential, text)


if __name__ == '__main__':
    unittest.main()
