"""Causal wiring for frozen TradeMind V2/V4/V8; separate from Dhan indicators.

The source V2 seed spans the supplied canonical replay prefix. V4 deliberately
re-seeds bounded tails. V8 uses one complete prior PRESENT session for RSI/PDH
and current-session ATR. These three initializations are intentionally distinct.
"""
from datetime import datetime, timezone, timedelta

from quant_trademind import STRATEGIES, Unavailable, V2State, V4State, V8State, indicator_series, primary_regime, usable

IST = timezone(timedelta(minutes=330))


def stamp(date, minute):
    return int((datetime.fromisoformat(date).replace(tzinfo=IST) + timedelta(minutes=minute)).timestamp() * 1000)


def aggregate(bars, minutes):
    """09:15 anchored complete contiguous buckets; no resampling/price fill."""
    result = []
    for start in range(555, 930, minutes):
        if start + minutes > 930 or not all(m in bars for m in range(start, start + minutes)):
            continue
        values = [bars[m] for m in range(start, start + minutes)]
        if not all(usable(b) for b in values):
            raise Unavailable(f'INVALID_UNDERLYING_BUCKET:{start}')
        result.append(dict(minute=start, open=values[0]['open'], high=max(b['high'] for b in values),
                           low=min(b['low'] for b in values), close=values[-1]['close'],
                           volume=sum(b.get('volume', 0) for b in values)))
    return result


def generate_signals(strategy, sessions):
    """Replay-prefix input; returns signals AND explicit warm-up/integrity outcomes.

    Requested execution dates must be filtered by the caller AFTER preparing the
    source-defined prefix. Omitting older seed data intentionally changes V2's
    source behavior and must never be described as seed-independent parity.
    """
    if strategy not in STRATEGIES[:3]:
        raise ValueError('V2/V4/V8 signal wiring only')
    if len({x['date'] for x in sessions}) != len(sessions):
        raise ValueError('Duplicate session dates')
    ordered = sorted(sessions, key=lambda x: x['date'])
    if any(s.get('break_before') for s in ordered[1:]):
        segments = [[]]
        for session in ordered:
            if session.get('break_before') and segments[-1]:
                segments.append([])
            segments[-1].append({**session,'break_before':False})
        signals, diagnostics = [], []
        for segment in segments:
            s,d = generate_signals(strategy,segment)
            signals.extend(s)
            diagnostics.extend(d)
        diagnostics.extend(dict(date=s['date'],status='EXPLICIT_UNDERLYING_ELIGIBILITY_BREAK_RESEEDED') for s in ordered if s.get('break_before'))
        return signals,diagnostics
    frames = {}
    for session in ordered:
        date, bars = session['date'], session['bars']
        frames[date] = {}
        for size in (2, 3, 5):
            frames[date][size] = [dict(b, date=date, timestamp=stamp(date, b['minute'])) for b in aggregate(bars, size)]
    signals, diagnostics = [], []

    def emit(date, bar, minutes, side, evidence):
        at = bar['minute'] + minutes
        signals.append(dict(date=date, timestamp=stamp(date, at), minute=at, side=side,
                            spot=bar['close'], asOfMinute=at - 1, evidence=evidence))

    if strategy == STRATEGIES[0]:
        state = V2State()
        all_five = [b for s in ordered for b in frames[s['date']][5]]
        values = indicator_series(all_five)
        for index, (bar, value) in enumerate(zip(all_five, values)):
            if index < 35:
                diagnostics.append(dict(date=bar['date'], minute=bar['minute'] + 5, status='WARMUP_36_5M'))
                continue
            regime = primary_regime(bar['close'], value)
            decision = state.evaluate(bar['timestamp'] + 300_000, regime, bar['close'], bar['high'], value['ema35'], value['rsi14'])
            if decision['entry']:
                emit(bar['date'], bar, 5, 'PE', {**value, **decision, 'regime': regime})
        return signals, diagnostics

    prior3, prior5 = [], []
    seeded = False
    warmup_five = 0
    for index, session in enumerate(ordered):
        date = session['date']
        current3, current5 = frames[date][3], frames[date][5]
        if strategy == STRATEGIES[1]:
            state = V4State()
            for i, bar in enumerate(current3):
                at = bar['minute'] + 3
                if not seeded:
                    count = warmup_five + sum(b['minute'] + 5 <= at for b in current5)
                    if count < 35:
                        diagnostics.append(dict(date=date, minute=at, status='WARMUP_35_5M'))
                        continue
                    # The initial seed includes all buckets complete when 35th 5m arrives.
                    seed_boundary = next((b['minute'] + 5 for j, b in enumerate(current5)
                                          if warmup_five + j + 1 >= 35), 930)
                    seeded = True
                    if at <= seed_boundary:
                        continue
                current = current3[:i + 1]
                if len(current) < 4:
                    continue
                window3 = (prior3 + current)[-(84 + len(current)):]
                atr = indicator_series(window3)[-1]['atr14']
                # 3m callback precedes a simultaneously completed 5m callback.
                current_eligible5 = [b for b in current5 if b['minute'] + 5 < at]
                all5 = (prior5 + current_eligible5)[-(50 + len(current_eligible5)):]
                regime = primary_regime(all5[-1]['close'], indicator_series(all5)[-1]) if len(all5) >= 35 else None
                decision = state.evaluate(bar['timestamp'] + 180_000, bar, current[-4:-1], atr, regime)
                if decision['signal']:
                    emit(date, bar, 3, 'PE', decision)
            warmup_five += len(current5)
            prior3.extend(current3)
            prior5.extend(current5)
            continue

        # V8 readiness chooses the immediately preceding PRESENT date, never an older substitute.
        if index == 0:
            diagnostics.append(dict(date=date, status='NO_PRIOR_SESSION_AVAILABLE'))
            continue
        prior = ordered[index - 1]
        if sorted(prior['bars']) != list(range(555, 930)):
            diagnostics.append(dict(date=date, status='REQUIRED_PRIOR_SESSION_INCOMPLETE', priorDate=prior['date']))
            continue
        bars2 = frames[date][2]
        prior2 = frames[prior['date']][2][-125:]
        rsi = indicator_series(prior2 + bars2)[len(prior2):]
        local = indicator_series(bars2)
        pdh = max(b['high'] for b in prior['bars'].values())
        state = V8State()
        for bar, val, local_val in zip(bars2, rsi, local):
            at = bar['minute'] + 2
            if any(m not in session['bars'] for m in range(555, at)):
                diagnostics.append(dict(date=date, minute=at, status='V8_TARGET_SESSION_SOURCE_DISCONTINUOUS'))
                break
            decision = state.evaluate(bar['timestamp'] + 120_000, bar, pdh, local_val['atr14'], val['rsi14'])
            if decision['signal']:
                emit(date, bar, 2, 'CE', {**decision, 'pdh': pdh, 'atr14': local_val['atr14'], 'rsi14': val['rsi14']})
    return signals, diagnostics
