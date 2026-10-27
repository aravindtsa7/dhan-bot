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


def continuation_candle(data,day,at,side,strike,previous):
    """Exact intrinsic change plus delta/gamma extrinsic and near-strike drift.

    previous = previous minute's observed/model OHLCV. Volatility is taken from
    the *previous* minute. Current near-strike OHLC informs model OHLC, with
    extrema explicitly approximate because co-movement order is unobserved.
    """
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
