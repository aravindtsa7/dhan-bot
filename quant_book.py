"""Book payoff structures with causal intraday filters and atomic basket fills.

References: Kakushadze & Serur, SSRN 3247865, sections 2.6/2.9,
2.44, 2.34/2.35, 10.3.1/10.4. Intraday filters are research adaptations.
"""
import json
import math
import numpy as np
import pandas as pd

from quant_option_model import continuation_candle, MissingObservedPrice, intrinsic, option_mark_state
from quant_strategy_models import basket_slippage, rolling_activity, fresh_pullback, spread_curve, profit_lock


SIDE_LABELS = {'BULL_CALL_SPREAD':'BULL CALL SPREAD', 'BEAR_PUT_SPREAD':'BEAR PUT SPREAD',
               'IRON_BUTTERFLY':'IRON BUTTERFLY', 'STRAP':'STRAP (2CE:1PE)', 'STRIP':'STRIP (1CE:2PE)'}

OBSERVED_SOURCES = ('observed', 'observed_price_volume_corrected')
RAW_BASKET_TOLERANCE = 1e-7  # floating-point arithmetic only; never a quote-noise allowance


class InvalidBasketObservation(MissingObservedPrice):
    """Invalid evidence invalidates the simulation; callers must not skip its trade."""
    def __init__(self, message, evidence):
        self.evidence = dict(evidence, simulation_invalidated=True,
                             contract_identity_verified=False,
                             identity_status='strike labels observed; dated expiry/contract identity unverified')
        super().__init__(message + '; simulation invalidated; raw evidence=' +
                         json.dumps(self.evidence, allow_nan=False, default=str))


def validate_raw_basket_rows(day, at, side, legs, rows, sources, open_only=False,
                             context=None):
    """Check synchronized raw OPEN/CLOSE evidence, never per-leg OHLC extremes.

    Zero/width are valid raw boundaries. Execution prices (adverse ticks,
    slippage and fees) have no such guarantee. A mixed modeled/observed path
    supplies only observed pairs; its missing legs are not raw observations.
    Structural consistency is conditional on the supplied strike labels and
    same-expiry assumption, not verification of dated contract identity.
    """
    if side not in ('BULL_CALL_SPREAD','BEAR_PUT_SPREAD','IRON_BUTTERFLY'):
        return
    pairs=[(0,2),(1,3)] if side=='IRON_BUTTERFLY' else [(0,1)]
    width=abs(legs[pairs[0][0]]['strike']-legs[pairs[0][1]]['strike'])
    fields=(0,) if open_only else (0,3)
    for field in fields:
        observed=[source in OBSERVED_SOURCES for source in sources]
        pair_values=[rows[h][field]-rows[l][field] if observed[h] and observed[l] else None
                     for h,l in pairs]
        full=all(observed)
        value=(-sum(pair_values) if side=='IRON_BUTTERFLY' else pair_values[0]) if full else None
        invalid_pair=any(v is not None and (not math.isfinite(v) or
            v < -RAW_BASKET_TOLERANCE or v > width+RAW_BASKET_TOLERANCE) for v in pair_values)
        invalid_joint=full and side=='IRON_BUTTERFLY' and (
            not math.isfinite(value) or value < -width-RAW_BASKET_TOLERANCE or value > RAW_BASKET_TOLERANCE)
        if invalid_pair or invalid_joint:
            def finite(v):
                return float(v) if math.isfinite(float(v)) else str(v)
            stamp=day.get('timestamp','')
            try: stamp=str(pd.Timestamp(stamp)+pd.Timedelta(minutes=at))
            except (TypeError,ValueError): stamp=str(stamp)
            evidence=dict(timestamp=stamp,minute=int(at),side=side,
                field='open' if field==0 else 'close',raw_basket_value=finite(value) if value is not None else None,
                raw_pair_values=[None if v is None else finite(v) for v in pair_values],width=float(width),
                raw_bounds=[-float(width),0.] if side=='IRON_BUTTERFLY' else [0.,float(width)],
                tolerance=RAW_BASKET_TOLERANCE,
                legs=[dict(side=leg['side'],strike=float(leg['strike']),ratio=leg.get('ratio'),
                    price_source=source,raw_price=finite(row[field]) if known else None,
                    raw_ohlcv=[finite(v) for v in row] if known else None)
                    for leg,row,source,known in zip(legs,rows,sources,observed)])
            evidence.update(context or {})
            raise InvalidBasketObservation(
                f'{day.get("timestamp", "")} minute {at}, field {field}: raw {side} observed wing bound violation',evidence)


