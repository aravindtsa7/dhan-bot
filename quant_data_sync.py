"""UI-requested ingestion only. Plans are offline; broker access starts in run_sync."""
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import pandas as pd

from quant_coverage import PRODUCTS, session_cutoff
from quant_data_quality import read_document, safe_path, inventory, stream_sessions, parquet_metadata, file_stamp

CHOICES=('All Datasets','NIFTY Spot + Options','SENSEX Spot + Options','BANKNIFTY Spot','MCX Commodities',*PRODUCTS,
         'NIFTY SPOT','NIFTY OPTIONS','SENSEX SPOT','SENSEX OPTIONS','BANKNIFTY SPOT','CRUDEOIL FUT','GOLDM FUT','NATURALGAS FUT')


def selected_products(selection):
    if selection=='All Datasets': return PRODUCTS
    if selection=='MCX Commodities': return PRODUCTS[3:]
    product=selection.split(' ')[0]
    if product not in PRODUCTS: raise ValueError('Choose a supported dataset group.')
    return (product,)


def templates(manifest, products):
    from step2_build_data_lake import FIELDS
    result={}
    for name,sid in [('NIFTY','13'),('SENSEX','51'),('BANKNIFTY','25')]:
        if name not in products: continue
        meta=dict(instrument=name,security_id=sid,exchange_segment='IDX_I',instrument_type='INDEX')
        result['spot_1m|'+name]=dict(dataset='spot_1m',meta=meta,endpoint='/charts/intraday',payload=dict(securityId=sid,exchangeSegment='IDX_I',instrument='INDEX',interval='1',oi=False))
        if name=='BANKNIFTY': continue
        offsets={'ATM','ATM-1','ATM+1'}
        offsets.update(m.group(1) for r in manifest.get('chunks',{}).values() if (m:=re.fullmatch(name+r'_(?:CE|PE)_(ATM(?:[+-]\d+)?)_WEEK1',r.get('instrument',''))))
        for side in ('CE','PE'):
            for offset in sorted(offsets):
                stream=f'{name}_{side}_{offset}_WEEK1';exchange='NSE_FNO' if name=='NIFTY' else 'BSE_FNO'
                opt=dict(meta,instrument=stream,option_type=side,moneyness=offset,expiry_flag='WEEK',expiry_code=1,
                         underlying_security_id=sid,underlying_exchange_segment='IDX_I',exchange_segment=exchange,instrument_type='OPTIDX')
                result['options_rolling_1m|'+stream]=dict(dataset='options_rolling_1m',meta=opt,endpoint='/charts/rollingoption',
                    payload=dict(securityId=int(sid),exchangeSegment=exchange,instrument='OPTIDX',interval=1,expiryFlag='WEEK',expiryCode=1,strike=offset,drvOptionType='CALL' if side=='CE' else 'PUT',requiredData=FIELDS))
    for r in manifest.get('chunks',{}).values():
        match=re.fullmatch(r'(CRUDEOIL|NATURALGAS|GOLDM)_(\d{4}-\d{2}-\d{2})_(\d+)',r.get('instrument',''))
        if not match or match[1] not in products: continue
        name,expiry,sid=r['instrument'],match[2],match[3]
        meta=dict(instrument=name,security_id=sid,exchange_segment='MCX_COMM',instrument_type='FUTCOM',contract_expiry=expiry)
        for ds,endpoint in [('spot_1m','/charts/intraday'),('daily','/charts/historical')]:
            result[ds+'|'+name]=dict(dataset=ds,meta=meta,endpoint=endpoint,payload=dict(securityId=sid,exchangeSegment='MCX_COMM',instrument='FUTCOM',oi=True,**({'interval':'1'} if ds=='spot_1m' else {'expiryCode':0})))
    return result


def _subtract(start,end,covered):
    cursor=start
    for lo,hi in sorted(covered):
        if hi<=cursor: continue
        if lo>cursor: yield cursor,min(lo,end)
        cursor=max(cursor,hi)
        if cursor>=end:return
    if cursor<end:yield cursor,end


