"""Read-only Dhan connectivity checks. Credentials and API bodies are never printed."""
import contextlib
import inspect
import io
import logging
import math
import os
import re
import sys
import time as clock
from datetime import datetime, time, timedelta
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

import dhanhq as sdk
import pandas as pd
import pyotp
import requests
from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / '.env'
IST = ZoneInfo('Asia/Kolkata')
API_URL = 'https://api.dhan.co/v2'
AUTH_URL = 'https://auth.dhan.co/app/generateAccessToken'
AUTH_CODES = {'DH-901', '807', '808', '809', '810', 'HTTP-401'}
RETRY_CODES = {'DH-904', 'DH-908', 'DH-909', '800', '805', 'HTTP-429',
               'HTTP-500', 'HTTP-502', 'HTTP-503', 'HTTP-504', 'NETWORK'}
ERROR_HELP = {
    'DH-901': 'Client ID or access token is invalid/expired',
    'DH-902': 'API access or Data API subscription is unavailable',
    'DH-903': 'Account/segment activation needs attention',
    'DH-905': 'Dhan rejected the request parameters',
    'DH-907': 'No data returned for the requested instrument/range',
    '806': 'Data API subscription is inactive', '807': 'Access token expired',
    '808': 'Authentication failed', '809': 'Access token invalid',
    '810': 'Client ID invalid',
    'NETWORK': 'Network request failed (credentials and URL suppressed)',
}


class CheckError(Exception):
    def __init__(self, code, detail=None):
        self.code = code
        super().__init__(f"{code}: {detail or ERROR_HELP.get(code, 'Request failed')}")


def error_code(body, default='API-ERROR'):
    # Allowlist code formats; never echo server messages, URLs or API bodies.
    if isinstance(body, dict):
        for key in ('errorCode', 'error_code', 'code'):
            code = str(body.get(key, ''))
            if re.fullmatch(r'(?:DH-)?\d{3}', code):
                return code
        for key in ('remarks', 'data'):
            if isinstance(body.get(key), dict):
                code = error_code(body[key], '')
                if code:
                    return code
    return default


def unwrap(body):
    # SDK v1/v2 envelopes can wrap another REST data/status envelope.
    for _ in range(4):
        if not isinstance(body, dict):
            return body
        if body.get('status') in ('failure', 'failed', 'error'):
            raise CheckError(error_code(body))
        if error_code(body, ''):
            raise CheckError(error_code(body))
        if 'data' not in body:
            return body
        body = body['data']
    raise CheckError('RESPONSE', 'Unexpected nested response')


