"""Atomic virtual option baskets; every leg needs a fresh real contract quote."""
from dataclasses import asdict, replace
from datetime import datetime
import math
import uuid

from paper_engine import PaperAccount, stamp
from paper_feed import Asset, IST
from quant_backtester import fees, minute
from quant_book import basket_allocation, SIDE_LABELS
SIDE_LABELS['EXPIRY_DAY']='EXPIRY DAY (CE+PE)'


class BasketPaperAccount(PaperAccount):
    def __init__(self,*args,**kwargs):
        self.quotes={}
        super().__init__(*args,**kwargs)

    def disconnect(self):
        self.quotes.clear()

    def fresh_legs(self,now):
        p=self.position
        if not p or not p.get('legs'): return None
        quotes=[self.quotes.get(Asset(**l['asset']).key) for l in p['legs']]
        return quotes if all(q and q.fresh(now) for q in quotes) else None

    def enter_basket(self,event,resolved,quotes,now):
        if self.position or not resolved: return False
        local=datetime.fromtimestamp(now,IST); date=local.date().isoformat(); at=local.hour*60+local.minute
        expiry_day=self.c.strategy=='expiry_day'
        if expiry_day:
            from quant_expiry import live_event
            if event!=live_event(self.c,now) or event[1]!='EXPIRY_DAY': return False
        elif event[0]!=at or local.weekday()>=5 or not minute(self.c.entry_start)<=at<=minute(self.c.entry_end): return False
        if event[1]=='IRON_BUTTERFLY' and not 585<=at<915: return False
        if self.session(now)['lock']: return False
        key=f'{date}:basket:{at}'
        if key in self.state['seen_signals']: return False
        if sum(t['entry_time'][:10]==date for t in self.state['trades'])>=self.c.max_trades_day: return False
        assets=[asset for _,asset in resolved]
        if len({a.expiry for a in assets})!=1 or any(a.expiry<date for a in assets) or len({a.point_value for a in assets})!=1: return False
        if expiry_day:
            from quant_expiry import previous_session
            if len(resolved)!=2 or {s['side'] for s,_ in resolved}!={'CE','PE'} or any(s['ratio']!=1 for s,_ in resolved): return False
            if (previous_session(assets[0].expiry) if self.c.expiry_entry_mode=='btst' else assets[0].expiry)!=date: return False
        legs=[]
        for spec,asset in resolved:
            q=quotes.get(asset.key)
            if not q or not q.fresh(now) or (spec['ratio']>0 and q.price<self.c.minimum_premium): return False
            legs.append(dict(spec,asset=asdict(asset),raw_entry_price=q.price,
                             entry_price=self.price(q,spec['ratio']>0),last_epoch=q.exchange_time))
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
            stop=entry-distance,initial_stop=entry-distance,stop_distance=distance,target=entry+abs(entry)*self.c.target_pct/100,
            best=entry,net_pnl=None,exit_reason='',pending_exit='',paper=True)
        if expiry_day:
            p.update(expiry_exit_session=assets[0].expiry,combined_entry_value=entry*qty,overnight=self.c.expiry_entry_mode=='btst')
        self.quotes.update(quotes); self.state['seen_signals'].append(key); self.state['trades'].append(p)
        self.save(); self.emit(f'PAPER OPEN {p["structure_label"]} | {p["contract"]} | net value {entry:.2f}')
        self.on_tick(quotes[assets[0].key],now)
        return True

    def mark(self,tick):
        p=self.position
        if not p or not p.get('legs'): return super().mark(tick)
        quotes=self.fresh_legs(tick.received_time)
        if quotes is None: return None
        return sum(l['ratio']*(self.price(q,l['ratio']<0)-l['entry_price'])*p['quantity']-
            fees(self.c,l['entry_price'],self.price(q,l['ratio']<0),p['quantity']*abs(l['ratio']),stamp(tick.received_time)[:10],
                 1 if l['ratio']>0 else -1)['total_cost'] for l,q in zip(p['legs'],quotes))

    def close(self,tick,now,reason):
        p=self.position
        if not p or not p.get('legs'): return super().close(tick,now,reason)
        quotes=self.fresh_legs(now)
        if quotes is None: self.request_exit(reason); return
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
                 exit_reason=reason,pending_exit='',gross_pnl=gross,gross_before_slippage=raw_gross,
                 slippage_cost=raw_gross-gross,net_pnl=net,roi_pct=100*net/p['capital_allocated'],**costs)
        if p['side']=='EXPIRY_DAY':
            ce=next(l for l in p['legs'] if l['side']=='CE');pe=next(l for l in p['legs'] if l['side']=='PE')
            p.update(combined_exit_value=p['exit_price']*p['quantity'],combined_return_pct=100*(p['exit_price']/p['entry_price']-1),
                     ce_entry_price=ce['entry_price'],ce_exit_price=ce['exit_price'],pe_entry_price=pe['entry_price'],pe_exit_price=pe['exit_price'])
        self.save(); self.emit(f'PAPER CLOSE {p["structure_label"]} {reason} | net INR {net:,.2f}')
        self.lock_if_needed(tick,now)

    def on_tick(self,tick,now):
        p=self.position
        if not p or not p.get('legs'): return super().on_tick(tick,now)
        leg=next((l for l in p['legs'] if Asset(**l['asset']).key==tick.key),None)
        if not leg or not tick.fresh(now) or tick.exchange_time<leg['last_epoch']: return
        leg['last_epoch']=tick.exchange_time; self.quotes[tick.key]=tick
        if self.fresh_legs(now) is None: return
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
        finish=915 if p['side']=='IRON_BUTTERFLY' else 920
        if p['entry_time'][:10]!=local.date().isoformat(): self.request_exit('overnight_recovery')
        elif local.hour*60+local.minute>=finish: self.request_exit('square_off')
        self.lock_if_needed(tick,now)
        value=sum(l['ratio']*q.price for l,q in zip(p['legs'],self.fresh_legs(now)))
        reason=p['pending_exit'] or self.session(now)['lock']
        if not reason and value<=p['stop']: reason='trailing_stop' if p['stop']!=p['initial_stop'] else 'stop_loss'
        if not reason and (p['side']=='IRON_BUTTERFLY' or self.c.exit_mode!='end_of_day') and value>=p['target']: reason='target'
        if reason: self.close(tick,now,reason); return
        p['best']=max(p['best'],value)
        if self.c.exit_mode in ('legacy','trailing_sl') and self.c.trail_pct>0:
            basis=abs(p['entry_price']); jump=basis*self.c.trail_jump_pct/100
            steps=math.floor(max(0,p['best']-p['entry_price'])/jump+1e-10)
            if steps: p['stop']=max(p['stop'],p['entry_price']-basis*self.c.trail_pct/100+steps*jump)
        self.save()

    def heartbeat(self,now):
        p=self.position
        if not p or not p.get('legs'): return super().heartbeat(now)
        local=datetime.fromtimestamp(now,IST)
        if p['side']=='EXPIRY_DAY':
            date=local.date().isoformat()
            if date>p['expiry_exit_session']: self.request_exit('overdue_expiry_recovery')
            elif date==p['expiry_exit_session'] and local.hour*60+local.minute>=minute(self.c.expiry_exit_cutoff): self.request_exit('square_off')
            return
        if p['entry_time'][:10]!=local.date().isoformat() or local.hour*60+local.minute>=(915 if p['side']=='IRON_BUTTERFLY' else 920):
            self.request_exit('square_off')
