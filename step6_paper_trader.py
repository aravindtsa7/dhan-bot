"""Dhan v2 live quotes with virtual execution ONLY. No broker order API calls."""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import time
from urllib.parse import urlencode

from dotenv import load_dotenv
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from paper_feed import (Asset, Tick, IST, SEGMENTS, FeedError, decode_packets, instruments, warmup,
                        master_path, option_asset, option_volume_confirmed)
from paper_engine import PaperAccount, AccountLock, MinuteBook, atomic, stamp
from quant_backtester import Config, HIGH_STRATEGIES, RESEARCH_STRATEGIES, STRATEGIES, BOOK_STRATEGIES, POINT_VALUE

BASE=Path(__file__).resolve().parent


def book_activity_confirmed(rows,c,event,now):
    """Same fixed-contract activity filter as historical book signals."""
    import pandas as pd
    from types import SimpleNamespace
    from quant_book import activity
    local=datetime.fromtimestamp(now,IST)
    candles={int((datetime.fromtimestamp(r['timestamp'],IST)-local.replace(hour=0,minute=0,second=0,microsecond=0)).total_seconds()/60):r
             for r in rows if datetime.fromtimestamp(r['timestamp'],IST).date()==local.date()}
    day={'option_meta':{(k,'CE',0):(r.get('oi',float('nan')),) for k,r in candles.items()}}
    data=SimpleNamespace(mcx=False,price=lambda d,k,s,strike:tuple(candles[k][v] for v in ('open','high','low','close','volume')) if k in candles else None)
    a=activity(data,day,pd.DataFrame({'volume':[0]*(c.momentum_lookback+1)}),c.momentum_lookback,event[0],'CE',0,c)
    return bool(a and a['signal_log_volume']>=math.log(c.volume_multiple) and
                (c.strategy!='book_vol_oi_momentum' or not a['signal_oi_available'] or a['signal_log_oi']>0))


def logger(root):
    log=logging.getLogger('paper.console'); log.setLevel(logging.INFO); log.handlers.clear(); log.propagate=False
    fmt=logging.Formatter('%(asctime)s %(message)s')
    for handler in (logging.StreamHandler(),RotatingFileHandler(root/'paper_trader.log',maxBytes=2_000_000,backupCount=3,encoding='utf-8')):
        handler.setFormatter(fmt); log.addHandler(handler)
    # A WebSocket URL contains credentials: never allow transport debug logging.
    transport=logging.getLogger('paper.transport'); transport.disabled=True
    logging.getLogger('websockets').disabled=True
    return log,transport


def configuration(args):
    if getattr(args,'saved_strategy',None):
        from quant_terminal import load_saved_strategies
        records=load_saved_strategies(args.strategies_file)
        record=next((r for r in records if args.saved_strategy in (r['strategy_id'],r['name'])),None)
        if record is None: raise ValueError('Saved strategy was not found')
        c=Config(**record['config'])
        if c.strategy not in STRATEGIES: raise ValueError('Saved strategy logic is unsupported')
        today=datetime.now(IST).date()
        c=replace(c,start=str(today),end=str(today+timedelta(days=1)))
        c.validate()
        return c
    if args.strategy=='h4' and args.trade_instrument!='CRUDEOIL':
        raise ValueError('H4 default is Crude Oil; use --strategy h6 for Natural Gas')
    if args.strategy=='h4' and args.max_trades!=1:
        raise ValueError('H4 permits one entry per session')
    today=datetime.now(IST).date()
    return Config(instrument=args.trade_instrument,strategy=HIGH_STRATEGIES[3 if args.strategy=='h4' else 5],
        timeframe='5m' if args.strategy=='h4' else '3m',start=str(today),end=str(today+timedelta(days=1)),
        capital=args.capital,capital_usage_pct=args.allocation,futures_margin_pct=args.margin,
        sizing_mode='fixed_lots',lots=args.lots,max_trades_day=args.max_trades,
        slippage=args.slippage if args.slippage is not None else .5 if args.trade_instrument=='NATURALGAS' else 1.,
        atr_sl=args.atr_stop,reward_risk=args.reward_risk,atr_trail_jump=args.trailing_jump,
        trail_pct=20,daily_loss_limit=args.daily_loss,daily_profit_target=args.daily_profit,
        compression_ratio=1,atr_expansion=1 if args.strategy=='h4' else 1.5,
        entry_start='17:30',entry_end='21:30')


