"""Separate H30 tick-feasibility variant: recorded asks/bids, no candle fills.

This is a research proxy, not live execution. Pure functions; no socket, broker,
DB, credential or file API. Persisted receive order is part of the input contract.
"""
from collections import Counter
from datetime import datetime, timezone, timedelta
import math

from quant_trademind import atm_lower_half, size_lots, source_fees


def day_start(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone(timedelta(minutes=330))).timestamp() * 1000)


def build_candles(observations, date, settle=2000):
    candles = {}
    start = day_start(date)
    for o in observations:
        if not o['newTrade'] or o['ltt'] is None or o['ltp'] is None:
            continue
        m = math.floor((o['ltt'] - start) / 60_000)
        if not 0 <= m < 1440:
            continue
        b = candles.setdefault(o['key'], {}).setdefault(m, dict(open=None, close=None, trades=0, lateTrades=0))
        if o['recv'] <= start + (m + 1) * 60_000 + settle:
            if b['trades'] == 0:
                b['open'] = o['ltp']
            b['close'] = o['ltp']
            b['trades'] += 1
        else:
            b['lateTrades'] += 1
    return {key:{m:b for m,b in values.items() if b['trades']} for key,values in candles.items()}


def gap_affected(date, minute, gaps, settle=2000):
    start = day_start(date) + minute * 60_000
    return any(g['fromRecv'] <= start + 60_000 + settle and (g['toRecv'] is None or g['toRecv'] >= start) for g in gaps)


def quote_ineligibility(o, side, eligible, generation, now):
    if o['recv'] < eligible or o['src'] is None or o['src'] < eligible:
        return 'NOT_POST_DECISION_QUOTE'
    if o['gen'] != generation:
        return 'GENERATION_MISMATCH'
    if now - o['src'] > 2000:
        return 'STALE_QUOTE'
    price = o['ask'] if side == 'BUY' else o['bid']
    quantity = o['askQty'] if side == 'BUY' else o['bidQty']
    if price is None or price <= 0:
        return 'MISSING_ASK' if side == 'BUY' else 'MISSING_OR_ZERO_BID'
    if quantity is None or quantity <= 0:
        return 'MISSING_QUANTITY'
    if o['bid'] is not None and o['ask'] is not None and o['bid'] > 0 and o['ask'] > 0:
        if o['bid'] >= o['ask']:
            return 'CROSSED_OR_LOCKED'
        if (o['ask'] - o['bid']) / ((o['ask'] + o['bid']) / 2) * 100 > 5:
            return 'WIDE_SPREAD'
    return None


def fill(leg, side, o, qty):
    mid = (o['ask'] + o['bid']) / 2 if o['ask'] and o['bid'] and o['ask'] > 0 and o['bid'] > 0 else None
    return dict(leg=leg, side=side, qty=qty, price=o['ask'] if side == 'BUY' else o['bid'],
                recv=o['recv'], seq=o['seq'], gen=o['gen'], mid=mid,
                quoteQty=o['askQty'] if side == 'BUY' else o['bidQty'])