def build_plan(root, selection, start=None, end=None, *, latest=False, skip=True, now=None, extra_templates=None):
    """Inclusive UI end; complete cached partitions are never partially overwritten."""
    from step2_build_data_lake import digest, windows
    root=Path(root);manifest,error=read_document(root/'ingestion_manifest.json')
    if error: raise ValueError(error)
    manifest=manifest or {'chunks':{}}
    if not isinstance(manifest.get('chunks'),dict):raise ValueError('Repair the manifest before syncing.')
    products=selected_products(selection);inv=inventory(root)
    specs=templates(manifest,products);specs.update(extra_templates or {})
    if selection.endswith(' SPOT'):specs={k:s for k,s in specs.items() if s['dataset']=='spot_1m'}
    if selection.endswith(' OPTIONS'):specs={k:s for k,s in specs.items() if s['dataset']=='options_rolling_1m'}
    cutoffs={p:session_cutoff(root,p,now) for p in products}
    if latest:
        today=pd.Timestamp(now).date() if now is not None else pd.Timestamp.now(tz='Asia/Kolkata').date()
        active={p:sorted({s['meta']['contract_expiry'] for s in specs.values() if s['meta']['instrument'].startswith(p+'_') and s['meta'].get('contract_expiry','')>=str(today)})[:2] for p in PRODUCTS[3:]}
        specs={k:s for k,s in specs.items() if not s['meta'].get('contract_expiry') or s['meta']['contract_expiry'] in active[s['meta']['instrument'].split('_')[0]]}
    jobs=[];starts=[];ends=[];unavailable=[]
    for key,spec in sorted(specs.items()):
        name=spec['meta']['instrument'];product=name.split('_')[0]
        cutoff=cutoffs[product]['date']
        if cutoff is None:raise ValueError('No completed-session boundary is available for '+product)
        if latest:
            stream=inv['streams'].get(key)
            lo=pd.Timestamp(stream['last']).date()+timedelta(days=1) if stream and stream['last'] else date(2025,1,1)
            # A previously empty future contract should not cause a latest sync
            # to silently request two years. Use the product's oldest active tail.
            if not stream or not stream['last']:
                tails=[pd.Timestamp(s['last']).date() for s in inv['streams'].values() if s['underlying']==product and s['interval']=='1 minute' and s['last']]
                lo=min(tails)+timedelta(days=1) if tails else cutoff
            hi=cutoff+timedelta(days=1)
        else:
            lo=pd.Timestamp(start).date();hi=pd.Timestamp(end).date()+timedelta(days=1)
            if hi<=lo or lo<date(2025,1,1):raise ValueError('Choose 2025-01-01 or later, with End Date on or after Start Date.')
            if hi>cutoff+timedelta(days=1):raise ValueError(f'{product}: choose an end date no later than {cutoff}; today may be unfinished.')
        if spec['meta'].get('contract_expiry'):
            hi=min(hi,pd.Timestamp(spec['meta']['contract_expiry']).date()+timedelta(days=1))
        if hi<=lo:continue
        starts.append(lo);ends.append(hi)
        records=[r for r in manifest['chunks'].values() if r.get('dataset')==spec['dataset'] and r.get('instrument')==name]
        protected=[];retry=[]
        for r in records:
            a,b=pd.Timestamp(r['start']).date(),pd.Timestamp(r['end']).date()
            if b<=lo or a>=hi:continue
            cached=False
            if r['status']=='complete' and r.get('path') and r.get('sha256'):
                path=safe_path(root,r['path'])
                cached=path.is_file() and digest(path)==r['sha256']
                # A successful partial provider response is not evidence that
                # the rest of its requested tail was stored. Refresh the full
                # original partition so latest sync cannot protect an empty tail.
                metadata,_=parquet_metadata(str(path),file_stamp(path)) if path.is_file() else (None,None)
                tail=metadata.get('last') if metadata else None
                if latest and tail and pd.Timestamp(tail).date()+timedelta(days=1)<min(b,hi):cached=False
            if skip and cached:protected.append((a,b))
            else:retry.append((a,b))
        # Preserve exact partition bounds when refreshing an overlapping record.
        merged=[]
        for a,b in sorted(retry):
            if merged and a<merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
            else:merged.append((a,b))
        spans=list(merged)
        for a,b in _subtract(lo,hi,protected+merged):spans.extend(windows(a,b))
        for a,b in spans:
            jobs.append(dict(**spec,start=str(a),end=str(b),key=f"{spec['dataset']}/{name}/{a}"))
    for product in products:
        if product in PRODUCTS[3:] and not any(s['meta']['instrument'].startswith(product+'_') for s in specs.values()):unavailable.append(product)
    jobs.sort(key=lambda j:(0 if j['dataset']=='spot_1m' and j['meta']['exchange_segment']=='IDX_I' else 1 if j['dataset']=='options_rolling_1m' else 2 if j['dataset']=='spot_1m' else 3,j['meta']['instrument'],j['start']))
    return dict(selection=selection,products=list(products),latest=latest,skip=skip,start=str(min(starts)) if starts else None,
                end_exclusive=str(max(ends)) if ends else None,jobs=jobs,cutoffs={p:dict(v,date=str(v['date']) if v['date'] else None) for p,v in cutoffs.items()},
                missing_contract_metadata=unavailable,manifest_fingerprint=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest())


