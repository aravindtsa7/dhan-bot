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
from quant_data_quality import require_current_audit

ENGINE_VERSION = '5.4.0'
OUT = ROOT / 'backtests'
LEGACY_STRATEGIES = ['EMA_Supertrend_VWAP_Directional', 'ORB_Option_Momentum_Breakout',
              'Dual_CE_PE_Straddle_Breakout', 'VWAP_Trend_Pullback_Buyer',
              'MCX_Evening_Session_Breakout']
LEGACY_STRATEGIES += HIGH_STRATEGIES
# Historical strategy keys stay readable for old research artifacts and tests.
# Existing research keys remain stable; book structures share portfolio policy.
RESEARCH_STRATEGIES = ['vwap_ema_pullback', 'orb_volume_breakout',
                       'supertrend_adx_trend', 'mcx_evening_breakout']
BOOK_STRATEGIES = ['book_vertical_spread', 'book_iron_butterfly',
                   'book_strap_strip', 'book_vol_oi_momentum']
STRATEGIES = RESEARCH_STRATEGIES + BOOK_STRATEGIES + ['expiry_day']
INSTRUMENTS = ['NIFTY', 'SENSEX', 'CRUDEOIL', 'NATURALGAS', 'GOLDM']
POINT_VALUE = {'CRUDEOIL': 100, 'NATURALGAS': 1250, 'GOLDM': 10}
LIMITATIONS = [
    'Out-of-band fixed strikes may use intrinsic plus bounded, distance-decayed wing time value; modeled_wing_fallback flags every affected trade. Strict-observed policy still rejects modeled fills.',
    'Book structures use atomic per-leg fills. Missing spread legs use prior pair value plus signed 0.20 spot change and an adjacent-spread correction capped at 0.05 times absolute spot change. These are research delta assumptions. Vertical values stay in [0.05, width-0.05], without an intrinsic floor; butterfly liability stays within one wing width. Exact observed quotes are retained. Costs and slippage are applied after bounds and may exceed payoff limits. Combined candles show synchronized open/close envelopes, not executable intrabar extrema.',
    'PROVISIONAL: rolling WEEK1 options have no expiry/contract ID. Matching strike is necessary but cannot prove contract identity.',
    'Missing option candles may use explicitly tagged delta/gamma continuity estimates. Zero unresolved positions does not verify modeled fills.',
    'H1 uses stored fixed-strike option VWAP; zero-volume spot has a separate TWAP proxy. Repaired stored inputs may affect signals; all affected sessions are disclosed.',
    'Synthetic flat zero-volume repair bars are modeled inputs, not provider quotes. Original partitions and quarantine evidence are retained; strict-observed runs reject synthetic inputs.',
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
    strategy: str = RESEARCH_STRATEGIES[0]
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
    momentum_lookback: int = 10
    momentum_threshold: float = 0.25
    expiry_entry_mode: str = 'btst'  # btst / expiry_intraday
    expiry_entry_signal: str = '15:25'
    expiry_exit_cutoff: str = '15:20'
    expiry_include_prior_session: bool = True  # Period selects expiry/exit dates
    expiry_calendar_mode: str = 'calendar'  # calendar / observed_reset (recorded evidence only)

    def validate(self):
        if self.instrument == 'BANKNIFTY':
            raise ValueError('BANKNIFTY spot is available, but option history and a dated contract schedule are not configured. Choose NIFTY 50 or SENSEX for option backtests.')
        if self.instrument not in INSTRUMENTS or self.strategy not in STRATEGIES + LEGACY_STRATEGIES:
            raise ValueError('Unknown instrument or strategy')
        if self.timeframe not in ('1m', '3m', '5m', '15m') or self.strike not in ('ATM', 'ATM-1', 'ATM+1'):
            raise ValueError('Unsupported timeframe / moneyness')
        if self.strategy in STRATEGIES and self.strike != 'ATM':
            raise ValueError('Research strategies require rolling ATM strike selection')
        if self.strategy == 'mcx_evening_breakout' and self.instrument not in ('NATURALGAS', 'CRUDEOIL'):
            raise ValueError('MCX Evening Momentum supports NATURALGAS and CRUDEOIL only')
        if self.strategy in BOOK_STRATEGIES[1:3] and self.instrument in POINT_VALUE:
            raise ValueError('Iron butterfly and strap/strip require index option streams; MCX futures cannot reproduce these payoffs')
        if self.strategy=='expiry_day' and self.instrument not in ('NIFTY','SENSEX'):
            raise ValueError('Expiry Day supports NIFTY and SENSEX weekly index options only; MCX is unsupported')
        if self.expiry_entry_mode not in ('btst','expiry_intraday') or self.expiry_calendar_mode not in ('calendar','observed_reset'):
            raise ValueError('Invalid Expiry Day entry or calendar mode')
        if not 925<=minute(self.expiry_entry_signal)<=928 or self.expiry_exit_cutoff not in ('15:15','15:20') or not isinstance(self.expiry_include_prior_session,bool):
            raise ValueError('Expiry Day signal must be 15:25–15:28; cutoff must be 15:15 or 15:20')
        if self.strategy not in STRATEGIES and (self.instrument in POINT_VALUE) != (self.strategy in (LEGACY_STRATEGIES[4],HIGH_STRATEGIES[3],HIGH_STRATEGIES[5])):
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
        if self.strategy in (LEGACY_STRATEGIES[2],HIGH_STRATEGIES[1]) and self.strike != 'ATM':
            raise ValueError('Dual-leg strategy uses ATM anchor strikes')
        if self.mode not in ('A', 'B') or self.lots < 1 or self.lot_size < 0:
            raise ValueError('Invalid mode or quantity')
        if self.strategy==LEGACY_STRATEGIES[2] and self.mode=='B' and self.max_trades_day!=2:
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
        if self.volume_multiple<(1.3 if self.strategy in STRATEGIES else 1.5) or self.option_lookback<1 or self.oi_min_change_pct<=0 or self.atr_expansion<=0 or self.compression_ratio<=0:
            raise ValueError('Invalid volume/OI/ATR filters')
        if self.momentum_lookback < 2 or int(self.momentum_lookback)!=self.momentum_lookback or not 0 < self.momentum_threshold < 1:
            raise ValueError('Invalid momentum lookback or threshold')
        if self.tick_size<0: raise ValueError('Tick size cannot be negative')
        if self.minimum_premium < 0 or self.option_side not in ('both', 'CE', 'PE'):
            raise ValueError('Invalid minimum premium or option side')
        if self.exit_mode not in ('legacy', 'end_of_day', 'trailing_sl', 'target_eod') or self.risk_basis not in ('legacy', 'percentage'):
            raise ValueError('Invalid exit mode or risk basis')
        if self.exit_mode == 'trailing_sl' and self.trail_pct <= 0:
            raise ValueError('Trailing SL requires a positive trailing distance')
        if self.option_side != 'both' and (self.instrument in POINT_VALUE or self.strategy=='expiry_day' or self.strategy in BOOK_STRATEGIES[:3] or self.strategy in (LEGACY_STRATEGIES[2], HIGH_STRATEGIES[1])):
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


def wilder(series, period):
    """Wilder RMA seeded by the first full SMA; no future backfilling."""
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    seed = []
    previous = np.nan
    for i, value in enumerate(values):
        if not np.isfinite(value):
            continue
        if np.isnan(previous):
            seed.append(value)
            if len(seed) < period:
                continue
            previous = float(np.mean(seed))
        else:
            previous += (value-previous)/period
        out[i] = previous
    return pd.Series(out, index=series.index)


def research_indicators(bars):
    """Wilder ATR/ADX/RSI and true ATR(10), multiplier-3 Supertrend."""
    f = bars.copy()
    for n in (9, 21, 50):
        f[f'ema{n}'] = f.close.ewm(span=n, adjust=False, min_periods=n).mean()
    tr = pd.concat([f.high-f.low, (f.high-f.close.shift()).abs(),
                    (f.low-f.close.shift()).abs()], axis=1).max(axis=1)
    atr10, atr14 = wilder(tr, 10), wilder(tr, 14)
    f['atr'], f['atr14'] = atr10, atr14
    up, down = f.high.diff(), -f.low.diff()
    plus = 100*wilder(up.where((up>down)&(up>0), 0), 14)/atr14
    minus = 100*wilder(down.where((down>up)&(down>0), 0), 14)/atr14
    dx = (100*(plus-minus).abs()/(plus+minus).replace(0, np.nan)).where((plus+minus).ne(0), 0)
    f['adx'] = wilder(dx, 14)
    delta = f.close.diff()
    gain, loss = wilder(delta.clip(lower=0), 14), wilder(-delta.clip(upper=0), 14)
    f['rsi'] = (100-100/(1+gain/loss.replace(0, np.nan))).where(loss.ne(0), 100).where((gain+loss).ne(0), 50)
    session = f.timestamp.dt.strftime('%Y-%m-%d')
    typical = (f.high+f.low+f.close)/3
    volume = f.volume.groupby(session).cumsum()
    f['vwap_proxy'] = volume.eq(0)
    f['vwap'] = (typical*f.volume).groupby(session).cumsum()/volume.replace(0, np.nan)
    f['vwap'] = f.vwap.fillna(typical.groupby(session).expanding().mean().reset_index(level=0, drop=True).reindex(f.index))
    upper = ((f.high+f.low)/2+3*atr10).to_numpy(copy=True)
    lower = ((f.high+f.low)/2-3*atr10).to_numpy(copy=True)
    trend = np.zeros(len(f), dtype=int)
    close = f.close.to_numpy()
    for i in range(len(f)):
        if not np.isfinite(upper[i]): continue
        if i == 0 or not trend[i-1]:
            trend[i] = -1  # conventional initial downtrend until a band break
            continue
        if not (upper[i]<upper[i-1] or close[i-1]>upper[i-1]): upper[i]=upper[i-1]
        if not (lower[i]>lower[i-1] or close[i-1]<lower[i-1]): lower[i]=lower[i-1]
        trend[i] = (1 if close[i]>upper[i] else -1) if trend[i-1]<0 else (-1 if close[i]<lower[i] else 1)
    f['trend'] = trend
    f['supertrend'] = np.where(trend>0, lower, upper)
    return f


def research_events(data, c, date, confirm_volume=True):
    """Close-confirmed signals, executed at the next minute open, fixed ATM strike.

    Spot index volume is usually zero. ORB then requires observed volume on
    the candidate fixed-strike option, over ten complete prior bars. It never
    invents spot volume or mixes rolling strikes to create a volume surge.
    """
    if c.strategy=='expiry_day':
        from quant_expiry import expiry_events
        return expiry_events(data,c,date)
    if c.strategy in BOOK_STRATEGIES:
        from quant_book import book_events
        return book_events(data, c, date, confirm_volume=confirm_volume)
    day = data.days[date]
    features = getattr(data, 'research_features', data.features)
    bars = features(c.timeframe)[date]
    n = int(c.timeframe[:-1])
    opening = 1020 if data.mcx else 555
    range_end = opening + (30 if data.mcx else 15)
    range_rows = [v for t,v in day['prices'].items() if opening<=t<range_end]
    orb_high = max((v[1] for v in range_rows), default=np.nan)
    orb_low = min((v[2] for v in range_rows), default=np.nan)
    result = []
    for i, row in enumerate(bars.itertuples()):
        at = row.timestamp.hour*60+row.timestamp.minute+n
        lo, hi = (565, 870) if c.strategy=='vwap_ema_pullback' or not data.mcx else (1020, 1365)
        if i<1 or not max(lo, minute(c.entry_start))<=at<=min(hi, minute(c.entry_end)):
            continue
        previous = bars.iloc[i-1]
        bullish = bearish = False
        volume_basis = 'underlying'
        if c.strategy == 'vwap_ema_pullback':
            # Previous candle touches EMA9 from the trend side; current close
            # clears its high/low. A one-sided inequality isn't a pullback.
            touch = previous.low <= previous.ema9 <= previous.high
            bullish = touch and row.close>row.vwap and row.ema9>row.ema21 and row.adx>=18 and row.close>previous.high
            bearish = touch and row.close<row.vwap and row.ema9<row.ema21 and row.adx>=18 and row.close<previous.low
        elif c.strategy == 'orb_volume_breakout':
            if at<=range_end: continue
            bullish = row.close>orb_high and 55<=row.rsi<=78
            bearish = row.close<orb_low and 22<=row.rsi<=45
        elif c.strategy == 'supertrend_adx_trend':
            # Continuation requires another close beyond the previous candle;
            # a flip itself qualifies. Re-entry still obeys the portfolio cap.
            bullish = row.adx>22 and row.trend==1 and (previous.trend==-1 or row.close>previous.high)
            bearish = row.adx>22 and row.trend==-1 and (previous.trend==1 or row.close<previous.low)
        elif c.strategy == 'mcx_evening_breakout':
            history = bars.iloc[max(0,i-12):i]
            if len(history)<12 or not np.isfinite(row.ema50): continue
            expanding = abs(row.close-row.open)>1.2*row.atr14
            bullish = expanding and row.close>history.high.max() and row.close>row.ema50
            bearish = expanding and row.close<history.low.min() and row.close<row.ema50
        if not (bullish or bearish): continue
        side = ('LONG' if bullish else 'SHORT') if data.mcx else ('CE' if bullish else 'PE')
        strike = 0 if data.mcx else day['rolling'].get((side,'ATM'),{}).get(at-1)
        if strike is None: continue
        if c.strategy == 'orb_volume_breakout' and confirm_volume:
            volumes = bars.volume.iloc[max(0,i-10):i]
            current_volume = row.volume
            if not data.mcx and not (volumes.sum()>0):
                volume_basis = 'fixed_strike_option'
                # Only timestamps known at signal close; ten preceding full bars.
                windows = []
                for start in range(at-11*n, at, n):
                    candles = [data.price(day,t,side,strike) for t in range(start,start+n)]
                    if any(v is None for v in candles): break
                    windows.append(sum(v[4] for v in candles))
                if len(windows)!=11: continue
                volumes, current_volume = pd.Series(windows[:-1]), windows[-1]
            if len(volumes)!=10 or volumes.mean()<=0 or current_volume<=1.3*volumes.mean(): continue
        evidence = dict(signal_time=row.timestamp+pd.Timedelta(minutes=n),
                        signal_vwap_proxy=bool(row.vwap_proxy), signal_volume_basis=volume_basis)
        day.setdefault('signal_evidence', {})[c.strategy,c.timeframe,at,side,strike] = evidence
        result.append((at,side,strike,None,float(row.atr14)))
    return result


class ResearchData:
    """One immutable snapshot per instrument; API credentials are never loaded."""
    def __init__(self, instrument, start='2025-01-01', end='2026-10-23', root=ROOT, expiry_config=None):
        self.instrument, self.start, self.end, self.root = instrument, start, end, Path(root)
        self.days, self.exclusions, self._features, self._events = {}, [], {}, {}
        self.eligibility = []
        report_path = self.root/'quality_report.json'
        self.quality = require_current_audit(self.root)
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
        self.expiry_config = expiry_config
        self.observed_expiry_dates = set()
        load_start = str((pd.Timestamp(start)-pd.Timedelta(days=10)).date()) if expiry_config and expiry_config.expiry_include_prior_session else start
        selections = {}
        with QuantDataLake(self.root) as lake:
            names = {name for ds, name in lake.instruments() if
                     (ds == 'spot_1m' and (name.startswith(instrument+'_') if self.mcx else name == instrument)) or
                     (not self.mcx and ds == 'options_rolling_1m' and
                      name in {f'{instrument}_{side}_{strike}_WEEK1' for side in ('CE','PE') for strike in ('ATM','ATM-1','ATM+1')})}
            records = [r for r in lake.manifest['chunks'].values() if r['status'] == 'complete'
                       and r['dataset'] in ('spot_1m', 'options_rolling_1m') and r['instrument'] in names
                       and r['start'] < end and (self.mcx or r['end'] > load_start)]
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
                spot = lake.query(instrument, load_start, end)
                opts = pd.concat([lake.query(f'{instrument}_{side}_{strike}_WEEK1', load_start, end,
                                            dataset='options_rolling_1m')
                                  for side in ('CE', 'PE') for strike in ('ATM', 'ATM-1', 'ATM+1')], ignore_index=True)
        if spot.empty:
            raise ValueError('No spot data in requested window')
        spot['date'] = spot.timestamp.dt.strftime('%Y-%m-%d')
        if not opts.empty:
            opts['date'] = opts.timestamp.dt.strftime('%Y-%m-%d')
        entry_dates=set()
        if expiry_config:
            from quant_expiry import observed_expiries, dependency_dates
            self.observed_expiry_dates=observed_expiries(opts)
            needed,entry_dates=dependency_dates(self,expiry_config,sorted(spot.date.unique()))
            spot=spot[spot.date.isin(needed)]
            if not opts.empty: opts=opts[opts.date.isin(needed)]
        option_days = dict(tuple(opts.groupby('date'))) if not opts.empty else {}
        self.available_days = (sorted(d for d in self.observed_sessions if start<=d<end) if expiry_config
                               else sorted(spot.date.unique()))
        coverage_file = self.root/'session_coverage.parquet'
        flagged = set()
        if coverage_file.exists():
            coverage = pd.read_parquet(coverage_file)
            flagged = set(zip(coverage.loc[coverage.short_session_review, 'instrument'],
                              coverage.loc[coverage.short_session_review, 'session'].astype(str)))
        for date, s in spot.groupby('date', sort=True):
            if expiry_config:
                self.required_windows=required_windows(False)
                if date in entry_dates and expiry_config.expiry_entry_mode=='btst':
                    self.required_windows=dict(underlying=['09:15','15:29'],options=['09:15','15:29'],inclusive=True,
                        basis='Engine 5.4.0 full-session checks extended through the BTST entry-session close')
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
                regular=s[s.minute.between(9*60+15,15*60+29)]
                valid=regular[['open','high','low','close','volume']].apply(pd.to_numeric,errors='coerce')
                dense=(regular.timestamp.nunique()>=370 and not regular.timestamp.duplicated().any()
                       and np.isfinite(valid).all().all() and valid.volume.ge(0).all()
                       and valid[['open','high','low','close']].gt(0).all().all()
                       and valid.high.ge(valid[['open','low','close']].max(axis=1)).all()
                       and valid.low.le(valid[['open','high','close']].min(axis=1)).all())
                if dense:
                    audit['evidence'].append(dict(problem='Observed full-length non-weekday session accepted; required-window checks still apply',stream=instrument,
                        calendar_verification='NSE Budget circular CMTR65729 / FAOP72352' if date in ('2025-02-01','2026-02-01') else 'Observed session evidence only; calendar identity unverified'))
                else:audit['reasons'].append(dict(problem='calendar: no regular index session', stream=instrument))
            stream = s.instrument.iloc[0]
            reasons, notes = stream_check(s, date, stream, self.required_windows['underlying'], (stream,date) in flagged)
            rejected, evidence = rejected_evidence(self.root, records, date, stream, self.required_windows['underlying'])
            audit['reasons'].extend(reasons+rejected); audit['evidence'].extend(notes+evidence)
            repair_frames=[s]
            option_map, rolling, option_meta, quotes = {}, {}, {}, {}
            if not self.mcx:
                od = option_days.get(date, pd.DataFrame())
                for side, strike in ((side, strike) for side in ('CE','PE') for strike in ('ATM','ATM-1','ATM+1')):
                    stream = f'{instrument}_{side}_{strike}_WEEK1'
                    g = od[od.instrument.eq(stream)].copy() if len(od) else od.copy()
                    reasons, notes = stream_check(g, date, stream, self.required_windows['options'], (stream,date) in flagged)
                    rejected, evidence = rejected_evidence(self.root, records, date, stream, self.required_windows['options'])
                    audit['reasons'].extend(reasons+rejected); audit['evidence'].extend(notes+evidence)
                    if g.empty: continue
                    repair_frames.append(g)
                    g['minute'] = g.timestamp.dt.hour*60+g.timestamp.dt.minute
                    rolling[side, strike] = {int(row.minute): float(row.strike) for row in g.itertuples()}
                    for row in g.itertuples():
                        key = (int(row.minute), side, float(row.strike))
                        value = (row.open, row.high, row.low, row.close, row.volume)
                        meta = (float(row.oi),float(row.iv),float(row.spot))
                        quotes.setdefault(key, {}).setdefault((value, meta), set()).add(stream)
                begin, finish = map(minute, self.required_windows['options'])
                for key, variants in quotes.items():
                    if len(variants) > 1:
                        affected = sorted(set().union(*variants.values()))
                        required = begin <= key[0] <= finish
                        evidence = dict(problem='Conflicting same-strike candles', streams=affected,
                                        timestamps=[(s.timestamp.iloc[0].normalize()+pd.Timedelta(minutes=key[0])).isoformat()],
                                        side=key[1], strike=key[2], outside_required_window=not required)
                        audit['reasons' if required else 'evidence'].append(evidence)
                        # Never pick a rolling stream's disputed OHLCV/metadata,
                        # including historical quotes outside the run window.
                        continue
                    value, meta = next(iter(variants))
                    option_map[key], option_meta[key] = value, meta
            repaired=[];price_sources={}
            for frame in repair_frames:
                if 'candle_source' not in frame:continue
                window=self.required_windows['options'] if 'option_type' in frame else self.required_windows['underlying']
                lo,hi=[pd.Timestamp(f'{date} {v}',tz=IST) for v in window]
                rows=frame[frame.timestamp.between(lo,hi)&frame.candle_source.isin(['synthetic_ffill','quarantine_volume_zero'])]
                repaired.extend(dict(stream=r.instrument,timestamp=r.timestamp.isoformat(),method=r.candle_source) for r in rows.itertuples())
                for row in rows.itertuples():
                    key=int(row.timestamp.hour*60+row.timestamp.minute) if self.mcx else (int(row.timestamp.hour*60+row.timestamp.minute),getattr(row,'option_type',None),float(getattr(row,'strike',0)))
                    price_sources[key]=row.candle_source
            audit['repaired_inputs']=repaired
            if repaired:audit['evidence'].append(dict(problem='Run includes repaired inputs; synthetic candles are not observed quotes',count=len(repaired)))
            if audit['reasons']:
                self.exclusions.append(dict(date=date, instrument=instrument, contract=contract if self.mcx else None,
                                            reason='; '.join(sorted({r['problem'] for r in audit['reasons']}))))
                continue
            audit['status'] = 'eligible'
            self.days[date] = dict(spot=s, prices={int(r.minute): (r.open,r.high,r.low,r.close,r.volume) for r in s.itertuples()},
                                  oi={int(r.minute):float(getattr(r,'oi',np.nan)) for r in s.itertuples()},
                                  options=option_map, option_meta=option_meta, rolling=rolling, contract=s.instrument.iloc[0],
                                  timestamp=s.timestamp.iloc[0].normalize(),repaired_inputs=repaired,price_sources=price_sources)
        # An all-excluded request still has a useful eligibility report and zero
        # executions; do not erase the evidence with a generic loader exception.
        current = require_current_audit(self.root)
        if any(current[k] != self.quality[k] for k in ('manifest_fingerprint','file_fingerprint','calendar_fingerprint')):
            raise ValueError('Lake evidence changed while loading. Run a local quality check and reload before backtesting.')
        self.fingerprint = hashlib.sha256(self.fingerprint.encode()+json.dumps(self.eligibility, sort_keys=True).encode()).hexdigest()[:16]

    def features(self, timeframe):
        if timeframe not in self._features:
            self._features[timeframe] = {d: make_bars(day['spot'], timeframe) for d,day in self.days.items()}
        return self._features[timeframe]

    def research_features(self, timeframe):
        key = ('research', timeframe)
        if key not in self._features:
            # Carry causal indicator warm-up across sessions of the same contract.
            # VWAP still resets daily. Never join different MCX contracts.
            groups = {}
            for date, day in self.days.items():
                contract = day['contract'] if self.mcx else self.instrument
                groups.setdefault(contract, []).append((date, day['spot']))
            result = {}
            for sessions in groups.values():
                bars = pd.concat([make_bars(f, timeframe) for _, f in sorted(sessions)], ignore_index=True)
                bars = research_indicators(bars)
                for date, frame in bars.groupby(bars.timestamp.dt.strftime('%Y-%m-%d')):
                    result[date] = frame.reset_index(drop=True)
            self._features[key] = result
        return self._features[key]

    def price(self, day, at, side, strike):
        return day['prices'].get(at) if self.mcx else day['options'].get((at, side, strike))

    def events(self, c, date):
        """Each event contains an entry minute, side, fixed strike, trigger, ATR."""
        key = (c.strategy,c.timeframe,c.strike,c.orb_minutes,c.anchor,c.mode,c.breakout_pct,c.entry_start,c.entry_end,c.adx_min,
               c.option_lookback,c.volume_multiple,c.oi_mode,c.oi_min_change_pct,c.atr_expansion,c.compression_ratio,c.momentum_lookback,c.momentum_threshold,date)
        if key in self._events:
            return self._events[key]
        if c.strategy in STRATEGIES:
            self._events[key] = research_events(self, c, date)
            return self._events[key]
        if c.strategy in HIGH_STRATEGIES:
            self._events[key]=high_edge_events(self,c,date)
            return self._events[key]
        day = self.days[date]
        result = []
        if c.strategy == LEGACY_STRATEGIES[2]:
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
                warmup = 1 if c.strategy == LEGACY_STRATEGIES[1] else 20
                if i < warmup or at < (max(1050,minute(c.entry_start)) if self.mcx else minute(c.entry_start)) or at > (min(1350,minute(c.entry_end)) if self.mcx else minute(c.entry_end)):
                    continue
                side = None
                if c.strategy == LEGACY_STRATEGIES[0]:
                    if row.adx >= c.adx_min:
                        if row.ema9>row.ema21 and row.trend>0 and row.close>row.vwap: side='CE'
                        elif row.ema9<row.ema21 and row.trend<0 and row.close<row.vwap: side='PE'
                elif c.strategy in (LEGACY_STRATEGIES[1],LEGACY_STRATEGIES[4]):
                    if at <= range_end: continue
                    if row.close>range_high: side='LONG' if self.mcx else 'CE'
                    elif row.close<range_low: side='SHORT' if self.mcx else 'PE'
                elif c.strategy == LEGACY_STRATEGIES[3]:
                    proximity = max(row.atr*0.25,row.close*0.0003)
                    if row.close>row.ema50 and row.low<=row.vwap+proximity and row.close>row.vwap and row.close>row.open and row.close-row.low>(row.high-row.low)*0.6: side='CE'
                    elif row.close<row.ema50 and row.high>=row.vwap-proximity and row.close<row.vwap and row.close<row.open and row.high-row.close>(row.high-row.low)*0.6: side='PE'
                if side is None: continue
                strike = 0 if self.mcx else day['rolling'][side,c.strike].get(at-1)
                if strike is None: continue
                if c.strategy == LEGACY_STRATEGIES[1]:
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
    if c.strategy=='expiry_day':
        from quant_expiry import expiry_steps
        return (yield from expiry_steps(data,c,date,event,trade_id,budget=budget,entry_equity=entry_equity))
    if not data.mcx and c.strategy in BOOK_STRATEGIES[:3]:
        from quant_book import basket_steps
        return (yield from basket_steps(data,c,date,event,trade_id,budget,entry_equity))
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
    atr_risk = data.mcx and (c.risk_basis == 'legacy' or c.strategy in BOOK_STRATEGIES)
    stop_distance = atr*c.atr_sl if atr_risk else entry*c.sl_pct/100
    if data.mcx and c.strategy in BOOK_STRATEGIES:
        stop_distance=min(stop_distance,entry*c.sl_pct/100)
    if stop_distance<=0: return None, []
    target_distance = stop_distance*c.reward_risk if atr_risk else entry*c.target_pct/100
    stop, target = entry-direction*stop_distance, entry+direction*target_distance
    initial_stop = stop
    best = entry
    trace = []
    modeled_bars=0
    modeled_wing_fallback=False
    previous=data.price(day,at-1,side,strike)
    final_source='observed'
    finish = 1395 if data.mcx else 920
    exit_price, exit_at, reason = None, at, 'unresolved'
    force_exit=None
    yield dict(minute=at-1,net_mtm=-reserve,reserved_capital=allocated+reserve)
    for current in range(at,finish+1):
        row = data.price(day,current,side,strike)
        source=dict(price_source='observed',model_neighbor_strike=np.nan,model_iv_pct=np.nan,model_expiry_assumption='')
        stored_source=day.get('price_sources',{}).get(current if data.mcx else (current,side,strike))
        if stored_source=='synthetic_ffill':
            if c.price_policy=='strict_observed':raise MissingObservedPrice('Strict-observed execution encountered a synthetic repaired candle')
            source['price_source']='synthetic_ffill';modeled_bars+=1
        elif stored_source=='quarantine_volume_zero':source['price_source']='observed_price_volume_corrected'
        if row is None:
            if data.mcx or c.price_policy=='strict_observed':
                raise MissingObservedPrice(f'{date} {side} strike {strike} missing at minute {current}; strict run is not rankable')
            try:
                row,source=continuation_candle(data,day,current,side,strike,previous)
            except MissingObservedPrice as exc:
                raise MissingObservedPrice(f'{date} {current//60:02d}:{current%60:02d} {side} {strike:g}: {exc}') from exc
            modeled_bars+=1
        modeled_wing_fallback |= bool(source.get('modeled_wing_fallback',False))
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
                modeled_wing_fallback=modeled_wing_fallback,
                contract_identity_verified=bool(data.mcx),
                verification_status='repaired_inputs' if day.get('repaired_inputs') else 'observed_candles_accounting_checked' if data.mcx else 'model_dependent' if modeled_bars else 'observed_strike_expiry_unverified',
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
    metrics['modeled_wing_fallback_trades']=int(closed.modeled_wing_fallback.sum()) if 'modeled_wing_fallback' in closed else 0
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


def persist_result(result, output=OUT):
    """Persist an already simulated session result, without rerunning it."""
    frames = result.get('_frames')
    if frames is None:
        raise ValueError('This session has no result tables to save. Run the backtest again.')
    path = Path(output)/'runs'/result['run_id']
    path.mkdir(parents=True, exist_ok=True)
    document = {k:v for k,v in result.items() if k != '_frames'}
    document['path'] = str(path.resolve())
    atomic_json(path/'eligibility.json', document['eligibility'])
    for name, frame in frames.items():
        temp = path/f'{name}.parquet.tmp'
        frame.to_parquet(temp, index=False)
        os.replace(temp, path/f'{name}.parquet')
    atomic_json(path/'result.json', document)  # completion marker last
    return document


def run_backtest(c, data=None, output=OUT, persist=True):
    c.validate()
    data=data or ResearchData(c.instrument,c.start,c.end,**({'expiry_config':c} if c.strategy=='expiry_day' else {}))
    if c.strategy=='expiry_day':
        from quant_expiry import check_prepared_windows
        check_prepared_windows(data,c)
    if (data.instrument,data.start,data.end)!=(c.instrument,c.start,c.end):
        raise ValueError('Prepared data does not match configuration window')
    if c.price_policy=='strict_observed' and any(e.get('method')=='synthetic_ffill' for d in data.days.values() for e in d.get('repaired_inputs',[])):
        raise MissingObservedPrice('Strict-observed run includes synthetic repaired inputs in a dependency window. Choose an unrepaired observed-data range or explicitly use the modeled-price policy.')
    identity=json.dumps(dict(config=asdict(c),engine=ENGINE_VERSION,pricing_revision='book-spread-continuity-v3',data=data.fingerprint),sort_keys=True)
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
                status='repaired_inputs' if any(d.get('repaired_inputs') for d in data.days.values()) else 'observed_candles_accounting_checked' if data.mcx else 'model_dependent' if metrics['modeled_trades'] else 'observed_strike_expiry_unverified',
                repaired_inputs=[r for d in data.days.values() for r in d.get('repaired_inputs',[])],
                repair_disclosure='Zero-volume corrections and synthetic flat candles retain provenance; eligibility is not proof of observed prices or full historical coverage.',
                verified_price_eligible=bool(data.mcx and not any(d.get('repaired_inputs') for d in data.days.values()) and metrics['unresolved_trades']==0 and len(trades)>0),
                limitations=LIMITATIONS,expiry_calendar_available=data.has_calendar,
                equity_basis='all entered trades closed; realized daily equity; modeled prices explicitly tagged',path=str(path.resolve()))
    underlying = pd.DataFrame()
    if c.strategy in STRATEGIES and len(trades) and hasattr(data, 'research_features'):
        features = data.research_features(c.timeframe)
        dates=set(trades.date)
        if c.strategy=='expiry_day': dates.update(trades.entry_time.dt.strftime('%Y-%m-%d'))
        underlying = pd.concat([features[d].assign(date=d) for d in sorted(dates) if d in features], ignore_index=True)
    if c.strategy=='expiry_day':
        result['limitations']=LIMITATIONS+['Expiry Day holds fixed CE+PE strikes overnight. WEEK1 has no verified expiry identity; calendar/recorded-reset labels do not establish that both sessions contain the same contracts. Combined thresholds use synchronized closes, exit next open; overnight gaps and costs can exceed stops.',
            'Period selects expiry exit sessions; optional prior-session loading is disclosed in the trade entry dates. Observed-reset mode requires recorded expiry/reset evidence, never a change in rolling ATM strike alone.']
    result['_frames'] = dict(trades=trades, equity=equity, monthly=monthly, stop_paths=pd.DataFrame(paths),
        skipped_entries=pd.DataFrame(skipped), excluded_sessions=pd.DataFrame(data.exclusions),
        daily_risk=pd.DataFrame(daily_risk), underlying=underlying)
    if persist:
        return persist_result(result, output)
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
