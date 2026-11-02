"""Observed-data, independent-account TradeMind research. Never paper/live enabled."""
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
import hashlib
import json
import math
import pandas as pd

from quant_trademind import (STRATEGIES, REVISION, Unavailable, atm_lower_half, close_then_open_exit,
                            floor_gate, orb_signal, phase_b_exit, rise5m, source_fees, size_lots,
                            t3_structure, t3_exit, observed_usable)
from trademind_signals import generate_signals, stamp

NATIVE_FRAMES = dict(zip(STRATEGIES, ('5m', '3m', '2m', '1m', '1m', '1m')))
SUMMARY_REVISION = 'annual-account-summary-v1'
LABELS = dict(zip(STRATEGIES, ('TradeMind V2 — TREND_DOWN PE', 'TradeMind V4 — Momentum Expansion PE',
                              'TradeMind V8 — Bullish Reclaim CE', 'TradeMind T3 — VIX ATM Iron Fly',
                              'TradeMind CE+PE Rise-5m H30', 'TradeMind NIFTY ORB Single Option')))


def evidence_json_safe(value):
    """Lossless tags for nonfinite optional evidence; never mutate input candles.

    Valid simulated prices still must pass the execution guards. An unused raw
    volume NaN is neither zero volume nor a reason to invent/drop a price.
    """
    if isinstance(value,float) and not math.isfinite(value):
        return {'nonfinite_float':'NaN' if math.isnan(value) else '+Infinity' if value>0 else '-Infinity'}
    if isinstance(value,dict):
        return {k:evidence_json_safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):
        return [evidence_json_safe(v) for v in value]
    return value


def validate_config(c):
    if c.instrument != 'NIFTY':
        raise ValueError('TradeMind frozen strategies support NIFTY only')
    if c.timeframe != NATIVE_FRAMES[c.strategy]:
        raise ValueError('TradeMind frozen native timeframe: ' + NATIVE_FRAMES[c.strategy])
    if c.price_policy != 'strict_observed':
        raise ValueError('TradeMind requires strict_observed; modeled/rolling fallback is forbidden')
    if c.trademind_provider not in ('dhan', 'trademind_retained') or c.trademind_scenario not in ('source_baseline', 'allocation_100'):
        raise ValueError('Unknown TradeMind provider/scenario')
    if not math.isfinite(c.capital) or c.capital <= 0 or c.capital_usage_pct != 100:
        raise ValueError('TradeMind requires positive capital and the declared 100% scenario allocation')
    if c.slippage != 0 or c.trail_pct != 0:
        raise ValueError('Frozen candle policies have no additional slippage/trailing overlay')
    target, stop, floor = (0., 0., 0.) if c.strategy == STRATEGIES[3] else (5., 25., 100.) if c.strategy in STRATEGIES[4:] else (6. if c.strategy == STRATEGIES[2] else 5., 5., 0.)
    if (c.target_pct, c.sl_pct, c.minimum_premium) != (target, stop, floor):
        raise ValueError('Frozen TradeMind target/stop/premium rules cannot be tuned')
    start, end = pd.Timestamp(c.start), pd.Timestamp(c.end)
    if pd.isna(start) or pd.isna(end) or start >= end:
        raise ValueError('end is exclusive and must follow start')
    if start.year != (end - pd.Timedelta(nanoseconds=1)).year:
        raise ValueError('TradeMind requires [start, end) within a single calendar year; run each year separately with fresh configured capital. January 1 of the next year is a valid exclusive end.')