def attempt_entry(obs, ce_key, pe_key, decision, generation, lot, cash, entry_charges):
    eligible, deadline = decision + 250, decision + 2250
    state, reasons, count = {}, Counter(), 0
    for o in obs:
        if o['recv'] < decision:
            continue
        if o['recv'] > deadline:
            break
        leg = 'CE' if o['key'] == ce_key else 'PE' if o['key'] == pe_key else None
        if leg is None:
            continue
        count += 1
        if o['recv'] < eligible:
            reasons['NOT_POST_DECISION_QUOTE'] += 1
            continue
        state[leg] = o
        if not all(s in state for s in ('CE', 'PE')):
            reasons['ONE_LEG_NO_QUOTE_YET'] += 1
            continue
        ce, pe = state['CE'], state['PE']
        why = [quote_ineligibility(q, 'BUY', eligible, generation, o['recv']) for q in (ce, pe)]
        if any(why):
            reasons.update(x for x in why if x)
            continue
        if not (ce['ask'] > 100 and pe['ask'] > 100):
            reasons['ASK_AT_OR_BELOW_PREMIUM_FLOOR'] += 1
            continue
        debit = ce['ask'] + pe['ask']
        cash_lots = size_lots(cash, debit, lot, lambda q:entry_charges(ce['ask'], pe['ask'], q))
        quote_lots = math.floor(min(ce['askQty'], pe['askQty']) / lot)
        if cash_lots <= 0 or quote_lots <= 0:
            reasons['CAPITAL_NO_AFFORDABLE_LOT' if cash_lots <= 0 else 'INSUFFICIENT_TOP_OF_BOOK_QUANTITY'] += 1
            continue
        lots = min(cash_lots, quote_lots)
        return dict(status='FILLED', lots=lots, qty=lots * lot, cashLots=cash_lots, quoteLots=quote_lots,
                    limitedBy='BOTH' if cash_lots == quote_lots else 'CASH' if cash_lots < quote_lots else 'TOP_OF_BOOK_QUANTITY',
                    askCe=ce['ask'], askPe=pe['ask'], debit=debit,
                    entryCharges=entry_charges(ce['ask'], pe['ask'], lots * lot),
                    fills=[fill('CE', 'BUY', ce, lots * lot), fill('PE', 'BUY', pe, lots * lot)],
                    filledAt=o['recv'], eligibleAt=eligible, deadline=deadline)
    ranked = sorted(((k,v) for k,v in reasons.items() if k not in ('ONE_LEG_NO_QUOTE_YET','NOT_POST_DECISION_QUOTE')), key=lambda x:(-x[1],x[0]))
    reason = 'NO_QUOTE_RECEIVED_IN_WINDOW' if count == 0 else ranked[0][0] if ranked else 'ONE_LEG_NO_QUOTE_IN_WINDOW' if reasons['ONE_LEG_NO_QUOTE_YET'] else 'NO_POST_DECISION_QUOTE_IN_WINDOW'
    return dict(status='FAILED', reason=reason, reasons=dict(reasons), eligibleAt=eligible, deadline=deadline, legObservationsInWindow=count)


def attempt_exit(obs, ce_key, pe_key, decision, generation, quantity):
    eligible, deadline = decision + 250, decision + 2250
    remaining, last, fills, reasons = dict(CE=quantity, PE=quantity), {}, [], Counter()
    for o in obs:
        if o['recv'] < decision:
            continue
        if o['recv'] > deadline:
            break
        leg = 'CE' if o['key'] == ce_key else 'PE' if o['key'] == pe_key else None
        if leg is None or remaining[leg] <= 0:
            continue
        why = quote_ineligibility(o, 'SELL', eligible, generation, o['recv'])
        if why:
            reasons[leg + ':' + why] += 1
            continue
        signature = (o['bid'], o['bidQty'])
        if last.get(leg) == signature:
            reasons[leg + ':REPEATED_UNCHANGED_BID'] += 1
            continue
        last[leg] = signature
        quantity_sold = min(remaining[leg], o['bidQty'])
        remaining[leg] -= quantity_sold
        fills.append(fill(leg, 'SELL', o, quantity_sold))
    return dict(fills=fills, remaining=remaining, complete=all(v == 0 for v in remaining.values()),
                reasons=dict(reasons), eligibleAt=eligible, deadline=deadline)


