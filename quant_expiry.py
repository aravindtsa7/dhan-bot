"""Fixed-strike CE+PE BTST baskets; expiry-date labels never verify WEEK1 identity."""
from datetime import timedelta
import json
import math

import pandas as pd

from quant_calendar import expiries, regular_session
from quant_option_model import MissingObservedPrice, continuation_candle

LABEL = 'EXPIRY DAY (CE+PE)'


def previous_session(date, observed=()):
    value=pd.Timestamp(date).date()-timedelta(days=1)
    while not regular_session(value): value-=timedelta(days=1)
    # A recorded special session after the regular predecessor must also pass
    # the loader's full-session checks. Never silently hop over an excluded day.
    return max([value.isoformat()]+[d for d in observed if value.isoformat()<d<date])


def observed_expiries(frame):
    """Only explicit dated expiry or weekly-reset evidence. ATM drift is not a roll."""
    result=set()
    if frame.empty: return result
    for column in ('expiry_date','contract_expiry','expiration_date'):
        if column in frame:
            values=frame[column].astype(str)
            result.update(values[values.str.fullmatch(r'\d{4}-\d{2}-\d{2}')])
    if 'weekly_reset' in frame:
        dates=sorted(frame.date.unique())
        reset=set(frame.loc[frame.weekly_reset.eq(True),'date'])
        result.update(dates[i-1] for i,d in enumerate(dates) if i and d in reset)
    return result


def expiry_pairs(data,c,observed=None):
    observed=sorted(observed if observed is not None else getattr(data,'observed_sessions',None) or
                    set(data.days)|set(getattr(data,'available_days',[])))
    calendar=(getattr(data,'observed_expiry_dates',set()) if c.expiry_calendar_mode=='observed_reset'
              else getattr(data,'expiry_calendar',set()) or set(expiries(c.instrument)))
    result=[]
    for expiry in sorted(calendar):
        if not c.start<=expiry<c.end: continue
        entry=expiry if c.expiry_entry_mode=='expiry_intraday' else previous_session(expiry,observed)
        if not c.expiry_include_prior_session and entry<c.start: continue
        result.append((entry,expiry))
    return result


def dependency_dates(data,c,observed):
    data.observed_sessions=list(observed)
    pairs=expiry_pairs(data,c,observed)
    return {d for pair in pairs for d in pair},{entry for entry,_ in pairs}


def expiry_events(data,c,date):
    from quant_backtester import minute
    at=minute(c.expiry_entry_signal)+1 if c.expiry_entry_mode=='btst' else 556
    return [(at,'EXPIRY_DAY',0,None,0)] if any(entry==date for entry,_ in expiry_pairs(data,c)) else []


def live_event(c,now):
    from datetime import datetime
    from paper_feed import IST
    from quant_calendar import next_expiry
    from quant_backtester import minute
    local=datetime.fromtimestamp(now,IST);date=local.date().isoformat()
    at=local.hour*60+local.minute
    expected=minute(c.expiry_entry_signal)+1 if c.expiry_entry_mode=='btst' else 556
    if at!=expected or local.second>15 or not regular_session(date): return None
    expiry=next_expiry(c.instrument,date)
    if (previous_session(expiry) if c.expiry_entry_mode=='btst' else expiry)!=date: return None
    return (at,'EXPIRY_DAY',0,None,0)


def paper_candidates(frame,instrument,spot,now):
    from paper_feed import option_asset, Asset
    atm=option_asset(frame,instrument,'CE',spot,now)
    exchange='NSE' if instrument=='NIFTY' else 'BSE'
    f=frame[(frame.EXCH_ID==exchange)&(frame.INSTRUMENT=='OPTIDX')&(frame.SYMBOL_NAME==instrument)].copy()
    f=f[pd.to_datetime(f.SM_EXPIRY_DATE,errors='coerce').dt.strftime('%Y-%m-%d').eq(atm.expiry)]
    result=[]
    for side in ('CE','PE'):
        rows=f[f.OPTION_TYPE.eq(side)].copy();rows['strike']=pd.to_numeric(rows.STRIKE_PRICE)
        strikes=sorted(rows.strike.unique())
        if not strikes: return []
        k=min(strikes,key=lambda k:(abs(k-spot),k));idx=strikes.index(k)
        for offset,index in [('ATM',idx),('ATM-1',idx-1),('ATM+1',idx+1)]:
            if not 0<=index<len(strikes): continue
            row=rows[rows.strike.eq(strikes[index])].sort_values('SECURITY_ID').iloc[0]
            result.append((offset,Asset(instrument,atm.segment,int(row.SECURITY_ID),atm.expiry,int(row.LOT_SIZE),strikes[index],side)))
    return result