def frozen_config(c, **overrides):
    """Explicit builder for source constants. Other existing strategies are untouched."""
    s = c.strategy
    target, stop, floor = (0., 0., 0.) if s == STRATEGIES[3] else (5., 25., 100.) if s in STRATEGIES[4:] else (6. if s == STRATEGIES[2] else 5., 5., 0.)
    return replace(c, timeframe=NATIVE_FRAMES[s], price_policy='strict_observed', slippage=0.,
                   target_pct=target, sl_pct=stop, trail_pct=0., minimum_premium=floor,
                   capital_usage_pct=100., compound=True, sizing_mode='allocation',
                   max_trades_day=0 if s in STRATEGIES[:3] else 1, max_lots=0,
                   entry_start='09:15', entry_end='15:30', exit_mode='end_of_day' if s == STRATEGIES[3] else 'target_eod',
                   **overrides)


def availability(strategy, provider='dhan', root=None):
    if provider == 'dhan':
        reason = 'DATA_UNAVAILABLE: Dhan WEEK1 rolling labels lack dated expiry/option contract IDs and PIT ownership evidence.'
        if strategy == STRATEGIES[3]:
            reason += ' Exact-time historical India VIX and broker-margin evidence are also unavailable.'
        return dict(runnable=False, status='DATA_UNAVAILABLE', reason=reason)
    from trademind_data import DEFAULT_ROOT
    root = Path(root or DEFAULT_ROOT)
    present = (root / 'manifests/EXPIRED_OPTION_1M').is_dir() and (root / 'research-underlying-certifications').is_dir()
    return dict(runnable=present, status='READY_FOR_OBSERVED_RESEARCH' if present else 'DATA_UNAVAILABLE',
                reason='Retained UPSTOX underlying / GROWW dated options; fee, calendar and margin limitations apply.' if present else 'Retained manifest-bound data are absent.')


def dated_lot(date, expiry):
    """Bounded source resolveLot authority. Never extrapolates to a current master."""
    days = (pd.Timestamp(expiry) - pd.Timestamp(date)).days
    if days < 0 or days > 7:
        return None
    if date >= '2025-01-01' and expiry == '2025-01-30':
        return 25
    if date >= '2025-01-01' and '2025-01-02' <= expiry <= '2025-12-30':
        return 75
    if date >= '2025-12-31' and '2026-01-06' <= expiry <= '2026-09-08':
        return 65
    if date >= '2025-10-29' and '2026-01-06' <= expiry <= '2026-01-20':
        return 65
    return None


