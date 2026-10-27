"""Resumable Dhan historical data lake. No orders; secrets never logged.

Run: .venv/Scripts/python.exe -u step2_build_data_lake.py
End date is exclusive; defaults to today IST, excluding the current session.
"""
import argparse
import calendar
import contextlib
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv, set_key
import pyotp
from step1_health_check import error_code, unwrap, new_access_token, AUTH_CODES, CheckError
from quant_lake import QuantDataLake, summary, ROOT, IST

FIELDS=['open','high','low','close','volume','oi','iv','spot','strike']
MASTER_URL='https://images.dhan.co/api-data/api-scrip-master-detailed.csv'


def atomic_json(path, value):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        json.dump(value,f,indent=2,default=str);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


@contextlib.contextmanager
def writer_lock(root):
    # OS-owned lock releases automatically even if the process crashes.
    p=root/'.writer.lock'
    with p.open('a+b') as f:
        f.seek(0);f.write(b'0');f.flush();f.seek(0)
        if os.name=='nt':
            import msvcrt
            try: msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError: raise RuntimeError('Another lake writer is running') from None
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try: yield
        finally:
            f.seek(0)
            if os.name=='nt': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f,fcntl.LOCK_UN)


class API:
    def __init__(self):
        load_dotenv(Path(__file__).with_name('.env'),override=True)
        logging.disable(logging.CRITICAL)
        self.session=requests.Session();self.last=0;self.refreshes=0;self.calls=0
        self.rate_lock=threading.Lock()
        self.auth_lock=threading.Lock()
        self.session.headers.update({'access-token':os.getenv('DHAN_ACCESS_TOKEN',''),
                                     'client-id':os.getenv('DHAN_CLIENT_ID','')})

    def post(self,endpoint,payload):
        for attempt in range(7):
            with self.rate_lock:
                time.sleep(max(0,0.35-(time.monotonic()-self.last)))
                self.last=time.monotonic();self.calls+=1
            try:
                headers=dict(self.session.headers)
                r=requests.post('https://api.dhan.co/v2'+endpoint,headers=headers,json=payload,timeout=(10,60),allow_redirects=False)
                try: body=r.json()
                except ValueError: body={}
                code=error_code(body,'')
                if code in AUTH_CODES or r.status_code==401:
                    with self.auth_lock:
                        if headers['access-token']!=self.session.headers['access-token']:continue
                        if self.refreshes>=1: raise CheckError('AUTH','Authentication failed after refresh')
                        token=new_access_token(os.getenv('DHAN_CLIENT_ID'),os.getenv('DHAN_PIN'),pyotp.TOTP(os.getenv('DHAN_TOTP_SECRET')))
                        self.session.headers['access-token']=token
                        set_key(str(Path(__file__).with_name('.env')),'DHAN_ACCESS_TOKEN',token)
                        self.refreshes+=1
                    continue
                if r.status_code==429 or r.status_code>=500 or code in ('DH-904','DH-908','DH-909','800','805'):
                    if attempt==6: raise CheckError(code or f'HTTP-{r.status_code}')
                    try: retry=float(r.headers.get('Retry-After','0'))
                    except ValueError: retry=0
                    delay=min(60,max(retry,2**attempt+random.random()))
                    print(f'  retry {attempt+1}/6 after {delay:.1f}s ({code or r.status_code})',flush=True)
                    time.sleep(delay);continue
                if not r.ok: raise CheckError(code or f'HTTP-{r.status_code}')
                return unwrap(body)
            except (requests.Timeout,requests.ConnectionError):
                if attempt==6: raise CheckError('NETWORK') from None
                time.sleep(min(60,2**attempt+random.random()))
        raise CheckError('RETRIES','Retry budget exhausted')


def windows(start,end):
    # Stable boundaries permit extension of a partial tail without overlap.
    cursor=date(2025,1,1)
    while cursor<end:
        stop=min(cursor+timedelta(days=28),end)
        if stop>start: yield max(cursor,start),stop
        cursor+=timedelta(days=28)


