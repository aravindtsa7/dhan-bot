"""Read-only Dhan v2 transport. No order/trading endpoint exists in this module."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import math
import os
import struct
import time
from zoneinfo import ZoneInfo
import pandas as pd
import requests

IST=ZoneInfo('Asia/Kolkata')
MASTER_URL='https://images.dhan.co/api-data/api-scrip-master-detailed.csv'
SEGMENTS={0:'IDX_I',5:'MCX_COMM'}
# WebSocket LTT encodes IST wall-clock seconds, unlike REST chart timestamps.
# Dhan support confirmation: https://madefortrade.in/t/57896
FEED_IST_OFFSET=19800


class FeedError(Exception):
    """Messages are fixed, credential-free codes, never upstream exception text."""


@dataclass(frozen=True)
class Asset:
    name: str
    segment: int
    security_id: int
    expiry: str=''
    point_value: int=1

    @property
    def key(self): return f'{self.segment}:{self.security_id}'


@dataclass(frozen=True)
class Tick:
    key: str
    price: float
    exchange_time: float
    received_time: float
    volume: int|None=None
    oi: int|None=None
    bid: float|None=None
    ask: float|None=None
    bid_size: int=0
    ask_size: int=0
    wire_epoch: int|None=None

    def fresh(self,now,max_age=15):
        return -2<=now-self.exchange_time<=max_age and 0<=now-self.received_time<=max_age


def decode_packets(message,received_time):
    """Keep full epoch timestamps and precision (SDK formatting discards the date)."""
    if not isinstance(message,bytes): raise FeedError('UNEXPECTED_TEXT_FRAME')
    ticks=[]; offset=0
    while offset<len(message):
        if len(message)-offset<8: raise FeedError('TRUNCATED_HEADER')
        code,size,segment,sid=struct.unpack_from('<BHB I',message,offset)
        if size<8 or offset+size>len(message): raise FeedError('INVALID_PACKET_LENGTH')
        packet=message[offset:offset+size]; offset+=size
        if code==50:
            if size<10: raise FeedError('TRUNCATED_DISCONNECT')
            reason=struct.unpack_from('<H',packet,8)[0]
            raise FeedError(f'FEED_DISCONNECT_{reason}')
        if code not in (2,4,8): continue  # OI-only, previous-close and status aren't trade ticks
        expected={2:16,4:50,8:162}[code]
        if size!=expected: raise FeedError('INVALID_QUOTE_LENGTH')
        ltp=struct.unpack_from('<f',packet,8)[0]
        wire_epoch=struct.unpack_from('<I',packet,12 if code==2 else 14)[0]
        epoch=wire_epoch-FEED_IST_OFFSET
        if not math.isfinite(ltp) or ltp<=0 or epoch<=0: continue
        volume=struct.unpack_from('<I',packet,22)[0] if code in (4,8) else None
        oi=struct.unpack_from('<I',packet,34)[0] if code==8 else None
        bid=ask=None; bsize=asize=0
        if code==8:
            bsize,asize,_,_,bid,ask=struct.unpack_from('<IIHHff',packet,62)
            if not all(math.isfinite(p) and p>0 for p in (bid,ask)) or bid>ask:
                bid=ask=None; bsize=asize=0
        ticks.append(Tick(f'{segment}:{sid}',ltp,epoch,received_time,volume,oi,bid,ask,bsize,asize,wire_epoch))
    return ticks


def instruments(root,now):
    """Select nearest non-expiry-day futures; never silently change an open contract."""
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    shared=Path(__file__).parent/'data_lake/scrip_master/master.csv'
    local=root/'master.csv'
    candidates=[p for p in (shared,local) if p.exists() and 0<=now-p.stat().st_mtime<86400]
    if candidates: path=max(candidates,key=lambda p:p.stat().st_mtime)
    else:
        try:
            response=requests.get(MASTER_URL,timeout=(10,45),allow_redirects=False)
            if response.status_code!=200: raise FeedError('MASTER_HTTP_FAILURE')
            tmp=local.with_suffix('.tmp'); tmp.write_bytes(response.content)
            header=pd.read_csv(tmp,nrows=0)
            if not {'SECURITY_ID','SYMBOL_NAME','SM_EXPIRY_DATE','INSTRUMENT','EXCH_ID'}.issubset(header.columns):
                raise FeedError('MASTER_SCHEMA_FAILURE')
            os.replace(tmp,local); path=local
        except requests.RequestException: raise FeedError('MASTER_NETWORK_FAILURE') from None
    frame=pd.read_csv(path,low_memory=False)
    today=datetime.fromtimestamp(now,IST).date().isoformat()
    result=[Asset('NIFTY',0,13),Asset('SENSEX',0,51)]
    for symbol,multiplier in [('CRUDEOIL',100),('NATURALGAS',1250)]:
        f=frame[(frame.EXCH_ID=='MCX')&(frame.INSTRUMENT=='FUTCOM')&(frame.SYMBOL_NAME==symbol)].copy()
        f['expiry']=pd.to_datetime(f.SM_EXPIRY_DATE,errors='coerce').dt.strftime('%Y-%m-%d')
        f=f[f.expiry.gt(today)].sort_values(['expiry','SECURITY_ID'])
        if f.empty: raise FeedError('NO_ACTIVE_FUTURES_CONTRACT')
        row=f.iloc[0]
        result.append(Asset(symbol,5,int(row.SECURITY_ID),row.expiry,multiplier))
    return result


def warmup(client_id,token,asset,now):
    """One allowed, read-only REST route; credentials are never passed to logging."""
    local=datetime.fromtimestamp(now,IST)
    start=local.replace(hour=9,minute=0,second=0,microsecond=0)
    end=local.replace(second=0,microsecond=0)
    if end<=start: return []
    payload=dict(securityId=str(asset.security_id),exchangeSegment=SEGMENTS[asset.segment],
        instrument='FUTCOM',interval='1',oi=True,fromDate=start.strftime('%Y-%m-%d %H:%M:%S'),toDate=end.strftime('%Y-%m-%d %H:%M:%S'))
    for attempt in range(3):
        try:
            r=requests.post('https://api.dhan.co/v2/charts/intraday',json=payload,
                headers={'access-token':token,'client-id':client_id},timeout=(5,15),allow_redirects=False)
        except requests.RequestException: raise FeedError('WARMUP_NETWORK_FAILURE') from None
        if r.status_code==429 or r.status_code>=500:
            if attempt<2: time.sleep(2**attempt); continue
        if r.status_code!=200: raise FeedError(f'WARMUP_HTTP_{r.status_code}')
        try: body=r.json()
        except ValueError: raise FeedError('WARMUP_INVALID_JSON') from None
        if not isinstance(body,dict): raise FeedError('WARMUP_SCHEMA_FAILURE')
        if 'data' in body and isinstance(body['data'],dict): body=body['data']
        keys=('timestamp','open','high','low','close','volume')
        if any(not isinstance(body.get(k),list) for k in keys): raise FeedError('WARMUP_SCHEMA_FAILURE')
        if len({len(body[k]) for k in keys})!=1: raise FeedError('WARMUP_ARRAY_LENGTHS')
        rows=[]
        for values in zip(*(body[k] for k in keys)):
            row=dict(zip(keys,values)); ts=float(row['timestamp'])
            if not start.timestamp()<=ts<end.timestamp() or int(ts)%60: continue
            if not all(math.isfinite(float(row[k])) for k in keys): continue
            if min(row[k] for k in ('open','high','low','close'))<=0 or row['volume']<0: continue
            if row['high']<max(row['open'],row['close'],row['low']) or row['low']>min(row['open'],row['close'],row['high']): continue
            rows.append(row)
        return rows
    raise FeedError('WARMUP_RETRY_EXHAUSTED')