def capital_ledger(trades, strategy, scenario, *, capital=100_000., net=False):
    """Whole-lot event cash; an entered unresolved path freezes all later equity.

    T3 produces an explicitly theoretical loss-capacity overlay, NEVER executable
    broker-margin sizing. Source-baseline assumed75 is named; dated scenario is
    separate. Net has a separate fresh cost-supported account, no gross carry-in.
    """
    cash, peak, drawdown, dd_pct = capital, capital, 0., 0.
    pending, rows = [], []
    for trade in sorted(trades, key=lambda t: (t['date'], t.get('entryMinute', 0), t.get('trade_id', ''))):
        if net and not '2026-03-01' <= trade['date'] <= '2026-09-08':
            rows.append(dict(date=trade['date'], status='COST_NOT_COMPUTABLE', equity=None, pnl=None))
            continue
        entry_time = stamp(trade['date'], trade.get('entryMinute', 0))
        # Source V2/V4/V8 convention: intrabar exit at the same timestamp is not
        # known to precede another entry OPEN. Release strictly earlier exits only.
        released = [p for p in pending if p['exit_time'] < entry_time]
        for p in released:
            cash += p['release']
        pending = [p for p in pending if p not in released]
        if cash is None:
            rows.append(dict(date=trade['date'], trade_id=trade.get('trade_id'), status='EQUITY_UNRESOLVED', equity=None, pnl=None))
            continue
        entries = trade.get('entryPrices')
        if not entries or trade['status'].startswith(('NO_TRADE', 'NO_ENTRY', 'INELIGIBLE')):
            rows.append(dict(date=trade['date'], trade_id=trade.get('trade_id'), status=trade['status'], equity=cash + sum(p['committed'] for p in pending), pnl=0., quantity=0))
            continue
        if cash <= 0:
            rows.append(dict(date=trade['date'],trade_id=trade.get('trade_id'),status='CAPITAL_NO_TRADE',equity=cash,pnl=0.,lots=0,quantity=0))
            continue
        lot = 75 if scenario == 'source_baseline' and strategy in STRATEGIES[:4] else dated_lot(trade['date'], trade['expiry'])
        if lot is None:
            cash = None
            pending = []
            rows.append(dict(date=trade['date'], status='QUANTITY_UNRESOLVED', equity=None, pnl=None))
            continue
        ratios = [-1, -1, 1, 1] if strategy == STRATEGIES[3] else [1] * len(entries)
        debit = trade['maxLossPoints'] if strategy == STRATEGIES[3] else sum(entries)

        def charges(exits, q, entry_only=False):
            total = 0.
            for ratio, op, cl in zip(ratios, entries, exits):
                buy, sell = (op, cl) if ratio > 0 else (cl, op)
                if entry_only:
                    # Cash fee reservation applies to long-only source capital.
                    total += source_fees(trade['date'], op, 0, q, orders=1)['totalCharges']
                else:
                    total += source_fees(trade['date'], buy, sell, q)['totalCharges']
            return total
        entry_fee = (lambda q: charges([0.] * len(entries), q, True)) if net and strategy != STRATEGIES[3] else (lambda q: 0.)
        lots = size_lots(cash, debit, lot, entry_fee)
        q = lots * lot
        if not q:
            rows.append(dict(date=trade['date'], trade_id=trade.get('trade_id'), status='CAPITAL_NO_TRADE', equity=cash + sum(p['committed'] for p in pending), pnl=0., lots=0, quantity=0))
            continue
        if trade['status'] != 'RESOLVED':
            committed = debit * q + entry_fee(q)
            cash = None
            pending = []
            rows.append(dict(date=trade['date'], trade_id=trade.get('trade_id'), status='EXIT_DATA_UNRESOLVED', equity=None, pnl=None, lots=lots, quantity=q, committed=committed))
            continue
        # Original T3 capital overlay replicated its assumed-75 per-lot costs.
        # Preserve that named source model; the dated scenario charges the real
        # simulated order quantity once per leg instead.
        source_t3_cost = net and strategy == STRATEGIES[3] and scenario == 'source_baseline'
        fees = charges(trade['exitPrices'], 75) * lots if source_t3_cost else charges(trade['exitPrices'], q) if net else 0.
        pnl = trade['points'] * q - fees
        committed = debit * q + entry_fee(q)
        before = cash + sum(p['committed'] for p in pending)
        cash -= committed
        pending.append(dict(exit_time=stamp(trade['date'], trade['exitMinute']), committed=committed, release=committed + pnl))
        # Realized outcome path reported in event order below; this row is an
        # execution ledger, not an intraday MTM mark.
        rows.append(dict(date=trade['date'], trade_id=trade.get('trade_id'), status='RESOLVED', equity_before=before,
                         equity=None, pnl=pnl, gross_pnl=trade['points'] * q, fees=fees, lots=lots, quantity=q,
                         lot=lot, lot_authority='SOURCE_ASSUMED_75' if lot == 75 and scenario == 'source_baseline' and strategy in STRATEGIES[:4] else 'BOUNDED_SOURCE_DATED_SCHEDULE',
                         committed=committed, exposure_pct=100 * committed / before,
                         exit_time=stamp(trade['date'], trade['exitMinute']),
                         cost_model='SOURCE_ASSUMED75_PER_LOT_REPLICATION' if source_t3_cost else 'ACTUAL_SIMULATED_ORDER_QUANTITY' if net else 'GROSS_NO_COSTS',
                         broker_margin='NOT_COMPUTABLE' if strategy == STRATEGIES[3] else 'NOT_APPLICABLE'))
    settled = capital
    for row in sorted((r for r in rows if r['status'] == 'RESOLVED'), key=lambda r: r['exit_time']):
        settled += row['pnl']
        peak = max(peak, settled)
        drawdown = max(drawdown, peak - settled)
        dd_pct = max(dd_pct, (peak - settled) / peak * 100)
        row['equity'] = settled
    unresolved = any(r['status'] in ('EXIT_DATA_UNRESOLVED', 'EQUITY_UNRESOLVED', 'QUANTITY_UNRESOLVED') for r in rows)
    pnls = [r['pnl'] for r in rows if r['status'] == 'RESOLVED']
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    unsupported = net and not any('2026-03-01' <= t['date'] <= '2026-09-08' for t in trades)
    return rows, dict(status='NOT_COMPUTABLE' if unsupported else 'UNRESOLVED' if unresolved else 'COMPUTABLE', trade_count=len(pnls), wins=len(wins), losses=len(losses),
                      gross_rupees=None if unresolved or unsupported else sum(r.get('gross_pnl', 0) for r in rows),
                      net_rupees=(sum(pnls) if net and not unresolved and not unsupported else None),
                      final_equity=None if unresolved or unsupported else settled, prefix_pnl=sum(pnls),
                      profit_factor=sum(wins) / -sum(losses) if losses and not unresolved else None,
                      profit_factor_infinite=bool(wins and not losses and not unresolved),
                      expectancy=sum(pnls) / len(pnls) if pnls and not unresolved else None,
                      max_drawdown=None if unresolved or unsupported else drawdown, max_drawdown_pct=None if unresolved or unsupported else dd_pct,
                      known_resolved_subtotal=dict(pnl=sum(pnls),gross=sum(r.get('gross_pnl',0) for r in rows),max_drawdown=drawdown,max_drawdown_pct=dd_pct),
                      affordability_skips=sum(r['status'] == 'CAPITAL_NO_TRADE' for r in rows),
                      sizing='THEORETICAL_MAX_LOSS_CAPACITY_NOT_BROKER_MARGIN' if strategy == STRATEGIES[3] else 'AVAILABLE_CASH_WHOLE_LOTS',
                      executable_margin_account='NOT_COMPUTABLE' if strategy == STRATEGIES[3] else 'NOT_APPLICABLE',
                      available_cash=None if unresolved or unsupported or strategy == STRATEGIES[3] else cash + sum(p['release'] for p in pending))


