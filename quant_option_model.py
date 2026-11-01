"""Explicitly synthetic continuation of a missing fixed-strike candle.

This is a sensitivity/research model, NOT recovered market prices. Only data
known by the minute being modeled is read. No model is used for entry signals.
"""
import math
import pandas as pd
from quant_calendar import next_expiry


class MissingObservedPrice(ValueError):
    pass


def intrinsic(spot,strike,side):
    return max(0.0,spot-strike if side=='CE' else strike-spot)


def greeks(spot,strike,sigma,years,side):
    years=max(years,1/(365.25*24*60)); sigma=max(sigma,0.01)
    d1=(math.log(spot/strike)+0.5*sigma*sigma*years)/(sigma*math.sqrt(years))
    density=math.exp(-0.5*d1*d1)/math.sqrt(2*math.pi)
    delta=0.5*(1+math.erf(d1/math.sqrt(2))) - (0 if side=='CE' else 1)
    gamma=density/(spot*sigma*math.sqrt(years))
    vega=spot*density*math.sqrt(years)
    return delta,gamma,vega


def option_mark_state(day,at,row,state,open_only=False):
    """Recalibrate from an exact or modeled mark, including overnight baskets."""
    if at not in day.get('prices',{}):return  # observed-only providers need no model state
    field=0 if open_only else 3
    state.update(price=row[field],spot=day['prices'][at][field],
                 time=day['timestamp']+pd.Timedelta(minutes=at+(0 if open_only else 1)))


def continuous_wing(data,day,at,side,strike,state,target,expiry_decay=None,open_only=False):
    from quant_calendar import is_expiry_day
    if not state:raise MissingObservedPrice('Wing continuity requires a previous option mark and spot')
    band=sorted({k for mon in ('ATM-1','ATM','ATM+1')
                 if (k:=day.get('rolling',{}).get((side,mon),{}).get(at)) is not None})
    spacing=min(b-a for a,b in zip(band,band[1:]))
    direction=1 if side=='CE' else -1
    expiry=is_expiry_day(data.instrument,str(day['timestamp'].date())) if expiry_decay is None else expiry_decay
    session_open=day['timestamp']+pd.Timedelta(minutes=555)
    session_end=day['timestamp']+pd.Timedelta(minutes=930)
    def advance(spot,anchor,when):
        prior_spot=state['spot'];price=state['price']
        elapsed=max(0.,(when-max(state['time'],session_open)).total_seconds()/60)
        extrinsic=max(0.,price-intrinsic(prior_spot,strike,side))
        remaining=max(1.,(session_end-max(state['time'],session_open)).total_seconds()/60)
        theta=extrinsic*min(1.,elapsed/remaining) if expiry else extrinsic*.0002*elapsed
        midpoint=direction*((spot+prior_spot)/2-strike)/spacing
        delta=min(.92,max(.08,.50+.38*math.tanh(.65*midpoint)))
        move=direction*delta*(spot-prior_spot)
        predicted=price+move-theta
        # At most 10% anchor weight, further bounded by this bar's movement
        # and theta. A rolling-band change alone cannot destroy time value.
        budget=.1*abs(move)+theta*.1
        correction=min(budget,max(-budget,.1*(anchor-predicted)))
        value=max(.05,intrinsic(spot,strike,side),predicted+correction)
        state.update(price=value,spot=spot,time=when)
        return value
    spot=day['prices'][at];when=day['timestamp']+pd.Timedelta(minutes=at)
    op=advance(spot[0],target[0],when)
    cl=op if open_only else advance(spot[3],target[3],when+pd.Timedelta(minutes=1))
    return (op,max(op,cl),min(op,cl),cl,0),dict(price_source='model_stateful_wing_continuity',
        modeled_wing_fallback=True,model_neighbor_strike=None,model_iv_pct=None,
        model_expiry_assumption='session_linear_decay' if expiry else 'normal_intraday_theta',
        candle_basis='modeled_open_close_envelope')


