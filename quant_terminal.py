"""Presentation models, explicit form wiring, and durable terminal run history."""
from dataclasses import asdict
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

import pandas as pd

from quant_backtester import Config, RESEARCH_STRATEGIES, STRATEGIES, BOOK_STRATEGIES, POINT_VALUE, atomic_json, persist_result
from quant_lake import QuantDataLake, IST
from step2_build_data_lake import writer_lock

PAGES = ['Overview', 'Data Lake', 'Data Quality', 'Backtests', 'Reports', 'Live Paper Trading']
INSTRUMENT_LABELS = {'NIFTY 50': 'NIFTY', 'SENSEX': 'SENSEX', 'BANKNIFTY': 'BANKNIFTY',
                     'CRUDEOIL': 'CRUDEOIL', 'NATURALGAS': 'NATURALGAS', 'GOLDM': 'GOLDM'}
STRATEGY_LABELS = {
    'VWAP + EMA(9/21) Pullback': (RESEARCH_STRATEGIES[0], 'both'),
    'Opening Range Breakout + Volume Surge': (RESEARCH_STRATEGIES[1], 'both'),
    'Supertrend (10, 3) + ADX Trend Follower': (RESEARCH_STRATEGIES[2], 'both'),
    'MCX Evening Momentum & Donchian Breakout': (RESEARCH_STRATEGIES[3], 'both'),
    'Vertical Bull/Bear Debit Spread (Sec 2.6 & 2.9)': (BOOK_STRATEGIES[0], 'both'),
    'Iron Butterfly Income Spread (Sec 2.44)': (BOOK_STRATEGIES[1], 'both'),
    'Strap / Strip Volatility Breakout (Sec 2.34 & 2.35)': (BOOK_STRATEGIES[2], 'both'),
    'Vol-Scaled Momentum + Volume/OI Activity Filter (Sec 10.3.1 & 10.4)': (BOOK_STRATEGIES[3], 'both'),
    'Expiry Day': ('expiry_day','both'),
}
INTERVALS = {'1 minute': '1m', '3 minutes': '3m', '5 minutes': '5m', '15 minutes': '15m'}
EXITS = {'End of day': 'end_of_day', 'Trailing SL': 'trailing_sl', 'Target / EOD': 'target_eod'}
EXPIRY_EXITS = {'Target + SL':'target_eod','Trailing SL':'trailing_sl','EOD':'end_of_day'}
STRATEGY_NOTES = {
    list(STRATEGY_LABELS)[0]: 'EMA9 pullback, EMA9/21 alignment, session VWAP and ADX ≥18; next-open execution. Zero-volume spot uses a disclosed TWAP proxy.',
    list(STRATEGY_LABELS)[1]: 'Index 09:15–09:30 / MCX 17:00–17:30 range, RSI and >1.3× prior 10-bar volume. Zero-volume indices require complete observed fixed-strike option volume. Maximum two entries per session.',
    list(STRATEGY_LABELS)[2]: 'Wilder ATR(10) Supertrend ×3 and ADX >22; confirmed flip or continuation. Supports Trailing SL.',
    list(STRATEGY_LABELS)[3]: 'NATURALGAS / CRUDEOIL only. 17:00–22:45; prior 12-bar Donchian channel, EMA50 and body >1.2× ATR14. Morning warm-up retained.',
    list(STRATEGY_LABELS)[4]: 'ATM debit vertical on VWAP/EMA alignment and ADX(14) ≥18. MCX uses directional futures with ATR-capped risk. Per-leg fees and adverse fills.',
    list(STRATEGY_LABELS)[5]: 'Index options only: sell ATM CE/PE, buy equidistant wings after 09:45, ADX <22 and within 0.25% of VWAP. Credit decay/expansion exits and 15:15 EOD; reserve max loss plus fees.',
    list(STRATEGY_LABELS)[6]: 'Index options only: 2CE:1PE strap or 1CE:2PE strip on ORB/Donchian break with fixed-contract volume expansion. Combined portfolio exits.',
    list(STRATEGY_LABELS)[7]: 'Mean tanh(return / lagged volatility), 10-bar lookback, threshold 0.25; log volume expansion and positive log OI activity where available. Missing OI is disclosed.',
    'Expiry Day': 'NIFTY / SENSEX only. Balanced fixed CE+PE basket, pre-expiry 15:25 signal / 15:26 fill, held overnight. Only combined TP/SL/trailing exits on expiry; no individual leg stops. WEEK1 expiry identity remains unverified.',
}

SAVED_STRATEGIES = Path(__file__).resolve().parent/'data'/'saved_strategies.json'