def paper_selection(c,candidates,histories,event,now):
    from datetime import datetime
    from paper_feed import IST
    local=datetime.fromtimestamp(now,IST)
    stamp=local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()+(event[0]-1)*60
    day={'options':{},'rolling':{}}
    for offset,asset in candidates:
        row=next((r for r in histories.get(asset.key,[]) if r['timestamp']==stamp),None)
        if row is None: continue
        day['rolling'][asset.option_type,offset]={event[0]-1:asset.strike}
        day['options'][event[0]-1,asset.option_type,asset.strike]=tuple(row[k] for k in ('open','high','low','close','volume'))
    legs=select_legs(day,event[0]-1,c.minimum_premium)
    return [(leg,next(a for _,a in candidates if (a.option_type,a.strike)==(leg['side'],leg['strike']))) for leg in legs]


def check_prepared_windows(data,c):
    # A normal ResearchData snapshot intentionally audits only through 15:20.
    # Do not accept it as evidence of late BTST entry prices or repaired inputs.
    if hasattr(data,'root'):
        prepared=getattr(data,'expiry_config',None)
        if prepared is None or any(getattr(prepared,k)!=getattr(c,k) for k in
                ('expiry_entry_mode','expiry_include_prior_session','expiry_calendar_mode')):
            raise ValueError('Prepare Expiry Day data with ResearchData(..., expiry_config=config); late entry and prior-session dependencies require their own audit windows')


def select_legs(day, signal_minute, minimum_premium=0):
    """Select from signal-close quotes only. Match premiums, then prefer near ATM."""
    candidates={}
    for side in ('CE','PE'):
        atm=day.get('rolling',{}).get((side,'ATM'),{}).get(signal_minute)
        rows={}
        for offset in ('ATM','ATM-1','ATM+1'):
            strike=day.get('rolling',{}).get((side,offset),{}).get(signal_minute)
            quote=day.get('options',{}).get((signal_minute,side,strike))
            if strike is None or quote is None: continue
            premium=quote[3]
            if not math.isfinite(premium) or premium<=0 or premium<minimum_premium: continue
            rows[strike]=dict(side=side,strike=float(strike),ratio=1,signal_premium=float(premium),
                              atm_distance=abs(strike-atm) if atm is not None else float('inf'))
        candidates[side]=list(rows.values())
    pairs=[(ce,pe) for ce in candidates['CE'] for pe in candidates['PE']]
    if not pairs: return []
    ce,pe=min(pairs,key=lambda pair:(abs(pair[0]['signal_premium']-pair[1]['signal_premium']),
        pair[0]['atm_distance']+pair[1]['atm_distance'],max(pair[0]['atm_distance'],pair[1]['atm_distance']),
        pair[0]['strike'],pair[1]['strike']))
    return [dict(ce),dict(pe)]