def simulate_tick_day(input_data, costs=None):
    date, universe, obs, gaps = (input_data[k] for k in ('date','universe','obs','gaps'))
    settle = input_data.get('settleMs', 2000)
    if any(a['recv'] > b['recv'] for a,b in zip(obs, obs[1:])):
        raise ValueError('Persisted receive order required; do not sort away regressions')
    result = dict(date=date, executionVariant='H30_TICK_FEASIBILITY_V1', status=None, points=None,
                  gross=None, net=None, finalCash=None)
    if universe is None:
        return {**result, 'status':'NOT_EVALUABLE_NO_LOCKED_UNIVERSE'}
    expiry = universe.get('frontExpiry')
    if not expiry:
        return {**result, 'status':'UNIVERSE_EXPIRY_AUTHORITY_UNRESOLVED'}
    candles = build_candles(obs, date, settle)
    def get(key, m):
        return None if gap_affected(date, m, gaps, settle) else candles.get(key, {}).get(m)
    spot = get(input_data['indexKey'], 660)
    if not spot:
        return {**result, 'status':'MISSING_REFERENCE_SPOT'}
    atm = atm_lower_half(spot['close'])
    contracts = [[c for c in universe['contracts'] if c['expiry'] == expiry and c['strike'] == atm and c['optionType'] == side] for side in ('CE','PE')]
    if any(len(c) != 1 for c in contracts):
        return {**result, 'status':'UNIVERSE_COVERAGE_FAILURE'}
    ce, pe = (c[0] for c in contracts)
    ce_key, pe_key = ce['instrumentKey'], pe['instrumentKey']
    refs = [get(key, 660) for key in (ce_key, pe_key)]
    if not all(refs):
        return {**result, 'status':'MISSING_REFERENCE_CANDLE'}
    if not all(b['close'] > 100 for b in refs):
        return {**result, 'status':'PREMIUM_FLOOR_REJECTION'}
    if not all(get(key, m) for key in (ce_key,pe_key) for m in range(655,661)):
        return {**result, 'status':'FILTER_FEATURE_UNAVAILABLE'}
    if sum(b['close'] for b in refs) <= sum(get(key,655)['close'] for key in (ce_key,pe_key)):
        return {**result, 'status':'FILTER_NOT_RISING'}
    def default_costs(a,b,x,y,q):
        entry = source_fees(date,a,0,q,orders=1)['totalCharges'] + source_fees(date,b,0,q,orders=1)['totalCharges']
        total = source_fees(date,a,x,q)['totalCharges'] + source_fees(date,b,y,q)['totalCharges']
        return dict(entryCharges=entry, exitCharges=total-entry, total=total)
    costs = costs or default_costs
    def generation(at):
        values = [o['gen'] for o in obs if o['recv'] <= at]
        return values[-1] if values else None
    def final(m):
        return day_start(date) + (m + 1) * 60_000 + settle
    lot = ce['lotSize'] if ce['lotSize'] == pe['lotSize'] else 0
    gen = generation(final(660))
    if lot <= 0 or gen is None:
        return {**result, 'status':'ENTRY_EXECUTION_FAILURE', 'reason':'LOT_OR_GENERATION_UNAVAILABLE'}
    entry = attempt_entry(obs, ce_key, pe_key, final(660), gen, lot, 100000., lambda a,b,q:costs(a,b,0,0,q)['entryCharges'])
    result['entry'] = entry
    if entry['status'] == 'FAILED':
        return {**result, 'status':'ENTRY_EXECUTION_FAILURE', 'reason':entry['reason']}
    trigger, signal = 'TIMEOUT', 690
    for m in range(661,691):
        values = [get(key,m) for key in (ce_key,pe_key)]
        if not all(values):
            return {**result, 'status':'UNRESOLVED_MONITORING', 'exposedQuantity':entry['qty'], 'missingMinute':m}
        value = sum(b['close'] for b in values)
        if value >= entry['debit'] * 1.05:
            trigger, signal = 'TARGET', m
            break
        if value <= entry['debit'] * .75:
            trigger, signal = 'STOP', m
            break
    gen = generation(final(signal))
    if gen is None:
        return {**result, 'status':'EXIT_INCOMPLETE_EXPOSED', 'exposedQuantity':entry['qty']}
    exit_result = attempt_exit(obs, ce_key, pe_key, final(signal), gen, entry['qty'])
    result.update(exit=exit_result, trigger=trigger, signalMinute=signal)
    if not exit_result['complete']:
        return {**result, 'status':'EXIT_INCOMPLETE_EXPOSED', 'exposedQuantity':exit_result['remaining']}
    proceeds = {side:sum(f['qty'] * f['price'] for f in exit_result['fills'] if f['leg'] == side) for side in ('CE','PE')}
    gross = sum(proceeds.values()) - entry['qty'] * entry['debit']
    fees = costs(entry['askCe'], entry['askPe'], proceeds['CE'] / entry['qty'], proceeds['PE'] / entry['qty'], entry['qty'])['total'] if lot == 65 else None
    return {**result, 'status':'ENTERED_EXITED', 'gross':gross, 'points':gross / entry['qty'],
            'fees':fees, 'net':gross-fees if fees is not None else None,
            'finalCash':100000+gross-fees if fees is not None else None}