def normalize(data,meta,start,end,daily=False):
    if not isinstance(data,dict) or not isinstance(data.get('timestamp'),list):
        raise ValueError('Missing timestamp array')
    n=len(data['timestamp'])
    if not n: return pd.DataFrame(),{'received':0}
    columns=['timestamp','open','high','low','close','volume']
    if meta.get('option_type'): columns+=['oi','iv','spot','strike']
    elif 'open_interest' in data: data=dict(data,oi=data['open_interest']);columns+=['oi']
    elif 'oi' in data: columns+=['oi']
    if any(not isinstance(data.get(c),list) or len(data[c])!=n for c in columns):
        raise ValueError('Unequal/missing required arrays')
    df=pd.DataFrame({c:data[c] for c in columns})
    for c in columns: df[c]=pd.to_numeric(df[c],errors='coerce')
    if not np.isfinite(df.to_numpy(dtype=float)).all(): raise ValueError('Nonfinite/null numeric values')
    df['timestamp']=pd.to_datetime(df.timestamp,unit='s',utc=True).dt.tz_convert(IST)
    exact=int(df.duplicated().sum());df=df.drop_duplicates()
    conflicting=df.timestamp.duplicated(keep=False)
    conflicts=df[conflicting].assign(rejection_reason='conflicting_duplicate_timestamp')
    df=df[~conflicting].copy()
    df=df.sort_values('timestamp')
    inside=(df.timestamp.dt.date>=start)&(df.timestamp.dt.date<end)
    outside=int((~inside).sum());df=df[inside].copy()
    quality={'received':n,'exact_duplicates':exact,'outside_request':outside}
    quality['conflicting_duplicate_rows']=len(conflicts)
    quality['ambiguous_timestamps']=int(conflicts.timestamp.nunique())
    if not daily:
        minute=df.timestamp.dt.hour*60+df.timestamp.dt.minute
        if meta['exchange_segment']=='MCX_COMM':
            # MCX energy/metals close follows US DST (23:30 summer, 23:55 winter).
            dst=df.timestamp.dt.tz_convert('America/New_York').map(lambda x:bool(x.dst()))
            close=np.where(dst,23*60+30,23*60+55);opening=9*60
        else: close=15*60+30;opening=9*60+15
        good=minute.ge(opening)&minute.le(close)&df.timestamp.dt.second.eq(0)
        quality['outside_market_hours']=int((~good).sum())
        rejected=pd.concat([conflicts,df[~good].assign(rejection_reason='outside_market_hours')],ignore_index=True)
        df=df[good].copy()
    else: rejected=conflicts
    prices=df[['open','high','low','close']]
    invalid=(prices.lt(0).any(axis=1) | df.volume.lt(0) |
             df.high.lt(prices.max(axis=1)) | df.low.gt(prices.min(axis=1)))
    if 'oi' in df: invalid |= df.oi.lt(0)
    quality['invalid_ohlcv']=int(invalid.sum())
    rejected=pd.concat([rejected,
                        df[invalid].assign(rejection_reason='invalid_ohlcv')],ignore_index=True)
    df=df[~invalid].copy()
    for k,v in meta.items(): df[k]=v
    if not daily:
        gaps=df.groupby(df.timestamp.dt.date).timestamp.diff().dt.total_seconds().dropna()
        quality['missing_internal_minutes']=int(((gaps/60-1).clip(lower=0)).sum())
    quality['stored']=len(df)
    return df,(quality,rejected)


