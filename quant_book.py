"""Book payoff structures with causal intraday filters and atomic basket fills.

References: Kakushadze & Serur, SSRN 3247865, sections 2.6/2.9,
2.44, 2.34/2.35, 10.3.1/10.4. Intraday filters are research adaptations.
"""
import json
import math
import numpy as np
import pandas as pd

from quant_option_model import continuation_candle, MissingObservedPrice, intrinsic


SIDE_LABELS = {'BULL_CALL_SPREAD':'BULL CALL SPREAD', 'BEAR_PUT_SPREAD':'BEAR PUT SPREAD',
               'IRON_BUTTERFLY':'IRON BUTTERFLY', 'STRAP':'STRAP (2CE:1PE)', 'STRIP':'STRIP (1CE:2PE)'}


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
        # Index volume is normally zero. Always use ONE fixed option contract
        # for activity, not stitched rolling strikes with artificial jumps.
        volumes=[]; basis='fixed_strike_option'
        for begin in range(at-(t+1)*n,at,n):
            rows=[data.price(day,k,side,strike) for k in range(begin,begin+n)]
            if any(r is None or day.get('price_sources',{}).get((k,side,strike))=='synthetic_ffill'
                   for k,r in zip(range(begin,begin+n),rows)): return None
            volumes.append(sum(r[4] for r in rows))
            oi.append(day.get('option_meta',{}).get((begin+n-1,side,strike),(np.nan,))[0])
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
    eta=np.tanh(returns/sigma.replace(0,np.nan)).rolling(t,min_periods=t).mean()
    opening=1020 if data.mcx else 555
    end=opening+c.orb_minutes
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
            if not row.adx>=18 or not (bull or bear): continue
            side=('LONG' if bull else 'SHORT') if data.mcx else ('BULL_CALL_SPREAD' if bull else 'BEAR_PUT_SPREAD')
        elif c.strategy=='book_iron_butterfly':
            if at<585 or not row.adx<22 or abs(row.close/row.vwap-1)>.0025: continue
            side='IRON_BUTTERFLY'; bull=True
        elif c.strategy=='book_strap_strip':
            if at<end or i<t: continue
            history=bars.iloc[i-t:i]
            bull=row.close>high or row.close>history.high.max()
            bear=row.close<low or row.close<history.low.min()
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
            if a is None or a['signal_log_volume']<math.log(c.volume_multiple): continue
            if c.strategy=='book_vol_oi_momentum' and a['signal_oi_available'] and a['signal_log_oi']<=0: continue
            evidence.update(a)
        day.setdefault('signal_evidence',{})[c.strategy,c.timeframe,at,side,strike]=evidence
        events.append((at,side,strike,None,atr))
    return events


def basket_allocation(c,date,legs,budget):
    """Reserve debit or credit max loss, plus all leg fees; credit is not cash."""
    from quant_backtester import quantity, fees
    debit=sum(l['ratio']*l['entry_price'] for l in legs)
    side_count=len(legs)
    if side_count==4:
        width=legs[2]['strike']-legs[0]['strike']
        if not -width<debit<0: return 0,0.,0.,debit
        risk=width+debit
    elif any(l['ratio']<0 for l in legs):
        width=abs(legs[1]['strike']-legs[0]['strike'])
        if not 0<debit<width: return 0,0.,0.,debit
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


