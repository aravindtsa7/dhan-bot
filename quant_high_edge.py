"""Causal H1-H4 signals. Option confirmations use observed fixed-strike data."""
import math
import numpy as np

HIGH_STRATEGIES = ['Option_VWAP_OI_Momentum','Dual_Leg_Straddle_Gamma_Breakout',
                   'Morning_ORB_Retest_Sniper','MCX_US_Open_Inventory_Breakout',
                   'Expiry_Day_Gamma_Explosion','Natural_Gas_Crude_High_Beta_Momentum']


def option_statistics(day):
    """One accumulator per side + fixed strike; gaps invalidate full-session VWAP."""
    accum={}; stats={}
    for (at,side,strike),row in sorted(day['options'].items()):
        pv,vol,count,first=accum.get((side,strike),(0.,0.,0,at))
        pv+=(row[1]+row[2]+row[3])/3*row[4]; vol+=row[4]; count+=1
        accum[side,strike]=(pv,vol,count,first)
        stats[at,side,strike]=dict(option_vwap=pv/vol if vol>0 else np.nan,
             vwap_full_session=first==555 and count==at-555+1,observed_minutes=count)
    return stats


def option_confirmation(day,at,side,strike,c,n=1):
    """at is the next fill minute; nothing at/after at is read here.

    Volume/OI confirmation is minute-based to retain meaningful morning signals
    with 15m spot charts. Option breakout high spans N complete signal bars.
    """
    key=(at,side,strike,c.option_lookback,n)
    cache=day.setdefault('option_context_cache',{})
    if key not in cache:
        row=day['options'].get((at-1,side,strike))
        past=[day['options'].get((t,side,strike)) for t in range(at-21,at-1)]
        highs=[day['options'].get((t,side,strike)) for t in range(at-n*(c.option_lookback+1),at-n)]
        meta=day['option_meta'].get((at-1,side,strike))
        oldmeta=day['option_meta'].get((at-6,side,strike))
        context=None
        if row and all(p is not None for p in past+highs) and meta and oldmeta and oldmeta[0]>0:
            mean=float(np.mean([p[4] for p in past]))
            if mean>0 and all(math.isfinite(v) for v in (meta[0],oldmeta[0])):
                context=dict(close=row[3],volume_ratio=row[4]/mean,
                             oi_change_pct=100*(meta[0]/oldmeta[0]-1),
                             option_previous_high=max(p[1] for p in highs),
                             **day['option_stats'][at-1,side,strike])
        cache[key]=context
    ctx=cache[key]
    if ctx is None or ctx['volume_ratio']<c.volume_multiple: return None
    change=ctx['oi_change_pct']
    if c.oi_mode=='rising' and change<c.oi_min_change_pct: return None
    if c.oi_mode=='unwinding' and change>-c.oi_min_change_pct: return None
    if c.oi_mode=='either' and abs(change)<c.oi_min_change_pct: return None
    return ctx