def instrument_defaults(instrument):
    """Conservative price-risk defaults, not fitted claims of profitability."""
    # Full-notional futures funding at 25% allocation must afford at least one
    # exchange lot plus costs; options and futures have very different units.
    return dict(capital={'NATURALGAS':2_000_000,'CRUDEOIL':4_000_000,'GOLDM':8_000_000}.get(instrument,100_000),
                allocation='25%',
                take_profit='5%' if instrument in POINT_VALUE else '25%',
                stop_loss='5%' if instrument in POINT_VALUE else '15%',
                minimum_premium=0. if instrument in POINT_VALUE else 50.)


def build_config(instrument, strategy, period, capital, allocation, interval,
                 take_profit, stop_loss, minimum_premium, exit_label, **expiry_settings):
    """Every visible form setting is preserved in the engine's hashed config."""
    if len(period) != 2:
        raise ValueError('Choose both the start and end of the Period.')
    begin, end = (pd.Timestamp(v).date() for v in period)
    if end < begin:
        raise ValueError('The Period end must be on or after its start.')
    code = INSTRUMENT_LABELS[instrument]
    engine_strategy, side = STRATEGY_LABELS[strategy]
    mcx = code in POINT_VALUE
    sl, tp = float(str(stop_loss).rstrip('%')), float(str(take_profit).rstrip('%'))
    config = Config(
        instrument=code, strategy=engine_strategy, option_side=side,
        start=begin.isoformat(), end=(end + timedelta(days=1)).isoformat(),
        capital=float(capital), capital_usage_pct=float(str(allocation).rstrip('%')),
        timeframe=INTERVALS[interval], sl_pct=sl, target_pct=tp,
        minimum_premium=float(minimum_premium), exit_mode={**EXITS,**EXPIRY_EXITS}[exit_label],
        trail_pct=sl if exit_label == 'Trailing SL' else 0, risk_basis='legacy' if mcx and engine_strategy in BOOK_STRATEGIES else 'percentage',
        sizing_mode='allocation', slippage=.5 if code=='NATURALGAS' or not mcx else 1.0,
        entry_start='17:00' if mcx and engine_strategy!='vwap_ema_pullback' else '09:25', volume_multiple=1.3,
        entry_end='22:45' if mcx and engine_strategy!='vwap_ema_pullback' else '14:30', price_policy='strict_observed' if mcx else 'model_continuity',
        **expiry_settings,
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
    """Shared cards use the same evidence-bound statuses as the data pages."""
    from quant_data_quality import inventory, report_state, group_status
    root = Path(root)
    inv = inventory(root)
    state = report_state(root, inv)
    markets = dict.fromkeys(('NIFTY', 'BANKNIFTY', 'SENSEX'))
    for name in markets:
        stream = inv['streams'].get('spot_1m|'+name)
        if not stream or not stream['last']:
            continue
        try:
            last = pd.Timestamp(stream['last'])
            with QuantDataLake(root) as lake:
                frame = lake.query(name, str((last-pd.Timedelta(days=12)).date()),
                                   str((last+pd.Timedelta(days=1)).date()))
            if frame.empty: continue
            closes = frame.groupby(frame.timestamp.dt.date).close.last()
            latest = float(closes.iloc[-1])
            prior = float(closes.iloc[-2]) if len(closes)>1 else None
            markets[name] = dict(price=latest, change=latest-prior if prior else None,
                change_pct=100*(latest/prior-1) if prior else None, asof=frame.timestamp.iloc[-1].isoformat())
        except Exception:
            # Data pages expose the exact file/manifest findings; a quote card must not crash navigation.
            continue
    coverage=[]
    for label, segment in [('Index spot','Index spot'), ('Expired options','Rolling options'), ('MCX commodities','Commodity futures')]:
        groups=[g for g in inv['groups'] if g['segment']==segment and g['interval']=='1 minute']
        starts=[g['first'] for g in groups if g['first']]
        ends=[g['last'] for g in groups if g['last']]
        first=min(starts,key=pd.Timestamp)[:10] if starts else None
        last=max(ends,key=pd.Timestamp)[:10] if ends else None
        statuses=[group_status(g,inv,state) for g in groups]
        status=next((s for s in ('Review needed','Audit outdated','Not checked','No data') if s in statuses), 'Checks passed' if statuses else 'No data')
        coverage.append(dict(dataset=label,interval='1 minute',first=first,last=last,
            coverage=(first+' / '+last) if first else 'Not ingested',status=status,
            rows=sum(g['rows'] for g in groups),gaps=None))
    catalog=[]
    for stream in inv['streams'].values():
        row=dict(dataset=stream['dataset'],instrument=stream['instrument'],rows=stream['rows'])
        row.update({s+'_chunks':sum(r['status']==s for r in stream['records']) for s in ('complete','empty','failed','unavailable')})
        catalog.append(row)
    return dict(markets=markets,coverage=coverage,quality=state['report'] or {},quality_state=state,
                catalog=catalog,rows=sum(g['rows'] for g in inv['groups']),
                partitions=sum(g['partitions'] for g in inv['groups']),
                expiry_mapping='Unverified' if any(s['dataset'].startswith('options_') for s in inv['streams'].values()) else 'Not applicable',
                available=any(g['rows'] for g in inv['groups']))


def _entry(result, display_id, created_at):
    return dict(display_id=display_id, run_id=result['run_id'], created_at=created_at,
                config=result['config'], metrics=result['metrics'],
                strategy_label=strategy_label(result['config']), engine_version=result['engine_version'],
                price_status=result['status'], status='Complete')


def load_registry(output):
    """Only explicitly saved results enter Reports. Never import result folders.

    Version-one auto-save registries are ignored without deleting their files.
    """
    output = Path(output)
    path = output / 'terminal' / 'registry.json'
    if path.exists():
        data = read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get('runs'), list) or not isinstance(data.get('strategies'), list):
            raise ValueError('Run history is invalid. Restore registry.json before saving.')
        if data.get('version') == 2:
            data['runs'] = [r for r in data['runs'] if r.get('explicit_save')]
            return data
    return dict(version=2, runs=[], strategies=[])


