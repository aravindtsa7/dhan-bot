"""Atomic virtual option baskets; every leg needs a fresh real contract quote."""
from dataclasses import asdict, replace
from datetime import datetime
import json
import math
import os
from pathlib import Path
import uuid

from paper_engine import PaperAccount, stamp
from paper_feed import Asset, IST
from quant_backtester import fees, minute
from quant_book import (basket_allocation, SIDE_LABELS, validate_raw_basket_rows,
                        InvalidBasketObservation)
from quant_strategy_models import basket_slippage, profit_lock
SIDE_LABELS['EXPIRY_DAY']='EXPIRY DAY (CE+PE)'
SIDE_LABELS['EXPIRY_INTRADAY']='EXPIRY INTRADAY (CE+PE)'


class BasketPaperAccount(PaperAccount):
    def price(self,tick,buy):
        from quant_backtester import adverse_tick
        depth=tick.ask if buy else tick.bid
        base=depth if depth is not None else tick.price
        return adverse_tick(base+(1 if buy else -1)*basket_slippage(self.c,tick.price),self.c,buy)

    def __init__(self,*args,allow_async_skew=False,**kwargs):
        self.quotes={}
        self.allow_async_skew=allow_async_skew
        root=Path(args[0] if args else kwargs['root'])
        self._frozen_evidence=None
        # An immutable invalidation intent is a durable fail-closed barrier,
        # independent of a denied/interrupted state+ledger publication. A torn
        # intent blocks restart rather than permitting a favorable new quote.
        for path in sorted((root/'basket_invalidations').glob('*.intent.json')):
            try:
                evidence=json.loads(path.read_text(encoding='utf-8'))
                if not evidence.get('simulation_invalidated'):raise ValueError('Invalid intent')
            except (OSError,ValueError) as exc:
                raise ValueError('Incomplete basket invalidation evidence; refuse paper simulation restart: '+str(path)) from exc
            if self._frozen_evidence is None:self._frozen_evidence=evidence
        super().__init__(*args,**kwargs)

    def _apply_invalidation(self):
        evidence=self._frozen_evidence or self.state.get('invalidation_evidence')
        if evidence:
            self._frozen_evidence=evidence
            self.state.update(simulation_status='INVALIDATED',invalidation_evidence=evidence)
            for p in self.state['trades']:
                if p['status']=='OPEN':
                    # Keep the original open position and reserve. There is no
                    # invented liquidation, realized P&L or subsequent exit.
                    p.update(simulation_status='INVALIDATED',invalidation_evidence=evidence)

    def save(self):
        self._apply_invalidation()
        return super().save()  # recoverable joint state/ledger publication

    def _ensure_valid_simulation(self):
        evidence=self._frozen_evidence or self.state.get('invalidation_evidence')
        if evidence:
            raise InvalidBasketObservation('Paper basket simulation frozen after invalid raw evidence',evidence)

    def _invalidate(self,evidence):
        evidence=dict(evidence,simulation_invalidated=True,contract_identity_verified=False,
            account_action='FROZEN; retain open position/reserve; no exit or realized P&L',
            paper=True)
        self._frozen_evidence=evidence
        self._apply_invalidation()
        path=self.root/'basket_invalidations'/(uuid.uuid4().hex+'.intent.json')
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            evidence['invalidation_evidence_path']=str(path.relative_to(self.root))
            with path.open('x',encoding='utf-8') as stream:
                json.dump(evidence,stream,indent=2,allow_nan=False)
                stream.flush();os.fsync(stream.fileno())
        except Exception as exc:
            evidence['invalidation_intent_failure']=dict(type=type(exc).__name__,reason=str(exc))
        try:self.save()
        except Exception as exc:
            evidence['invalidation_publication_failure']=dict(type=type(exc).__name__,reason=str(exc))
            # Report secondary publication failures without overwriting the
            # original intent; the T2 journal also retains its recovery proof.
            try:
                failure=path.with_name(path.stem+'.publication_failure.json')
                with failure.open('x',encoding='utf-8') as stream:
                    json.dump(evidence['invalidation_publication_failure'],stream,indent=2)
                    stream.flush();os.fsync(stream.fileno())
            except Exception as secondary:
                evidence['secondary_evidence_failure']=dict(type=type(secondary).__name__,reason=str(secondary))
        raise InvalidBasketObservation('Invalid raw paper basket snapshot; simulation frozen',evidence)

    def _validate_snapshot(self,side,legs,quotes,now,phase):
        self._ensure_valid_simulation()
        if side not in ('BULL_CALL_SPREAD','BEAR_PUT_SPREAD','IRON_BUTTERFLY'):return
        def number(value):
            return str(value) if isinstance(value,float) and not math.isfinite(value) else value
        snapshot=[dict(asset=l['asset'],side=l['side'],strike=l['strike'],ratio=l['ratio'],
            raw_price=number(q.price),bid=number(q.bid),ask=number(q.ask),
            exchange_time=q.exchange_time,received_time=q.received_time,
            exchange_time_ist=stamp(q.exchange_time),received_time_ist=stamp(q.received_time),
            wire_epoch=q.wire_epoch) for l,q in zip(legs,quotes)]
        times=[q.exchange_time for q in quotes]
        local=datetime.fromtimestamp(now,IST);at=local.hour*60+local.minute
        context=dict(phase=phase,instrument=self.c.instrument,strategy=self.c.strategy,
            trade_id=self.position['trade_id'] if self.position else None,
            position_open=self.position is not None,price_policy=self.c.price_policy,
            decision_time_epoch=now,decision_time_ist=stamp(now),
            quote_snapshot=snapshot,quote_time_spread_seconds=max(times)-min(times),
            quote_exchange_times_equal=len(set(times))==1,snapshot_synchronization_verified=False,
            time_basis='fresh live LTP snapshot; asynchronous exchange times retained, not a synchronized candle',
            contract_identity_status='supplied resolved asset IDs and expiry; independent provider identity not verified')
        rows=[(q.price,)*4+(0,) for q in quotes]
        try:validate_raw_basket_rows(dict(timestamp=local.replace(hour=0,minute=0,second=0,microsecond=0)),
            at,side,legs,rows,['observed']*len(legs),open_only=True,context=context)
        except InvalidBasketObservation as exc:
            if (self.allow_async_skew or any(q.wire_epoch is not None for q in quotes)) and phase in ('paper_on_tick','paper_mark') and len(set(times))>1:
                return False
            self._invalidate(exc.evidence)
        return context

    def enter(self,*args,**kwargs):
        self._ensure_valid_simulation()
        return super().enter(*args,**kwargs)

    def request_exit(self,reason):
        self._ensure_valid_simulation()
        return super().request_exit(reason)

    def lock_if_needed(self,tick,now):
        self._ensure_valid_simulation()
        return super().lock_if_needed(tick,now)

    def disconnect(self):
        self.quotes.clear()

    def fresh_legs(self,now):
        p=self.position
        if not p or not p.get('legs'): return None
        quotes=[self.quotes.get(Asset(**l['asset']).key) for l in p['legs']]
        return quotes if all(q and q.fresh(now) for q in quotes) else None

    def enter_basket(self,event,resolved,quotes,now):
        self._ensure_valid_simulation()
        if self.position or not resolved: return False
        local=datetime.fromtimestamp(now,IST); date=local.date().isoformat(); at=local.hour*60+local.minute
        minute_start=int(now)//60*60
        expiry_day=self.c.strategy=='expiry_day'
        if expiry_day:
            from quant_expiry import live_event
            if event!=live_event(self.c,minute_start) or event[1]!='EXPIRY_DAY': return False
        elif self.c.strategy=='expiry_intraday':
            from quant_expiry_intraday import live_event
            if event!=live_event(self.c,minute_start):return False
        elif event[0]!=at or local.weekday()>=5 or not minute(self.c.entry_start)<=at<=minute(self.c.entry_end): return False
        if event[1]=='IRON_BUTTERFLY' and not 585<=at<915: return False
        if self.session(now)['lock']: return False
        key=f'{date}:basket:{at}'
        if key in self.state['seen_signals']: return False
        today=[t for t in self.state['trades'] if t['entry_time'][:10]==date]
        cap=1 if self.c.strategy in ('book_strap_strip','expiry_day','expiry_intraday') else self.c.max_trades_day
        if len(today)>=cap: return False
        if self.c.strategy=='book_vertical_spread' and today and today[-1]['exit_reason'] in ('stop_loss','trailing_stop'):
            if now-datetime.fromisoformat(today[-1]['exit_time']).timestamp()<3*int(self.c.timeframe[:-1])*60:return False
        assets=[asset for _,asset in resolved]
        if len({a.expiry for a in assets})!=1 or any(a.expiry<date for a in assets) or len({a.point_value for a in assets})!=1: return False
        if expiry_day:
            from quant_expiry import previous_session
            if len(resolved)!=2 or {s['side'] for s,_ in resolved}!={'CE','PE'} or any(s['ratio']!=1 for s,_ in resolved): return False
            if (previous_session(assets[0].expiry) if self.c.expiry_entry_mode=='btst' else assets[0].expiry)!=date: return False
        if self.c.strategy=='expiry_intraday':
            if assets[0].expiry!=date or len(resolved)!=2 or {s['side'] for s,_ in resolved}!={'CE','PE'} or any(s['ratio']!=1 for s,_ in resolved):return False
        legs=[]
        for spec,asset in resolved:
            q=quotes.get(asset.key)
            if not q or not q.fresh(now): return False
            legs.append(dict(spec,asset=asdict(asset),raw_entry_price=q.price,
                             entry_price=self.price(q,spec['ratio']>0),last_epoch=q.exchange_time))
        raw_snapshot=self._validate_snapshot(event[1],legs,[quotes[a.key] for a in assets],now,'paper_entry')
        if any(l['ratio']>0 and l['raw_entry_price']<self.c.minimum_premium for l in legs):return False
        allocation=replace(self.c,lot_size=assets[0].point_value)
        budget=max(0,min(self.c.capital,self.equity))*self.c.capital_usage_pct/100
        qty,allocated,reserve,entry=basket_allocation(allocation,date,legs,budget)
        if not qty: return False
        distance=abs(entry)*self.c.sl_pct/100
        p=dict(trade_id='paper_'+uuid.uuid4().hex[:16],instrument=self.c.instrument,strategy=self.c.strategy,
            asset=asdict(assets[0]),security_id=assets[0].security_id,expiry=assets[0].expiry,status='OPEN',
            side=event[1],structure_label=SIDE_LABELS[event[1]],legs=legs,direction=1,
            contract=' / '.join(f'{l["ratio"]:+g}x{l["strike"]:g}{l["side"]}' for l in legs),
            entry_time=stamp(now),exit_time='',entry_price=entry,exit_price=None,quantity=qty,lots=qty/assets[0].point_value,
            entry_value_type='credit' if entry<0 else 'debit',capital_allocated=allocated,capital_reserved=allocated+reserve,
            raw_entry_price=sum(l['ratio']*l['raw_entry_price'] for l in legs),
            capital_basis=('full_wing_width_plus_execution_debit' if entry>0 else 'full_wing_width_margin')
                if event[1]=='IRON_BUTTERFLY' else 'net_option_debit',
            stop=entry-distance,initial_stop=entry-distance,stop_distance=distance,target=entry+abs(entry)*self.c.target_pct/100,
            best=entry,net_pnl=None,exit_reason='',pending_exit='',paper=True)
        if raw_snapshot:p.update(entry_raw_quote_snapshot=raw_snapshot,contract_identity_verified=False)
        if expiry_day:
            p.update(expiry_exit_session=assets[0].expiry,combined_entry_value=entry*qty,overnight=self.c.expiry_entry_mode=='btst')
        if self.c.strategy=='expiry_intraday':
            p.update(expiry_exit_session=date,combined_entry_value=entry*qty,overnight=False)
        self.quotes.update(quotes); self.state['seen_signals'].append(key); self.state['trades'].append(p)
        self.save(); self.emit(f'PAPER OPEN {p["structure_label"]} | {p["contract"]} | net value {entry:.2f}')
        self.on_tick(quotes[assets[0].key],now)
        return True

    def mark(self,tick):
        self._ensure_valid_simulation()
        p=self.position
        if not p or not p.get('legs'): return super().mark(tick)
        quotes=self.fresh_legs(tick.received_time)
        if quotes is None: return None
        if self._validate_snapshot(p['side'],p['legs'],quotes,tick.received_time,'paper_mark') is False: return None
        return sum(l['ratio']*(self.price(q,l['ratio']<0)-l['entry_price'])*p['quantity']-
            fees(self.c,l['entry_price'],self.price(q,l['ratio']<0),p['quantity']*abs(l['ratio']),stamp(tick.received_time)[:10],
                 1 if l['ratio']>0 else -1)['total_cost'] for l,q in zip(p['legs'],quotes))

    def close(self,tick,now,reason):
        self._ensure_valid_simulation()
        p=self.position
        if not p or not p.get('legs'): return super().close(tick,now,reason)
        quotes=self.fresh_legs(now)
        if quotes is None: self.request_exit(reason); return
        raw_snapshot=self._validate_snapshot(p['side'],p['legs'],quotes,now,'paper_close')
        costs={}; gross=raw_gross=0.
        for leg,q in zip(p['legs'],quotes):
            price=self.price(q,leg['ratio']<0); qty=p['quantity']*abs(leg['ratio'])
            leg.update(exit_price=price,raw_exit_price=q.price,quantity=qty)
            charges=fees(self.c,leg['entry_price'],price,qty,stamp(now)[:10],1 if leg['ratio']>0 else -1)
            leg.update(charges)
            for k,v in charges.items(): costs[k]=costs.get(k,0)+v
            gross+=leg['ratio']*(price-leg['entry_price'])*p['quantity']
            raw_gross+=leg['ratio']*(q.price-leg['raw_entry_price'])*p['quantity']
        net=gross-costs['total_cost']
        p.update(status='CLOSED',exit_time=stamp(now),exit_price=sum(l['ratio']*l['exit_price'] for l in p['legs']),
                 raw_exit_price=sum(l['ratio']*l['raw_exit_price'] for l in p['legs']),
                 exit_reason=reason,pending_exit='',gross_pnl=gross,gross_before_slippage=raw_gross,
                 slippage_cost=raw_gross-gross,net_pnl=net,roi_pct=100*net/p['capital_allocated'],**costs)
        if raw_snapshot:p['exit_raw_quote_snapshot']=raw_snapshot
        if p['side'] in ('EXPIRY_DAY','EXPIRY_INTRADAY'):
            ce=next(l for l in p['legs'] if l['side']=='CE');pe=next(l for l in p['legs'] if l['side']=='PE')
            p.update(combined_exit_value=p['exit_price']*p['quantity'],combined_return_pct=100*(p['exit_price']/p['entry_price']-1),
                     ce_entry_price=ce['entry_price'],ce_exit_price=ce['exit_price'],pe_entry_price=pe['entry_price'],pe_exit_price=pe['exit_price'])
        self.save(); self.emit(f'PAPER CLOSE {p["structure_label"]} {reason} | net INR {net:,.2f}')
        self.lock_if_needed(tick,now)

    def on_tick(self,tick,now):
        self._ensure_valid_simulation()
        p=self.position
        if not p or not p.get('legs'): return super().on_tick(tick,now)
        leg=next((l for l in p['legs'] if Asset(**l['asset']).key==tick.key),None)
        if not leg or not tick.fresh(now) or tick.exchange_time<leg['last_epoch']: return
        leg['last_epoch']=tick.exchange_time; self.quotes[tick.key]=tick
        snapshot=self.fresh_legs(now)
        if snapshot is None: return
        if self._validate_snapshot(p['side'],p['legs'],snapshot,now,'paper_on_tick') is False: return
        local=datetime.fromtimestamp(now,IST)
        if p['side']=='EXPIRY_DAY':
            date=local.date().isoformat();expiry=p['expiry_exit_session']
            if date<expiry:
                self.lock_if_needed(tick,now)
                reason=p['pending_exit'] or self.session(now)['lock']
                if reason: self.close(tick,now,reason)
                return
            if date>expiry: self.request_exit('overdue_expiry_recovery')
            elif local.hour*60+local.minute>=minute(self.c.expiry_exit_cutoff): self.request_exit('square_off')
            self.lock_if_needed(tick,now)
            value=sum(q.price for q in self.fresh_legs(now))
            reason=p['pending_exit'] or self.session(now)['lock']
            if not reason and value<=p['stop']: reason='trailing_stop' if p['stop']!=p['initial_stop'] else 'stop_loss'
            if not reason and self.c.exit_mode!='end_of_day' and value>=p['target']: reason='target'
            if reason: self.close(tick,now,reason); return
            p['best']=max(p['best'],value)
            if self.c.exit_mode in ('legacy','trailing_sl') and self.c.trail_pct>0:
                jump=p['entry_price']*self.c.trail_jump_pct/100
                steps=math.floor(max(0,p['best']-p['entry_price'])/jump+1e-10)
                if steps: p['stop']=max(p['stop'],p['entry_price']*(1-self.c.trail_pct/100)+steps*jump)
            self.save();return
        finish=minute(self.c.intraday_exit_cutoff) if p['side']=='EXPIRY_INTRADAY' else 915 if p['side']=='IRON_BUTTERFLY' else 920
        if p['entry_time'][:10]!=local.date().isoformat(): self.request_exit('overnight_recovery')
        elif local.hour*60+local.minute>=finish: self.request_exit('square_off')
        self.lock_if_needed(tick,now)
        value=sum(l['ratio']*q.price for l,q in zip(p['legs'],self.fresh_legs(now)))
        reason=p['pending_exit'] or self.session(now)['lock']
        if not reason and value<=p['stop']: reason='trailing_stop' if p['stop']!=p['initial_stop'] else 'stop_loss'
        if not reason and self.c.exit_mode!='end_of_day' and value>=p['target']: reason='target'
        if reason: self.close(tick,now,reason); return
        p['best']=max(p['best'],value)
        p['stop']=profit_lock(self.c,p['entry_price'],p['best'],p['stop'])
        if (self.c.exit_mode=='trailing_sl' or (self.c.exit_mode=='legacy' and self.c.strategy!='expiry_intraday')) and self.c.trail_pct>0:
            basis=abs(p['entry_price']); jump=basis*self.c.trail_jump_pct/100
            steps=math.floor(max(0,p['best']-p['entry_price'])/jump+1e-10)
            if steps: p['stop']=max(p['stop'],p['entry_price']-basis*self.c.trail_pct/100+steps*jump)
        self.save()

    def heartbeat(self,now):
        self._ensure_valid_simulation()
        p=self.position
        if not p or not p.get('legs'): return super().heartbeat(now)
        local=datetime.fromtimestamp(now,IST)
        if p['side']=='EXPIRY_DAY':
            date=local.date().isoformat()
            if date>p['expiry_exit_session']: self.request_exit('overdue_expiry_recovery')
            elif date==p['expiry_exit_session'] and local.hour*60+local.minute>=minute(self.c.expiry_exit_cutoff): self.request_exit('square_off')
            return
        if p['entry_time'][:10]!=local.date().isoformat() or local.hour*60+local.minute>=(minute(self.c.intraday_exit_cutoff) if p['side']=='EXPIRY_INTRADAY' else 915 if p['side']=='IRON_BUTTERFLY' else 920):
            self.request_exit('square_off')
