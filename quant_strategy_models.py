"""Causal activity proxies and explicit basket execution/model assumptions."""
import math
import numpy as np

DIRECTIONAL_OPTIONS=('vwap_ema_pullback','orb_volume_breakout','supertrend_adx_trend',
                     'book_vertical_spread','book_vol_oi_momentum')


def futures_risk(c,entry,atr):
    """ATR-scaled risk: UI TP30/SL15 maps to 2.2/1.2 entry ATR."""
    return atr*1.2*c.sl_pct/15,atr*2.2*c.target_pct/30


def profit_lock(c,entry,best,stop,direction=1,atr=None):
    if c.exit_mode!='trailing_sl':return stop
    gain=direction*(best-entry)
    if atr is not None:
        if gain<1.2*atr:return stop
        jump=.25*atr
        locked=.2*atr+math.floor((gain-1.2*atr+1e-10)/jump)*jump
    else:
        if c.strategy not in DIRECTIONAL_OPTIONS or c.exit_mode=='end_of_day' or gain<.18*entry:return stop
        jump=entry*c.trail_jump_pct/100
        locked=.10*entry+math.floor((gain-.18*entry+1e-10)/jump)*jump
    proposed=entry+direction*locked
    return max(stop,proposed) if direction==1 else min(stop,proposed)


def basket_slippage(c, raw):
    tick=c.tick_size or .05
    proportional=math.floor(.0125*raw/tick+.5+1e-10)*tick
    return min(c.slippage,.50,max(.05,proportional))


def rolling_activity(day, at, n, lookback, side=None):
    """Complete current/prior bars of active ATM activity, not option returns."""
    volumes=[];oi=[]
    for begin in range(at-(lookback+1)*n,at,n):
        volume=0.;last_oi=[]
        for minute in range(begin,begin+n):
            for option in ([side] if side else ['CE','PE']):
                k=day.get('rolling',{}).get((option,'ATM'),{}).get(minute)
                row=day.get('options',{}).get((minute,option,k))
                if row is None or day.get('price_sources',{}).get((minute,option,k))=='synthetic_ffill':return None
                if not math.isfinite(row[4]) or row[4]<0:return None
                volume+=row[4]
                if minute==begin+n-1:last_oi.append(day.get('option_meta',{}).get((minute,option,k),(np.nan,))[0])
        volumes.append(volume);oi.append(sum(last_oi))
    return volumes,oi


def activity_frame(day):
    """Copy underlying minutes; never change audited input partitions."""
    f=day['spot'].copy();f['activity_proxy']=False
    for idx,row in f.iterrows():
        if row.volume!=0:continue
        at=row.timestamp.hour*60+row.timestamp.minute
        activity=rolling_activity(day,at+1,1,0)
        if activity is not None:
            f.at[idx,'volume']=activity[0][0];f.at[idx,'activity_proxy']=True
    return f


def fresh_pullback(previous,row,bull):
    """A fresh EMA touch/cross and directional body with strengthening ADX."""
    if not row.adx>previous.adx or row.adx<18:return False
    touch=previous.low<=previous.ema9<=previous.high
    if bull:
        cross=previous.close<=previous.ema9 and row.close>row.ema9
        return row.close>row.open and ((touch and row.close>previous.high) or cross)
    cross=previous.close>=previous.ema9 and row.close<row.ema9
    return row.close<row.open and ((touch and row.close<previous.low) or cross)


def spread_curve(spot, origin_spot, origin_value, strike, width, direction, scale):
    """Entry-anchored smooth moneyness model; not a recovered market quote/IV.

    Integrate a .28 -> .42 sigmoid delta, using observed extrinsic as its
    transition scale. The favorable intrinsic-aware floor is entry anchored.
    """
    scale=max(width/6,min(width*2,scale))
    def potential(s):
        m=direction*(s-strike);x=max(0.,m)/scale
        log_cosh=x+math.log1p(math.exp(-2*x))-math.log(2)
        return .28*m+.14*scale*log_cosh
    value=origin_value+potential(spot)-potential(origin_spot)
    move=direction*(spot-origin_spot)
    if move>0:
        intrinsic=min(width,max(0.,direction*(spot-strike)))
        value=max(value,min(width-.05,origin_value+.32*move,.85*intrinsic))
    return min(width-.05,max(.05,value))
