"""Deterministic virtual execution and crash-recoverable local paper account."""
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import csv
import hashlib
import json
import math
import os
import uuid
import pandas as pd

from paper_feed import IST, Asset, Tick
from quant_backtester import (Config, make_bars, adverse_tick, order_allocation, fees, HIGH_STRATEGIES,
                              RESEARCH_STRATEGIES, STRATEGIES, BOOK_STRATEGIES, POINT_VALUE, research_events, research_indicators, minute)
from quant_high_edge import high_edge_events


def stamp(epoch): return datetime.fromtimestamp(epoch,IST).isoformat(timespec='seconds')


def atomic(path,value):
    path=Path(path); tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        json.dump(value,f,indent=2,allow_nan=False); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)


class AccountLock:
    """An OS lock, released on process death; a stale file doesn't block recovery."""
    def __init__(self,root): self.path=Path(root)/'.account.lock'; self.file=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open('a+b'); self.file.seek(0,2)
        if not self.file.tell(): self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close(); raise ValueError('This paper account is already running') from None
        return self
    def __exit__(self,*args): self.file.close()


class MinuteBook:
    """No forward-filled gap candles. REST warmup never creates retroactive trades."""
    def __init__(self,history_days=0):
        self.history_days=history_days
        self.rows={}; self.current=None; self.cumulative=None; self.last_epoch=None; self.last_received=None
        self.last_signal_minute=None; self.day=None
    def seed(self,rows,now):
        day=datetime.fromtimestamp(now,IST).date()
        if self.day!=day: self.rows={}; self.current=None; self.day=day
        for row in rows:
            age=(day-datetime.fromtimestamp(row['timestamp'],IST).date()).days
            if 0<=age<=self.history_days and row['timestamp']+60<=now:
                self.rows[int(row['timestamp'])]=dict(row)
    def disconnect(self):
        if self.current: self.current['valid']=False
        self.last_received=None; self.cumulative=None
    def ingest(self,tick):
        day=datetime.fromtimestamp(tick.exchange_time,IST).date()
        if day!=self.day:
            self.rows={}; self.current=None; self.cumulative=None; self.last_epoch=None; self.day=day
        if self.last_epoch is not None and tick.exchange_time<self.last_epoch: return
        minute=int(tick.exchange_time)//60*60
        volume=tick.volume
        delta=0 if volume is None or self.cumulative is None else max(0,volume-self.cumulative)
        reset=volume is not None and self.cumulative is not None and volume<self.cumulative
        gap=self.last_received is None or tick.received_time-self.last_received>15
        if self.current and self.current['timestamp']!=minute:
            old=self.current
            if old['valid'] and not gap:
                self.rows[old['timestamp']]={k:old[k] for k in ('timestamp','open','high','low','close','volume','oi')}
            self.current=None
        if self.current is None:
            self.current=dict(timestamp=minute,open=tick.price,high=tick.price,low=tick.price,close=tick.price,volume=delta,
                              valid=not gap and not reset)
        else:
            self.current.update(high=max(self.current['high'],tick.price),low=min(self.current['low'],tick.price),close=tick.price)
            self.current['volume']+=delta
            self.current['valid'] &= not gap and not reset
        self.current['oi']=tick.oi
        self.cumulative=volume; self.last_epoch=tick.exchange_time; self.last_received=tick.received_time
    def signal(self,c,now):
        local=datetime.fromtimestamp(now,IST); at=local.hour*60+local.minute
        n=int(c.timeframe[:-1]); minute_epoch=int(now)//60*60
        if at%n or self.last_signal_minute==minute_epoch: return None
        self.last_signal_minute=minute_epoch
        research = c.strategy in STRATEGIES
        if not (minute(c.entry_start)<=at<=minute(c.entry_end) if research else 1050<=at<=1290) or local.second>15: return None
        # Require every warmup/current session minute since 16:30. A missing
        # quote interval invalidates entry rather than becoming a flat candle.
        hour, mins = (9,0) if c.instrument in POINT_VALUE else (9,15)
        begin=int(local.replace(hour=hour if research else 16,minute=mins if research else 30,second=0,microsecond=0).timestamp())
        required=range(begin,minute_epoch,60)
        if any(t not in self.rows for t in required): return None
        rows=[r for t,r in sorted(self.rows.items()) if t<minute_epoch]
        if len(rows)<60: return None
        frame=pd.DataFrame(rows)
        frame['timestamp']=pd.to_datetime(frame.timestamp,unit='s',utc=True).dt.tz_convert(IST)
        bars=make_bars(frame,c.timeframe)
        if research: bars=research_indicators(bars)
        date=local.date().isoformat()
        today=frame[frame.timestamp.dt.date.eq(local.date())]
        prices={r.timestamp.hour*60+r.timestamp.minute:(r.open,r.high,r.low,r.close,r.volume) for r in today.itertuples()}
        day=dict(prices=prices,timestamp=pd.Timestamp(local.date(),tz=IST))
        day['oi']={r.timestamp.hour*60+r.timestamp.minute:getattr(r,'oi',float('nan')) for r in today.itertuples()}
        bars=bars[bars.timestamp.dt.date.eq(local.date())].reset_index(drop=True)
        mcx=c.instrument in POINT_VALUE
        # Live index ORB confirmation is supplied by the resolved option's
        # observed warm-up in the runner, never by fabricated index volume.
        day['rolling']={(side,'ATM'):{at-1:0} for side in ('CE','PE')}
        data=SimpleNamespace(mcx=mcx,days={date:day},features=lambda tf:{date:bars},price=lambda *args:None)
        events=research_events(data,c,date,confirm_volume=mcx) if research else high_edge_events(data,c,date)
        return next((e for e in reversed(events) if e[0]==at),None)