def bounded_spread_rows(day, at, side, legs, rows, sources, state=None):
    """Causal pair valuation: last mark + spot delta, with a bounded local anchor.

    The .20 delta and .25 sensitivity ceiling are research assumptions, not
    universal option Greek bounds. Observed quotes win; no intrinsic floor or
    independent wing time-value decay is imposed on the spread.
    State is trade-local and advances entry open -> close -> next open.
    """
    if side not in ('BULL_CALL_SPREAD','BEAR_PUT_SPREAD','IRON_BUTTERFLY'):
        return rows, False
    mutable=[s not in ('observed','observed_price_volume_corrected') for s in sources]
    if state is None:
        if any(mutable): raise MissingObservedPrice('Spread continuity requires its previous pair mark and spot')
        return rows, False
    pairs=[(0,2),(1,3)] if side=='IRON_BUTTERFLY' else [(0,1)]
    result=[list(r) for r in rows]
    for field in (0,3):
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
                return quotes
            quotes=observed_pair(near,far)
            if quotes is None:
                # A stored offset can contain a different strike spacing. Use
                # another actually observed one-width pair, never treat that
                # wider quote difference as this spread's value.
                strikes={day.get('rolling',{}).get((option,m),{}).get(at) for m in ('ATM-1','ATM','ATM+1')}-{None}
                candidates=[(abs(a-legs[h]['strike']),a,b,q) for a in strikes for b in strikes
                            if (q:=observed_pair(a,b)) is not None]
                if not candidates:
                    raise MissingObservedPrice(f'{day.get("timestamp", "")} {at//60:02d}:{at%60:02d} {option}: spread continuity requires observed adjacent strikes of matching width')
                _,near,far,quotes=min(candidates,key=lambda item:item[:3])
            clip=lambda v,lo,hi:min(hi,max(lo,v))
            adjacent=clip(quotes[0][field]-quotes[1][field],.05,width-.05)
            anchor=clip(adjacent+direction*.20*(near-legs[h]['strike']),.05,width-.05)
            ds=spot-prior_spot
            predicted=previous+direction*.20*ds
            correction=clip(.25*(anchor-predicted),-.05*abs(ds),.05*abs(ds))
            low,high=(0.,width) if side=='IRON_BUTTERFLY' else (.05,width-.05)
            if mutable[l] and not mutable[h]: high=min(high,result[h][field]-.05)
            if low>high+1e-8: raise MissingObservedPrice('Observed leg cannot support positive bounded spread premiums')
            value=clip(predicted+correction,low,high)
            # Reject inconsistent exact quotes rather than manufacture a jump
            # to preserve positivity. Width clipping can only reduce a move.
            prior_bounded=clip(previous,0. if side=='IRON_BUTTERFLY' else .05,width if side=='IRON_BUTTERFLY' else width-.05)
            radius=.25*abs(ds)
            low=max(low,prior_bounded-radius);high=min(high,prior_bounded+radius)
            if low>high+1e-8: raise MissingObservedPrice('Observed leg incompatible with spread delta continuity')
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
                raise MissingObservedPrice('Observed butterfly pair violates its wing bound')
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
    for i,changed in enumerate(mutable):
        if changed:
            result[i][1]=max(result[i][0],result[i][3])
            result[i][2]=min(result[i][0],result[i][3])
    return [tuple(r) for r in result], any(mutable)


