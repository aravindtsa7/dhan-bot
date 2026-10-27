"""Observed-signal options/futures research with explicit price provenance.

The entered strike never changes. Missing execution candles either receive a
tagged continuity estimate or fail the whole strict-observed run. Synthetic
prices and calendar-inferred expiry identities never become verified quotes.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib
import json
import math
import os
import time

import numpy as np
import pandas as pd

from quant_lake import QuantDataLake, ROOT, IST
from quant_calendar import CALENDAR_HASH, expiries, next_expiry, is_expiry_day, regular_session
from quant_option_model import continuation_candle, MissingObservedPrice, intrinsic
from quant_high_edge import HIGH_STRATEGIES, high_edge_events
from quant_run_data import required_windows, verify_files, prior_volume, select_contracts, stream_check, rejected_evidence

ENGINE_VERSION = '5.3.0'
OUT = ROOT / 'backtests'
STRATEGIES = ['EMA_Supertrend_VWAP_Directional', 'ORB_Option_Momentum_Breakout',
              'Dual_CE_PE_Straddle_Breakout', 'VWAP_Trend_Pullback_Buyer',
              'MCX_Evening_Session_Breakout']
STRATEGIES += HIGH_STRATEGIES
INSTRUMENTS = ['NIFTY', 'SENSEX', 'CRUDEOIL', 'NATURALGAS', 'GOLDM']
POINT_VALUE = {'CRUDEOIL': 100, 'NATURALGAS': 1250, 'GOLDM': 10}
LIMITATIONS = [
    'PROVISIONAL: rolling WEEK1 options have no expiry/contract ID. Matching strike is necessary but cannot prove contract identity.',
    'Missing option candles may use explicitly tagged delta/gamma continuity estimates. Zero unresolved positions does not verify modeled fills.',
    'H1 uses observed fixed-strike option VWAP; zero-volume spot has a separate TWAP proxy. No modeled data is used in signals.',
    'MCX is a front-active-contract sample, not continuous history since 2025; daily contract choice uses prior-session volume.',
    'Expiry flags use dated exchange rules and holidays; EOD IV/extrinsic checks are retrospective diagnostics, never signal inputs or proof of expiry identity.',
    'Fees and lot schedules are configurable research assumptions; historical contract notes are the authority.',
    'Full-window grid ranking is in-sample. 2025/2026 splits are descriptive, not untouched out-of-sample validation.',
    'Capital allocation reserves premium or assumed futures margin plus estimated round-trip fees. Futures margin is user supplied, not a historical broker margin schedule.',
    'Daily limits check net realized plus open liquidation P&L at minute close and exit next open. Gaps can exceed limits. Allocation percentage limits committed capital, not maximum possible loss.',
]


@dataclass(frozen=True)
class Config:
    instrument: str = 'NIFTY'
    strategy: str = HIGH_STRATEGIES[0]
    timeframe: str = '5m'
    strike: str = 'ATM'
    start: str = '2025-01-01'
    end: str = '2026-10-23'  # exclusive
    sl_pct: float = 25
    target_pct: float = 50
    trail_pct: float = 20
    trail_jump_pct: float = 5
    breakout_pct: float = 15
    orb_minutes: int = 15
    anchor: str = '09:30'
    mode: str = 'A'
    entry_start: str = '09:30'
    entry_end: str = '14:30'
    lots: int = 1
    lot_size: int = 0  # 0 = dated schedule / commodity point value
    capital: float = 1_000_000
    capital_usage_pct: float = 100
    sizing_mode: str = 'fixed_lots'  # allocation fills the budget with whole lots
    max_lots: int = 100
    compound: bool = False
    futures_margin_pct: float = 100  # explicit research assumption, not broker margin
    strategy_mode: str = 'steady'
    daily_profit_target: float = 0  # zero disables the respective limit
    daily_loss_limit: float = 0
    daily_profit_pct: float = 0  # percentage of start-of-session account equity
    daily_loss_pct: float = 0
    slippage: float = 0.5
    atr_sl: float = 1.5
    atr_trail_jump: float = 0.25  # fraction of initial ATR stop distance, MCX only
    reward_risk: float = 2
    adx_min: float = 20
    max_trades_day: int = 2
    brokerage: float = 20
    nse_exchange_rate: float = 0.0003503
    bse_exchange_rate: float = 0.000325
    mcx_exchange_rate: float = 0.000021
    sebi_rate: float = 0.000001
    nse_ipft_rate: float = 0.000005
    gst_rate: float = 0.18
    price_policy: str = 'model_continuity'  # strict_observed fails instead of inventing a fill
    option_lookback: int = 3
    volume_multiple: float = 1.5
    oi_mode: str = 'either'
    oi_min_change_pct: float = 0.5
    atr_expansion: float = 1.0
    compression_ratio: float = 0.8
    breakeven_trigger_pct: float = 15
    tick_size: float = 0  # 0 selects option 0.05 / commodity contract tick
    minimum_premium: float = 0  # observed entry premium, before slippage; options only
    option_side: str = 'both'  # both / CE / PE
    exit_mode: str = 'legacy'  # legacy / end_of_day / trailing_sl / target_eod
    risk_basis: str = 'legacy'  # legacy ATR futures stops, or percentage of entry

    def validate(self):
        if self.instrument == 'BANKNIFTY':
            raise ValueError('BANKNIFTY spot is available, but option history and a dated contract schedule are not configured. Choose NIFTY 50 or SENSEX for option backtests.')
        if self.instrument not in INSTRUMENTS or self.strategy not in STRATEGIES:
            raise ValueError('Unknown instrument or strategy')
        if self.timeframe not in ('1m', '3m', '5m', '15m') or self.strike not in ('ATM', 'ATM-1', 'ATM+1'):
            raise ValueError('Unsupported timeframe / moneyness')
        if (self.instrument in POINT_VALUE) != (self.strategy in (STRATEGIES[4],HIGH_STRATEGIES[3],HIGH_STRATEGIES[5])):
            raise ValueError('S5/H4/H6 support MCX; other strategies support index options')
        if self.strategy==HIGH_STRATEGIES[5] and self.instrument not in ('CRUDEOIL','NATURALGAS'):
            raise ValueError('H6 supports only Crude Oil and Natural Gas')
        if self.strategy==HIGH_STRATEGIES[4] and self.exit_mode=='legacy' and self.trail_pct<=0:
            raise ValueError('H5 requires an active trailing stop')
        if any(isinstance(v,(int,float)) and not math.isfinite(v) for v in asdict(self).values()):
            raise ValueError('Numeric settings must be finite')
        if not 0<self.capital_usage_pct<=100 or not 0<self.futures_margin_pct<=100:
            raise ValueError('Capital usage and futures margin must be in (0,100]')
        if self.sizing_mode not in ('fixed_lots','allocation') or self.strategy_mode not in ('steady','growth'):
            raise ValueError('Invalid sizing or strategy mode')
        if self.max_lots<1 or int(self.max_lots)!=self.max_lots or int(self.lots)!=self.lots or int(self.lot_size)!=self.lot_size:
            raise ValueError('Lots and multipliers must be whole numbers')
        if min(self.daily_profit_target,self.daily_loss_limit,self.daily_profit_pct,self.daily_loss_pct)<0:
            raise ValueError('Daily limits cannot be negative')
        if self.strategy in (STRATEGIES[2],HIGH_STRATEGIES[1]) and self.strike != 'ATM':
            raise ValueError('Dual-leg strategy uses ATM anchor strikes')
        if self.mode not in ('A', 'B') or self.lots < 1 or self.lot_size < 0:
            raise ValueError('Invalid mode or quantity')
        if self.strategy==STRATEGIES[2] and self.mode=='B' and self.max_trades_day!=2:
            raise ValueError('Simultaneous straddle requires a two-leg daily entry cap')
        if not 0 < self.sl_pct < 100 or self.target_pct <= 0 or self.trail_pct < 0 or self.trail_jump_pct <= 0:
            raise ValueError('Invalid stop, target or trail')
        if self.capital <= 0 or self.slippage < 0 or self.atr_sl <= 0 or self.atr_trail_jump <= 0 or self.reward_risk <= 0:
            raise ValueError('Invalid capital, slippage or ATR parameters')
        if pd.Timestamp(self.start) >= pd.Timestamp(self.end):
            raise ValueError('end is exclusive and must follow start')
        if self.max_trades_day not in (1,2) or self.orb_minutes not in (15, 30):
            raise ValueError('Invalid trade cap or opening range')
        if self.price_policy not in ('strict_observed','model_continuity') or self.oi_mode not in ('rising','unwinding','either'):
            raise ValueError('Invalid pricing or OI policy')
        if self.volume_multiple<1.5 or self.option_lookback<1 or self.oi_min_change_pct<=0 or self.atr_expansion<=0 or self.compression_ratio<=0:
            raise ValueError('Invalid volume/OI/ATR filters')
        if self.tick_size<0: raise ValueError('Tick size cannot be negative')
        if self.minimum_premium < 0 or self.option_side not in ('both', 'CE', 'PE'):
            raise ValueError('Invalid minimum premium or option side')
        if self.exit_mode not in ('legacy', 'end_of_day', 'trailing_sl', 'target_eod') or self.risk_basis not in ('legacy', 'percentage'):
            raise ValueError('Invalid exit mode or risk basis')
        if self.exit_mode == 'trailing_sl' and self.trail_pct <= 0:
            raise ValueError('Trailing SL requires a positive trailing distance')
        if self.option_side != 'both' and (self.instrument in POINT_VALUE or self.strategy in (STRATEGIES[2], HIGH_STRATEGIES[1])):
            raise ValueError('Futures and dual-leg strategies require both sides')
        for value in (self.entry_start, self.entry_end, self.anchor):
            minute(value)
        if minute(self.entry_start)>=minute(self.entry_end):
            raise ValueError('Entry window start must precede end')
        if minute(self.entry_end)>=(1395 if self.instrument in POINT_VALUE else 920):
            raise ValueError('Last entry must precede mandatory square-off')
        if not 555 < minute(self.anchor) < 920:
            raise ValueError('Option anchor must be inside the regular session')
        if min(self.brokerage,self.nse_exchange_rate,self.bse_exchange_rate,self.mcx_exchange_rate,self.sebi_rate,self.nse_ipft_rate,self.gst_rate)<0:
            raise ValueError('Fees cannot be negative')


def minute(value):
    h, m = map(int, value.split(':'))
    if h not in range(24) or m not in range(60):
        raise ValueError('Time must be HH:MM')
    return h * 60 + m


def quantity(c, date):
    if c.lot_size:
        return c.lot_size * c.lots
    unit = POINT_VALUE.get(c.instrument)
    if unit is None:
        expiry=next_expiry(c.instrument,str(date))
        unit = (25 if expiry=='2025-01-30' else 75 if expiry<='2025-12-30' else 65) if c.instrument == 'NIFTY' else (10 if expiry in ('2025-01-03','2025-01-28') else 20)
    return unit * c.lots


def adverse_tick(price,c,buy):
    tick=c.tick_size or (0.1 if c.instrument=='NATURALGAS' else 1.0 if c.instrument in POINT_VALUE else 0.05)
    rounded=(math.ceil(price/tick-1e-9) if buy else math.floor(price/tick+1e-9))*tick
    return round(max(tick,rounded),8)


def fees(c, entry, exit_price, qty, date, direction=1):
    """Per completed leg, two executed orders. Futures use CTT, not STT."""
    buy = (entry if direction == 1 else exit_price) * qty
    sell = (exit_price if direction == 1 else entry) * qty
    turnover = buy + sell
    mcx = c.instrument in POINT_VALUE
    rate = c.mcx_exchange_rate if mcx else (c.nse_exchange_rate if c.instrument == 'NIFTY' else c.bse_exchange_rate)
    brokerage = 2 * c.brokerage
    exchange = round(turnover * rate, 2)
    sebi = round(turnover * c.sebi_rate, 2)
    ipft = round(turnover * c.nse_ipft_rate, 2) if c.instrument == 'NIFTY' else 0
    tax_rate = 0.0001 if mcx else (0.001 if str(date) < '2026-04-01' else 0.0015)
    stt_ctt = math.floor(sell * tax_rate + 0.5)
    stamp = math.floor(buy * (0.00002 if mcx else 0.00003) + 0.5)
    gst = round((brokerage + exchange + sebi + ipft) * c.gst_rate, 2)
    result = dict(brokerage=brokerage, exchange=exchange, sebi=sebi, ipft=ipft,
                  stt_ctt=stt_ctt, stamp=stamp, gst=gst)
    result['total_cost'] = round(sum(result.values()), 2)
    return result


def indicators(bars):
    """Causal indicators. Reset at session boundaries by caller."""
    f = bars.copy()
    for period in (9, 21, 50):
        f[f'ema{period}'] = f.close.ewm(span=period, adjust=False).mean()
    prev = f.close.shift()
    tr = pd.concat([f.high-f.low, (f.high-prev).abs(), (f.low-prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=0.1, adjust=False).mean()
    up, down = f.high.diff(), -f.low.diff()
    plus = up.where((up > down) & (up > 0), 0).ewm(alpha=1/14, adjust=False).mean()
    minus = down.where((down > up) & (down > 0), 0).ewm(alpha=1/14, adjust=False).mean()
    f['adx'] = (100*(plus-minus).abs()/(plus+minus).replace(0, np.nan)).ewm(alpha=1/14, adjust=False).mean().fillna(0)
    f['atr'] = atr
    f['atr_baseline'] = atr.shift(1).rolling(20,min_periods=1).mean()
    typical = (f.high+f.low+f.close)/3
    f['vwap'] = (typical*f.volume).cumsum()/f.volume.cumsum().replace(0, np.nan)
    f['vwap_proxy'] = not bool(f.volume.sum() > 0)
    f['vwap'] = f.vwap.fillna(typical.expanding().mean())
    upper = ((f.high+f.low)/2+3*atr).to_numpy(copy=True)
    lower = ((f.high+f.low)/2-3*atr).to_numpy(copy=True)
    close = f.close.to_numpy()
    trend = np.ones(len(f), dtype=int)
    for i in range(1, len(f)):
        upper[i] = upper[i] if upper[i] < upper[i-1] or close[i-1] > upper[i-1] else upper[i-1]
        lower[i] = lower[i] if lower[i] > lower[i-1] or close[i-1] < lower[i-1] else lower[i-1]
        trend[i] = (1 if close[i] > upper[i-1] else -1) if trend[i-1] < 0 else (-1 if close[i] < lower[i-1] else 1)
    f['trend'] = trend
    f['supertrend'] = np.where(trend > 0, lower, upper)
    return f


def make_bars(frame, timeframe):
    n = int(timeframe[:-1])
    f = frame.set_index('timestamp')
    r = f.resample(f'{n}min', origin='start_day')
    bars = r.agg(dict(open='first', high='max', low='min', close='last', volume='sum'))
    bars = bars[r.size().eq(n)].dropna(subset=['open'])
    return indicators(bars).reset_index()


class ResearchData:
    """One immutable snapshot per instrument; API credentials are never loaded."""
    def __init__(self, instrument, start='2025-01-01', end='2026-10-23', root=ROOT):
        self.instrument, self.start, self.end, self.root = instrument, start, end, Path(root)
        self.days, self.exclusions, self._features, self._events = {}, [], {}, {}
        self.eligibility = []
        report_path = self.root/'quality_report.json'
        if not report_path.exists():
            raise ValueError('Run audit_quant_lake.py first: quality report is required')
        self.quality = json.loads(report_path.read_text())
        if self.quality.get('issues'):
            raise ValueError('Lake integrity issues must be resolved before backtesting')
        self.fingerprint = hashlib.sha256((self.root/'ingestion_manifest.json').read_bytes()+report_path.read_bytes()+CALENDAR_HASH.encode()).hexdigest()[:16]
        self.expiry_calendar = set(expiries(instrument))
        calendar = self.root/'expiry_calendar.csv'
        if calendar.exists():
            cf = pd.read_csv(calendar)
            self.expiry_calendar = set(cf.loc[cf.instrument.eq(instrument), 'date'].astype(str))
            self.fingerprint = hashlib.sha256(self.fingerprint.encode()+calendar.read_bytes()).hexdigest()[:16]
        self.has_calendar = bool(self.expiry_calendar)
        self.mcx = instrument in POINT_VALUE
        self.required_windows = required_windows(self.mcx)
        selections = {}
        with QuantDataLake(self.root) as lake:
            names = {name for ds, name in lake.instruments() if
                     (ds == 'spot_1m' and (name.startswith(instrument+'_') if self.mcx else name == instrument)) or
                     (not self.mcx and ds == 'options_rolling_1m' and
                      name in {f'{instrument}_{side}_{strike}_WEEK1' for side in ('CE','PE') for strike in ('ATM','ATM-1','ATM+1')})}
            records = [r for r in lake.manifest['chunks'].values() if r['status'] == 'complete'
                       and r['dataset'] in ('spot_1m', 'options_rolling_1m') and r['instrument'] in names
                       and r['start'] < end and (self.mcx or r['end'] > start)]
            verify_files(self.root, records)
            if self.mcx:
                frames = [lake.query(name, start, end) for ds, name in lake.instruments()
                          if ds == 'spot_1m' and name.startswith(instrument+'_')]
                frames = [f for f in frames if not f.empty]
                if not frames:
                    raise ValueError(f'No data for {instrument}')
                allspot = pd.concat(frames, ignore_index=True)
                allspot['date'] = allspot.timestamp.dt.strftime('%Y-%m-%d')
                selections = select_contracts(prior_volume(lake, records, end), sorted(allspot.date.unique()))
                # Keep observed execution dates, even those without a selectable
                # contract, so every exclusion can be inspected.
                spot = allspot
                opts = pd.DataFrame()
            else:
                spot = lake.query(instrument, start, end)
                opts = pd.concat([lake.query(f'{instrument}_{side}_{strike}_WEEK1', start, end,
                                            dataset='options_rolling_1m')
                                  for side in ('CE', 'PE') for strike in ('ATM', 'ATM-1', 'ATM+1')], ignore_index=True)
        if spot.empty:
            raise ValueError('No spot data in requested window')
        spot['date'] = spot.timestamp.dt.strftime('%Y-%m-%d')
        if not opts.empty:
            opts['date'] = opts.timestamp.dt.strftime('%Y-%m-%d')
        option_days = dict(tuple(opts.groupby('date'))) if not opts.empty else {}
        self.available_days = sorted(spot.date.unique())
        coverage_file = self.root/'session_coverage.parquet'
        flagged = set()
        if coverage_file.exists():
            coverage = pd.read_parquet(coverage_file)
            flagged = set(zip(coverage.loc[coverage.short_session_review, 'instrument'],
                              coverage.loc[coverage.short_session_review, 'session'].astype(str)))
        for date, s in spot.groupby('date', sort=True):
            contract, prior, selection_error = selections.get(date, (instrument, None, None))
            audit = dict(date=date, instrument=instrument, selected_contract=contract if self.mcx else None,
                         selection_session=prior, required_windows=self.required_windows,
                         status='excluded', reasons=[], evidence=[])
            self.eligibility.append(audit)
            if self.mcx:
                s = s[s.instrument.eq(contract)]
                if selection_error or s.empty:
                    reason = selection_error or 'Prior-session selected contract has no execution candles'
                    audit['reasons'].append(dict(problem=reason, stream=contract))
                    self.exclusions.append(dict(date=date, instrument=instrument, contract=contract, reason=reason))
                    continue
            s = s.sort_values('timestamp').copy()
            s['minute'] = s.timestamp.dt.hour*60+s.timestamp.dt.minute
            if not self.mcx and not regular_session(date):
                audit['reasons'].append(dict(problem='calendar: no regular index session', stream=instrument))
            stream = s.instrument.iloc[0]
            reasons, notes = stream_check(s, date, stream, self.required_windows['underlying'], (stream,date) in flagged)
            rejected, evidence = rejected_evidence(self.root, records, date, stream, self.required_windows['underlying'])
            audit['reasons'].extend(reasons+rejected); audit['evidence'].extend(notes+evidence)
            option_map, rolling, option_meta = {}, {}, {}
            if not self.mcx:
                od = option_days.get(date, pd.DataFrame())
                for side, strike in ((side, strike) for side in ('CE','PE') for strike in ('ATM','ATM-1','ATM+1')):
                    stream = f'{instrument}_{side}_{strike}_WEEK1'
                    g = od[od.instrument.eq(stream)].copy() if len(od) else od.copy()
                    reasons, notes = stream_check(g, date, stream, self.required_windows['options'], (stream,date) in flagged)
                    rejected, evidence = rejected_evidence(self.root, records, date, stream, self.required_windows['options'])
                    audit['reasons'].extend(reasons+rejected); audit['evidence'].extend(notes+evidence)
                    if g.empty: continue
                    g['minute'] = g.timestamp.dt.hour*60+g.timestamp.dt.minute
                    rolling[side, strike] = {int(row.minute): float(row.strike) for row in g.itertuples()}
                    for row in g.itertuples():
                        key = (int(row.minute), side, float(row.strike))
                        value = (row.open, row.high, row.low, row.close, row.volume)
                        if key in option_map and option_map[key] != value:
                            audit['reasons'].append(dict(problem='Conflicting same-strike candles', stream=stream,
                                                         timestamps=[row.timestamp.isoformat()]))
                        option_map[key] = value
                        option_meta[key] = (float(row.oi),float(row.iv),float(row.spot))
            if audit['reasons']:
                self.exclusions.append(dict(date=date, instrument=instrument, contract=contract if self.mcx else None,
                                            reason='; '.join(sorted({r['problem'] for r in audit['reasons']}))))
                continue
            audit['status'] = 'eligible'
            self.days[date] = dict(spot=s, prices={int(r.minute): (r.open,r.high,r.low,r.close,r.volume) for r in s.itertuples()},
                                  options=option_map, option_meta=option_meta, rolling=rolling, contract=s.instrument.iloc[0],
                                  timestamp=s.timestamp.iloc[0].normalize())
        # An all-excluded request still has a useful eligibility report and zero
        # executions; do not erase the evidence with a generic loader exception.
        self.fingerprint = hashlib.sha256(self.fingerprint.encode()+json.dumps(self.eligibility, sort_keys=True).encode()).hexdigest()[:16]

    def features(self, timeframe):
        if timeframe not in self._features:
            self._features[timeframe] = {d: make_bars(day['spot'], timeframe) for d,day in self.days.items()}
        return self._features[timeframe]

    def price(self, day, at, side, strike):
        return day['prices'].get(at) if self.mcx else day['options'].get((at, side, strike))

    def events(self, c, date):
        """Each event contains an entry minute, side, fixed strike, trigger, ATR."""
        key = (c.strategy,c.timeframe,c.strike,c.orb_minutes,c.anchor,c.mode,c.breakout_pct,c.entry_start,c.entry_end,c.adx_min,
               c.option_lookback,c.volume_multiple,c.oi_mode,c.oi_min_change_pct,c.atr_expansion,c.compression_ratio,date)
        if key in self._events:
            return self._events[key]
        if c.strategy in HIGH_STRATEGIES:
            self._events[key]=high_edge_events(self,c,date)
            return self._events[key]
        day = self.days[date]
        result = []
        if c.strategy == STRATEGIES[2]:
            anchor = minute(c.anchor)
            refs = {}
            for side in ('CE','PE'):
                strike = day['rolling'][side,'ATM'].get(anchor-1)
                row = self.price(day,anchor-1,side,strike)
                if row and row[3] > 0:
                    refs[side] = (strike,row[3]*(1+c.breakout_pct/100))
            if len(refs) == 2:
                if c.mode == 'B':
                    result = [(anchor,side,refs[side][0],None,0) for side in ('CE','PE')]
                else:
                    # Real stop entry using previously known fixed anchor strike.
                    seen = set()
                    for at in range(anchor, min(920,minute(c.entry_end))):
                        breached = [(side, strike, trigger) for side,(strike,trigger) in refs.items()
                                    if (row:=self.price(day,at,side,strike)) and row[1]>=trigger]
                        if len(breached) == 2 and not seen:
                            # OHLC cannot identify which leg triggered first.
                            seen.update(('CE','PE')); break
                        for side,strike,trigger in breached:
                            result.append((at,side,strike,trigger,0)); seen.add(side)
        else:
            bars = self.features(c.timeframe)[date]
            n = int(c.timeframe[:-1])
            opening = 1020 if self.mcx else 555
            range_end = opening + (30 if self.mcx else c.orb_minutes)
            range_rows = [v for k,v in day['prices'].items() if opening<=k<range_end]
            range_high = max(r[1] for r in range_rows)
            range_low = min(r[2] for r in range_rows)
            for i,row in enumerate(bars.itertuples()):
                at = row.timestamp.hour*60+row.timestamp.minute+n
                warmup = 1 if c.strategy == STRATEGIES[1] else 20
                if i < warmup or at < (max(1050,minute(c.entry_start)) if self.mcx else minute(c.entry_start)) or at > (min(1350,minute(c.entry_end)) if self.mcx else minute(c.entry_end)):
                    continue
                side = None
                if c.strategy == STRATEGIES[0]:
                    if row.adx >= c.adx_min:
                        if row.ema9>row.ema21 and row.trend>0 and row.close>row.vwap: side='CE'
                        elif row.ema9<row.ema21 and row.trend<0 and row.close<row.vwap: side='PE'
                elif c.strategy in (STRATEGIES[1],STRATEGIES[4]):
                    if at <= range_end: continue
                    if row.close>range_high: side='LONG' if self.mcx else 'CE'
                    elif row.close<range_low: side='SHORT' if self.mcx else 'PE'
                elif c.strategy == STRATEGIES[3]:
                    proximity = max(row.atr*0.25,row.close*0.0003)
                    if row.close>row.ema50 and row.low<=row.vwap+proximity and row.close>row.vwap and row.close>row.open and row.close-row.low>(row.high-row.low)*0.6: side='CE'
                    elif row.close<row.ema50 and row.high>=row.vwap-proximity and row.close<row.vwap and row.close<row.open and row.high-row.close>(row.high-row.low)*0.6: side='PE'
                if side is None: continue
                strike = 0 if self.mcx else day['rolling'][side,c.strike].get(at-1)
                if strike is None: continue
                if c.strategy == STRATEGIES[1]:
                    history = [self.price(day,t,side,strike) for t in range(at-21,at)]
                    option_orb = [self.price(day,t,side,strike) for t in range(opening,range_end)]
                    if any(v is None for v in history+option_orb): continue
                    if history[-1][3] <= max(v[1] for v in option_orb) or history[-1][4]<=1.5*np.mean([v[4] for v in history[:-1]]): continue
                result.append((at,side,strike,None,row.atr))
        self._events[key] = result
        return result


def order_allocation(c,date,entry,budget,direction=1):
    unit=quantity(c,date)//c.lots
    rate=c.futures_margin_pct/100 if c.instrument in POINT_VALUE else 1
    lots=min(c.max_lots,int(max(0,budget)//(entry*unit*rate))) if c.sizing_mode=='allocation' else c.lots
    while lots>0:
        qty=unit*lots
        principal=entry*qty*rate
        reserve=fees(c,entry,entry,qty,date,direction)['total_cost']
        if principal+reserve<=budget+1e-8: return qty,principal,reserve
        if c.sizing_mode=='fixed_lots': break
        lots-=1
    return 0,0.,0.


def simulate_trade(data,c,date,event,trade_id,**account):
    """Single-position compatibility API; portfolio orchestration uses the same steps."""
    steps=_trade_steps(data,c,date,event,trade_id,**account)
    while True:
        try: next(steps)
        except StopIteration as done: return done.value


def _trade_steps(data, c, date, event, trade_id, budget=None, entry_equity=None):
    at, side, strike, trigger, atr = event
    day = data.days[date]
    direction = -1 if side=='SHORT' else 1
    first = data.price(day,at,side,strike)
    if first is None or first[0]<=0: return None, []
    raw_entry = max(first[0],trigger) if trigger else first[0]
    if not data.mcx and (raw_entry < c.minimum_premium or c.option_side not in ('both', side)):
        return None, []
    entry = adverse_tick(raw_entry+direction*c.slippage,c,direction==1)
    entry_equity=c.capital if entry_equity is None else entry_equity
    budget=c.capital*c.capital_usage_pct/100 if budget is None else budget
    qty,allocated,reserve=order_allocation(c,date,entry,budget,direction)
    if not qty: return None, []
    atr_risk = data.mcx and c.risk_basis == 'legacy'
    stop_distance = atr*c.atr_sl if atr_risk else entry*c.sl_pct/100
    if stop_distance<=0: return None, []
    target_distance = stop_distance*c.reward_risk if atr_risk else entry*c.target_pct/100
    stop, target = entry-direction*stop_distance, entry+direction*target_distance
    initial_stop = stop
    best = entry
    trace = []
    modeled_bars=0
    previous=data.price(day,at-1,side,strike)
    final_source='observed'
    finish = 1395 if data.mcx else 920
    exit_price, exit_at, reason = None, at, 'unresolved'
    force_exit=None
    yield dict(minute=at-1,net_mtm=-reserve,reserved_capital=allocated+reserve)
    for current in range(at,finish+1):
        row = data.price(day,current,side,strike)
        source=dict(price_source='observed',model_neighbor_strike=np.nan,model_iv_pct=np.nan,model_expiry_assumption='')
        if row is None:
            if data.mcx or c.price_policy=='strict_observed':
                raise MissingObservedPrice(f'{date} {side} strike {strike} missing at minute {current}; strict run is not rankable')
            row,source=continuation_candle(data,day,current,side,strike,previous)
            modeled_bars+=1
        previous=row
        final_source=source['price_source']
        op,hi,lo,cl,_ = row
        trace.append(dict(trade_id=trade_id,timestamp=day['timestamp']+pd.Timedelta(minutes=current),stop=stop,target=target,mark=cl,
                          open=op,high=hi,low=lo,close=cl,entry_strike=strike,**source))
        if current==finish:
            exit_price,exit_at,reason=op-direction*c.slippage,current,'square_off'; break
        if force_exit:
            exit_price,exit_at,reason=op-direction*c.slippage,current,force_exit; break
        hit_stop = lo<=stop if direction==1 else hi>=stop
        hit_target = c.exit_mode != 'end_of_day' and (hi>=target if direction==1 else lo<=target)
        # Stop wins ambiguous OHLC collisions. Gaps use the adverse open.
        if hit_stop:
            exit_price=(min(op,stop) if direction==1 else max(op,stop))-direction*c.slippage
            exit_at,reason=current,('trailing_stop' if stop!=initial_stop else 'stop_loss'); break
        if hit_target:
            exit_price=target-direction*c.slippage
            exit_at,reason=current,'target'; break
        favorable = hi if direction==1 else lo
        best = max(best,favorable) if direction==1 else min(best,favorable)
        if c.exit_mode=='legacy' and c.strategy==HIGH_STRATEGIES[2] and best>=entry*(1+c.breakeven_trigger_pct/100):
            stop=max(stop,entry)  # premium breakeven, not net-of-cost breakeven; next minute
        jump = stop_distance*c.atr_trail_jump if atr_risk else entry*c.trail_jump_pct/100
        if c.exit_mode in ('legacy', 'trailing_sl') and c.trail_pct>0 and direction*(best-entry)>=jump:
            distance = stop_distance if atr_risk else entry*c.trail_pct/100
            steps=math.floor((direction*(best-entry)+1e-10)/jump)
            # Price can leap through several jumps in one minute; only complete
            # increments advance the trail. Overshoot is retained for next time.
            proposed = entry-direction*distance+direction*steps*jump
            stop = max(stop,proposed) if direction==1 else min(stop,proposed)
        liquidation=adverse_tick(cl-direction*c.slippage,c,direction==-1)
        mtm=direction*(liquidation-entry)*qty-fees(c,entry,liquidation,qty,date,direction)['total_cost']
        force_exit=yield dict(minute=current,net_mtm=mtm,reserved_capital=allocated+reserve)
    closed=exit_price is not None
    if not closed: raise MissingObservedPrice('Position did not reach a priced terminal exit')
    raw_exit_price=exit_price+direction*c.slippage
    exit_price=adverse_tick(exit_price,c,direction==-1)
    costs=fees(c,entry,exit_price,qty,date,direction) if closed else {k:np.nan for k in ('brokerage','exchange','sebi','ipft','stt_ctt','stamp','gst','total_cost')}
    gross=direction*(exit_price-entry)*qty if closed else np.nan
    record=dict(trade_id=trade_id,date=date,side=side,strike=strike,entry_strike=strike,
                contract=day['contract'] if data.mcx else f'{data.instrument}_{side}_{strike}_EXPIRY_UNKNOWN',
                entry_time=day['timestamp']+pd.Timedelta(minutes=at),exit_time=day['timestamp']+pd.Timedelta(minutes=exit_at),
                entry_price=entry,exit_price=exit_price,quantity=qty,initial_stop=initial_stop,target=target,
                raw_entry_price=raw_entry,raw_exit_price=raw_exit_price,
                gross_before_slippage=direction*(raw_exit_price-raw_entry)*qty,
                gross_pnl=gross,net_pnl=gross-costs['total_cost'],status='closed' if closed else 'censored',exit_reason=reason,
                expiry_day=('expiry' if date in data.expiry_calendar else 'non_expiry') if data.has_calendar else 'unknown',
                is_expiry_day=is_expiry_day(c.instrument,date) if not data.mcx else False,
                inferred_expiry=next_expiry(c.instrument,date) if not data.mcx else '',
                modeled_bars=modeled_bars,used_modeled_prices=modeled_bars>0,exit_price_source=final_source,
                contract_identity_verified=bool(data.mcx),
                verification_status='observed_candles_accounting_checked' if data.mcx else 'model_dependent' if modeled_bars else 'observed_strike_expiry_unverified',
                **day.get('signal_evidence',{}).get((c.strategy,c.timeframe,at,side,strike),{}),
                instrument=c.instrument,strategy=c.strategy,
                capital_allocated=allocated,capital_reserved=allocated+reserve,allocation_budget=budget,
                capital_used_pct=100*allocated/entry_equity,entry_equity=entry_equity,
                roi_pct=100*(gross-costs['total_cost'])/allocated,
                capital_basis='assumed_futures_margin' if data.mcx else 'option_premium',
                slippage_cost=direction*((entry-raw_entry)+(raw_exit_price-exit_price))*qty,**costs)
    return record,trace


def performance(trades, dates, capital):
    closed=trades[trades.status.eq('closed')] if not trades.empty else trades
    pnl=closed.groupby('date').net_pnl.sum() if not closed.empty else pd.Series(dtype=float)
    daily=pd.DataFrame({'date':sorted(dates)})
    daily['net_pnl']=daily.date.map(pnl).fillna(0)
    daily['equity']=capital+daily.net_pnl.cumsum()
    daily['peak']=daily.equity.cummax().clip(lower=capital)
    daily['drawdown']=daily.equity-daily.peak
    daily['drawdown_pct']=100*daily.drawdown/daily.peak
    # Realized daily returns use the previous session's actual account equity.
    returns=daily.net_pnl/daily.equity.shift(1,fill_value=capital).where(lambda v:v>0)
    net=float(daily.net_pnl.sum())
    wins=closed.net_pnl[closed.net_pnl>0] if len(closed) else pd.Series(dtype=float)
    losses=closed.net_pnl[closed.net_pnl<0] if len(closed) else pd.Series(dtype=float)
    dd=float(-daily.drawdown.min()) if len(daily) else 0
    span=max((pd.Timestamp(daily.date.iloc[-1])-pd.Timestamp(daily.date.iloc[0])).days+1,1) if len(daily) else 1
    annual=(1+net/capital)**(365.25/span)-1 if net>-capital else -1
    metrics=dict(net_pnl=net,roi_pct=100*net/capital,total_trades=len(closed),
                 censored_trades=int(len(trades)-len(closed)),win_rate=100*len(wins)/len(closed) if len(closed) else 0,
                 profit_factor=float(wins.sum()/-losses.sum()) if len(losses) else (None if not len(wins) else 999.0),
                 expectancy=net/len(closed) if len(closed) else 0,max_drawdown=dd,
                 max_drawdown_pct=float(-daily.drawdown_pct.min()) if len(daily) else 0,
                 sharpe=float(returns.mean()/returns.std(ddof=1)*np.sqrt(252)) if len(returns)>1 and returns.std(ddof=1)>0 else 0,
                 calmar=float(annual/(dd/capital)) if dd>0 else None,
                 total_cost=float(closed.total_cost.sum()) if len(closed) else 0,
                 train_2025_net=float(daily.loc[daily.date<'2026-01-01','net_pnl'].sum()),
                 validation_2026_net=float(daily.loc[daily.date>='2026-01-01','net_pnl'].sum()))
    metrics['unresolved_trades']=int(len(trades)-len(closed))
    metrics['modeled_trades']=int(closed.used_modeled_prices.sum()) if 'used_modeled_prices' in closed else 0
    metrics['modeled_bars']=int(closed.modeled_bars.sum()) if 'modeled_bars' in closed else 0
    metrics['all_closed_trades_accounted']=metrics['unresolved_trades']==0
    # Infinite PF represented as null + explicit flag, never a fake finite ratio.
    metrics['profit_factor_infinite']=bool(len(wins) and not len(losses))
    if metrics['profit_factor_infinite']: metrics['profit_factor']=None
    monthly=daily.assign(month=daily.date.str[:7]).groupby('month',as_index=False).net_pnl.sum()
    monthly['return_pct']=monthly.net_pnl/capital*100
    return metrics,daily,monthly


def atomic_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8')
    os.replace(tmp,path)


def run_backtest(c, data=None, output=OUT, persist=True):
    c.validate()
    data=data or ResearchData(c.instrument,c.start,c.end)
    if (data.instrument,data.start,data.end)!=(c.instrument,c.start,c.end):
        raise ValueError('Prepared data does not match configuration window')
    identity=json.dumps(dict(config=asdict(c),engine=ENGINE_VERSION,data=data.fingerprint),sort_keys=True)
    run_id=hashlib.sha256(identity.encode()).hexdigest()[:16]
    path=Path(output)/'runs'/run_id
    marker=path/'result.json'
    if persist and marker.exists() and (path/'eligibility.json').exists() and all((path/f'{name}.parquet').exists() for name in ('trades','equity','monthly','stop_paths','excluded_sessions','skipped_entries','daily_risk')):
        return json.loads(marker.read_text())
    began=time.perf_counter()
    from quant_portfolio import execute_portfolio
    daily_risk=[]
    records,paths,skipped=execute_portfolio(data,c,run_id,daily_audit=daily_risk)
    trades=pd.DataFrame(records)
    if trades.empty:
        trades=pd.DataFrame(columns=['trade_id','date','side','strike','entry_strike','entry_time','exit_time','entry_price','exit_price','net_pnl','status','total_cost','expiry_day','used_modeled_prices','modeled_bars','verification_status'])
    metrics,equity,monthly=performance(trades,data.available_days,c.capital)
    metrics['skipped_entries']=len(skipped)
    metrics['max_intraday_drawdown_1m_close']=max((r['max_drawdown_1m_close'] for r in daily_risk),default=0.)
    metrics['profit_lock_sessions']=sum(r['lock_reason']=='daily_profit_target' for r in daily_risk)
    metrics['loss_lock_sessions']=sum(r['lock_reason']=='daily_loss_limit' for r in daily_risk)
    splits={}
    if len(trades):
        closed=trades[trades.status.eq('closed')]
        for col in ('side','expiry_day'):
            splits[col]=closed.groupby(col).agg(trades=('net_pnl','size'),net_pnl=('net_pnl','sum')).reset_index().to_dict('records')
    result=dict(run_id=run_id,config=asdict(c),metrics=metrics,splits=splits,engine_version=ENGINE_VERSION,
                data_fingerprint=data.fingerprint,eligible_sessions=len(data.days),excluded_sessions=len(data.exclusions),
                eligibility=getattr(data,'eligibility',[]),
                eligibility_scope='Run dependency windows, not whole-session historical health or verified options identity',
                first_session=min(data.days,default=None),last_session=max(data.days,default=None),runtime_seconds=round(time.perf_counter()-began,3),
                status='observed_candles_accounting_checked' if data.mcx else 'model_dependent' if metrics['modeled_trades'] else 'observed_strike_expiry_unverified',
                verified_price_eligible=bool(data.mcx and metrics['unresolved_trades']==0 and len(trades)>0),
                limitations=LIMITATIONS,expiry_calendar_available=data.has_calendar,
                equity_basis='all entered trades closed; realized daily equity; modeled prices explicitly tagged',path=str(path.resolve()))
    if persist:
        path.mkdir(parents=True,exist_ok=True)
        atomic_json(path/'eligibility.json', result['eligibility'])
        for name,frame in [('trades',trades),('equity',equity),('monthly',monthly),('stop_paths',pd.DataFrame(paths)),('skipped_entries',pd.DataFrame(skipped)),('excluded_sessions',pd.DataFrame(data.exclusions)),('daily_risk',pd.DataFrame(daily_risk))]:
            temp=path/f'{name}.parquet.tmp'
            frame.to_parquet(temp,index=False); os.replace(temp,path/f'{name}.parquet')
        atomic_json(marker,result)  # completion marker written last
    return result


def refresh_leaderboard(output=OUT):
    output=Path(output); rows=[]
    for p in (output/'runs').glob('*/result.json'):
        r=json.loads(p.read_text())
        if r['engine_version'] != ENGINE_VERSION:
            continue
        rows.append(dict(run_id=r['run_id'],**r['config'],**r['metrics'],
                         eligible_sessions=r['eligible_sessions'],excluded_sessions=r['excluded_sessions'],
                         first_session=r['first_session'],last_session=r['last_session'],status=r['status'],
                         verified_price_eligible=r['verified_price_eligible']))
    table=pd.DataFrame(rows)
    if not table.empty:
        table=table.sort_values('net_pnl',ascending=False).reset_index(drop=True)
        temp=output/'leaderboard.csv.tmp'; table.to_csv(temp,index=False); os.replace(temp,output/'leaderboard.csv')
        table[table.verified_price_eligible].to_csv(output/'verified_leaderboard.csv',index=False)
    return table