class PaperAccount:
    """One virtual position, fixed-contract identity and account-level daily locks."""
    def __init__(self,root,c,asset,emit=print):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.c=c; c.validate(); self.asset=asset; self.emit=emit
        policy=asdict(c); policy.pop('start'); policy.pop('end')
        # These fields do not affect existing strategies. Preserve their
        # durable policy hashes when loading an account created before books.
        if c.strategy not in BOOK_STRATEGIES:
            policy.pop('momentum_lookback'); policy.pop('momentum_threshold')
        signature=hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest()
        path=self.root/'state.json'
        if path.exists():
            self.state=json.loads(path.read_text(encoding='utf-8'))
            if self.state.get('schema')!=1 or self.state.get('policy_hash')!=signature:
                raise ValueError('Paper account settings differ; use the original settings or a new --output directory')
            if sum(t['status']=='OPEN' for t in self.state['trades'])>1: raise ValueError('Invalid paper state: multiple open positions')
            if len({t['trade_id'] for t in self.state['trades']})!=len(self.state['trades']): raise ValueError('Duplicate trade IDs in state')
        else:
            self.state=dict(schema=1,mode='PAPER_ONLY',policy=policy,policy_hash=signature,trades=[],sessions={},seen_signals=[])
        self.save()
    @property
    def position(self): return next((t for t in self.state['trades'] if t['status']=='OPEN'),None)
    @property
    def equity(self): return self.c.capital+sum(t['net_pnl'] for t in self.state['trades'] if t['status']=='CLOSED')
    def session(self,now):
        date=datetime.fromtimestamp(now,IST).date().isoformat()
        if date not in self.state['sessions']:
            self.state['sessions'][date]=dict(opening_equity=self.equity,lock='',lock_time='',lock_pnl=None)
        return self.state['sessions'][date]
    def realized_today(self,now):
        date=stamp(now)[:10]
        return sum(t['net_pnl'] for t in self.state['trades'] if t['status']=='CLOSED' and t['exit_time'][:10]==date)
    def save(self):
        atomic(self.root/'state.json',self.state)
        records=[]
        for t in self.state['trades']:
            records.append({k:v for k,v in t.items() if k not in ('asset',)})
        columns=['trade_id','instrument','strategy','security_id','expiry','status','side','entry_time','exit_time',
            'entry_price','exit_price','quantity','lots','capital_allocated','capital_reserved','stop','target','net_pnl','exit_reason']
        columns+=sorted(set().union(*(r.keys() for r in records))-set(columns)) if records else []
        tmp=self.root/'ledger.csv.tmp'
        with tmp.open('w',newline='',encoding='utf-8') as f:
            writer=csv.DictWriter(f,fieldnames=columns); writer.writeheader(); writer.writerows(records)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp,self.root/'ledger.csv')
    def price(self,tick,buy):
        depth=tick.ask if buy else tick.bid
        base=depth if depth is not None else tick.price
        return adverse_tick(base+(self.c.slippage if buy else -self.c.slippage),self.c,buy)
    def mark(self,tick):
        p=self.position
        if p is None or tick.key!=Asset(**p['asset']).key: return None
        direction=p['direction']; price=self.price(tick,direction==-1)
        cost=fees(self.c,p['entry_price'],price,p['quantity'],stamp(tick.received_time)[:10],direction)
        return direction*(price-p['entry_price'])*p['quantity']-cost['total_cost']
    def lock_if_needed(self,tick,now):
        session=self.session(now)
        if session['lock']: return False
        net=self.realized_today(now)+(self.mark(tick) or 0)
        positive=lambda *xs:min((v for v in xs if v>0),default=float('inf'))
        goal=positive(self.c.daily_profit_target,session['opening_equity']*self.c.daily_profit_pct/100)
        loss=positive(self.c.daily_loss_limit,session['opening_equity']*self.c.daily_loss_pct/100)
        reason='daily_loss_limit' if net<=-loss+1e-8 else 'daily_profit_target' if net>=goal-1e-8 else ''
        if reason:
            session.update(lock=reason,lock_time=stamp(now),lock_pnl=net)
            self.emit(f'PAPER LOCK {reason} | day net INR {net:,.2f}')
            self.save(); return True
        return False
    def enter(self,event,tick,now):
        if self.position or tick.key!=self.asset.key or not tick.fresh(now): return False
        self.lock_if_needed(tick,now)
        local=datetime.fromtimestamp(now,IST); date=local.date().isoformat()
        research=self.c.strategy in STRATEGIES
        at=local.hour*60+local.minute
        in_window=minute(self.c.entry_start)<=at<=minute(self.c.entry_end) if research else 1050<=at<=1290
        if local.weekday()>=5 or not in_window or self.asset.expiry<date: return False
        if event[0]!=at or event[1] not in ('LONG','SHORT','CE','PE'): return False
        if event[1] in ('CE','PE') and self.asset.option_type!=event[1]: return False
        signal_key=f'{date}:{self.asset.key}:{event[0]}'
        if self.session(now)['lock'] or signal_key in self.state['seen_signals']: return False
        count=sum(t['entry_time'][:10]==date for t in self.state['trades'])
        cap=1 if self.c.strategy==HIGH_STRATEGIES[3] else self.c.max_trades_day
        if count>=cap: return False
        self.state['seen_signals'].append(signal_key)
        side=event[1]; direction=-1 if side=='SHORT' else 1
        entry=self.price(tick,direction==1)
        if self.asset.option_type and (tick.price<self.c.minimum_premium or self.c.option_side not in ('both',side)): return False
        budget=max(0,min(self.c.capital,self.equity))*self.c.capital_usage_pct/100
        allocation_config=replace(self.c,lot_size=self.asset.point_value)
        qty,allocated,reserve=order_allocation(allocation_config,date,entry,budget,direction)
        atr_risk=self.c.instrument in POINT_VALUE and (self.c.risk_basis=='legacy' or self.c.strategy in BOOK_STRATEGIES)
        distance=event[4]*self.c.atr_sl if atr_risk else entry*self.c.sl_pct/100
        if self.c.instrument in POINT_VALUE and self.c.strategy in BOOK_STRATEGIES: distance=min(distance,entry*self.c.sl_pct/100)
        target_distance=distance*self.c.reward_risk if atr_risk else entry*self.c.target_pct/100
        if qty<=0 or not math.isfinite(distance) or distance<=0:
            self.emit('PAPER SKIP insufficient allocation or invalid ATR'); self.save(); return False
        p=dict(trade_id='paper_'+uuid.uuid4().hex[:16],instrument=self.asset.name,strategy=self.c.strategy,
            asset=asdict(self.asset),security_id=self.asset.security_id,expiry=self.asset.expiry,status='OPEN',side=side,direction=direction,
            entry_time=stamp(now),entry_exchange_time=stamp(tick.exchange_time),exit_time='',entry_price=entry,exit_price=None,
            quantity=qty,lots=qty/self.asset.point_value,capital_allocated=allocated,capital_reserved=allocated+reserve,
            capital_used_pct=100*allocated/self.equity,stop=entry-direction*distance,initial_stop=entry-direction*distance,
            stop_distance=distance,target=entry+direction*target_distance,best=entry,
            net_pnl=None,exit_reason='',pending_exit='',last_ltp=tick.price,last_quote_time=stamp(tick.exchange_time),last_epoch=tick.exchange_time,
            entry_fill_basis='best_quote_plus_slippage' if tick.bid is not None else 'ltp_plus_slippage',paper=True)
        self.state['trades'].append(p); self.save()
        self.emit(f'PAPER OPEN {side} {self.asset.name} {self.asset.security_id} | {p["lots"]:g} lot(s) @ {entry:.2f} | SL {p["stop"]:.2f} target {p["target"]:.2f}')
        # Entry slippage/cost alone can cross the daily cutoff.
        self.on_tick(tick,now)
        return True
    def close(self,tick,now,reason):
        p=self.position
        if p is None: return
        if not tick.fresh(now) or tick.key!=Asset(**p['asset']).key or tick.exchange_time<p.get('last_epoch',0):
            self.request_exit(reason); return
        price=self.price(tick,p['direction']==-1)
        costs=fees(self.c,p['entry_price'],price,p['quantity'],stamp(now)[:10],p['direction'])
        gross=p['direction']*(price-p['entry_price'])*p['quantity']
        p.update(status='CLOSED',exit_time=stamp(now),exit_exchange_time=stamp(tick.exchange_time),exit_price=price,
            exit_reason=reason,pending_exit='',gross_pnl=gross,net_pnl=gross-costs['total_cost'],
            roi_pct=100*(gross-costs['total_cost'])/p['capital_allocated'],
            exit_fill_basis='best_quote_plus_slippage' if tick.bid is not None else 'ltp_plus_slippage',**costs)
        self.save(); self.emit(f'PAPER CLOSE {p["instrument"]} {reason} @ {price:.2f} | net INR {p["net_pnl"]:,.2f} | day INR {self.realized_today(now):,.2f}')
        self.lock_if_needed(tick,now)
    def request_exit(self,reason):
        p=self.position
        if p and not p['pending_exit']:
            p['pending_exit']=reason; self.save(); self.emit(f'PAPER EXIT PENDING {reason}: waiting for a fresh quote')
    def on_tick(self,tick,now):
        p=self.position
        if not p or tick.key!=Asset(**p['asset']).key or not tick.fresh(now): return
        if tick.exchange_time<p.get('last_epoch',0): return
        p.update(last_ltp=tick.price,last_quote_time=stamp(tick.exchange_time),last_epoch=tick.exchange_time)
        local=datetime.fromtimestamp(now,IST)
        if p['entry_time'][:10]!=local.date().isoformat():
            self.session(now)['lock']='overnight_recovery'; self.request_exit('overnight_recovery')
        elif local.hour*60+local.minute>=(1395 if self.c.instrument in POINT_VALUE else 920): self.request_exit('square_off')
        self.lock_if_needed(tick,now)
        reason=p['pending_exit'] or self.session(now)['lock']
        direction=p['direction']
        # Trigger on live LTP; execute on bid/ask plus adverse slippage.
        if not reason and direction*(tick.price-p['stop'])<=0:
            reason='trailing_stop' if p['stop']!=p['initial_stop'] else 'stop_loss'
        if not reason and self.c.exit_mode!='end_of_day' and direction*(tick.price-p['target'])>=0: reason='target'
        if reason: self.close(tick,now,reason); return
        p['best']=max(p['best'],tick.price) if direction==1 else min(p['best'],tick.price)
        atr_risk=self.c.instrument in POINT_VALUE and (self.c.risk_basis=='legacy' or self.c.strategy in BOOK_STRATEGIES)
        jump=p['stop_distance']*self.c.atr_trail_jump if atr_risk else p['entry_price']*self.c.trail_jump_pct/100
        steps=math.floor(max(0,direction*(p['best']-p['entry_price']))/jump+1e-10)
        distance=p['stop_distance'] if atr_risk else p['entry_price']*self.c.trail_pct/100
        proposed=p['entry_price']-direction*distance+direction*steps*jump
        stop=max(p['stop'],proposed) if direction==1 else min(p['stop'],proposed)
        if self.c.exit_mode in ('legacy','trailing_sl') and self.c.trail_pct>0 and stop!=p['stop']:
            p['stop']=stop; self.save()
            self.emit(f'PAPER TRAIL {p["instrument"]} SL -> {stop:.2f}')
    def heartbeat(self,now):
        p=self.position
        if p:
            local=datetime.fromtimestamp(now,IST)
            if p['entry_time'][:10]!=local.date().isoformat() or local.hour*60+local.minute>=(1395 if self.c.instrument in POINT_VALUE else 920):
                self.request_exit('square_off')