def basket_steps(data,c,date,event,trade_id,budget=None,entry_equity=None):
    from quant_backtester import adverse_tick, fees
    from quant_calendar import next_expiry, is_expiry_day
    at,side,strike,_,atr=event; day=data.days[date]
    legs=leg_specs(day,at,side,strike)
    if not legs: return None,[]
    for leg in legs:
        row=data.price(day,at,leg['side'],leg['strike'])
        if row is None or row[0]<=0 or (leg['ratio']>0 and row[0]<c.minimum_premium): return None,[]
        stored=day.get('price_sources',{}).get((at,leg['side'],leg['strike']))
        if stored=='synthetic_ffill' and c.price_policy=='strict_observed': raise MissingObservedPrice('Synthetic basket entry')
        direction=1 if leg['ratio']>0 else -1
        leg.update(raw_entry_price=row[0],entry_price=adverse_tick(row[0]+direction*c.slippage,c,direction==1),
                   previous=data.price(day,at-1,leg['side'],leg['strike']),modeled_bars=0,
                   modeled_wing_fallback=False,spread_bounds_applied=False,exit_price_source='observed')
    budget=c.capital*c.capital_usage_pct/100 if budget is None else budget
    entry_equity=c.capital if entry_equity is None else entry_equity
    qty,allocated,reserve,entry=basket_allocation(c,date,legs,budget)
    if not qty: return None,[]
    spread=side in ('BULL_CALL_SPREAD','BEAR_PUT_SPREAD','IRON_BUTTERFLY')
    pairs=[(0,2),(1,3)] if side=='IRON_BUTTERFLY' else [(0,1)]
    spread_state={(h,l):(legs[h]['raw_entry_price']-legs[l]['raw_entry_price'],day['prices'][at][0],legs[l]['raw_entry_price'])
                  for h,l in pairs} if spread else None
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
                        row,meta=continuation_candle(data,day,current,leg['side'],leg['strike'],leg['previous'])
                    except MissingObservedPrice as exc:
                        raise MissingObservedPrice(f'{date} {current//60:02d}:{current%60:02d} {side} {leg["side"]} {leg["strike"]:g}: {exc}') from exc
                    source=meta['price_source']; wing=meta.get('modeled_wing_fallback',False)
                modeled=True
            leg['previous']=row; leg['modeled_bars']+=int(modeled)
            leg['modeled_wing_fallback'] |= wing; leg['exit_price_source']=source
            rows.append(row); sources.append(source)
        rows,bounded=bounded_spread_rows(day,current,side,legs,rows,sources,spread_state)
        for leg,row,source in zip(legs,rows,sources):
            leg['previous']=row
            leg['spread_bounds_applied'] |= bounded and source not in ('observed','observed_price_volume_corrected')
        modeled=any(s not in ('observed','observed_price_volume_corrected') for s in sources)
        modeled_bars+=int(modeled)
        op=sum(l['ratio']*r[0] for l,r in zip(legs,rows))
        cl=sum(l['ratio']*r[3] for l,r in zip(legs,rows))
        trace.append(dict(trade_id=trade_id,timestamp=day['timestamp']+pd.Timedelta(minutes=current),
            open=op,high=max(op,cl),low=min(op,cl),close=cl,mark=cl,stop=stop,target=target,
            price_source='model_basket' if modeled else 'observed',
            modeled_wing_fallback=any(l['modeled_wing_fallback'] for l in legs),
            spread_bounds_applied=bounded,
            candle_basis='synchronized_open_close_envelope'))
        if force or current==finish:
            reason=force or 'square_off'; exit_at=current
            for leg,row in zip(legs,rows):
                direction=1 if leg['ratio']>0 else -1
                leg.update(raw_exit_price=row[0],exit_price=adverse_tick(row[0]-direction*c.slippage,c,direction==-1))
            break
        # Synchronized portfolio closes avoid imaginary simultaneous OHLC highs.
        if cl<=stop: force='trailing_stop' if stop!=initial_stop else 'stop_loss'
        elif (credit or c.exit_mode!='end_of_day') and cl>=target: force='target'
        best=max(best,cl)
        if c.exit_mode in ('legacy','trailing_sl') and c.trail_pct>0:
            jump=basis*c.trail_jump_pct/100
            steps=math.floor(max(0,best-entry)/jump+1e-10)
            if steps: stop=max(stop,entry-basis*c.trail_pct/100+steps*jump)
        liquidation=[adverse_tick(r[3]-(1 if l['ratio']>0 else -1)*c.slippage,c,l['ratio']<0) for l,r in zip(legs,rows)]
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
    exit_value=sum(l['ratio']*l['exit_price'] for l in legs)
    net=gross-costs['total_cost']
    contract=' / '.join(f'{l["ratio"]:+g}x{l["strike"]:g}{l["side"]}' for l in legs)
    record=dict(trade_id=trade_id,date=date,side=side,strike=strike,entry_strike=strike,contract=contract,
        structure_label=SIDE_LABELS[side],legs=json.dumps(legs,allow_nan=False),leg_count=len(legs),
        entry_time=day['timestamp']+pd.Timedelta(minutes=at),exit_time=day['timestamp']+pd.Timedelta(minutes=exit_at),
        entry_price=entry,exit_price=exit_value,entry_value_type='credit' if credit else 'debit',
        entry_debit=max(0,entry),entry_credit=max(0,-entry),quantity=qty,
        initial_stop=initial_stop,target=target,raw_entry_price=sum(l['ratio']*l['raw_entry_price'] for l in legs),
        raw_exit_price=sum(l['ratio']*l['raw_exit_price'] for l in legs),gross_before_slippage=raw_gross,
        gross_pnl=gross,net_pnl=net,slippage_cost=raw_gross-gross,status='closed',exit_reason=reason,
        modeled_bars=modeled_bars,used_modeled_prices=modeled_bars>0,
        modeled_wing_fallback=any(l['modeled_wing_fallback'] for l in legs),
        spread_bounds_applied=any(l['spread_bounds_applied'] for l in legs),
        exit_price_source='model_basket' if any(l['exit_price_source'].startswith(('model','synthetic')) for l in legs) else 'observed',
        verification_status='repaired_inputs' if day.get('repaired_inputs') else 'model_dependent' if modeled_bars else 'observed_strike_expiry_unverified',
        contract_identity_verified=False,
        expiry_day=('expiry' if date in data.expiry_calendar else 'non_expiry') if data.has_calendar else 'unknown',
        is_expiry_day=is_expiry_day(c.instrument,date),inferred_expiry=next_expiry(c.instrument,date),
        instrument=c.instrument,strategy=c.strategy,capital_allocated=allocated,capital_reserved=allocated+reserve,
        allocation_budget=budget,capital_used_pct=100*allocated/entry_equity,entry_equity=entry_equity,
        capital_basis='defined_risk_max_loss' if credit else 'net_option_debit',roi_pct=100*net/allocated,
        theoretical_max_loss=allocated,**costs,
        **day.get('signal_evidence',{}).get((c.strategy,c.timeframe,at,side,strike),{}))
    return record,trace
