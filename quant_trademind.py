"""Independent TradeMind research rules. No broker, lake writer or modeled prices.

Source-bound semantics and limitations: README_TRADEMIND_STRATEGIES.md.
Minute-keyed execution inputs are candle START labels in Asia/Kolkata.
These functions never activate Paper/Live and never recover missing observations.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

REVISION = 'trademind-observed-v2'
STRATEGIES = ('v2_trend_down_pe', 'v4_momentum_expansion_pe', 'v8_bullish_reclaim_ce',
              't3_vix_atm_iron_fly', 'ce_pe_rise5m_h30', 'nifty_orb_single_option')
FINGERPRINTS = dict(zip(STRATEGIES[:3], ('f8a0ee53d6fdeb8a', '549aacaca3149ffd', 'c815f819acd71e98')))


class Unavailable(ValueError):
    """Required evidence is absent; this is never a zero-P&L result."""


def js_round(value):
    return math.floor(value + .5)


def strategy_fingerprint(parameters):
    """Frozen strategy serializer: sorted objects, arrays in order, compact JSON."""
    def normalize(value):
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in sorted(value.items())}
        if isinstance(value, (tuple, list)):
            return [normalize(v) for v in value]
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    content = json.dumps(normalize(parameters), ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def atm_lower_half(price):
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Positive finite underlying reference required')
    cents = js_round(price * 100)
    base = cents // 5000 * 5000
    return (base if cents - base <= 2500 else base + 5000) / 100


def usable(bar, *, full=True):
    fields = ('open', 'high', 'low', 'close') if full else ('open', 'close')
    if not bar or any(k not in bar or not math.isfinite(bar[k]) or bar[k] <= 0 for k in fields):
        return False
    return not full or (bar['high'] >= max(bar['open'], bar['close'], bar['low'])
                        and bar['low'] <= min(bar['open'], bar['close']))


def observed_usable(bar):
    """Respect adapter validity only where a policy actually consumes a candle."""
    return bool(bar) and bar.get('input_valid', True) and usable(bar, full=False)


def indicator_series(bars, period=14):
    """Literal source recurrences, including its unusual ADX sum-seed/average update.

    Returns None until each indicator's own source seed exists. No pandas/library
    smoothing defaults, rounding, gap fill or hidden lookback truncation.
    """
    result = []
    gains, losses, tr, plus, minus, dxs = [], [], [], [], [], []
    emas = {15: None, 35: None}
    atr = avg_gain = avg_loss = smtr = smplus = smminus = adx = None
    closes = []
    previous = None
    for bar in bars:
        if not usable(bar):
            raise ValueError('Invalid indicator candle; split/reject the source segment explicitly')
        closes.append(bar['close'])
        for p, old in list(emas.items()):
            if old is not None:
                emas[p] = (bar['close'] - old) * (2 / (p + 1)) + old
            elif len(closes) >= p:
                emas[p] = sum(closes[:p]) / p
        true_range = bar['high'] - bar['low'] if previous is None else max(
            bar['high'] - bar['low'], abs(bar['high'] - previous['close']), abs(bar['low'] - previous['close']))
        tr.append(true_range)
        if atr is not None:
            atr = (atr * (period - 1) + true_range) / period
        elif len(tr) == period:
            atr = sum(tr) / period
        rsi = None
        if previous is not None:
            change = bar['close'] - previous['close']
            gain, loss = max(change, 0), max(-change, 0)
            gains.append(gain)
            losses.append(loss)
            if avg_gain is not None:
                avg_gain = (avg_gain * (period - 1) + gain) / period
                avg_loss = (avg_loss * (period - 1) + loss) / period
            elif len(gains) == period:
                avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
            if avg_gain is not None:
                rsi = (100 if avg_gain > 0 else 50) if avg_loss == 0 else (
                    0 if avg_gain == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
        up = bar['high'] - previous['high'] if previous else 0
        down = previous['low'] - bar['low'] if previous else 0
        plus.append(up if up > down and up > 0 else 0)
        minus.append(down if down > up and down > 0 else 0)
        if smtr is not None:
            smtr = (smtr * (period - 1) + true_range) / period
            smplus = (smplus * (period - 1) + plus[-1]) / period
            smminus = (smminus * (period - 1) + minus[-1]) / period
        elif len(tr) == period:
            smtr, smplus, smminus = sum(tr), sum(plus), sum(minus)
        pdi = mdi = None
        if smtr is not None:
            pdi = 0 if smtr == 0 else 100 * smplus / smtr
            mdi = 0 if smtr == 0 else 100 * smminus / smtr
            dx = 0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi)
            dxs.append(dx)
            if adx is not None:
                adx = (adx * (period - 1) + dx) / period
            elif len(dxs) == period:
                adx = sum(dxs) / period
        result.append(dict(ema15=emas[15], ema35=emas[35], rsi14=rsi, atr14=atr,
                           adx14=adx, plusDI=pdi, minusDI=mdi))
        previous = bar
    return result


def primary_regime(close, values):
    needed = ('ema15', 'ema35', 'rsi14', 'adx14', 'atr14')
    if any(values.get(k) is None or not math.isfinite(values[k]) for k in needed):
        return None
    if values['adx14'] < 20 or abs(values['ema15'] - values['ema35']) / close * 100 <= .05:
        return 'SIDEWAYS'
    if values['ema15'] > values['ema35'] and values['rsi14'] > 50:
        return 'TREND_UP'
    if values['ema15'] < values['ema35'] and values['rsi14'] < 50:
        return 'TREND_DOWN'
    return 'SIDEWAYS'


@dataclass
class V2State:
    last_entry: int | None = None

    def evaluate(self, timestamp, regime, close, high, ema35, rsi14):
        if any(v is None for v in (close, high, ema35, rsi14)) or ema35 <= 0:
            return dict(entry=False, reason='BLOCKED_NOT_READY', proximityPercent=None, cooldownEligible=False)
        proximity = abs(high - ema35) / ema35 * 100
        cooldown = self.last_entry is None or timestamp - self.last_entry >= 600_000
        reason = ('BLOCKED_NOT_TREND_DOWN' if regime != 'TREND_DOWN' else 'BLOCKED_RSI' if rsi14 >= 35 else
                  'BLOCKED_PROXIMITY' if not (close < ema35 and proximity <= .2) else
                  'BLOCKED_COOLDOWN' if not cooldown else 'ENTER_TREND_DOWN_PE')
        if reason == 'ENTER_TREND_DOWN_PE':
            self.last_entry = timestamp
        return dict(entry=reason == 'ENTER_TREND_DOWN_PE', reason=reason, proximityPercent=proximity, cooldownEligible=cooldown)


@dataclass
class V4State:
    last_entry: int | None = None

    def evaluate(self, timestamp, bar, prior_three, atr, regime):
        if len(prior_three) != 3 or atr is None or atr <= 0:
            return dict(signal=False, rejectionReason='ATR_OR_COMPRESSION_NOT_READY')
        span = max(x['high'] for x in prior_three) - min(x['low'] for x in prior_three)
        body = abs(bar['close'] - bar['open'])
        threshold = min(x['low'] for x in prior_three) - atr * .1
        breakout = bar['close'] <= threshold and bar['close'] < bar['open']
        cooldown = self.last_entry is None or timestamp - self.last_entry >= 300_000
        reason = ('COMPRESSION_RANGE_EXCEEDS_2_ATR' if span > atr * 2 else
                  'EXPANSION_BODY_BELOW_1_ATR' if body < atr else 'BREAKOUT_THRESHOLD_NOT_MET' if not breakout else
                  'REGIME_NOT_TREND_DOWN' if regime != 'TREND_DOWN' else 'COOLDOWN_ACTIVE' if not cooldown else 'V4_MOMENTUM_PE_SIGNAL')
        if reason == 'V4_MOMENTUM_PE_SIGNAL':
            self.last_entry = timestamp
        return dict(timestamp=timestamp, close=bar['close'], regime=regime, atr=atr, compressionRange=span,
                    compressionRangeAtr=span / atr, body=body, bodyAtr=body / atr, breakoutThreshold=threshold,
                    breakoutPassed=breakout, regimeAligned=regime == 'TREND_DOWN', cooldownEligible=cooldown,
                    signal=reason == 'V4_MOMENTUM_PE_SIGNAL', rejectionReason=reason)


@dataclass
class V8State:
    armed: bool = False
    armed_at: int | None = None
    last_signal: int | None = None

    def evaluate(self, timestamp, bar, pdh, atr, rsi):
        before = self.armed
        reason = 'ATR_UNAVAILABLE' if atr is None or atr <= 0 else 'PDH_UNAVAILABLE' if pdh is None else None
        threshold = None if reason else pdh + atr * .1
        if reason is None and (rsi is None or rsi <= 50):
            reason = 'RSI_NOT_ABOVE_THRESHOLD'
        if reason is None:
            interaction = bar['low'] <= threshold and bar['close'] <= threshold
            reclaim = bar['close'] > bar['open'] and bar['close'] >= threshold
            if interaction and not self.armed:
                self.armed, self.armed_at = True, timestamp - 120_000
            if not before:
                reason = 'INTERACTION_ARMED_WAITING_FOR_RECLAIM' if interaction else 'AWAITING_PDH_INTERACTION'
            elif not (reclaim and abs(bar['close'] - bar['open']) >= atr * .25):
                reason = ('BULLISH_CLOSE_REQUIRED' if bar['close'] <= bar['open'] else
                          'RECLAIM_THRESHOLD_NOT_MET' if bar['close'] < threshold else 'BULLISH_BODY_BELOW_THRESHOLD')
            elif self.last_signal is not None and timestamp - self.last_signal < 300_000:
                self.armed = False
                reason = 'COOLDOWN'
            else:
                self.last_signal, self.armed, self.armed_at = timestamp, False, None
                reason = 'SIGNAL'
        return dict(signal=reason == 'SIGNAL', reason=reason, armedBefore=before, armedAfter=self.armed,
                    reclaimThreshold=threshold)


def rise5m(entry, session, ce, pe):
    if ce is None or pe is None:
        return dict(status='NOT_SELECTED', reason='NO_LOCKED_PAIR', qualifies=False)
    expected = range(entry - 6, entry)
    if not all(m in session for m in expected):
        return dict(status='FEATURE_UNAVAILABLE', reason='INSUFFICIENT_SAME_SESSION_LOOKBACK', qualifies=False)
    if any(m not in ce or m not in pe for m in expected):
        return dict(status='FEATURE_UNAVAILABLE', reason='MISSING_COMMON_LOOKBACK_CANDLE', qualifies=False)
    if any(not math.isfinite(b[m]['close']) or b[m]['close'] <= 0 for m in expected for b in (ce, pe)):
        return dict(status='FEATURE_UNAVAILABLE', reason='INVALID_LOOKBACK_CLOSE', qualifies=False)
    now = ce[entry - 1]['close'] + pe[entry - 1]['close']
    old = ce[entry - 6]['close'] + pe[entry - 6]['close']
    return dict(status='AVAILABLE', reason=None, cNow=now, cOld=old, changePoints=now - old, qualifies=now > old)


def orb_signal(spot):
    if any(m not in spot for m in range(555, 570)):
        return dict(status='FAIL_CLOSED_SPOT_RANGE_MISSING')
    high = max(spot[m]['high'] for m in range(555, 570))
    low = min(spot[m]['low'] for m in range(555, 570))
    for m in range(570, 660):
        if m not in spot:
            return dict(status='FAIL_CLOSED_MISSING_SCAN_CANDLE', minute=m)
        close = spot[m]['close']
        if close > high or close < low:
            return dict(status='SIGNAL', minute=m, side='CE' if close > high else 'PE', close=close)
    return dict(status='NO_SIGNAL')


def floor_gate(bars, signal):
    if signal not in bars or not observed_usable(bars[signal]):
        return 'SKIP_MISSING_SIGNAL_MINUTE_CANDLE'
    if not bars[signal]['close'] > 100:
        return 'SKIP_PREMIUM_FLOOR_SIGNAL_CLOSE'
    if signal + 1 not in bars or not observed_usable(bars[signal+1]):
        return 'SKIP_MISSING_ENTRY_CANDLE'
    if not bars[signal + 1]['open'] > 100:
        return 'SKIP_PREMIUM_FLOOR_ENTRY_OPEN'
    return None


def close_then_open_exit(entry, session, legs, *, kind):
    """H30 two BUYs and ORB one BUY remain separate policies, never intrabar OHLC fills."""
    if kind not in ('ce_pe_rise5m_h30', 'nifty_orb_single_option'):
        raise ValueError('Explicit candle policy required')
    if len(legs) != (2 if kind == 'ce_pe_rise5m_h30' else 1):
        raise ValueError('Incorrect structure')
    if not session or entry not in session:
        raise ValueError('Historical session/entry authority required')
    timeout = min(entry + 30, max(session)) if len(legs) == 2 else entry + 30
    result = dict(status='UNRESOLVED', reason=None, entryMinute=entry, entryPrices=None,
                  exitMinute=None, exitPrices=None, points=None, trigger=None, signalMinute=None,
                  timeoutMinute=timeout, monitored=0)
    if len(legs) == 1 and any(m not in session for m in range(entry, timeout + 1)):
        result['reason'] = 'SESSION_BOUNDARY'
        return result
    if any(entry not in b or not observed_usable(b[entry]) for b in legs):
        result['reason'] = 'MISSING_ENTRY_CANDLE'
        return result
    prices = [b[entry]['open'] for b in legs]
    if not all(math.isfinite(x) and x > (100 if len(legs) == 2 else 0) for x in prices):
        result.update(status='NO_TRADE_ENTRY_PREMIUM_FLOOR', reason='NO_TRADE_ENTRY_PREMIUM_FLOOR')
        return result
    result['entryPrices'] = prices
    debit = sum(prices)
    for m in sorted(x for x in session if entry <= x < timeout):
        if any(m not in b or not observed_usable(b[m]) for b in legs):
            result['reason'] = f'MISSING_MONITORING_CANDLE:{m}'
            return result
        result['monitored'] += 1
        value = sum(b[m]['close'] for b in legs)
        # ORB source uses integer paise; H30 source compares unrounded sums.
        a, d = (js_round(value * 100), js_round(debit * 100)) if len(legs) == 1 else (value, debit)
        if len(legs) == 1:
            trigger = 'TARGET' if a * 100 >= d * 105 else 'STOP' if a * 100 <= d * 75 else None
        else:
            trigger = 'TARGET' if value >= debit * (1 + 5 / 100) else 'STOP' if value <= debit * .75 else None
        if trigger and m + 1 in session and m + 1 <= timeout:
            result.update(trigger=trigger, signalMinute=m, exitMinute=m + 1)
            break
    if result['exitMinute'] is None:
        if timeout not in session:
            result['reason'] = 'EXACT_TIMEOUT_OUTSIDE_HISTORICAL_SESSION'
            return result
        result.update(trigger='TIMEOUT', exitMinute=timeout)
    if any(result['exitMinute'] not in b or not observed_usable(b[result['exitMinute']]) for b in legs):
        result['reason'] = f'MISSING_EXIT_CANDLE:{result["exitMinute"]}'
        return result
    exits = [b[result['exitMinute']]['open'] for b in legs]
    result.update(status='RESOLVED', exitPrices=exits, points=sum(exits) - debit)
    return result


def phase_b_exit(decision, bars, strategy, *, require_contiguous=True):
    """Original Phase-B OHLC policy; strict coverage is an explicit evidence guard.

    The legacy source skips holes and deduplicates/filter-invalid rows upstream.
    This port's observed backtests refuse a pre-exit hole. Set require_contiguous
    False ONLY for a labelled legacy-policy diagnostic, not a real-data baseline.
    """
    if strategy not in STRATEGIES[:3]:
        raise ValueError('Not a Phase-B strategy')
    target_pct = 6 if strategy == STRATEGIES[2] else 5
    deadline = min(decision + 15, 930)
    entry = next((m for m in sorted(bars) if m > decision and usable(bars[m]) and bars[m].get('input_valid',True)), None)
    if entry is None or entry > deadline - 1:
        return dict(status='NO_ENTRY_OBSERVATION', reason='FIRST_OBSERVED_CANDLE_AFTER_TRADE_DEADLINE')
    if require_contiguous and entry != decision + 1:
        return dict(status='NO_ENTRY_OBSERVATION', reason='STRICT_MISSING_EXACT_POST_DECISION_ENTRY', requestedEntryMinute=decision+1)
    price = bars[entry]['open']
    target, stop = price * (1 + target_pct / 100), price * .95
    result = dict(status='PNL_UNRESOLVED', entryMinute=entry, entryPrices=[price],
                  exitMinute=None, exitPrices=None, points=None, trigger=None,
                  policy='STRICT_CONTIGUOUS_PHASE_B_V1' if require_contiguous else 'LEGACY_PHASE_B_OHLC_V1')
    for m in range(entry, deadline):
        if m not in bars or not usable(bars[m]) or not bars[m].get('input_valid',True):
            if require_contiguous:
                return {**result, 'reason': f'MISSING_OR_INVALID_MONITORING_CANDLE:{m}'}
            continue
        b = bars[m]
        if b['open'] <= stop:
            exit_price, reason = b['open'], 'STOP_GAP'
        elif b['open'] >= target:
            exit_price, reason = target, 'TARGET_GAP'
        elif b['low'] <= stop and b['high'] >= target:
            return {**result, 'reason': 'AMBIGUOUS_SAME_CANDLE_TARGET_AND_STOP', 'trigger': 'AMBIGUOUS_SAME_CANDLE', 'exitMinute': m}
        elif b['low'] <= stop:
            exit_price, reason = stop, 'STOP_LOSS'
        elif b['high'] >= target:
            exit_price, reason = target, 'TARGET'
        elif m == deadline - 1:
            exit_price, reason = b['close'], 'TIME_EXIT' if decision + 15 <= 930 else 'EOD_EXIT'
        else:
            continue
        return {**result, 'status': 'RESOLVED', 'reason': None, 'exitMinute': m,
                'exitPrices': [exit_price], 'points': exit_price - price, 'trigger': reason}
    return {**result, 'reason': 'EXIT_UNRESOLVED_NO_OBSERVATION_AT_DEADLINE'}


def t3_structure(spot, vix, sessions_to_expiry, reference_ce, reference_pe):
    if vix is None or not math.isfinite(vix) or vix <= 0:
        return dict(status='NO_TRADE_VIX_DATA')
    if vix < 15:
        return dict(status='VIX_BELOW_15')
    if vix >= 20:
        return dict(status='VIX_AT_OR_ABOVE_20')
    if sessions_to_expiry is None:
        return dict(status='NO_TRADE_EXPIRY_DISTANCE_UNKNOWN')
    if sessions_to_expiry < 1:
        return dict(status='NO_TRADE_EXPIRY_DAY')
    if any(x is None or not math.isfinite(x) or x <= 0 for x in (reference_ce, reference_pe)):
        return dict(status='NO_TRADE_STRADDLE_REFERENCE_DATA')
    atm = atm_lower_half(spot)
    wing = math.ceil(js_round((reference_ce + reference_pe) * 100) / 5000) * 50
    return dict(status='SELECTED', atm=atm, wingDistance=wing,
                legs=[dict(side='CE', strike=atm, ratio=-1), dict(side='PE', strike=atm, ratio=-1),
                      dict(side='CE', strike=atm + wing, ratio=1), dict(side='PE', strike=atm - wing, ratio=1)])


def t3_exit(wing, legs):
    """Only exact 09:31/15:01 opens. No target, stop, rolling or projection."""
    if len(legs) != 4:
        raise ValueError('T3 requires its four ordered short/short/long/long legs')
    if any(571 not in b or not observed_usable(b[571]) for b in legs):
        return dict(status='NO_TRADE_ENTRY_DATA')
    entry = [b[571]['open'] for b in legs]
    credit = entry[0] + entry[1] - entry[2] - entry[3]
    if not 0 < credit < wing:
        return dict(status='INELIGIBLE_INVALID_STRUCTURE', entryPrices=entry, credit=credit)
    result = dict(status='POST_ENTRY_UNRESOLVED_DATA', entryMinute=571, entryPrices=entry,
                  credit=credit, maxProfitPoints=credit, maxLossPoints=wing - credit,
                  exitMinute=None, exitPrices=None, points=None, trigger='FIXED_1501_OPEN',
                  brokerMargin='NOT_COMPUTABLE')
    if any(901 not in b or not observed_usable(b[901]) for b in legs):
        return result
    exit_prices = [b[901]['open'] for b in legs]
    debit = exit_prices[0] + exit_prices[1] - exit_prices[2] - exit_prices[3]
    if not -1e-6 <= debit <= wing + 1e-6:
        return {**result, 'status': 'DATA_INTEGRITY_UNRESOLVED', 'rawExitPrices': exit_prices, 'rawExitDebit': debit}
    return {**result, 'status': 'RESOLVED', 'exitMinute': 901, 'exitPrices': exit_prices,
            'exitDebit': debit, 'points': credit - debit}


def source_fees(date, buy_premium, sell_premium, quantity, *, orders=2):
    """TradeMind STANDARD conditional tariff; no component/rupee rounding in source."""
    if date < '2026-03-01':
        raise Unavailable('NOT_COMPUTABLE: source fee authority starts 2026-03-01')
    if not (math.isfinite(buy_premium) and buy_premium > 0 and math.isfinite(sell_premium) and sell_premium >= 0
            and isinstance(quantity, int) and quantity > 0):
        raise ValueError('Invalid source charge inputs')
    buy, sell = buy_premium * quantity, sell_premium * quantity
    brokerage = 20 * orders
    exchange = (buy + sell) * (.03553 / 100)
    values = dict(brokerage=brokerage, stt=sell * (.1 / 100 if date < '2026-04-01' else .15 / 100),
                  exchangeTransactionCharges=exchange, sebiCharges=((buy + sell) / 10_000_000) * 10,
                  stampDuty=buy * (.003 / 100), otherCharges=0., gst=(brokerage + exchange) * .18)
    return {**values, 'totalCharges': sum(values.values())}


def size_lots(cash, debit, lot, entry_fees=lambda q: 0.):
    if not math.isfinite(cash) or cash < 0 or debit <= 0 or not isinstance(lot, int) or lot <= 0:
        raise ValueError('Explicit positive whole dated lot and valid cash/debit required')
    low, high = 0, math.floor(cash / (debit * lot))
    while low < high:
        mid = math.ceil((low + high) / 2)
        if mid * lot * debit + entry_fees(mid * lot) <= cash:
            low = mid
        else:
            high = mid - 1
    return low


@dataclass(frozen=True)
class Contract:
    provider: str
    contract_id: str
    expiry: str
    strike: float
    side: str
    known_at: int
    lot: int | None = None
    tick: float | None = None
    evidence: str = ''


def select_contract(contracts, *, provider, expiry, side, spot, decision, exact=False):
    """PIT candidates from a caller-proven ownership plan, never a rolling label."""
    eligible = [c for c in contracts if c.provider == provider and c.expiry == expiry and c.side == side
                and c.contract_id and c.evidence and c.known_at <= decision]
    if exact:
        eligible = [c for c in eligible if c.strike == atm_lower_half(spot)]
        if len(eligible) > 1:
            raise Unavailable('AMBIGUOUS_EXACT_DATED_CONTRACT')
    eligible.sort(key=lambda c: (abs(c.strike - spot), c.strike, c.contract_id))
    return eligible[0] if eligible else None