def leg_specs(day, at, side, anchor):
    """Resolve every strike at signal close, never using entry-bar rolling ATM."""
    definitions = {
        'BULL_CALL_SPREAD':[('CE','ATM',1),('CE','ATM+1',-1)],
        'BEAR_PUT_SPREAD':[('PE','ATM',1),('PE','ATM-1',-1)],
        'IRON_BUTTERFLY':[('CE','ATM',-1),('PE','ATM',-1),('CE','ATM+1',1),('PE','ATM-1',1)],
        'STRAP':[('CE','ATM',2),('PE','ATM',1)],
        'STRIP':[('CE','ATM',1),('PE','ATM',2)],
    }
    legs=[]
    for option, offset, ratio in definitions[side]:
        strike=day.get('rolling',{}).get((option,offset),{}).get(at-1)
        if strike is None: return []
        legs.append(dict(side=option,strike=float(strike),ratio=ratio))
    if side=='BULL_CALL_SPREAD' and legs[1]['strike']<=legs[0]['strike']: return []
    if side=='BEAR_PUT_SPREAD' and legs[1]['strike']>=legs[0]['strike']: return []
    if side=='IRON_BUTTERFLY':
        if not legs[3]['strike']<legs[0]['strike']==legs[1]['strike']<legs[2]['strike']: return []
        if not math.isclose(legs[2]['strike']-legs[0]['strike'],legs[0]['strike']-legs[3]['strike']): return []
    return legs


def activity(data, day, bars, i, at, side, strike, c):
    """Prior T complete bars as baseline. Missing OI is disclosed, never invented."""
    n=int(c.timeframe[:-1]); t=c.momentum_lookback
    if i<t: return None
    volumes=bars.volume.iloc[i-t:i+1].to_numpy(dtype=float)
    basis='underlying'; oi=[]
    if not data.mcx:
        history=rolling_activity(day,at,n,t,side)
        if history is None:return None
        volumes,oi=history;basis='rolling_atm_option'
    else:
        oi=[day.get('oi',{}).get(k,np.nan) for k in range(at-t*n-1,at,n)]
    baseline=float(np.mean(volumes[:-1]))
    if baseline<=0 or volumes[-1]<=0: return None
    v=math.log(volumes[-1]/baseline)
    oi=np.array(oi,dtype=float)
    available=bool(len(oi)==t+1 and np.isfinite(oi).all() and (oi>0).all())
    u=math.log(oi[-1]/oi[:-1].mean()) if available else None
    return dict(signal_log_volume=v,signal_log_oi=u,signal_oi_available=available,
                signal_volume_basis=basis)