def discover_contracts(products, start):
    """Public current instrument master, only during an explicit sync action."""
    import requests
    import io
    from step2_build_data_lake import MASTER_URL
    response=requests.get(MASTER_URL,timeout=(10,60),allow_redirects=False);response.raise_for_status()
    frame=pd.read_csv(io.BytesIO(response.content),low_memory=False)
    frame=frame[(frame.EXCH_ID=='MCX')&(frame.INSTRUMENT=='FUTCOM')&frame.SYMBOL_NAME.isin(products)]
    frame=frame[pd.to_datetime(frame.SM_EXPIRY_DATE).dt.date>=pd.Timestamp(start).date()]
    chunks={}
    for row in frame.itertuples():
        name=f'{row.SYMBOL_NAME}_{pd.Timestamp(row.SM_EXPIRY_DATE).date()}_{int(row.SECURITY_ID)}'
        chunks[name]=dict(instrument=name)
    return templates({'chunks':chunks},products)


def _promote(root, stage, records, manifest):
    """Rollback file promotions if the final manifest commit fails."""
    from step2_build_data_lake import atomic_json
    backup=stage/'backup';backup.mkdir(exist_ok=True);moves=[]
    path=root/'ingestion_manifest.json';original=path.read_bytes() if path.exists() else None
    try:
        for record in records:
            for field in ('path','quarantine_path'):
                if not record.get(field):continue
                target=safe_path(root,record[field]);source=safe_path(stage,record[field]);target.parent.mkdir(parents=True,exist_ok=True)
                old=backup/str(len(moves))
                if target.exists():shutil.copy2(target,old)
                moves.append((target,old if old.exists() else None))
                os.replace(source,target)
        if original:
            archive=root/'ingestion_history';archive.mkdir(exist_ok=True)
            (archive/(hashlib.sha256(original).hexdigest()+'.json')).write_bytes(original)
        atomic_json(path,manifest)
    except Exception:
        for target,old in reversed(moves):
            if old:os.replace(old,target)
            else:target.unlink(missing_ok=True)
        if original is None:path.unlink(missing_ok=True)
        else:path.write_bytes(original)
        raise