def apply_summary(result, config, records, gross_metrics, net_metrics):
    """Derive unit and account summaries identically for fresh/reused executions.

    Affordability belongs to the account ledger; it never rewrites unit outcomes.
    Unresolved net exposure and unsupported full-period costs are separate facts.
    """
    unit_unresolved = any(r.get('entryPrices') and r['status'] != 'RESOLVED' for r in records)
    account_unresolved = any(m['status'] == 'UNRESOLVED' for m in (gross_metrics, net_metrics))
    covered = config.start >= '2026-03-01' and config.end <= '2026-09-09'
    full_net = None
    if net_metrics['status'] == 'UNRESOLVED':
        net_status = 'UNRESOLVED'
    elif not covered:
        net_status = 'NOT_COMPUTABLE_FOR_FULL_REQUESTED_PERIOD'
    elif config.strategy == STRATEGIES[3]:
        net_status = 'NOT_COMPUTABLE_BROKER_MARGIN'
    elif net_metrics['status'] != 'COMPUTABLE' or net_metrics['net_rupees'] is None:
        net_status = 'NOT_COMPUTABLE_ACCOUNT_EVIDENCE'
    else:
        full_net = net_metrics['net_rupees']
        net_status = 'COMPUTABLE'
    result['strategy_unit_status'] = 'UNRESOLVED' if unit_unresolved else 'BACKTEST_COMPLETED'
    result['status'] = 'UNRESOLVED' if unit_unresolved or account_unresolved else 'BACKTEST_COMPLETED'
    result['summary_revision'] = SUMMARY_REVISION
    result['summary_source_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result['metrics'].update(gross=gross_metrics, net_cost_supported_fresh_account=net_metrics,
        unit_trades=len(records), unit_resolved=sum(r['status'] == 'RESOLVED' for r in records),
        unit_gross_points=sum(r.get('points') or 0 for r in records), net_pnl=full_net,
        total_trades=gross_metrics['trade_count'],
        max_drawdown_pct=gross_metrics['max_drawdown_pct'] if gross_metrics['status'] == 'COMPUTABLE' else None,
        net_status=net_status,
        net_cost_coverage='SUPPORTED_FULL_REQUESTED_PERIOD' if covered else 'UNSUPPORTED_FOR_FULL_REQUESTED_PERIOD')
    return result