def high_edge_events(data,c,date):
    from quant_backtester import minute
    from quant_calendar import is_expiry_day
    day=data.days[date]; n=int(c.timeframe[:-1]); bars=data.features(c.timeframe)[date]
    if c.strategy==HIGH_STRATEGIES[4] and not is_expiry_day(c.instrument,date): return []
    if 'option_stats' not in day and not data.mcx: day['option_stats']=option_statistics(day)
    events=[]; broken={'CE':False,'PE':False}
    start=minute(c.entry_start); end=minute(c.entry_end)
    spot=day['prices']
    anchor=minute(c.anchor)
    refs={}
    if c.strategy==HIGH_STRATEGIES[1]:
        if anchor>=810 and not is_expiry_day(c.instrument,date): return []
        for side in ('CE','PE'):
            k=day['rolling'][side,'ATM'].get(anchor-1)
            r=day['options'].get((anchor-1,side,k))
            if r: refs[side]=(k,r[3])
        if len(refs)!=2: return []
    opening=1020 if data.mcx else 555
    orb_end=1050 if data.mcx else 570
    opening_rows=[p for t,p in spot.items() if opening<=t<orb_end]
    if not opening_rows: return []
    oh=max(p[1] for p in opening_rows); ol=min(p[2] for p in opening_rows)
    for i,row in enumerate(bars.itertuples()):
        at=row.timestamp.hour*60+row.timestamp.minute+n
        if at<start or at>end or (not data.mcx and 690<=at<795 and c.strategy!=HIGH_STRATEGIES[4]): continue
        if i<5 or row.adx<c.adx_min: continue
        baseline=float(row.atr_baseline) if hasattr(row,'atr_baseline') else float(bars.atr.iloc[max(0,i-20):i].mean())
        if not baseline>0 or row.atr<baseline*c.atr_expansion: continue
        side=None; k=None
        if c.strategy==HIGH_STRATEGIES[5]:
            if not 1050<=at<=1290 or i<20 or row.atr<baseline*max(1.5,c.atr_expansion): continue
            history=bars.iloc[i-20:i]
            if history.volume.mean()<=0 or row.volume<c.volume_multiple*history.volume.mean(): continue
            box=history.tail(c.option_lookback)
            if row.close>box.high.max() and row.close>row.open: side='LONG'
            elif row.close<box.low.min() and row.close<row.open: side='SHORT'
            if side:
                events.append((at,side,0,None,row.atr))
                day.setdefault('signal_evidence',{})[c.strategy,c.timeframe,at,side,0]=dict(volume_ratio=float(row.volume/history.volume.mean()),atr_ratio=float(row.atr/baseline))
            continue
        if c.strategy==HIGH_STRATEGIES[3]:
            if not 1050<=at<=1290: continue
            before=[p for t,p in spot.items() if 990<=t<1020]
            if len(before)<30: continue
            earlier_range=max(p[1] for p in before)-min(p[2] for p in before)
            if oh-ol>earlier_range*c.compression_ratio: continue
            if row.close>oh and row.close>row.open: side='LONG'
            elif row.close<ol and row.close<row.open: side='SHORT'
            if side: events.append((at,side,0,None,row.atr))
            continue
        if c.strategy==HIGH_STRATEGIES[4]:
            if not 780<=at<=870: continue
            # The compression box excludes the completed breakout bar.
            box=[p for t,p in spot.items() if at-n-30<=t<at-n]
            if len(box)!=30: continue
            bh=max(p[1] for p in box); bl=min(p[2] for p in box)
            if bh-bl>baseline*c.compression_ratio*3: continue
            if row.close>bh and row.close>row.open: side='CE'
            elif row.close<bl and row.close<row.open: side='PE'
        elif c.strategy==HIGH_STRATEGIES[0]:
            if row.ema9>row.ema21 and row.trend>0: side='CE'
            elif row.ema9<row.ema21 and row.trend<0: side='PE'
        elif c.strategy==HIGH_STRATEGIES[2]:
            if not 570<=at<=675: continue
            # Breakout must already have happened on an earlier completed bar.
            tolerance=row.atr*0.2
            if broken['CE'] and row.low<=oh+tolerance and row.close>oh and row.close>row.open: side='CE'
            elif broken['PE'] and row.high>=ol-tolerance and row.close<ol and row.close<row.open: side='PE'
            broken['CE'] |= row.close>oh
            broken['PE'] |= row.close<ol
        elif c.strategy==HIGH_STRATEGIES[1]:
            if at<=anchor: continue
            compressed=[p for t,p in spot.items() if max(555,anchor-15)<=t<anchor]
            if len(compressed)<10: continue
            ch=max(p[1] for p in compressed); cl=min(p[2] for p in compressed)
            if ch-cl>row.atr*c.compression_ratio*3: continue
            # Confirmation at a completed close, then next-open execution: using
            # this bar's final volume at an earlier stop touch would look ahead.
            candidates=[]
            for s,(fixed,premium) in refs.items():
                opt=day['options'].get((at-1,s,fixed))
                if opt and opt[3]>=premium*(1+c.breakout_pct/100) and ((s=='CE' and row.close>ch) or (s=='PE' and row.close<cl)):
                    candidates.append((s,fixed))
            if len(candidates)==1: side,k=candidates[0]
        if side is None: continue
        if k is None: k=day['rolling'][side,c.strike].get(at-1)
        if k is None: continue
        if c.strategy==HIGH_STRATEGIES[4]:
            atm=day['rolling'][side,'ATM'].get(at-1)
            if atm is None or (side=='CE' and k<atm) or (side=='PE' and k>atm): continue
        ctx=option_confirmation(day,at,side,k,c,n if c.strategy==HIGH_STRATEGIES[0] else 1)
        if ctx is None: continue
        if c.strategy==HIGH_STRATEGIES[4] and ctx['close']<=ctx['option_previous_high']: continue
        if c.strategy==HIGH_STRATEGIES[0]:
            if not ctx['vwap_full_session'] or not ctx['close']>ctx['option_vwap'] or not ctx['close']>ctx['option_previous_high']: continue
        elif c.strategy==HIGH_STRATEGIES[2]:
            history=[day['options'].get((t,side,k)) for t in range(555,570)]
            if any(p is None for p in history) or ctx['close']<=max(p[1] for p in history): continue
        events.append((at,side,k,None,row.atr))
        day.setdefault('signal_evidence',{})[c.strategy,c.timeframe,at,side,k]=ctx
    return events
