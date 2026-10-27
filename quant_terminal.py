"""Presentation models, explicit form wiring, and durable terminal run history."""
from dataclasses import asdict
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

import pandas as pd

from quant_backtester import Config, STRATEGIES, HIGH_STRATEGIES, POINT_VALUE, atomic_json
from quant_lake import QuantDataLake, IST
from step2_build_data_lake import writer_lock

PAGES = ['Overview', 'Data Lake', 'Data Quality', 'Strategy Builder', 'Backtests', 'Reports', 'Live Paper Trading']
INSTRUMENT_LABELS = {'NIFTY 50': 'NIFTY', 'SENSEX': 'SENSEX', 'BANKNIFTY': 'BANKNIFTY',
                     'CRUDEOIL': 'CRUDEOIL', 'NATURALGAS': 'NATURALGAS'}
STRATEGY_LABELS = {
    'Buy CE / Buy PE': (STRATEGIES[0], 'both'),
    'Momentum CE': (STRATEGIES[1], 'CE'),
    'Reversal PE': (STRATEGIES[3], 'PE'),
    'Straddle Breakout': (STRATEGIES[2], 'both'),
    'Expiry Gamma': (HIGH_STRATEGIES[4], 'both'),
    'MCX Breakout': (STRATEGIES[4], 'both'),
}
INTERVALS = {'1 minute': '1m', '3 minutes': '3m', '5 minutes': '5m', '15 minutes': '15m'}
EXITS = {'End of day': 'end_of_day', 'Trailing SL': 'trailing_sl', 'Target / EOD': 'target_eod'}
STRATEGY_NOTES = {
    'Buy CE / Buy PE': 'EMA / Supertrend trend signals select calls or puts.',
    'Momentum CE': 'Call-only opening-range breakout with option-volume confirmation.',
    'Reversal PE': 'Put-only bearish VWAP pullback with candle confirmation.',
    'Straddle Breakout': 'Fixed ATM call / put breakout; the first qualifying leg enters.',
    'Expiry Gamma': 'Afternoon momentum on calendar-inferred expiry sessions.',
    'MCX Breakout': 'Completed candles break the 17:00–17:30 commodity range.',
}


def build_config(instrument, strategy, period, capital, allocation, interval,
                 take_profit, stop_loss, minimum_premium, exit_label):
    """Every visible form setting is preserved in the engine's hashed config."""
    if len(period) != 2:
        raise ValueError('Choose both the start and end of the Period.')
    begin, end = (pd.Timestamp(v).date() for v in period)
    if end < begin:
        raise ValueError('The Period end must be on or after its start.')
    code = INSTRUMENT_LABELS[instrument]
    engine_strategy, side = STRATEGY_LABELS[strategy]
    mcx = code in POINT_VALUE
    if mcx != (strategy == 'MCX Breakout'):
        raise ValueError('Choose MCX Breakout for CRUDEOIL / NATURALGAS, or an index instrument for option strategies.')
    sl, tp = float(str(stop_loss).rstrip('%')), float(str(take_profit).rstrip('%'))
    config = Config(
        instrument=code, strategy=engine_strategy, option_side=side,
        start=begin.isoformat(), end=(end + timedelta(days=1)).isoformat(),
        capital=float(capital), capital_usage_pct=float(str(allocation).rstrip('%')),
        timeframe=INTERVALS[interval], sl_pct=sl, target_pct=tp,
        minimum_premium=float(minimum_premium), exit_mode=EXITS[exit_label],
        trail_pct=sl if exit_label == 'Trailing SL' else 0, risk_basis='percentage',
        sizing_mode='allocation', slippage=1.0 if mcx else .5,
        entry_start='17:30' if mcx else '13:00' if strategy == 'Expiry Gamma' else '09:30',
        entry_end='21:30' if mcx else '14:30', price_policy='strict_observed' if mcx else 'model_continuity',
    )
    config.validate()
    return config


def strategy_label(config):
    for label, (strategy, side) in STRATEGY_LABELS.items():
        if config.get('strategy') == strategy and config.get('option_side', 'both') == side:
            return label
    return {'Option_VWAP_OI_Momentum': 'Option VWAP momentum',
            'Dual_Leg_Straddle_Gamma_Breakout': 'Straddle gamma',
            'Morning_ORB_Retest_Sniper': 'Morning ORB retest',
            'MCX_US_Open_Inventory_Breakout': 'MCX US-open breakout',
            'Natural_Gas_Crude_High_Beta_Momentum': 'MCX momentum'}.get(
                config.get('strategy'), config.get('strategy', 'Unknown strategy').replace('_', ' '))


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'{Path(path).name} could not be read. Restore or repair this file before continuing.') from exc