def run_sync(root, selection, start=None, end=None, *, latest=False, skip=True, now=None, progress=None,
             downloader=None, audit=None, discovery=None, check_credentials=True):
    """Synchronous local job; never invoked on page render or rerun alone."""
    from dotenv import dotenv_values
    from step2_build_data_lake import Builder, writer_lock
    from quant_data_quality import run_quality_check
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    if check_credentials:
        env={**os.environ,**{k:v for k,v in dotenv_values(Path(__file__).with_name('.env')).items() if v}}
        if not env.get('DHAN_CLIENT_ID') or not (env.get('DHAN_ACCESS_TOKEN') or (env.get('DHAN_PIN') and env.get('DHAN_TOTP_SECRET'))):
            raise ValueError('Missing Dhan credentials. Configure DHAN_CLIENT_ID and an access token, or PIN + TOTP secret, in the local .env. Values are never shown.')
    audit=audit or run_quality_check
    failures=[];committed=[];outcomes=[];stopped=None
    with writer_lock(root):
        before=inventory(root);before_sessions={sid:set(stream_sessions(s)) for sid,s in before['streams'].items()}
        plan=build_plan(root,selection,start,end,latest=latest,skip=skip,now=now)
        commodity=[p for p in plan['products'] if p in PRODUCTS[3:]]
        if commodity:
            try:
                extra=(discovery or discover_contracts)(commodity,plan['start'] or session_cutoff(root,commodity[0],now)['date'])
                plan=build_plan(root,selection,start,end,latest=latest,skip=skip,now=now,extra_templates=extra)
            except Exception:
                failures.append('Current MCX instrument master unavailable; only recorded contracts can be requested. Expired contracts may be unavailable from the provider.')
        with tempfile.TemporaryDirectory(prefix='sync_',dir=root) as directory:
            stage=Path(directory)
            builder=None if downloader else Builder(stage,date(2025,1,1),pd.Timestamp(plan['end_exclusive'] or date.today()).date())
            manifest=json.loads(json.dumps(before['manifest']));manifest.setdefault('chunks',{})
            failed_streams=set()
            for i,job in enumerate(plan['jobs'],1):
                stream_key=(job['dataset'],job['meta']['instrument'])
                outcome=dict(dataset=job['dataset'],instrument=job['meta']['instrument'],start=job['start'],end_exclusive=job['end'],status='attempting',reason='')
                outcomes.append(outcome)
                if stopped or stream_key in failed_streams:
                    outcome.update(status='not attempted',reason=stopped or 'Earlier request for this stream failed; other streams continue');continue
                if progress:progress(i,len(plan['jobs']),job['meta']['instrument'])
                try:
                    record=(downloader(job,stage) if downloader else builder.chunk(job['dataset'],job['meta'],pd.Timestamp(job['start']).date(),pd.Timestamp(job['end']).date(),job['endpoint'],dict(job['payload'],fromDate=job['start'],toDate=job['end'])))
                    outcome['status']=record['status']
                    from step2_build_data_lake import provider_message
                    detail=provider_message({'message':record.get('provider_message','')})
                    outcome['provider_message']=detail
                    outcome['endpoint']=job['endpoint']
                    outcome['query_windows']=record.get('query_windows',[])
                    if job['dataset']=='options_rolling_1m':
                        outcome['rolling_parameters']={k:job['payload'][k] for k in ('securityId','exchangeSegment','instrument','interval','expiryFlag','expiryCode','strike','drvOptionType')}
                    if re.fullmatch(r'(?:DH-\d{3}|HTTP-\d{3}|NETWORK|AUTH)',str(record.get('error',''))):outcome['reason']=record['error']
                    if record['status']=='failed':
                        code=str(record.get('error',''))
                        safe_code=code if re.fullmatch(r'(?:DH-\d{3}|HTTP-\d{3}|NETWORK|AUTH|[A-Za-z]+Error)',code) else 'Provider error'
                        failures.append(job['meta']['instrument']+' '+job['start']+': '+safe_code+(' — '+detail if detail else ' (provider description unavailable)')+'; existing stored data retained.')
                        outcome['reason']=safe_code
                        if code in ('NETWORK','AUTH','DH-901','807','808','809','810','HTTP-401'):
                            stopped='Connectivity/authentication failed; resolve credentials or network before retrying'
                            failures.append('Remaining requests stopped after '+stopped+'.');continue
                        failed_streams.add(stream_key);continue
                    old=[(k,r) for k,r in manifest['chunks'].items() if r['dataset']==job['dataset'] and r['instrument']==job['meta']['instrument'] and r['start']<job['end'] and r['end']>job['start']]
                    if record['status']!='complete' and any(r['status']=='complete' for _,r in old):
                        outcome['reason']='Provider returned no candles; previous partition retained'
                        if job['dataset']=='options_rolling_1m':
                            outcome['reason']+='; the expired rolling endpoint may not yet publish recent sessions; no contract/expiry fallback was substituted'
                        failures.append(job['meta']['instrument']+': provider returned no candles; existing partition retained.');continue
                    for key,r in old:
                        if r['start']<job['start'] or r['end']>job['end']:raise ValueError('Replacement would truncate an existing partition')
                    if record['status']=='complete':
                        path=safe_path(stage,record['path'])
                        from step2_build_data_lake import digest
                        if digest(path)!=record['sha256']:raise ValueError('Staged checksum mismatch')
                        old_files=[str(safe_path(root,r['path'])) for _,r in old if r['status']=='complete']
                        if old_files:
                            import duckdb
                            with duckdb.connect() as db:
                                lost=db.execute('SELECT count(*) FROM (SELECT timestamp FROM read_parquet(?,union_by_name=true) EXCEPT SELECT timestamp FROM read_parquet(?))',[old_files,str(path)]).fetchone()[0]
                            if lost:
                                outcome.update(status='rejected replacement',reason=f'{lost} previously stored timestamps omitted')
                                failures.append(job['meta']['instrument']+f': replacement omitted {lost} previously stored timestamps; existing partition retained.');continue
                    for key,r in old:manifest['chunks'].pop(key)
                    manifest['chunks'][job['key']]=record;committed.append(record)
                except Exception:
                    outcome.update(status='failed',reason='Download or staged validation failed; previous data retained')
                    failures.append(job['meta']['instrument']+' '+job['start']+' / '+job['end']+': download failed; existing stored data retained. Check connectivity, subscription and credentials.')
                    failed_streams.add(stream_key)
            if committed:
                manifest['requested_start']=min(manifest.get('requested_start',plan['start']),plan['start'])
                manifest['requested_end_exclusive']=max(manifest.get('requested_end_exclusive',plan['end_exclusive']),plan['end_exclusive'])
                _promote(root,stage,committed,manifest)
        try:
            report=audit(root,_writer_locked=True)
            audit_current=bool(report['stable_snapshot'])
        except Exception:
            audit_current=False;failures.append('Audit refresh failed. Stored downloads are preserved, but readiness is not verified. Run a local quality check before backtesting.')
        from quant_data_quality import _atomic_report
        history=root/'sync_history';history.mkdir(exist_ok=True)
        log=history/(pd.Timestamp.now(tz='Asia/Kolkata').strftime('%Y%m%dT%H%M%S%f')+'.json')
        _atomic_report(log,dict(selection=selection,latest=latest,checked_at=pd.Timestamp.now(tz='Asia/Kolkata').isoformat(),outcomes=outcomes,failures=failures,audit_current=audit_current))
    after=inventory(root)
    sessions_added=sum(len(set(stream_sessions(s))-before_sessions.get(sid,set())) for sid,s in after['streams'].items())
    before_products={p:{d for sid,s in before['streams'].items() if s['underlying']==p for d in before_sessions[sid]} for p in PRODUCTS}
    product_sessions_added=sum(len({d for s in after['streams'].values() if s['underlying']==p for d in stream_sessions(s)}-before_products[p]) for p in PRODUCTS)
    return dict(requests=len(plan['jobs']),committed=len(committed),rows_added=sum(g['rows'] for g in after['groups'])-sum(g['rows'] for g in before['groups']),
                stream_sessions_added=sessions_added,product_sessions_added=product_sessions_added,audit_current=audit_current,failures=failures,plan=plan,
                outcomes=outcomes,outcome_log=log.relative_to(root).as_posix(),attempted=sum(o['status']!='not attempted' for o in outcomes))