def run_observed(c, data):
    """Run one independent strategy/config over a read-only dated-contract adapter."""
    c.validate()
    if hasattr(data,'begin_run'):
        data.begin_run()
    records, eligibility, signal_diagnostics = [], [], []
    sessions = []
    years = range(pd.Timestamp(c.start).year, pd.Timestamp(c.end).year + 1)
    for year in years:
        if stamp(str(year) + '-01-01', 0) >= stamp(c.end, 0):
            continue
        sessions.extend(data.underlying(year))
    requested = [s for s in sessions if c.start <= s['date'] < c.end]
    if not requested:
        raise Unavailable('NO_RETAINED_UNDERLYING_IN_REQUESTED_RANGE')
    for date, row in getattr(data,'coverage',{}).items():
        if c.start <= date < c.end and row.get('status') == 'DATA_UNAVAILABLE':
            eligibility.append(dict(date=date,strategy=c.strategy,provider='GROWW_OPTIONS_UPSTOX_UNDERLYING',**row))
    for row in data.known_study_dates() if hasattr(data,'known_study_dates') else []:
        if c.start <= row['date'] < c.end and row['date'] not in {s['date'] for s in requested}:
            eligibility.append(dict(date=row['date'],strategy=c.strategy,provider='GROWW_OPTIONS_UPSTOX_UNDERLYING',
                status='DATA_UNAVAILABLE',reason='SOURCE_CALENDAR_UNCERTIFIED' if not row.get('calendarCoverage') else 'NO_RETAINED_CERTIFIED_UNDERLYING_PARTITION',
                originalDisposition=row.get('disposition')))
    if c.strategy == STRATEGIES[3] and hasattr(data, 'load_vix'):
        data.load_vix()
    by_date = {s['date']: s for s in sessions}
    signal_by_date = {}
    if c.strategy in STRATEGIES[:3]:
        # Underlying certification origin is the declared seed; execution is
        # filtered after initialization. No out-of-request trades.
        signals, signal_diagnostics = generate_signals(c.strategy, sessions)
        for signal in signals:
            if c.start <= signal['date'] < c.end:
                signal_by_date.setdefault(signal['date'], []).append(signal)
    for session in requested:
        date, spot = session['date'], session['bars']
        common = dict(date=date, strategy=c.strategy, provider='GROWW_OPTIONS_UPSTOX_UNDERLYING')
        try:
            if c.strategy == STRATEGIES[3]:
                if data.calendar[date] != set(range(555, 930)):
                    eligibility.append({**common, 'status': 'NON_REGULAR_SESSION'})
                    continue
                vix = getattr(data, 'vix', {}).get(date)
                if vix is None or not math.isfinite(vix) or vix <= 0:
                    eligibility.append({**common, 'status': 'NO_TRADE_VIX_DATA'})
                    continue
                if not 15 <= vix < 20:
                    eligibility.append({**common, 'status': 'NO_TRADE_VIX_BELOW_15' if vix < 15 else 'NO_TRADE_VIX_AT_OR_ABOVE_20'})
                    continue
            expiry = data.owning_expiry(date)
            attempts = signal_by_date.get(date, [])
            if c.strategy == STRATEGIES[4]:
                if 660 not in spot:
                    raise Unavailable('MISSING_EXACT_1100_SPOT_REFERENCE')
                attempts = [dict(minute=661, spot=spot[660]['close'], side='BOTH')]
            elif c.strategy == STRATEGIES[5]:
                scan = orb_signal(spot)
                if scan['status'] != 'SIGNAL':
                    eligibility.append({**common, **scan})
                    continue
                attempts = [dict(minute=scan['minute'] + 1, spot=scan['close'], side=scan['side'], scan=scan)]
            elif c.strategy == STRATEGIES[3]:
                if data.calendar[date] != set(range(555, 930)):
                    raise Unavailable('NON_REGULAR_SESSION')
                attempts = [dict(minute=570, spot=spot[569]['close'], side='IRON_FLY')]
            if not attempts:
                eligibility.append({**common, 'status': 'NO_SIGNAL_OR_SOURCE_WARMUP'})
            for signal in attempts:
                at = signal['minute']
                if c.strategy in STRATEGIES[:3]:
                    contract = data.select(date, expiry, signal['side'], signal['spot'], at, exact=False)
                    if contract is None:
                        eligibility.append({**common, 'status': 'NO_ELIGIBLE_CONTRACT', 'decisionMinute': at})
                        continue
                    outcome = phase_b_exit(at, data.option(date, contract.contract_id), c.strategy)
                    contracts = [contract]
                elif c.strategy in STRATEGIES[4:]:
                    sides = ('CE', 'PE') if c.strategy == STRATEGIES[4] else (signal['side'],)
                    contracts = [data.select(date, expiry, side, signal['spot'], at, exact=True) for side in sides]
                    if any(x is None for x in contracts):
                        eligibility.append({**common, 'status': 'SKIP_CATALOG_CONTRACT_MISSING', 'decisionMinute': at})
                        continue
                    legs = [data.option(date, x.contract_id) for x in contracts]
                    if c.strategy == STRATEGIES[4]:
                        if not all(660 in b and observed_usable(b[660]) for b in legs):
                            eligibility.append({**common, 'status': 'DATA_UNAVAILABLE', 'reason':'MISSING_EXACT_1100_OPTION_REFERENCE'})
                            continue
                        if not all(b[660]['close'] > 100 for b in legs):
                            eligibility.append({**common, 'status': 'SKIP_PREMIUM_FLOOR_SIGNAL_CLOSE'})
                            continue
                        feature = rise5m(661, data.calendar[date], *legs)
                        if not feature['qualifies']:
                            eligibility.append({**common, **feature, 'status': 'FILTER_REJECTED' if feature['status'] == 'AVAILABLE' else feature['status']})
                            continue
                    else:
                        reason = floor_gate(legs[0], at - 1)
                        if reason:
                            eligibility.append({**common, 'status': reason})
                            continue
                    outcome = close_then_open_exit(at, data.calendar[date], legs, kind=c.strategy)
                else:
                    # VIX data is injected from a frozen 09:29-only reference.
                    vix = getattr(data, 'vix', {}).get(date)
                    distance = None
                    if data.ownership is not None and data.ownership.get(expiry) == {expiry}:
                        distance = sum(date < d <= expiry for d in data.ownership)
                    if distance is None or distance < 1:
                        eligibility.append({**common, 'status':'NO_TRADE_EXPIRY_DISTANCE_UNKNOWN' if distance is None else 'NO_TRADE_EXPIRY_DAY'})
                        continue
                    atm_legs = [data.select(date, expiry, side, signal['spot'], 570, exact=True) for side in ('CE', 'PE')]
                    if any(x is None for x in atm_legs):
                        eligibility.append({**common, 'status': 'NO_TRADE_DATA_SHORT_LEG_CONTRACT'})
                        continue
                    ref_bars = [data.option(date, x.contract_id).get(569) for x in atm_legs]
                    refs = [b['close'] if observed_usable(b) else None for b in ref_bars]
                    structure = t3_structure(signal['spot'], vix, distance, *refs)
                    if structure['status'] != 'SELECTED':
                        eligibility.append({**common, **structure})
                        continue
                    contracts = atm_legs + [data.select(date, expiry, x['side'], x['strike'], 570, exact=True) for x in structure['legs'][2:]]
                    if any(x is None for x in contracts):
                        eligibility.append({**common, 'status': 'NO_TRADE_DATA_WING_CONTRACT'})
                        continue
                    outcome = t3_exit(structure['wingDistance'], [data.option(date, x.contract_id) for x in contracts])
                record = {**common, **outcome, 'expiry': expiry, 'contracts': [asdict(x) for x in contracts],
                          'signal': signal, 'trade_id': f'{c.strategy}_{date}_{at}_{len(records):06d}'}
                evidence = []
                ratios = [-1,-1,1,1] if c.strategy == STRATEGIES[3] else [1]*len(contracts)
                for i, (contract, ratio) in enumerate(zip(contracts, ratios)):
                    bars = data.option(date, contract.contract_id)
                    entry_minute, exit_minute = outcome.get('entryMinute'), outcome.get('exitMinute')
                    entry_bar, exit_bar = bars.get(entry_minute), bars.get(exit_minute)
                    entry_prices, exit_prices = outcome.get('entryPrices'), outcome.get('exitPrices')
                    evidence.append(dict(contract=asdict(contract), ratio=ratio,
                        entryCandle=entry_bar, exitCandle=exit_bar,
                        entryPrice=entry_prices[i] if entry_prices else None,
                        exitPrice=exit_prices[i] if exit_prices else None,
                        unitPnl=ratio*(exit_prices[i]-entry_prices[i]) if entry_prices and exit_prices else None,
                        exitPriceBasis=('LEGACY_CANDLE_BARRIER_ASSUMPTION' if c.strategy in STRATEGIES[:3] and outcome.get('trigger') in ('TARGET','TARGET_GAP','STOP_LOSS') else
                                        'OBSERVED_CLOSE_AT_DEADLINE' if c.strategy in STRATEGIES[:3] and outcome.get('trigger') in ('TIME_EXIT','EOD_EXIT') else 'OBSERVED_OPEN'),
                        independentMarketMatching='NOT VERIFIED'))
                record['legEvidence'] = evidence
                records.append(record)
                eligibility.append({**common, 'status': outcome['status'], 'trade_id': record['trade_id']})
        except (Unavailable, OSError, KeyError) as exc:
            eligibility.append({**common, 'status': 'DATA_UNAVAILABLE', 'reason': str(exc)})
    gross, gross_metrics = capital_ledger(records, c.strategy, c.trademind_scenario, capital=c.capital)
    net, net_metrics = capital_ledger(records, c.strategy, c.trademind_scenario, capital=c.capital, net=True)
    data.verify_preserved()
    inputs = sorted(getattr(data,'active_inputs',data.inputs).items())
    source_files = ('quant_trademind.py','trademind_signals.py','trademind_data.py','trademind_backtest.py','trademind_ticks.py')
    port_hashes = {name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in source_files}
    identity = json.dumps(dict(config=asdict(c), revision=REVISION, inputs=inputs, port_sources=port_hashes,
                               summary_revision=SUMMARY_REVISION, summary_source_sha256=port_hashes['trademind_backtest.py']), sort_keys=True)
    run_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
    unresolved = any(r.get('entryPrices') and r['status'] != 'RESOLVED' for r in records)
    result = dict(run_id=run_id, engine_version='5.5.0', pricing_revision=REVISION, config=asdict(c),
                  status='UNRESOLVED' if unresolved else 'BACKTEST_COMPLETED', strategy_revision=REVISION,
                  eligible_sessions=len({r['date'] for r in records}), excluded_sessions=sum(r['status'] == 'DATA_UNAVAILABLE' for r in eligibility),
                  eligibility=eligibility, metrics=dict(gross=gross_metrics, net_cost_supported_fresh_account=net_metrics,
                  unit_trades=len(records), unit_resolved=sum(r['status'] == 'RESOLVED' for r in records),
                  unit_gross_points=sum(r.get('points') or 0 for r in records)),
                  source_fingerprints=inputs, port_source_fingerprints=port_hashes, signal_diagnostics=signal_diagnostics,
                  signal_records=[s for values in signal_by_date.values() for s in values],
                  actual_coverage=dict(first=requested[0]['date'],last=requested[-1]['date'],underlying_sessions=len(requested),
                                       underlying_identity={y:v for y,v in getattr(data,'underlying_identity',{}).items()
                                                            if pd.Timestamp(c.start).year<=y and stamp(str(y)+'-01-01',0)<stamp(c.end,0)},
                                       requested_start=c.start,requested_end_exclusive=c.end,
                                       outside_certificate='Dates outside retained certificate coverage are unavailable; no exchange sessions are invented.'),
                  independent_market_matching='NOT VERIFIED', price_provenance='OBSERVED_DATED_CONTRACTS_ONLY',
                  limitations=['No quote/depth or live-fill certification.', 'Source fee schedule before 2026-03-01 unavailable; fresh supported-net account ends 2026-09-08.',
                               'Source baseline V2/V4/V8/T3 assumes lot75; allocation_100 uses bounded dated source schedule.',
                               'T3 loss-capacity sizing is theoretical; broker margin NOT_COMPUTABLE.',
                               'Strict contiguous management rejects holes that legacy Phase-B silently traversed.',
                               'Post-entry unresolved evidence freezes subsequent account equity; no silent trade deletion.'])
    apply_summary(result, c, records, gross_metrics, net_metrics)
    frames = dict(trades=pd.DataFrame(records), equity=pd.DataFrame(net), monthly=pd.DataFrame(),
                  stop_paths=pd.DataFrame(), excluded_sessions=pd.DataFrame(eligibility), skipped_entries=pd.DataFrame(),
                  daily_risk=pd.DataFrame(), underlying=pd.DataFrame(), gross_equity=pd.DataFrame(gross))
    # Keep nested values as JSON columns for portable Parquet, preserving raw leg evidence.
    for column in ('contracts', 'signal', 'legEvidence', 'entryPrices', 'exitPrices', 'rawExitPrices'):
        if column in frames['trades']:
            frames['trades'][column] = frames['trades'][column].map(lambda x: json.dumps(evidence_json_safe(x), allow_nan=False) if isinstance(x, (list, dict)) else None)
    result['_frames'] = frames
    result['_trade_records'] = records
    return result