def lake_snapshot(root):
    """Use the lake catalog and committed chunks, never decorative example figures."""
    root = Path(root)
    with QuantDataLake(root) as lake:
        catalog = lake.catalog()
        chunks = list(lake.manifest['chunks'].values())
        complete = [c for c in chunks if c['status'] == 'complete' and c.get('rows', 0)]
        markets = {}
        for name in ('NIFTY', 'BANKNIFTY', 'SENSEX'):
            records = [c for c in complete if c['dataset'] == 'spot_1m' and c['instrument'] == name]
            if not records:
                markets[name] = None
                continue
            last = max(pd.Timestamp(c.get('last', c['end'])) for c in records)
            frame = lake.query(name, str((last - pd.Timedelta(days=12)).date()),
                               str((last + pd.Timedelta(days=1)).date()))
            if frame.empty:
                markets[name] = None
                continue
            closes = frame.groupby(frame.timestamp.dt.date).close.last()
            latest = float(closes.iloc[-1])
            prior = float(closes.iloc[-2]) if len(closes) > 1 else None
            markets[name] = dict(price=latest, change=latest-prior if prior else None,
                                 change_pct=100*(latest/prior-1) if prior else None,
                                 asof=frame.timestamp.iloc[-1].isoformat())
    quality_path = root / 'quality_report.json'
    quality = read_json(quality_path) if quality_path.exists() else {}
    coverage = []
    groups = {
        'Index spot': lambda c: c['dataset'] == 'spot_1m' and c['instrument'] in ('NIFTY', 'BANKNIFTY', 'SENSEX'),
        'Expired options': lambda c: c['dataset'].startswith('options_'),
        'MCX commodities': lambda c: c['dataset'] == 'spot_1m' and any(c['instrument'].startswith(i+'_') for i in POINT_VALUE),
    }
    for label, predicate in groups.items():
        records = [c for c in complete if predicate(c)]
        requested = [c for c in chunks if predicate(c)]
        first = min((c.get('first', c['start'])[:10] for c in records), default=None)
        last = max((c.get('last', c['end'])[:10] for c in records), default=None)
        gaps = sum(c.get('quality', {}).get('missing_internal_minutes', 0) for c in records)
        rejected = any(c.get('quality', {}).get('conflicting_duplicate_rows', 0) or
                       c.get('quality', {}).get('invalid_ohlcv', 0) for c in records)
        partial = any(c['status'] in ('failed', 'unavailable') for c in requested)
        span = 'Not ingested'
        if first:
            span = f'{first[:4]} – {last[:4]}' if first[:4] != last[:4] else f'{pd.Timestamp(first):%b} – {pd.Timestamp(last):%b %Y}'
        coverage.append(dict(dataset=label, interval='1 minute', first=first, last=last,
                             coverage=span,
                             status='Not available' if not records else 'Review gaps' if gaps or rejected or partial else 'Ready',
                             rows=sum(c['rows'] for c in records), gaps=gaps))
    rolling = any(c['dataset'] == 'options_rolling_1m' for c in complete)
    return dict(markets=markets, coverage=coverage, quality=quality, catalog=catalog,
                rows=sum(c['rows'] for c in complete), partitions=len(complete),
                expiry_mapping='Unverified' if rolling else 'Not audited',
                available=bool(complete))


def _entry(result, display_id, created_at):
    return dict(display_id=display_id, run_id=result['run_id'], created_at=created_at,
                config=result['config'], metrics=result['metrics'],
                strategy_label=strategy_label(result['config']), engine_version=result['engine_version'],
                price_status=result['status'], status='Complete')


def load_registry(output):
    """Old results remain inspectable after engine upgrades, with original configs."""
    output = Path(output)
    path = output / 'terminal' / 'registry.json'
    if path.exists():
        data = read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get('runs'), list) or not isinstance(data.get('strategies'), list):
            raise ValueError('Run history is invalid. Restore registry.json before saving.')
        return data
    runs = []
    paths = sorted((output / 'runs').glob('*/result.json'), key=lambda p: (p.stat().st_mtime_ns, p.parent.name))
    for path in paths:
        try:
            result = read_json(path)
            if not all((path.parent / f'{name}.parquet').exists() for name in ('trades', 'equity')):
                continue
            runs.append(_entry(result, f'BT-{len(runs)+1:03d}',
                               pd.Timestamp.fromtimestamp(path.stat().st_mtime, tz=IST).isoformat()))
        except (ValueError, KeyError):
            continue
    return dict(version=1, runs=runs, strategies=[])


def register_run(output, result, label):
    output = Path(output)
    directory = output / 'terminal'
    directory.mkdir(parents=True, exist_ok=True)
    with writer_lock(directory):
        registry = load_registry(output)
        entry = next((r for r in registry['runs'] if r['run_id'] == result['run_id']), None)
        if entry is None:
            next_id = max((int(r['display_id'].split('-')[1]) for r in registry['runs']), default=0) + 1
            entry = _entry(result, f'BT-{next_id:03d}', pd.Timestamp.now(tz=IST).isoformat())
            registry['runs'].append(entry)
        entry['strategy_label'] = label
        entry['last_opened'] = pd.Timestamp.now(tz=IST).isoformat()
        for strategy in registry['strategies']:
            if strategy['config'] == result['config']:
                strategy['last_run'] = entry['display_id']
        atomic_json(directory / 'registry.json', registry)
    return entry


def save_strategy(output, config, label):
    output = Path(output)
    directory = output / 'terminal'
    directory.mkdir(parents=True, exist_ok=True)
    values = asdict(config)
    identifier = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:12]
    with writer_lock(directory):
        registry = load_registry(output)
        matching = next((r for r in reversed(registry['runs']) if r['config'] == values), None)
        record = dict(strategy_id=identifier, name=label, config=values,
                      saved_at=pd.Timestamp.now(tz=IST).isoformat(),
                      last_run=matching['display_id'] if matching else None)
        registry['strategies'] = [s for s in registry['strategies'] if s['strategy_id'] != identifier] + [record]
        atomic_json(directory / 'registry.json', registry)
    return record


def form_values(config, label):
    """Restore a saved terminal configuration without silently changing defaults."""
    return dict(instrument=next(k for k, v in INSTRUMENT_LABELS.items() if v == config['instrument']),
                strategy=label, period=(date.fromisoformat(config['start']), date.fromisoformat(config['end'])-timedelta(days=1)),
                capital=int(config['capital']), allocation=f"{config['capital_usage_pct']:g}%",
                interval=next(k for k, v in INTERVALS.items() if v == config['timeframe']),
                take_profit=f"{config['target_pct']:g}%", stop_loss=f"{config['sl_pct']:g}%",
                minimum_premium=float(config['minimum_premium']),
                exit_label=next(k for k, v in EXITS.items() if v == config['exit_mode']))