def book_events(data,c,date,confirm_volume=True):
    from quant_backtester import minute
    day=data.days[date]
    features=getattr(data,'research_features',data.features)
    bars=features(c.timeframe)[date]
    n=int(c.timeframe[:-1]); t=c.momentum_lookback
    returns=np.log(bars.close/bars.close.shift())
    sigma=returns.rolling(t,min_periods=t).std(ddof=1).shift(1)
    eta=np.tanh(returns/sigma.replace(0,np.nan)).rolling(t,min_periods=1).mean()
    opening=1020 if data.mcx else 555
    end=opening+(30 if c.strategy=='book_strap_strip' else c.orb_minutes)
    opening_rows=[r for k,r in day['prices'].items() if opening<=k<end]
    high=max((r[1] for r in opening_rows),default=np.nan)
    low=min((r[2] for r in opening_rows),default=np.nan)
    events=[]
    for i,row in enumerate(bars.itertuples()):
        at=row.timestamp.hour*60+row.timestamp.minute+n
        if not minute(c.entry_start)<=at<=minute(c.entry_end): continue
        if at>=(1395 if data.mcx else 915): continue
        atr=float(getattr(row,'atr14',row.atr))
        if not np.isfinite(atr) or atr<=0: continue
        bull=row.close>row.vwap and row.ema9>row.ema21
        bear=row.close<row.vwap and row.ema9<row.ema21
        evidence=dict(signal_time=row.timestamp+pd.Timedelta(minutes=n),
                      signal_vwap_proxy=bool(getattr(row,'vwap_proxy',False)))
        if c.strategy=='book_vertical_spread':
            if i<1 or not row.adx>=18 or not (bull or bear): continue
            previous=bars.iloc[i-1]
            if not fresh_pullback(previous,row,bull):continue
            if confirm_volume:
                history=rolling_activity(day,at,n,10) if not data.mcx else (list(bars.volume.iloc[max(0,i-10):i+1]),[])
                if history is None or len(history[0])!=11 or np.mean(history[0][:-1])<=0 or history[0][-1]<=c.volume_multiple*np.mean(history[0][:-1]):continue
                evidence['signal_volume_basis']='underlying' if data.mcx else 'rolling_atm_ce_pe'
            side=('LONG' if bull else 'SHORT') if data.mcx else ('BULL_CALL_SPREAD' if bull else 'BEAR_PUT_SPREAD')
        elif c.strategy=='book_iron_butterfly':
            if at<585 or not row.adx<22 or abs(row.close/row.vwap-1)>.0025: continue
            side='IRON_BUTTERFLY'; bull=True
        elif c.strategy=='book_strap_strip':
            if at<end or at>=810 or i<t or not row.adx>=20 or (high-low)/row.close>=.0065: continue
            bull=row.close>high and bars.iloc[i-1].close<=high
            bear=row.close<low and bars.iloc[i-1].close>=low
            if not (bull or bear): continue
            side='STRAP' if bull else 'STRIP'
        else:
            signal=float(eta.iloc[i])
            if not np.isfinite(signal) or abs(signal)<c.momentum_threshold: continue
            bull=signal>0
            side=('LONG' if bull else 'SHORT') if data.mcx else ('CE' if bull else 'PE')
            evidence['signal_eta']=signal
        option='CE' if bull else 'PE'
        strike=0 if data.mcx else day.get('rolling',{}).get((option,'ATM'),{}).get(at-1)
        if strike is None: continue
        if c.strategy in ('book_strap_strip','book_vol_oi_momentum') and confirm_volume:
            a=activity(data,day,bars,i,at,option,strike,c)
            if a is None or a['signal_log_volume']<=math.log(max(1.4,c.volume_multiple) if c.strategy=='book_strap_strip' else c.volume_multiple): continue
            if c.strategy=='book_vol_oi_momentum' and a['signal_oi_available'] and a['signal_log_oi']<=0: continue
            evidence.update(a)
        day.setdefault('signal_evidence',{})[c.strategy,c.timeframe,at,side,strike]=evidence
        events.append((at,side,strike,None,atr))
    return events