def rescenario(result, config):
    """Reuse frozen unit executions for another named sizing scenario, never reselect/reprice."""
    import copy
    config.validate()
    if any(asdict(config)[k] != result['config'][k] for k in result['config'] if k != 'trademind_scenario'):
        raise ValueError('Only the sizing scenario may change in this reuse path')
    answer = copy.deepcopy(result)
    gross, gm = capital_ledger(result['_trade_records'],config.strategy,config.trademind_scenario,capital=config.capital)
    net, nm = capital_ledger(result['_trade_records'],config.strategy,config.trademind_scenario,capital=config.capital,net=True)
    answer['config'] = asdict(config)
    apply_summary(answer, config, result['_trade_records'], gm, nm)
    identity = json.dumps(dict(config=answer['config'],revision=REVISION,inputs=result['source_fingerprints'],port_sources=result['port_source_fingerprints'],
                               summary_revision=answer['summary_revision'],summary_source_sha256=answer['summary_source_sha256']),sort_keys=True)
    answer['run_id'] = hashlib.sha256(identity.encode()).hexdigest()[:16]
    answer['_frames']['equity'] = pd.DataFrame(net)
    answer['_frames']['gross_equity'] = pd.DataFrame(gross)
    answer['unit_execution_reused_from'] = result['run_id']
    return answer