def demo(args,c,emit):
    """Synthetic execution exercise, deliberately separate from the live account."""
    asset=Asset('CRUDEOIL',5,999999,'2026-11-19',100)
    now=datetime(2026,10,23,17,35,tzinfo=IST).timestamp()
    account=PaperAccount(args.output,c,asset,emit)
    tick=lambda price,t:Tick(asset.key,price,t,t,1000,10000,price-.5,price+.5,100,100)
    emit('DEMO: synthetic ticks and an injected signal; no live data or broker access')
    if not account.state['trades']:
        account.enter((1055,'LONG',0,None,10),tick(100,now),now)
    if account.position:
        now=max(now,account.position.get('last_epoch',now))
        for i,p in enumerate((110,125,105,85),1): account.on_tick(tick(p,now+i),now+i)
    account.save()
    emit(f'DEMO finished | trades={len(account.state["trades"])} open={int(account.position is not None)} | ledger={args.output/"ledger.csv"}')


async def live(args,c,emit,transport):
    load_dotenv(BASE/'.env')
    client_id=os.environ.get('DHAN_CLIENT_ID','').strip(); token=os.environ.get('DHAN_ACCESS_TOKEN','').strip()
    if not client_id or not token: raise ValueError('Missing DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in .env')
    assets=await asyncio.to_thread(instruments,args.output,time.time())
    chosen=next(a for a in assets if a.name==c.instrument)
    from paper_basket import BasketPaperAccount
    from paper_feed import option_structure_assets
    account=BasketPaperAccount(args.output,c,chosen,emit)
    if account.position:
        opened=Asset(**account.position['asset'])
        if opened.key not in {a.key for a in assets}: assets.append(opened)
        for leg in account.position.get('legs',[]):
            opened=Asset(**leg['asset'])
            if opened.key not in {a.key for a in assets}: assets.append(opened)
    by_key={a.key:a for a in assets}; book=MinuteBook(history_days=5 if c.strategy in STRATEGIES else 0)
    option_master=None; pending=None
    if c.instrument not in POINT_VALUE:
        import pandas as pd
        option_master=pd.read_csv(master_path(args.output,time.time()),low_memory=False)
    emit('PAPER ONLY | virtual fills; no order placement/cancellation endpoints are available')
    emit(f'Strategy {c.strategy} {chosen.name} signal contract {chosen.security_id}; PAPER ONLY')
    emit(f'Capital INR {c.capital:,.0f} | allocation {c.capital_usage_pct:g}% | assumed margin {c.futures_margin_pct:g}% | daily loss/profit {c.daily_loss_limit:g}/{c.daily_profit_target:g}')
    stopping=False; shutdown_started=None; started=time.monotonic(); ws=None
    last_print=0.; last_seed=0.; seed_task=None; last_message=0.; retry_at=0.; attempts=0
    latest={}; last_epochs={}; counters={a.name:0 for a in assets}; rejects=0; seed_count=0
    fresh_counts={a.name:0 for a in assets}
    stop_reason='shutdown'; fatal=None
    def stop(*_):
        nonlocal stopping,shutdown_started
        stopping=True
        if shutdown_started is None: shutdown_started=time.monotonic()
    old_signals={}
    for sig in (signal.SIGINT,signal.SIGTERM):
        old_signals[sig]=signal.signal(sig,stop)
    async def drop():
        nonlocal ws,latest,retry_at,attempts
        if ws:
            try: await ws.close()
            except Exception: pass
        ws=None; latest={}; book.disconnect(); account.disconnect(); attempts+=1
        retry_at=time.monotonic()+min(30,2**min(attempts-1,5))
    try:
        while True:
            now=time.time(); mono=time.monotonic(); local=datetime.fromtimestamp(now,IST)
            if (args.output/'STOP').exists(): stop()
            if args.duration and mono-started>=args.duration: stop()
            if local.hour*60+local.minute>=(1396 if c.instrument in POINT_VALUE else 921): stop()
            if stopping:
                account.request_exit(stop_reason)
                p=account.position
                if p:
                    q=latest.get(Asset(**p['asset']).key)
                    if ws and q and q.fresh(now): account.close(q,now,p['pending_exit'] or stop_reason)
                if not account.position or mono-shutdown_started>=10: break
            account.heartbeat(now)
            p=account.position
            if p and p['pending_exit']:
                q=latest.get(Asset(**p['asset']).key)
                if ws and q and q.fresh(now): account.close(q,now,p['pending_exit'])
            if ws is None and mono>=retry_at:
                try:
                    uri='wss://api-feed.dhan.co?'+urlencode(dict(version=2,token=token,clientId=client_id,authType=2))
                    ws=await connect(uri,open_timeout=10,close_timeout=2,ping_interval=10,ping_timeout=20,max_queue=32,logger=transport)
                    for code in (15,21):
                        group=[a for a in assets if (15 if a.segment==0 else 21)==code]
                        await ws.send(json.dumps(dict(RequestCode=code,InstrumentCount=len(group),InstrumentList=[dict(ExchangeSegment=SEGMENTS[a.segment],SecurityId=str(a.security_id)) for a in group])))
                    last_message=mono; latest={}; book.disconnect(); account.disconnect()
                    emit('FEED connected; subscriptions sent for '+', '.join(a.name for a in assets))
                    last_seed=0
                except Exception:
                    emit('FEED connection failed; credentials and upstream exception text suppressed')
                    await drop()
            if ws and seed_task is None and mono-last_seed>=60 and not stopping:
                last_seed=mono; snapshot=now
                seed_task=asyncio.create_task(asyncio.to_thread(warmup,client_id,token,chosen,snapshot,5 if c.strategy in STRATEGIES else 0))
                seed_task.snapshot=snapshot
            if seed_task and seed_task.done():
                try:
                    rows=seed_task.result(); book.seed(rows,seed_task.snapshot); seed_count+=1
                    if seed_count==1: emit(f'WARMUP loaded {len(rows)} completed minute candles; no historical entries replayed')
                except FeedError as exc:
                    emit(f'WARMUP {exc}; entries wait for complete observed candles')
                    if str(exc) in ('WARMUP_HTTP_401','WARMUP_HTTP_403'):
                        fatal=str(exc); stop()
                except Exception: emit('WARMUP failed; response details suppressed')
                seed_task=None
            if ws:
                ticks=[]
                try:
                    raw=await asyncio.wait_for(ws.recv(),timeout=1)
                    received=time.time(); ticks=decode_packets(raw,received); last_message=time.monotonic()
                except TimeoutError:
                    if mono-last_message>45:
                        emit('FEED no market packets for 45s; reconnecting'); await drop()
                except FeedError as exc:
                    emit(f'FEED {exc}')
                    if str(exc).startswith('FEED_DISCONNECT_') and str(exc).split('_')[-1] in ('806','807','808','809','810'):
                        fatal=str(exc); stop(); await drop()
                    else: rejects+=1; await drop()
                except (WebSocketException,OSError):
                    emit('FEED interrupted; open positions retained, entries paused'); await drop()
                # Engine/persistence errors must halt the runner, not masquerade
                # as transport failures and retry indefinitely.
                for tick in ticks:
                    if tick.key not in by_key: continue
                    name=by_key[tick.key].name; counters[name]+=1
                    if not tick.fresh(received):
                        latest[tick.key]=tick; rejects+=1; continue
                    if tick.exchange_time<last_epochs.get(tick.key,0): rejects+=1; continue
                    last_epochs[tick.key]=tick.exchange_time
                    latest[tick.key]=tick
                    fresh_counts[name]+=1
                    attempts=0
                    account.on_tick(tick,received)
                    if pending and isinstance(pending[1],list):
                        event,resolved,deadline=pending
                        if received>deadline: pending=None
                        elif not account.position and not stopping:
                            if account.enter_basket(event,resolved,latest,received): pending=None
                    elif pending and tick.key==pending[1].key:
                        event,asset,deadline=pending
                        if received<=deadline and not account.position and not stopping:
                            account.asset=asset
                            account.enter(event,tick,received)
                        pending=None
                    if tick.key==chosen.key:
                        book.ingest(tick)
                        event=book.signal(c,received)
                        if event and not args.monitor_only and not stopping and not account.position:
                            if c.instrument in POINT_VALUE:
                                account.enter(event,tick,received)
                            elif c.strategy in BOOK_STRATEGIES[:3]:
                                resolved=option_structure_assets(option_master,c.instrument,event[1],tick.price,received)
                                confirmed=True
                                if c.strategy=='book_strap_strip':
                                    asset=next(a for spec,a in resolved if spec['side']==('CE' if event[1]=='STRAP' else 'PE'))
                                    rows=await asyncio.to_thread(warmup,client_id,token,asset,received)
                                    confirmed=book_activity_confirmed(rows,c,event,received)
                                if confirmed:
                                    for _,asset in resolved:
                                        if asset.key not in by_key:
                                            assets.append(asset); by_key[asset.key]=asset
                                            await ws.send(json.dumps(dict(RequestCode=21,InstrumentCount=1,InstrumentList=[dict(ExchangeSegment=SEGMENTS[asset.segment],SecurityId=str(asset.security_id))])))
                                    pending=(event,resolved,int(received)//60*60+15)
                            else:
                                asset=option_asset(option_master,c.instrument,event[1],tick.price,received)
                                confirmed=True
                                if c.strategy in ('orb_volume_breakout','book_vol_oi_momentum'):
                                    rows=await asyncio.to_thread(warmup,client_id,token,asset,received)
                                    confirmed=book_activity_confirmed(rows,c,event,received) if c.strategy=='book_vol_oi_momentum' else option_volume_confirmed(rows,c,event,received)
                                if confirmed:
                                    if asset.key not in by_key:
                                        assets.append(asset); by_key[asset.key]=asset
                                        await ws.send(json.dumps(dict(RequestCode=21,InstrumentCount=1,InstrumentList=[dict(ExchangeSegment=SEGMENTS[asset.segment],SecurityId=str(asset.security_id))])))
                                    pending=(event,asset,int(received)//60*60+15)
            else: await asyncio.sleep(.25)
            if mono-last_print>=args.status_seconds:
                last_print=mono; now=time.time(); quotes={}; parts=[]
                for asset in assets:
                    q=latest.get(asset.key); fresh=bool(q and q.fresh(now))
                    quotes[asset.name]=dict(ltp=q.price if q else None,exchange_time=stamp(q.exchange_time) if q else None,
                        fresh=fresh,age_seconds=round(now-q.exchange_time,1) if q else None,wire_epoch=q.wire_epoch if q else None)
                    parts.append(f'{asset.name}={q.price:.2f}{"" if fresh else "(STALE)"}' if q else f'{asset.name}=WAIT')
                p=account.position; mark=None
                if p:
                    q=latest.get(Asset(**p['asset']).key)
                    if q and q.fresh(now): mark=account.mark(q)
                positions=[]
                if p:
                    q=latest.get(Asset(**p['asset']).key)
                    live_value=q.price if q and q.fresh(now) else None
                    quote_time=stamp(q.exchange_time) if q else None
                    if p.get('legs'):
                        leg_quotes=account.fresh_legs(now)
                        live_value=sum(l['ratio']*q.price for l,q in zip(p['legs'],leg_quotes)) if leg_quotes else None
                        quote_time=stamp(min(q.exchange_time for q in leg_quotes)) if leg_quotes else None
                    positions.append(dict(trade_id=p['trade_id'],security_id=p['security_id'],
                        ltp=live_value,quote_time=quote_time,net_pnl=mark,contract=p.get('contract','')))
                status=dict(mode='PAPER_ONLY',timestamp=stamp(now),running=True,connected=ws is not None,quotes=quotes,positions=positions,
                    open_positions=int(p is not None),net_open_pnl=mark,realized_today=account.realized_today(now),
                    realized_equity=account.equity,daily_lock=account.session(now)['lock'],ticks=counters,
                    stale_or_rejected=rejects,completed_candles=len(book.rows),monitor_only=args.monitor_only)
                atomic(args.output/'status.json',status); account.save()
                emit('LTP '+' | '.join(parts)+f' | OPEN={int(p is not None)} MTM={f"INR {mark:,.2f}" if mark is not None else "N/A"} realized/day=INR {account.realized_today(now):,.2f}')
        account.save()
    except Exception:
        fatal='LOCAL_RUNTIME_ERROR'
        raise
    finally:
        if seed_task:
            seed_task.cancel()
            try: await seed_task
            except (asyncio.CancelledError,Exception): pass
        if ws:
            try: await ws.send(json.dumps({'RequestCode':12})); await ws.close()
            except Exception: pass
        for sig,handler in old_signals.items(): signal.signal(sig,handler)
        status_path=args.output/'status.json'
        if status_path.exists():
            status=json.loads(status_path.read_text(encoding='utf-8'))
            status.update(connected=False,running=False,timestamp=stamp(time.time()),open_positions=int(account.position is not None))
            atomic(status_path,status)
        atomic(args.output/'last_run.json',dict(mode='PAPER_ONLY',stopped_at=stamp(time.time()),ticks=counters,
            fresh_ticks=fresh_counts,stale_or_rejected=rejects,warmup_fetches=seed_count,open_positions=int(account.position is not None),
            fatal_code=fatal,monitor_only=args.monitor_only))
        emit(f'STOPPED | open paper positions={int(account.position is not None)} | state and ledger saved')
    return 2 if fatal else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['live','demo'],default='live')
    parser.add_argument('--strategy',choices=['h4','h6'],default='h4')
    parser.add_argument('--saved-strategy',help='User strategy ID or name from saved_strategies.json')
    parser.add_argument('--strategies-file',type=Path,default=BASE/'data'/'saved_strategies.json')
    parser.add_argument('--trade-instrument',choices=['CRUDEOIL','NATURALGAS'],default='CRUDEOIL')
    parser.add_argument('--capital',type=float,default=500000)
    parser.add_argument('--allocation',type=float,default=50)
    parser.add_argument('--margin',type=float,default=20,help='Assumed futures margin percentage, not broker margin')
    parser.add_argument('--lots',type=int,default=1)
    parser.add_argument('--max-trades',type=int,choices=[1,2],default=1)
    parser.add_argument('--daily-loss',type=float,default=1500)
    parser.add_argument('--daily-profit',type=float,default=2000)
    parser.add_argument('--atr-stop',type=float,default=2)
    parser.add_argument('--reward-risk',type=float,default=3)
    parser.add_argument('--trailing-jump',type=float,default=.5)
    parser.add_argument('--slippage',type=float)
    parser.add_argument('--monitor-only',action='store_true',help='Receive live feed and manage existing paper positions, without new entries')
    parser.add_argument('--duration',type=float,default=0,help='Bounded run in seconds; 0 runs until STOP/Ctrl+C/23:16 IST')
    parser.add_argument('--status-seconds',type=float,default=10)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.mode=='demo' and args.saved_strategy: parser.error('Saved strategies deploy in live paper mode only')
    if not math.isfinite(args.duration) or args.duration<0 or not math_is_positive(args.status_seconds): parser.error('Invalid duration/status interval')
    if args.mode=='demo' and (args.strategy!='h4' or args.trade_instrument!='CRUDEOIL'): parser.error('Demo uses Crude H4 execution settings')
    args.output=(args.output or BASE/'data_lake'/('paper_demo' if args.mode=='demo' else 'paper_trades')).resolve()
    args.output.mkdir(parents=True,exist_ok=True)
    log,transport=logger(args.output)
    try:
        c=configuration(args); c.validate()
        with AccountLock(args.output):
            atomic(args.output/'process.json',dict(pid=os.getpid(),started_at=stamp(time.time()),mode=args.mode))
            if (args.output/'STOP').exists(): log.info('STOP file exists; remove it before starting this account'); return 0
            if args.mode=='demo': demo(args,c,log.info); return 0
            return asyncio.run(live(args,c,log.info,transport))
    except ValueError as exc:
        # Only our fixed validation messages are safe to display; external
        # libraries can put response content/URLs into ValueError messages.
        log.error('Configuration/state validation failed; check settings and account directory (%s)',type(exc).__name__); return 2
    except FeedError as exc: log.error('Read-only data setup failed: %s',exc); return 2
    except KeyboardInterrupt: log.info('Interrupted; persisted positions will be recovered on restart'); return 130
    except Exception as exc: log.error('Runner stopped (%s); details suppressed to protect credentials',type(exc).__name__); return 2


def math_is_positive(value):
    import math
    return math.isfinite(value) and value>0


if __name__=='__main__': raise SystemExit(main())