def repair_dataset_gaps(root, selection='All Datasets', *, offline=False, progress=None, api=None):
    """Explicit correction transaction. Originals and raw quarantine are retained.

    Provider retry precedes synthetic repair when credentials are configured.
    No opening/closing extensions, long holes, disputed prices or MCX fills.
    """
    from step2_build_data_lake import writer_lock, digest, normalize, fetch_candles
    from quant_data_quality import run_quality_check
    from quant_repairs import recover_negative, fill_isolated
    from dotenv import dotenv_values
    root=Path(root);products=selected_products(selection);events=[];failures=[];changed=[]
    if not offline and api is None:
        env={**os.environ,**{k:v for k,v in dotenv_values(Path(__file__).with_name('.env')).items() if v}}
        if env.get('DHAN_CLIENT_ID') and env.get('DHAN_ACCESS_TOKEN'):
            from step2_build_data_lake import API
            api=API()
        else:failures.append('No active access-token configuration; repair uses retained quarantine and explicitly labeled offline synthetic fills.')
    with writer_lock(root):
        inv=inventory(root)
        if inv['findings']:raise ValueError('Resolve inventory/file errors before repairing. No partition was changed.')
        manifest=json.loads(json.dumps(inv['manifest']));specs=templates(manifest,products)
        records=[(key,r) for key,r in manifest.get('chunks',{}).items() if r['status']=='complete' and r['dataset']!='daily' and r['instrument'].split('_')[0] in products]
        if selection.endswith(' SPOT'):records=[(k,r) for k,r in records if r['dataset']=='spot_1m']
        if selection.endswith(' OPTIONS'):records=[(k,r) for k,r in records if r['dataset']=='options_rolling_1m']
        with tempfile.TemporaryDirectory(prefix='repair_',dir=root) as directory:
            stage=Path(directory)
            for i,(key,r) in enumerate(records,1):
                if progress:progress(i,len(records),r['instrument'])
                path=safe_path(root,r['path'])
                if not r.get('sha256') or digest(path)!=r['sha256']:raise ValueError('Checksum mismatch; repair refused: '+r['instrument'])
                frame=pd.read_parquet(path)
                rejected=pd.read_parquet(safe_path(root,r['quarantine_path'])) if r.get('quarantine_path') else pd.DataFrame()
                for f in (frame,rejected):
                    if len(f):f['timestamp']=pd.to_datetime(f.timestamp,utc=True).dt.tz_convert('Asia/Kolkata')
                frame,recovered=recover_negative(frame,rejected)
                candidate,synthetic=fill_isolated(frame,rejected,r['instrument'].split('_')[0])
                fetched=[]
                if synthetic and api is not None:
                    spec=specs.get(r['dataset']+'|'+r['instrument'])
                    for day in sorted({e['timestamp'][:10] for e in synthetic}):
                        try:
                            a=pd.Timestamp(day).date();b=a+timedelta(days=1)
                            raw,_=fetch_candles(api,spec['endpoint'],spec['payload'],spec['meta'],a,b)
                            fresh,_=normalize(raw,spec['meta'],a,b)
                            if fresh.empty:continue
                            wanted={pd.Timestamp(e['timestamp']) for e in synthetic if e['timestamp'].startswith(day)}
                            # Fill only omitted timestamps. Existing quotes and
                            # quarantined conflicts are never overwritten.
                            fresh=fresh[fresh.timestamp.isin(wanted)]
                            if 'candle_source' in fresh:fresh=fresh[~fresh.candle_source.eq('quarantine_volume_zero')]
                            if len(fresh):
                                fresh=fresh.copy();fresh['candle_source']='provider_refetch'
                                frame=pd.concat([frame,fresh],ignore_index=True).sort_values('timestamp')
                                fetched.extend(dict(timestamp=t.isoformat(),method='provider_refetch') for t in fresh.timestamp)
                        except Exception:failures.append(r['instrument']+' '+day+': provider retry unavailable; conservative offline repair rules used.')
                    candidate,synthetic=fill_isolated(frame,rejected,r['instrument'].split('_')[0])
                modifications=recovered+fetched+synthetic
                if not modifications:continue
                frame=candidate.sort_values('timestamp').reset_index(drop=True)
                # Persistent content-addressed original, written before promotion.
                archive=root/'repair_history'/r['sha256'];archive.mkdir(parents=True,exist_ok=True)
                original=archive/'original.parquet'
                if not original.exists():shutil.copy2(path,original)
                (archive/'record.json').write_text(json.dumps(r,indent=2,default=str),encoding='utf-8')
                target=safe_path(stage,r['path']);target.parent.mkdir(parents=True,exist_ok=True)
                frame.to_parquet(target,index=False,compression='zstd')
                if r.get('quarantine_path'):
                    q=safe_path(stage,r['quarantine_path']);q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(safe_path(root,r['quarantine_path']),q)
                stamped=[dict(e,recorded_at=pd.Timestamp.now(tz='Asia/Kolkata').isoformat(),original_sha256=r['sha256']) for e in modifications]
                r.update(rows=len(frame),sha256=digest(target),first=str(frame.timestamp.min()),last=str(frame.timestamp.max()),repairs=r.get('repairs',[])+stamped)
                r.setdefault('quality',{})['stored']=len(frame)
                changed.append(r);events.extend(dict(e,instrument=r['instrument'],dataset=r['dataset']) for e in stamped)
            if changed:_promote(root,stage,changed,manifest)
        report=run_quality_check(root,_writer_locked=True)
    return dict(partitions_changed=len(changed),restored=sum(e['method']=='quarantine_volume_zero' for e in events),
                synthetic=sum(e['method']=='synthetic_ffill' for e in events),refetched=sum(e['method']=='provider_refetch' for e in events),
                events=events,failures=failures,audit_current=bool(report['stable_snapshot']))