def expiry_steps(data,c,date,event,trade_id,budget=None,entry_equity=None,exit_date=None):
    from quant_backtester import minute, adverse_tick, fees
    from quant_book import basket_allocation
    exit_date=exit_date or next((e for d,e in expiry_pairs(data,c) if d==date),None)
    if exit_date is None or date not in data.days or exit_date not in data.days: return None,[]
    at=event[0]; day=data.days[date]
    legs=select_legs(day,at-1,c.minimum_premium)
    if len(legs)!=2: return None,[]
    for leg in legs:
        quote=data.price(day,at,leg['side'],leg['strike'])
        if quote is None or quote[0]<=0 or quote[0]<c.minimum_premium: return None,[]
        leg.update(raw_entry_price=quote[0],entry_price=adverse_tick(quote[0]+c.slippage,c,True),
                   previous=data.price(day,at-1,leg['side'],leg['strike']),modeled_bars=0,
                   modeled_wing_fallback=False,exit_price_source='observed')
    budget=c.capital*c.capital_usage_pct/100 if budget is None else budget
    entry_equity=c.capital if entry_equity is None else entry_equity
    qty,allocated,reserve,entry=basket_allocation(c,date,legs,budget)
    if not qty: return None,[]
    stop=entry*(1-c.sl_pct/100); initial_stop=stop; target=entry*(1+c.target_pct/100)
    best=entry; force=None; trace=[]; modeled_bars=0
    finish=minute(c.expiry_exit_cutoff)
    itinerary=([(date,t) for t in range(at,930)] if date!=exit_date else [])+[(exit_date,t) for t in range(at if date==exit_date else 555,finish+1)]
    yield dict(date=date,minute=at-1,net_mtm=-reserve,reserved_capital=allocated+reserve)
    for session,current in itinerary:
        current_day=data.days[session]; rows=[]; sources=[]
        for leg in legs:
            row=data.price(current_day,current,leg['side'],leg['strike'])
            stored=current_day.get('price_sources',{}).get((current,leg['side'],leg['strike']))
            source='observed_price_volume_corrected' if stored=='quarantine_volume_zero' else 'observed'
            modeled=False; wing=False
            if stored=='synthetic_ffill':
                if c.price_policy=='strict_observed': raise MissingObservedPrice('Strict-observed Expiry Day encountered a synthetic repaired candle')
                source=stored; modeled=True
            if row is None:
                if c.price_policy=='strict_observed': raise MissingObservedPrice(f'{session} {current}: unpriceable fixed-strike Expiry Day {leg["side"]} {leg["strike"]}')
                row,meta=continuation_candle(data,current_day,current,leg['side'],leg['strike'],leg['previous'])
                source=meta['price_source']; modeled=True; wing=meta.get('modeled_wing_fallback',False)
            leg.update(previous=row,exit_price_source=source)
            leg['modeled_bars']+=int(modeled);leg['modeled_wing_fallback'] |= wing
            rows.append(row);sources.append(source)
        modeled=any(s not in ('observed','observed_price_volume_corrected') for s in sources)
        modeled_bars+=int(modeled)
        op=sum(r[0] for r in rows);cl=sum(r[3] for r in rows)
        trace.append(dict(trade_id=trade_id,timestamp=current_day['timestamp']+pd.Timedelta(minutes=current),
            open=op,high=max(op,cl),low=min(op,cl),close=cl,mark=cl,stop=stop,target=target,
            ce_mark=rows[0][3],pe_mark=rows[1][3],combined_return_pct=100*(cl-entry)/entry,
            price_source='model_basket' if modeled else 'observed',modeled_wing_fallback=any(l['modeled_wing_fallback'] for l in legs),
            candle_basis='synchronized_open_close_envelope'))
        if force or (session==exit_date and current==finish):
            reason=force or 'square_off';exit_at=current;closed_date=session
            for leg,row in zip(legs,rows): leg.update(raw_exit_price=row[0],exit_price=adverse_tick(row[0]-c.slippage,c,False))
            break
        if session==exit_date:
            # Never inspect a leg's own return, high, low, stop, or target.
            if cl<=stop: force='trailing_stop' if stop!=initial_stop else 'stop_loss'
            elif c.exit_mode!='end_of_day' and cl>=target: force='target'
            best=max(best,cl)
            if c.exit_mode in ('legacy','trailing_sl') and c.trail_pct>0:
                jump=entry*c.trail_jump_pct/100; steps=math.floor(max(0,best-entry)/jump+1e-10)
                if steps: stop=max(stop,entry*(1-c.trail_pct/100)+steps*jump)
        liquidation=[adverse_tick(r[3]-c.slippage,c,False) for r in rows]
        mtm=sum((p-l['entry_price'])*qty-fees(c,l['entry_price'],p,qty,session)['total_cost'] for l,p in zip(legs,liquidation))
        daily_force=yield dict(date=session,minute=current,net_mtm=mtm,reserved_capital=allocated+reserve)
        if daily_force: force=daily_force
    costs={};gross=raw_gross=0.
    for leg in legs:
        charges=fees(c,leg['entry_price'],leg['exit_price'],qty,closed_date)
        leg.update(quantity=qty,**charges);leg.pop('previous')
        gross+=(leg['exit_price']-leg['entry_price'])*qty
        raw_gross+=(leg['raw_exit_price']-leg['raw_entry_price'])*qty
        for k,v in charges.items(): costs[k]=costs.get(k,0)+v
    value=sum(l['exit_price'] for l in legs);net=gross-costs['total_cost']
    repaired=day.get('repaired_inputs',[])+data.days[exit_date].get('repaired_inputs',[])
    record=dict(trade_id=trade_id,date=closed_date,entry_date=date,expiry_exit_session=exit_date,side='EXPIRY_DAY',structure_label=LABEL,
        strike=legs[0]['strike'],entry_strike=legs[0]['strike'],contract=' / '.join(f'+1x{l["strike"]:g}{l["side"]}' for l in legs),
        legs=json.dumps(legs,allow_nan=False),leg_count=2,entry_time=day['timestamp']+pd.Timedelta(minutes=at),
        signal_time=day['timestamp']+pd.Timedelta(minutes=at-1),exit_time=data.days[closed_date]['timestamp']+pd.Timedelta(minutes=exit_at),
        entry_price=entry,exit_price=value,quantity=qty,initial_stop=initial_stop,target=target,entry_value_type='debit',entry_debit=entry,entry_credit=0.,
        combined_entry_value=entry*qty,combined_exit_value=value*qty,combined_return_pct=100*(value-entry)/entry,
        ce_entry_price=legs[0]['entry_price'],ce_exit_price=legs[0]['exit_price'],pe_entry_price=legs[1]['entry_price'],pe_exit_price=legs[1]['exit_price'],
        raw_entry_price=sum(l['raw_entry_price'] for l in legs),raw_exit_price=sum(l['raw_exit_price'] for l in legs),
        gross_before_slippage=raw_gross,gross_pnl=gross,slippage_cost=raw_gross-gross,net_pnl=net,status='closed',exit_reason=reason,
        modeled_bars=modeled_bars,used_modeled_prices=modeled_bars>0,modeled_wing_fallback=any(l['modeled_wing_fallback'] for l in legs),
        exit_price_source='model_basket' if any(l['exit_price_source'].startswith(('model','synthetic')) for l in legs) else 'observed',
        verification_status='repaired_inputs' if repaired else 'model_dependent' if modeled_bars else 'rolling_btst_expiry_unverified',
        contract_identity_verified=False,expiry_day='expiry',is_expiry_day=True,inferred_expiry=exit_date,
        expiry_session_basis=c.expiry_calendar_mode,overnight=date!=closed_date,instrument=c.instrument,strategy=c.strategy,
        retrospective_expiry_inference=c.expiry_calendar_mode=='observed_reset',
        capital_allocated=allocated,capital_reserved=allocated+reserve,allocation_budget=budget,entry_equity=entry_equity,
        capital_used_pct=100*allocated/entry_equity,capital_basis='combined_option_debit',roi_pct=100*net/allocated,theoretical_max_loss=allocated,**costs)
    return record,trace