def register_run(output, result, label, note=''):
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
        entry['explicit_save'] = True
        entry['note'] = note.strip()
        entry['last_opened'] = pd.Timestamp.now(tz=IST).isoformat()
        for strategy in registry['strategies']:
            if strategy['config'] == result['config']:
                strategy['last_run'] = entry['display_id']
        atomic_json(directory / 'registry.json', registry)
    return entry


def save_result(output, result, note=''):
    load_registry(output)  # Validate history before writing any result artifact.
    document = persist_result(result, output)
    return register_run(output, document, strategy_label(document['config']), note)


def saved_strategy_path(output=None):
    # Isolate temporary/custom workspaces while using the requested repo path
    # for the real terminal and CLI.
    default_output = Path(__file__).resolve().parent/'data_lake'/'backtests'
    return SAVED_STRATEGIES if output is None or Path(output).resolve() == default_output else Path(output)/'terminal'/'saved_strategies.json'


def load_saved_strategies(path=None):
    path = Path(path) if path is not None else SAVED_STRATEGIES
    if not path.exists(): return []
    data = read_json(path)
    if not isinstance(data, list): raise ValueError('saved_strategies.json must contain a list')
    seen = set()
    for record in data:
        if not isinstance(record,dict) or not isinstance(record.get('name'),str) or not record['name'].strip() or not record.get('strategy_id') or not isinstance(record.get('config'),dict):
            raise ValueError('Invalid saved strategy record')
        if record['strategy_id'] in seen: raise ValueError('Duplicate saved strategy ID')
        seen.add(record['strategy_id'])
        try:
            config = Config(**record['config'])
        except TypeError as exc:
            raise ValueError('Invalid parameters in saved_strategies.json') from exc
        config.validate()
        if config.strategy not in STRATEGIES:
            raise ValueError('Saved strategies must use a supported terminal strategy')
    return data


def save_strategy(output, config, label):
    name = label.strip()
    if not name or name in STRATEGY_LABELS:
        raise ValueError('Enter a custom Strategy Name before saving.')
    config.validate()
    if config.strategy not in STRATEGIES:
        raise ValueError('Save a supported terminal strategy')
    path = saved_strategy_path(output)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    values = asdict(config)
    identifier = hashlib.sha256(name.casefold().encode()).hexdigest()[:12]
    with writer_lock(directory):
        records = load_saved_strategies(path)
        record = dict(strategy_id=identifier, name=name, config=values,
                      instrument=config.instrument, strategy_key=config.strategy,
                      candle_interval=config.timeframe, capital=config.capital,
                      capital_allocation_pct=config.capital_usage_pct,
                      take_profit_pct=config.target_pct, stop_loss_pct=config.sl_pct,
                      minimum_premium=config.minimum_premium, exit_mode=config.exit_mode,
                      saved_at=pd.Timestamp.now(tz=IST).isoformat(),
                      last_run=None)
        records = [s for s in records if s['strategy_id'] != identifier] + [record]
        atomic_json(path, records)
    return record


def form_values(config, label):
    """Restore a saved terminal configuration without silently changing defaults."""
    return dict(instrument=next(k for k, v in INSTRUMENT_LABELS.items() if v == config['instrument']),
                strategy=strategy_label(config), period=(date.fromisoformat(config['start']), date.fromisoformat(config['end'])-timedelta(days=1)),
                capital=int(config['capital']), allocation=f"{config['capital_usage_pct']:g}%",
                interval=next(k for k, v in INTERVALS.items() if v == config['timeframe']),
                take_profit=f"{config['target_pct']:g}%", stop_loss=f"{config['sl_pct']:g}%",
                minimum_premium=float(config['minimum_premium']),
                exit_label=next(k for k, v in (EXPIRY_EXITS if config['strategy']=='expiry_day' else EXITS).items() if v == config['exit_mode']),
                **{k:v for k,v in config.items() if k.startswith('expiry_')})