class Builder:
    def __init__(self,root,start,end):
        self.root=root;self.start=start;self.end=end;self.api=API();self.failures=0;self.skipped=0
        self.path=root/'ingestion_manifest.json'
        self.manifest_lock=threading.Lock()
        self.manifest=json.loads(self.path.read_text()) if self.path.exists() else {'schema_version':1,'chunks':{}}
        if self.manifest.get('requested_start',str(start)) != str(start):
            raise ValueError('Use the original start date for this lake, or select a new --root')
        self.manifest.update(requested_start=str(start),requested_end_exclusive=str(end))

    def chunk(self,dataset,meta,start,end,endpoint,payload):
        key=f"{dataset}/{meta['instrument']}/{start}"
        entry=self.manifest['chunks'].get(key,{})
        target=self.root/key;target=target.with_suffix('.parquet')
        if entry.get('end')==str(end):
            if entry.get('status')=='complete' and target.exists() and digest(target)==entry.get('sha256'):
                self.skipped+=1;return entry
            # Empty responses are not proof that a historical period was traded.
            if entry.get('status') in ('empty','unavailable') and time.time()-entry.get('checked_epoch',0)<86400:
                self.skipped+=1;return entry
        record=dict(dataset=dataset,instrument=meta['instrument'],start=str(start),end=str(end),
                    endpoint=endpoint,payload=payload,status='pending',checked_epoch=time.time())
        try:
            if meta.get('option_type'):
                # Seven-day requests avoid gateway timeouts on dense rolling data.
                # Storage retains stable 28-day partitions for resumability.
                parts=[];a=start
                while a<end:
                    b=min(a+timedelta(days=7),end)
                    part=self.api.post(endpoint,dict(payload,fromDate=str(a),toDate=str(b)))
                    part=part.get(meta['option_type'].lower())
                    if not isinstance(part,dict) or 'timestamp' not in part:
                        raise ValueError('Missing rolling option side')
                    f=pd.DataFrame(part)
                    stamps=pd.to_datetime(f.timestamp,unit='s',utc=True).dt.tz_convert(IST)
                    parts.append(f[(stamps.dt.date>=a)&(stamps.dt.date<b)])
                    a=b
                data=pd.concat(parts,ignore_index=True).to_dict(orient='list')
                record['request_window_days']=7
            else:
                data=self.api.post(endpoint,payload)
            df,q=normalize(data,meta,start,end,dataset=='daily')
            if isinstance(q,tuple): quality,rejected=q
            else: quality,rejected=q,pd.DataFrame()
            record['quality']=quality
            if df.empty:
                record['status']='empty';record['rows']=0
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                tmp=target.with_suffix('.tmp');df.to_parquet(tmp,index=False,compression='zstd');os.replace(tmp,target)
                record.update(status='complete',path=target.relative_to(self.root).as_posix(),rows=len(df),
                              sha256=digest(target),first=str(df.timestamp.min()),last=str(df.timestamp.max()))
            if not rejected.empty:
                reject=self.root/'quarantine'/dataset/meta['instrument']/f'{start}.parquet'
                reject.parent.mkdir(parents=True,exist_ok=True);rejected.to_parquet(reject,index=False)
                record['quarantine_path']=reject.relative_to(self.root).as_posix()
            print(f"{record['status'].upper():8} {key} -> {end} | {len(df):,} rows",flush=True)
        except Exception as exc:
            # Never serialize raw HTTP exceptions, which can contain auth URLs.
            unavailable=isinstance(exc,CheckError) and exc.code=='DH-907' and meta['exchange_segment']=='MCX_COMM'
            record.update(status='unavailable' if unavailable else 'failed',
                          error=exc.code if isinstance(exc,CheckError) else type(exc).__name__)
            self.failures+=int(not unavailable)
            print(f"{record['status'].upper():8} {key} | {record['error']}",flush=True)
        with self.manifest_lock:
            self.manifest['chunks'][key]=record
            atomic_json(self.path,self.manifest)
        return record

    def spot(self,meta,start=None):
        for a,b in windows(self.start,self.end):
            if start is not None and b <= start:
                continue
            payload=dict(securityId=meta['security_id'],exchangeSegment=meta['exchange_segment'],
                         instrument=meta['instrument_type'],interval='1',oi=meta['exchange_segment']=='MCX_COMM',
                         fromDate=str(a),toDate=str(b))
            self.chunk('spot_1m',meta,a,b,'/charts/intraday',payload)

    def options(self,meta,wing=1):
        if not 1 <= wing <= 10:
            raise ValueError('Index option wing must be between 1 and 10')
        jobs=[]
        for side in ['CE','PE']:
            for strike in ['ATM'] + [f'ATM{offset:+d}' for offset in range(-wing, wing+1) if offset]:
                opt=dict(meta,instrument=f"{meta['instrument']}_{side}_{strike}_WEEK1",option_type=side,
                         moneyness=strike,expiry_flag='WEEK',expiry_code=1,
                         underlying_security_id=meta['security_id'],underlying_exchange_segment='IDX_I',
                         exchange_segment='NSE_FNO' if meta['security_id']=='13' else 'BSE_FNO',instrument_type='OPTIDX')
                for a,b in windows(self.start,self.end):
                    payload=dict(securityId=int(meta['security_id']),exchangeSegment='NSE_FNO' if meta['security_id']=='13' else 'BSE_FNO',
                        instrument='OPTIDX',interval='1',expiryFlag='WEEK',expiryCode=1,strike=strike,
                        drvOptionType='CALL' if side=='CE' else 'PUT',requiredData=FIELDS,fromDate=str(a),toDate=str(b))
                    jobs.append(('options_rolling_1m',opt,a,b,'/charts/rollingoption',payload))
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda job:self.chunk(*job),jobs))

    def commodities(self):
        cache=self.root/'scrip_master';cache.mkdir(exist_ok=True)
        p=cache/'master.csv'
        if not p.exists() or time.time()-p.stat().st_mtime>86400:
            r=requests.get(MASTER_URL,timeout=(10,60));r.raise_for_status()
            tmp=p.with_suffix('.tmp');tmp.write_bytes(r.content);os.replace(tmp,p)
        df=pd.read_csv(p,low_memory=False)
        atomic_json(cache/'source.json',dict(url=MASTER_URL,sha256=digest(p),fetched_epoch=p.stat().st_mtime))
        df=df[(df.EXCH_ID=='MCX')&(df.INSTRUMENT=='FUTCOM')&df.SYMBOL_NAME.isin(['CRUDEOIL','NATURALGAS','GOLDM'])].copy()
        df['expiry']=pd.to_datetime(df.SM_EXPIRY_DATE).dt.date
        df=df[df.expiry>=self.end].sort_values(['SYMBOL_NAME','expiry'])
        df.to_parquet(cache/'active_mcx_contracts.parquet',index=False)
        for _,r in df.iterrows():
            meta=dict(instrument=f'{r.SYMBOL_NAME}_{r.expiry}_{int(r.SECURITY_ID)}',security_id=str(int(r.SECURITY_ID)),
                      exchange_segment='MCX_COMM',instrument_type='FUTCOM',contract_expiry=str(r.expiry))
            payload=dict(securityId=meta['security_id'],exchangeSegment='MCX_COMM',instrument='FUTCOM',
                         expiryCode=0,oi=True,fromDate=str(self.start),toDate=str(self.end))
            record=self.chunk('daily',meta,self.start,self.end,'/charts/historical',payload)
            if record['status']=='complete':
                self.spot(meta,max(self.start,pd.Timestamp(record['first']).date()))
            else:
                # Some futures have intraday data even if daily history fails.
                self.spot(meta)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start',default='2025-01-01');p.add_argument('--end',default=datetime.now(ZoneInfo(IST)).date().isoformat())
    p.add_argument('--root',type=Path,default=ROOT);p.add_argument('--summary-only',action='store_true')
    p.add_argument('--only',choices=['all','spot','options','mcx'],default='all')
    p.add_argument('--option-wing',type=int,choices=range(1,11),default=1,
                   help='Fetch ATM plus this many strikes on each side; use 10 for the full index ladder')
    args=p.parse_args();start=date.fromisoformat(args.start);end=date.fromisoformat(args.end)
    if start<date(2025,1,1) or end<=start or end>datetime.now(ZoneInfo(IST)).date():
        p.error('Require 2025-01-01 <= start < end <= today IST')
    args.root.mkdir(parents=True,exist_ok=True)
    for folder in ['spot_1m','options_rolling_1m','scrip_master','daily','quarantine']:
        (args.root/folder).mkdir(exist_ok=True)
    if args.summary_only:
        print(summary(args.root).to_string(index=False));return 0
    with writer_lock(args.root):
        b=Builder(args.root,start,end)
        indices=[dict(instrument=n,security_id=s,exchange_segment='IDX_I',instrument_type='INDEX')
                 for n,s in [('NIFTY','13'),('SENSEX','51'),('BANKNIFTY','25')]]
        if args.only in ('all','spot'):
            for meta in indices:b.spot(meta)
        if args.only in ('all','options'):
            for meta in indices[:2]:b.options(meta,args.option_wing)
        if args.only in ('all','mcx'):b.commodities()
        result=summary(args.root)
        result.to_csv(args.root/'summary.csv',index=False)
        print('\nDATA LAKE SUMMARY\n'+result.to_string(index=False),flush=True)
        print(f'API calls: {b.api.calls} | cached chunks skipped: {b.skipped} | failures this run: {b.failures}',flush=True)
        pending=sum(v['status']=='failed' for v in b.manifest['chunks'].values())
        unavailable=sum(v['status']=='unavailable' for v in b.manifest['chunks'].values())
        print(f'Unresolved failed chunks: {pending} | Provider-unavailable chunks: {unavailable}',flush=True)
        return int(pending>0)

if __name__=='__main__':
    raise SystemExit(main())