def execute_expiry_portfolio(data,c,run_id,daily_audit=None):
    from quant_backtester import minute
    records=[];paths=[];skipped=[];equity=c.capital
    for serial,(entry,expiry) in enumerate(expiry_pairs(data,c)):
        if entry not in data.days or expiry not in data.days:
            skipped.append(dict(date=expiry,entry_date=entry,side='EXPIRY_DAY',reason='missing_or_ineligible_entry_or_expiry_session'))
            continue
        if equity<=0: continue
        base=equity if c.compound else min(c.capital,equity);budget=max(0,base)*c.capital_usage_pct/100
        at=minute(c.expiry_entry_signal)+1 if c.expiry_entry_mode=='btst' else 556
        steps=expiry_steps(data,c,entry,(at,'EXPIRY_DAY',0,None,0),f'{run_id}_{serial:06d}',budget,equity,expiry)
        audits={};force=None
        while True:
            try: state=steps.send(force)
            except StopIteration as done:
                trade,trace=done.value
                if trade is None: skipped.append(dict(date=expiry,entry_date=entry,side='EXPIRY_DAY',reason='no_balanced_quotes_or_insufficient_capital'))
                else:
                    records.append(trade);paths.extend(trace);equity+=trade['net_pnl']
                    if trade['date'] in audits: audits[trade['date']].update(net_pnl=trade['net_pnl'],trades=1)
                break
            date=state['date'];net=state['net_mtm']
            a=audits.setdefault(date,dict(date=date,start_equity=equity,net_pnl=0.,trades=0,peak_net_pnl=0.,minimum_net_pnl=0.,
                max_drawdown_1m_close=0.,lock_reason='',lock_minute=None,lock_trigger_pnl=None,eligible=True))
            a['peak_net_pnl']=max(a['peak_net_pnl'],net);a['minimum_net_pnl']=min(a['minimum_net_pnl'],net)
            a['max_drawdown_1m_close']=max(a['max_drawdown_1m_close'],a['peak_net_pnl']-net)
            positive=lambda *xs:min((x for x in xs if x>0),default=float('inf'))
            if not force:
                if net>=positive(c.daily_profit_target,equity*c.daily_profit_pct/100): force='daily_profit_target'
                elif net<=-positive(c.daily_loss_limit,equity*c.daily_loss_pct/100): force='daily_loss_limit'
                if force: a.update(lock_reason=force,lock_minute=state['minute'],lock_trigger_pnl=net)
        if daily_audit is not None: daily_audit.extend(audits.values())
    return records,paths,skipped