def continuation_candle(data,day,at,side,strike,previous,state=None,expiry_decay=None,open_only=False):
    """Stateful out-of-band marks, or in-band delta/gamma near-strike drift.

    previous = previous minute's observed/model OHLCV. Volatility is taken from
    the *previous* minute. Current near-strike OHLC informs model OHLC, with
    extrema explicitly approximate because co-movement order is unobserved.
    """
    wing = wing_fallback(day, at, side, strike)
    if wing is not None:
        if state is None and previous is not None and at-1 in day['prices']:
            state={};option_mark_state(day,at-1,previous,state)
        if state is not None:return continuous_wing(data,day,at,side,strike,state,wing[0],expiry_decay,open_only)
        return wing
    spot_prev=day['prices'].get(at-1)
    spot_now=day['prices'].get(at)
    if previous is None or spot_prev is None or spot_now is None:
        raise MissingObservedPrice('Continuation requires previous mark and contiguous underlying candles')
    candidates=[]
    for mon in ('ATM','ATM-1','ATM+1'):
        k=day['rolling'][side,mon].get(at)
        if k==strike: continue  # also prevents target leakage in holdout diagnostics
        now=day['options'].get((at,side,k)); old=day['options'].get((at-1,side,k))
        meta=day['option_meta'].get((at-1,side,k))
        if now and old and meta and math.isfinite(meta[1]) and meta[1]>0:
            candidates.append((abs(k-strike),k,now,old,meta))
    if not candidates:
        raise MissingObservedPrice('No contemporaneous neighboring strike with valid lagged IV; run cannot be completed honestly')
    _,near,now,old,meta=min(candidates,key=lambda r:r[0])
    sigma=meta[1]/100  # Dhan IV is in percent; never silently infer units.
    expiry=pd.Timestamp(next_expiry(data.instrument,str(day['timestamp'].date())),tz=day['timestamp'].tz)+pd.Timedelta(hours=15,minutes=30)
    ts=day['timestamp']+pd.Timedelta(minutes=at)
    years=max((expiry-ts).total_seconds(),60)/(365.25*86400)
    s0=spot_prev[3]
    delta,gamma,vega=greeks(s0,strike,sigma,years,side)
    nd,ng,nv=greeks(s0,near,sigma,years,side)
    vega_ratio=min(2.0,max(0.0,vega/max(nv,1e-8)))
    previous_extrinsic=max(0,previous[3]-intrinsic(s0,strike,side))
    def mark(s,near_price):
        ds=s-s0
        exact_intrinsic_change=intrinsic(s,strike,side)-intrinsic(s0,strike,side)
        extrinsic_delta_gamma=delta*ds+0.5*gamma*ds*ds-exact_intrinsic_change
        near_residual=near_price-old[3]-nd*ds-0.5*ng*ds*ds
        value=intrinsic(s,strike,side)+max(0,previous_extrinsic+extrinsic_delta_gamma+vega_ratio*near_residual)
        return max(0.05,min(value,s if side=='CE' else strike))
    op=mark(spot_now[0],now[0]); cl=mark(spot_now[3],now[3])
    # Pair extrema by option direction. These are synthetic envelopes, not fills.
    a=mark(spot_now[1] if side=='CE' else spot_now[2],now[1])
    b=mark(spot_now[2] if side=='CE' else spot_now[1],now[2])
    return (op,max(op,cl,a,b),min(op,cl,a,b),cl,0),dict(
        price_source='model_delta_gamma_continuity',model_neighbor_strike=near,
        model_iv_pct=meta[1],model_expiry_assumption=expiry.date().isoformat())


def wing_fallback(day, at, side, strike):
    """Out-of-band wing anchor/diagnostic, not an open position's direct fill.

    No lagged IV, previous option mark, future quote or calendar expiry is
    needed. Time value is capped by one observed wing spacing and attenuated
    with distance. This is a deterministic research estimate, never a quote.
    Open position callers use continuous_wing with persistent mark/spot state.
    Exact observed candles always win (including a strike in another offset).
    """
    if (at, side, strike) in day.get('options', {}): return None
    spot = day.get('prices', {}).get(at)
    if spot is None: return None
    band = sorted({k for mon in ('ATM-1', 'ATM', 'ATM+1')
                   if (k := day.get('rolling', {}).get((side, mon), {}).get(at)) is not None and math.isfinite(k)})
    if len(band)<2 or band[0]<=strike<=band[-1]: return None
    observed = []
    for mon in ('ATM-1', 'ATM', 'ATM+1'):
        k = day.get('rolling', {}).get((side, mon), {}).get(at)
        row = day.get('options', {}).get((at, side, k))
        source = day.get('price_sources', {}).get((at, side, k))
        if k is not None and row and source != 'synthetic_ffill' and all(math.isfinite(v) and v > 0 for v in row[:4]):
            observed.append((k, mon, row))
    wings = [(k, row) for k, mon, row in observed if mon in ('ATM-1', 'ATM+1')]
    if not wings: return None
    near, row = min(wings, key=lambda x: (abs(x[0]-strike), x[0]))
    width = min(b-a for a,b in zip(band, band[1:]))
    decay = math.exp(-abs(near-strike)/width)
    def mark(s, premium):
        tv = min(width, max(0., premium-intrinsic(s, near, side))) * decay
        return max(.05, intrinsic(s, strike, side)+tv)
    op, cl = mark(spot[0], row[0]), mark(spot[3], row[3])
    hi = mark(spot[1] if side=='CE' else spot[2], row[1])
    lo = mark(spot[2] if side=='CE' else spot[1], row[2])
    return (op, max(op, cl, hi, lo), min(op, cl, hi, lo), cl, 0), dict(
        price_source='model_intrinsic_wing_fallback', modeled_wing_fallback=True,
        model_neighbor_strike=near, model_iv_pct=None, model_expiry_assumption='none')