def basket_allocation(c,date,legs,budget):
    """Reserve net debit or the FULL butterfly wing width plus every leg fee."""
    from quant_backtester import quantity, fees
    debit=sum(l['ratio']*l['entry_price'] for l in legs)
    side_count=len(legs)
    if side_count==4:
        width=legs[2]['strike']-legs[0]['strike']
        if width<=0 or not math.isfinite(debit): return 0,0.,0.,debit
        # Raw evidence is validated separately before execution. Costs may
        # consume the observed credit entirely; reserve that extra debit too.
        risk=width+max(0.,debit)
    elif any(l['ratio']<0 for l in legs):
        if not math.isfinite(debit) or debit<=0: return 0,0.,0.,debit
        risk=debit
    else:
        if debit<=0: return 0,0.,0.,debit
        risk=debit
    unit=quantity(c,date)//c.lots
    lots=c.lots if c.sizing_mode=='fixed_lots' else min(c.max_lots,int(max(0,budget)//(risk*unit)))
    while lots>0:
        qty=unit*lots
        reserve=sum(fees(c,l['entry_price'],l['entry_price'],qty*abs(l['ratio']),date,
                         1 if l['ratio']>0 else -1)['total_cost'] for l in legs)
        allocated=risk*qty
        if allocated+reserve<=budget+1e-8: return qty,allocated,reserve,debit
        if c.sizing_mode=='fixed_lots': break
        lots-=1
    return 0,0.,0.,debit


def bounded_spread_rows(day, at, side, legs, rows, sources, state=None, open_only=False, context=None):
    """Causal pair valuation with an entry-anchored moneyness curve.

    The .28 to .42 moneyness delta range is a research assumption, not
    universal option Greek bounds. The curve includes a favorable intrinsic
    floor. Subsequent exact observations recalibrate its level; fill continuity
    takes priority over returning abruptly to a stale entry calibration.
    State is trade-local and advances entry open -> close -> next open.
    """
    if side not in ('BULL_CALL_SPREAD','BEAR_PUT_SPREAD','IRON_BUTTERFLY'):
        return rows, False
    # Validate immutable observations BEFORE any continuity transformation,
    # including fully observed marks and the state-less entry path.
    validate_raw_basket_rows(day,at,side,legs,rows,sources,open_only,context)
    mutable=[s not in ('observed','observed_price_volume_corrected') for s in sources]
    if state is None:
        if any(mutable): raise MissingObservedPrice('Spread continuity requires its previous pair mark and spot')
        return rows, False
    pairs=[(0,2),(1,3)] if side=='IRON_BUTTERFLY' else [(0,1)]
    result=[list(r) for r in rows]
    if open_only:result=[[r[0],r[0],r[0],r[0],r[4]] for r in rows]
    for field in ((0,) if open_only else (0,3)):
        spot=day['prices'][at][field]; intervals=[]; values=[]
        for h,l in pairs:
            width=abs(legs[h]['strike']-legs[l]['strike'])
            option=legs[h]['side']; direction=1 if option=='CE' else -1
            if not (mutable[h] or mutable[l]):
                value=result[h][field]-result[l][field]
                intervals.append((value,value));values.append(value);continue
            previous,prior_spot,prior_low=state[h,l]
            near=day.get('rolling',{}).get((option,'ATM'),{}).get(at)
            far=day.get('rolling',{}).get((option,'ATM+1' if option=='CE' else 'ATM-1'),{}).get(at)
            def observed_pair(a,b):
                quotes=[day.get('options',{}).get((at,option,k)) for k in (a,b)]
                if (a is None or b is None or not math.isclose(direction*(b-a),width) or
                    any(q is None or not math.isfinite(q[field]) or q[field]<=0 for q in quotes) or
                    any(day.get('price_sources',{}).get((at,option,k))=='synthetic_ffill' for k in (a,b))):return None
                validate_raw_basket_rows(day,at,'BULL_CALL_SPREAD' if option=='CE' else 'BEAR_PUT_SPREAD',
                    [dict(side=option,strike=a,ratio=1),dict(side=option,strike=b,ratio=-1)],
                    quotes,['observed','observed'],open_only=field==0,
                    context=dict(context or {},phase='continuity_calibration'))
                return quotes
            quotes=observed_pair(near,far)
            if quotes is None:
                # A stored offset can contain a different strike spacing. Use
                # another actually observed one-width pair, never treat that
                # wider quote difference as this spread's value.
                strikes={day.get('rolling',{}).get((option,m),{}).get(at) for m in ('ATM-1','ATM','ATM+1')}-{None}
                candidates=[(abs(a-legs[h]['strike']),a,b,q) for a in strikes for b in strikes
                            if (q:=observed_pair(a,b)) is not None]
                if candidates:
                    _,near,far,quotes=min(candidates,key=lambda item:item[:3])
                else:
                    # Engine-eligible repaired minutes may contain an entirely
                    # synthetic band. Retain the prior calibration; never use
                    # those flat repairs as observed extrinsic/IV evidence.
                    repaired=(len(strikes)==3 and all(day.get('price_sources',{}).get((at,option,k))=='synthetic_ffill' for k in strikes))
                    if not repaired or ('curve',h,l) not in state:
                        raise MissingObservedPrice(f'{day.get("timestamp", "")} {at//60:02d}:{at%60:02d} {option}: spread continuity requires observed adjacent strikes of matching width')
                    state['retained_repair_calibration']=True
            clip=lambda v,lo,hi:min(hi,max(lo,v))
            ds=spot-prior_spot
            key=('curve',h,l)
            if key not in state:
                extrinsic=[max(.05,q[field]-intrinsic(spot,k,option)) for k,q in zip((near,far),quotes)]
                state[key]=(prior_spot,previous,max(width/6,sum(extrinsic)/len(extrinsic)))
            origin_spot,origin_value,scale=state[key]
            local_scale=scale if quotes is None else sum(max(.05,q[field]-intrinsic(spot,k,option)) for k,q in zip((near,far),quotes))/2
            scale=(scale+local_scale)/2
            curve=lambda s:spread_curve(s,origin_spot,origin_value,legs[h]['strike'],width,direction,scale)
            # Calibrate to the latest observed pair rather than jumping back
            # to an entry curve after an observed stop-triggering close.
            predicted=previous+curve(spot)-curve(prior_spot)
            low,high=(0.,width) if side=='IRON_BUTTERFLY' else (.05,width-.05)
            if mutable[l] and not mutable[h]: high=min(high,result[h][field]-.05)
            if low>high+1e-8: raise MissingObservedPrice('Observed leg cannot support positive bounded spread premiums')
            value=clip(predicted,low,high)
            # Reject inconsistent exact quotes rather than manufacture a jump
            # to preserve positivity. Width clipping can only reduce a move.
            prior_bounded=clip(previous,0. if side=='IRON_BUTTERFLY' else .05,width if side=='IRON_BUTTERFLY' else width-.05)
            radius=.42*abs(ds)
            feasible_low,feasible_high=low,high
            low=max(low,prior_bounded-radius);high=min(high,prior_bounded+radius)
            if low>high+1e-8:
                if side!='IRON_BUTTERFLY':raise MissingObservedPrice('Observed leg incompatible with spread delta continuity')
                # An observed short option can decay below the prior pair
                # liability even at unchanged spot. Preserve that observation
                # and a positive modeled wing; joint width/quote feasibility
                # takes priority over an individual pair's spot-only delta.
                low,high=feasible_low,feasible_high
            value=clip(value,low,high)
            if mutable[h] and mutable[l]:
                # Allocate the pair value to positive legs without letting an
                # independently collapsed time value determine the difference.
                result[l][field]=max(.05,prior_low+intrinsic(spot,legs[l]['strike'],option)-intrinsic(prior_spot,legs[l]['strike'],option))
            # The butterfly has one joint liability budget. An exact observed
            # pair can consume that budget, requiring its modeled companion to
            # shrink further than the standalone delta band (even to zero).
            intervals.append((0. if side=='IRON_BUTTERFLY' else low,high));values.append(value)
        if side=='IRON_BUTTERFLY' and any(mutable):
            width=abs(legs[0]['strike']-legs[2]['strike'])
            if any(v<0 or v>width for v in values):
                raise MissingObservedPrice(f'{day.get("timestamp", "")} minute {at}, field {field}: observed butterfly pair violates its wing bound: {values}, width={width}')
            excess=max(0.,sum(values)-width)
            capacity=sum(v-lo for v,(lo,_) in zip(values,intervals))
            if excess>capacity+1e-8:
                raise MissingObservedPrice('Butterfly wing bound incompatible with observed quotes')
            if excess and capacity:
                values=[v-excess*(v-lo)/capacity for v,(lo,_) in zip(values,intervals)]
        for (h,l),value in zip(pairs,values):
            if mutable[h]: result[h][field]=result[l][field]+value
            elif mutable[l]: result[l][field]=result[h][field]-value
            state[h,l]=(value,spot,result[l][field])
    if open_only:
        for r in result:r[1:4]=[r[0]]*3
    for i,changed in enumerate(mutable):
        if changed:
            result[i][1]=max(result[i][0],result[i][3])
            result[i][2]=min(result[i][0],result[i][3])
    return [tuple(r) for r in result], bool(any(mutable))


def basket_steps(data,c,date,event,trade_id,budget=None,entry_equity=None):
    from quant_backtester import adverse_tick, fees
    from quant_calendar import next_expiry, is_expiry_day
    at,side,strike,_,atr=event; day=data.days[date]
    legs=leg_specs(day,at,side,strike)
    if not legs: return None,[]
    entry_rows=[];entry_sources=[]
    for leg in legs:
        row=data.price(day,at,leg['side'],leg['strike'])
        if row is None: return None,[]
        stored=day.get('price_sources',{}).get((at,leg['side'],leg['strike']))
        if stored=='synthetic_ffill' and c.price_policy=='strict_observed': raise MissingObservedPrice('Synthetic basket entry')
        entry_rows.append(row)
        entry_sources.append('synthetic_ffill' if stored=='synthetic_ffill' else
                             'observed_price_volume_corrected' if stored=='quarantine_volume_zero' else 'observed')
    validate_raw_basket_rows(day,at,side,legs,entry_rows,entry_sources,open_only=True,
        context=dict(trade_id=trade_id,phase='entry',position_open=False,price_policy=c.price_policy,
                     instrument=c.instrument,strategy=c.strategy))
    if any(row[0]<=0 or (leg['ratio']>0 and row[0]<c.minimum_premium)
           for leg,row in zip(legs,entry_rows)): return None,[]
    for leg,row,source in zip(legs,entry_rows,entry_sources):
        direction=1 if leg['ratio']>0 else -1
        leg.update(raw_entry_price=row[0],entry_price_source=source,
                   entry_price=adverse_tick(row[0]+direction*basket_slippage(c,row[0]),c,direction==1),
                   previous=data.price(day,at-1,leg['side'],leg['strike']),modeled_bars=0,
                   modeled_wing_fallback=False,spread_bounds_applied=False,exit_price_source='observed')
        leg['pricing_state']={};option_mark_state(day,at,row,leg['pricing_state'],open_only=True)
    budget=max(0.0,c.capital if entry_equity is None else entry_equity)*(c.capital_usage_pct/100.0) if budget is None else budget
    entry_equity=c.capital if entry_equity is None else entry_equity
    qty,allocated,reserve,entry=basket_allocation(c,date,legs,budget)
    if not qty: return None,[]
    spread=side in ('BULL_CALL_SPREAD','BEAR_PUT_SPREAD','IRON_BUTTERFLY')
    pairs=[(0,2),(1,3)] if side=='IRON_BUTTERFLY' else [(0,1)]
    spread_state={(h,l):(legs[h]['raw_entry_price']-legs[l]['raw_entry_price'],day['prices'][at][0],legs[l]['raw_entry_price'])
                  for h,l in pairs} if spread else None
    if spread:
        for h,l in pairs:
            width=abs(legs[h]['strike']-legs[l]['strike']);spot=day['prices'][at][0]
            scale=sum(max(.05,legs[j]['raw_entry_price']-intrinsic(spot,legs[j]['strike'],legs[j]['side'])) for j in (h,l))/2
            spread_state['curve',h,l]=(spot,legs[h]['raw_entry_price']-legs[l]['raw_entry_price'],scale)
    credit=side=='IRON_BUTTERFLY'; basis=abs(entry)
    stop=entry-basis*c.sl_pct/100; target=entry+basis*c.target_pct/100
    initial_stop=stop; best=entry; trace=[]; force=None; modeled_bars=0
    finish=915 if credit else 920
    yield dict(minute=at-1,net_mtm=-reserve,reserved_capital=allocated+reserve)
    for current in range(at,finish+1):
        rows=[]; sources=[]
        for leg in legs:
            row=data.price(day,current,leg['side'],leg['strike'])
            source='observed'; modeled=False; wing=False
            stored=day.get('price_sources',{}).get((current,leg['side'],leg['strike']))
            if stored=='synthetic_ffill':
                if c.price_policy=='strict_observed': raise MissingObservedPrice('Strict-observed basket encountered synthetic repaired candle')
                source=stored; modeled=True
            elif stored=='quarantine_volume_zero': source='observed_price_volume_corrected'
            if row is None:
                if c.price_policy=='strict_observed': raise MissingObservedPrice(f'{date} unpriceable fixed-strike basket leg {leg["side"]} {leg["strike"]}')
                if spread:
                    band=[day.get('rolling',{}).get((leg['side'],m),{}).get(current) for m in ('ATM-1','ATM','ATM+1')]
                    band=[k for k in band if k is not None]
                    wing=bool(band and not min(band)<=leg['strike']<=max(band))
                    source='model_spread_wing_continuity' if wing else 'model_spread_continuity'
                    row=(.05,.05,.05,.05,0)  # replaced jointly before any valuation
                else:
                    try:
                        row,meta=continuation_candle(data,day,current,leg['side'],leg['strike'],leg['previous'],
                            state=leg['pricing_state'],open_only=bool(force or current==finish))
                    except MissingObservedPrice as exc:
                        raise MissingObservedPrice(f'{date} {current//60:02d}:{current%60:02d} {side} {leg["side"]} {leg["strike"]:g}: {exc}') from exc
                    source=meta['price_source']; wing=meta.get('modeled_wing_fallback',False)
                modeled=True
            leg['previous']=row; leg['modeled_bars']+=int(modeled)
            leg['modeled_wing_fallback'] |= wing; leg['exit_price_source']=source
            rows.append(row); sources.append(source)
        # A pending close or scheduled square-off is filled at this OPEN.
        # Later quotes/spot in the same candle cannot veto or price that fill.
        quoted_rows=rows[:]
        rows,bounded=bounded_spread_rows(day,current,side,legs,rows,sources,spread_state,
            open_only=bool(force or current==finish),context=dict(trade_id=trade_id,
                phase='terminal_exit' if force or current==finish else 'open_position',
                position_open=True,price_policy=c.price_policy,instrument=c.instrument,strategy=c.strategy))
        for leg,row,source,quoted in zip(legs,rows,sources,quoted_rows):
            leg['previous']=row
            option_mark_state(day,current,row,leg['pricing_state'],open_only=bool(force or current==finish))
            leg['spread_bounds_applied'] |= bounded and source not in ('observed','observed_price_volume_corrected')
            leg['exit_price_source']=source
        modeled=any(s not in ('observed','observed_price_volume_corrected') for s in sources)
        modeled_bars+=int(modeled)
        op=sum(l['ratio']*r[0] for l,r in zip(legs,rows))
        cl=sum(l['ratio']*r[3] for l,r in zip(legs,rows))
        # Preserve raw, synchronized evidence independently of bounded model
        # marks. A missing/model leg has no invented raw observation.
        terminal=bool(force or current==finish)
        observed=all(s in OBSERVED_SOURCES for s in sources)
        raw_op=sum(l['ratio']*r[0] for l,r in zip(legs,quoted_rows)) if observed else None
        raw_cl=(raw_op if terminal else sum(l['ratio']*r[3] for l,r in zip(legs,quoted_rows))) if observed else None
        raw_legs=[dict(side=l['side'],strike=l['strike'],ratio=l['ratio'],price_source=s,
            raw_open=r[0] if s in OBSERVED_SOURCES else None,
            raw_close=(r[0] if terminal else r[3]) if s in OBSERVED_SOURCES else None)
            for l,r,s in zip(legs,quoted_rows,sources)]
        trace.append(dict(trade_id=trade_id,timestamp=day['timestamp']+pd.Timedelta(minutes=current),
            open=op,high=max(op,cl),low=min(op,cl),close=cl,mark=cl,stop=stop,target=target,
            raw_open=raw_op,raw_close=raw_cl,raw_legs=json.dumps(raw_legs,allow_nan=False),
            raw_bounds_validated=spread,raw_bounds_tolerance=RAW_BASKET_TOLERANCE,
            contract_identity_verified=False,
            price_source='model_basket' if modeled else 'observed',
            modeled_wing_fallback=any(l['modeled_wing_fallback'] for l in legs),
            spread_bounds_applied=bounded,
            candle_basis='synchronized_open_close_envelope'))
        if force or current==finish:
            reason=force or 'square_off'; exit_at=current
            for leg,row in zip(legs,rows):
                direction=1 if leg['ratio']>0 else -1
                leg.update(raw_exit_price=row[0],exit_price=adverse_tick(row[0]-direction*basket_slippage(c,row[0]),c,direction==-1))
            break
        # Synchronized portfolio closes avoid imaginary simultaneous OHLC highs.
        if cl<=stop: force='trailing_stop' if stop!=initial_stop else 'stop_loss'
        elif c.exit_mode!='end_of_day' and cl>=target: force='target'
        best=max(best,cl)
        stop=profit_lock(c,entry,best,stop)
        if c.exit_mode in ('legacy','trailing_sl') and c.trail_pct>0:
            jump=basis*c.trail_jump_pct/100
            steps=math.floor(max(0,best-entry)/jump+1e-10)
            if steps: stop=max(stop,entry-basis*c.trail_pct/100+steps*jump)
        liquidation=[adverse_tick(r[3]-(1 if l['ratio']>0 else -1)*basket_slippage(c,r[3]),c,l['ratio']<0) for l,r in zip(legs,rows)]
        mtm=sum(l['ratio']*(p-l['entry_price'])*qty-fees(c,l['entry_price'],p,qty*abs(l['ratio']),date,
                    1 if l['ratio']>0 else -1)['total_cost'] for l,p in zip(legs,liquidation))
        daily_force=yield dict(minute=current,net_mtm=mtm,reserved_capital=allocated+reserve)
        if daily_force: force=daily_force
    costs={k:0. for k in ('brokerage','exchange','sebi','ipft','stt_ctt','stamp','gst','total_cost')}
    gross=raw_gross=0.
    for leg in legs:
        direction=1 if leg['ratio']>0 else -1
        leg['quantity']=qty*abs(leg['ratio'])
        leg_cost=fees(c,leg['entry_price'],leg['exit_price'],leg['quantity'],date,direction)
        gross+=leg['ratio']*(leg['exit_price']-leg['entry_price'])*qty
        raw_gross+=leg['ratio']*(leg['raw_exit_price']-leg['raw_entry_price'])*qty
        leg.update(leg_cost)
        for k,v in leg_cost.items(): costs[k]+=v
        leg.pop('previous')
        leg.pop('pricing_state')
    exit_value=sum(l['ratio']*l['exit_price'] for l in legs)
    net=gross-costs['total_cost']
    contract=' / '.join(f'{l["ratio"]:+g}x{l["strike"]:g}{l["side"]}' for l in legs)
    record=dict(trade_id=trade_id,date=date,side=side,strike=strike,entry_strike=strike,contract=contract,
        structure_label=SIDE_LABELS[side],legs=json.dumps(legs,allow_nan=False),leg_count=len(legs),
        entry_time=day['timestamp']+pd.Timedelta(minutes=at),exit_time=day['timestamp']+pd.Timedelta(minutes=exit_at),
        entry_price=entry,exit_price=exit_value,entry_value_type='credit' if entry<0 else 'debit',
        entry_debit=max(0,entry),entry_credit=max(0,-entry),quantity=qty,
        initial_stop=initial_stop,target=target,raw_entry_price=sum(l['ratio']*l['raw_entry_price'] for l in legs),
        raw_exit_price=sum(l['ratio']*l['raw_exit_price'] for l in legs),gross_before_slippage=raw_gross,
        gross_pnl=gross,net_pnl=net,slippage_cost=raw_gross-gross,status='closed',exit_reason=reason,
        modeled_bars=modeled_bars,used_modeled_prices=modeled_bars>0,
        modeled_wing_fallback=any(l['modeled_wing_fallback'] for l in legs),
        spread_bounds_applied=any(l['spread_bounds_applied'] for l in legs),
        retained_repair_calibration=bool(spread_state and spread_state.get('retained_repair_calibration')),
        exit_price_source='model_basket' if any(l['exit_price_source'].startswith(('model','synthetic')) for l in legs) else 'observed',
        verification_status='repaired_inputs' if day.get('repaired_inputs') else 'model_dependent' if modeled_bars else 'observed_strike_expiry_unverified',
        contract_identity_verified=False,
        expiry_day=('expiry' if date in data.expiry_calendar else 'non_expiry') if data.has_calendar else 'unknown',
        is_expiry_day=is_expiry_day(c.instrument,date),inferred_expiry=next_expiry(c.instrument,date),
        instrument=c.instrument,strategy=c.strategy,capital_allocated=allocated,capital_reserved=allocated+reserve,
        allocation_budget=budget,capital_used_pct=100*allocated/entry_equity,entry_equity=entry_equity,
        capital_basis=('full_wing_width_plus_execution_debit' if entry>0 else 'full_wing_width_margin') if credit else 'net_option_debit',roi_pct=100*net/allocated,
        theoretical_max_loss=(legs[2]['strike']-legs[0]['strike']+entry)*qty if credit else allocated,**costs,
        **day.get('signal_evidence',{}).get((c.strategy,c.timeframe,at,side,strike),{}))
    return record,trace