def http_json(method, url, **kwargs):
    try:
        response = requests.request(method, url, timeout=(10, 40),
                                    allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise CheckError('NETWORK') from None
    try:
        body = response.json()
    except ValueError:
        raise CheckError(f'HTTP-{response.status_code}', 'Non-JSON response') from None
    if not 200 <= response.status_code < 300:
        raise CheckError(error_code(body, f'HTTP-{response.status_code}'))
    return body


class DhanClient:
    def __init__(self, client_id, token):
        self.client_id, self.token = client_id, token
        # 2.0 and 2.3 have different constructors; inspect rather than guess.
        try:
            inspect.signature(sdk.dhanhq).bind(client_id, token)
        except TypeError:
            factory = next((getattr(sdk, n) for n in ('DhanContext', 'dhanContext')
                            if callable(getattr(sdk, n, None))), None)
            if factory is None:
                raise CheckError('SDK', 'Dhan context constructor unavailable')
            self.client = sdk.dhanhq(factory(client_id, token))
        else:
            self.client = sdk.dhanhq(client_id, token)

    def call(self, name, endpoint, *, kwargs=None, payload=None, method='POST'):
        fn = getattr(self.client, name, None)
        use_sdk = callable(fn)
        if use_sdk:
            try:
                inspect.signature(fn).bind(**(kwargs or {}))
            except TypeError:
                use_sdk = False
        for attempt in range(3):
            try:
                if use_sdk:
                    # Suppress SDK output: some versions log raw error bodies.
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        body = fn(**(kwargs or {}))
                else:
                    # Older SDKs lack quote/expiry methods or historical dates.
                    body = http_json(method, API_URL + endpoint,
                                     headers={'access-token': self.token, 'client-id': self.client_id},
                                     **({'json': payload} if payload is not None else {}))
                return unwrap(body)
            except CheckError as exc:
                if exc.code not in RETRY_CODES or attempt == 2:
                    raise
                clock.sleep(3 * (attempt + 1))


def new_access_token(client_id, pin, totp):
    remaining = totp.interval - clock.time() % totp.interval
    if remaining < 3:
        clock.sleep(remaining + 0.2)
    body = http_json('POST', AUTH_URL,
                     params={'dhanClientId': client_id, 'pin': pin, 'totp': totp.now()})
    if not isinstance(body, dict) or not isinstance(body.get('accessToken'), str) or not body['accessToken']:
        raise CheckError(error_code(body, 'AUTH'), 'Token generation rejected; check PIN/TOTP configuration')
    return body['accessToken']


def check_funds(client):
    funds = client.call('get_fund_limits', '/fundlimit', method='GET')
    if not isinstance(funds, dict):
        raise CheckError('FUNDS', 'Missing fund-limit data')
    balance = funds.get('availabelBalance', funds.get('availableBalance'))
    try:
        valid = balance is not None and math.isfinite(float(balance))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise CheckError('FUNDS', 'Missing/invalid available balance')
    return funds


def check_quotes(client):
    securities = {'IDX_I': [13, 51]}
    data = client.call('ticker_data', '/marketfeed/ltp',
                       kwargs={'securities': securities}, payload=securities)
    indices = data.get('IDX_I', {}) if isinstance(data, dict) else {}
    prices = []
    for security_id in ('13', '51'):
        try:
            price = float(indices[security_id]['last_price'])
        except (KeyError, TypeError, ValueError):
            raise CheckError('LTP', 'Missing index price') from None
        if not math.isfinite(price) or price <= 0:
            raise CheckError('LTP', 'Invalid index price')
        prices.append(price)
    return f'LTP quotes OK | Nifty 50: {prices[0]:,.2f} | Sensex: {prices[1]:,.2f}'


def check_expiries(client):
    data = client.call('expiry_list', '/optionchain/expirylist',
                       kwargs={'under_security_id': 13, 'under_exchange_segment': 'IDX_I'},
                       payload={'UnderlyingScrip': 13, 'UnderlyingSeg': 'IDX_I'})
    if not isinstance(data, list) or not data:
        raise CheckError('EXPIRY', 'Empty expiry list')
    try:
        dates = sorted({datetime.strptime(d, '%Y-%m-%d').date() for d in data})
    except (TypeError, ValueError):
        raise CheckError('EXPIRY', 'Invalid expiry dates') from None
    dates = [d for d in dates if d >= datetime.now(IST).date()]
    if not dates:
        raise CheckError('EXPIRY', 'No current/future expiries')
    return 'Nifty expiries OK | Next 3: ' + ', '.join(map(str, dates[:3]))


def check_history(client):
    now = datetime.now(IST)
    # Exclude today's unfinished session; allow final-candle publication time.
    end = now.date() + timedelta(days=1) if now.time() >= time(15, 35) else now.date()
    start = end - timedelta(days=21)
    kwargs = dict(security_id='13', exchange_segment='IDX_I', instrument_type='INDEX',
                  from_date=str(start), to_date=str(end), interval=1)
    payload = dict(securityId='13', exchangeSegment='IDX_I', instrument='INDEX',
                   fromDate=str(start), toDate=str(end), interval='1', oi=False)
    data = client.call('intraday_minute_data', '/charts/intraday', kwargs=kwargs, payload=payload)
    columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    if not isinstance(data, dict) or not all(isinstance(data.get(k), list) for k in columns):
        raise CheckError('HISTORY', 'Missing OHLCV/timestamp arrays')
    if not data['timestamp'] or len({len(data[k]) for k in columns}) != 1:
        raise CheckError('HISTORY', 'Empty or unequal candle arrays')
    frame = pd.DataFrame({k: data[k] for k in columns})
    for col in columns:
        frame[col] = pd.to_numeric(frame[col], errors='coerce')
    if frame.isna().any().any() or not frame.map(math.isfinite).all().all():
        raise CheckError('HISTORY', 'Invalid numeric candle values')
    frame['datetime_ist'] = pd.to_datetime(frame['timestamp'], unit='s', utc=True).dt.tz_convert(IST)
    if frame['timestamp'].duplicated().any():
        raise CheckError('HISTORY', 'Duplicate candle timestamps')
    frame = frame.sort_values('timestamp')
    frame['session'] = frame['datetime_ist'].dt.date
    frame = frame[(frame['session'] >= start) & (frame['session'] < end)]
    sessions = sorted(frame['session'].unique())[-5:]
    if len(sessions) != 5:
        raise CheckError('HISTORY', 'Fewer than five completed trading sessions returned')
    frame = frame[frame['session'].isin(sessions)].copy()
    prices = frame[['open', 'high', 'low', 'close']]
    if ((prices <= 0).any().any() or (frame['volume'] < 0).any()
            or (frame['high'] < prices.max(axis=1)).any()
            or (frame['low'] > prices.min(axis=1)).any()):
        raise CheckError('HISTORY', 'Invalid OHLC/volume values')
    for _, group in frame.groupby('session'):
        gaps = group['timestamp'].diff().dropna()
        if len(gaps) == 0 or not (gaps == 60).all():
            raise CheckError('HISTORY', 'Incomplete or non-1-minute session data')
    output = ROOT / 'data' / 'nifty_1min_last5_trading_days.csv'
    output.parent.mkdir(exist_ok=True)
    frame.to_csv(output, index=False)
    counts = frame.groupby('session').size()
    return (f'Historical data OK | {len(frame):,} 1-min candles | 5 completed trading days\n'
            f"      Sessions (IST): {', '.join(f'{d} ({n})' for d, n in counts.items())}\n"
            f'      Saved: {output.relative_to(ROOT).as_posix()}')


def main():
    load_dotenv(ENV_FILE, override=True)
    logging.disable(logging.CRITICAL)
    print('============================================================')
    print('DHAN OPTIONS BOT - STEP 1: SYSTEM HEALTH CHECK')
    print(f"Python {sys.version.split()[0]} | dhanhq {version('dhanhq')} | {datetime.now(IST):%Y-%m-%d %H:%M:%S} IST")
    print('============================================================')
    client_id = os.getenv('DHAN_CLIENT_ID', '').strip()
    pin = os.getenv('DHAN_PIN', '').strip()
    secret = os.getenv('DHAN_TOTP_SECRET', '').strip()
    token = os.getenv('DHAN_ACCESS_TOKEN', '').strip()
    passed = 0
    try:
        totp = pyotp.TOTP(secret)
        code = totp.now()
        if not secret or not re.fullmatch(r'\d{6}', code) or not totp.verify(code, valid_window=1):
            raise ValueError
        print('[1/5] PASS | TOTP generation OK (code hidden)')
        passed += 1
    except Exception:
        print('[1/5] FAIL | Invalid/missing DHAN_TOTP_SECRET')
        for number in (2, 3, 4, 5):
            print(f'[{number}/5] SKIP | Authentication configuration unavailable')
        print('RESULT: 0/5 checks passed.')
        return 1
    client = None
    try:
        if not client_id:
            raise CheckError('CONFIG', 'DHAN_CLIENT_ID is missing')
        if token:
            client = DhanClient(client_id, token)
            try:
                check_funds(client)
            except CheckError as exc:
                if exc.code not in AUTH_CODES:
                    raise
                token = ''
        refreshed = not token
        if refreshed:
            if not pin:
                raise CheckError('CONFIG', 'DHAN_PIN is required for token generation')
            print('      Access token unavailable/invalid; attempting TOTP auto-login...')
            token = new_access_token(client_id, pin, totp)
            client = DhanClient(client_id, token)
            check_funds(client)
            # Persist only after a real fund-limit call verifies the new token.
            set_key(str(ENV_FILE), 'DHAN_ACCESS_TOKEN', token)
            os.environ['DHAN_ACCESS_TOKEN'] = token
        print('[2/5] PASS | Account fund limits OK | ' +
              ('Token refreshed and saved to .env' if refreshed else 'Access token active'))
        passed += 1
    except CheckError as exc:
        print(f'[2/5] FAIL | {exc}')
        client = None
    except Exception as exc:
        print(f'[2/5] FAIL | {type(exc).__name__} (sensitive details suppressed)')
        client = None
    for number, check in ((3, check_quotes), (4, check_expiries), (5, check_history)):
        if client is None:
            print(f'[{number}/5] SKIP | Authentication/fund-limit check failed')
            continue
        try:
            message = check(client)
            print(f'[{number}/5] PASS | {message}')
            passed += 1
        except CheckError as exc:
            print(f'[{number}/5] FAIL | {exc}')
        except Exception as exc:
            print(f'[{number}/5] FAIL | {type(exc).__name__} (sensitive details suppressed)')
    print('============================================================')
    print(f'RESULT: {passed}/5 checks passed.')
    if passed == 5:
        print('ALL SYSTEMS READY FOR BACKTESTING ENGINE!')
    else:
        print('Health check incomplete; resolve failed checks before proceeding.')
    print('============================================================')
    return 0 if passed == 5 else 1


if __name__ == '__main__':
    sys.exit(main())
