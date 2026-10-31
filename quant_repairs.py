"""Explicit, provenance-preserving corrections. Synthetic candles are never quotes."""
import numpy as np
import pandas as pd


def negative_only(row):
    fields=('open','high','low','close','volume')
    try:
        op,hi,lo,cl,volume=(float(row[k]) for k in fields)
        if not all(np.isfinite([op,hi,lo,cl,volume])):return False
        if not (volume<0 and min(op,lo,cl)>0 and hi>=max(op,lo,cl) and lo<=min(op,hi,cl)):return False
        return all(np.isfinite(float(row[k])) and float(row[k])>=0 for k in ('oi','iv','spot','strike') if k in row)
    except (ValueError,TypeError,KeyError):return False


def recover_negative(frame, rejected):
    events=[];added=[]
    if rejected.empty or 'rejection_reason' not in rejected:return frame,events
    stamps=set(frame.timestamp)
    conflicted=set(rejected.loc[rejected.rejection_reason.eq('conflicting_duplicate_timestamp'),'timestamp'])
    for stamp,rows in rejected.groupby('timestamp'):
        if stamp in stamps or stamp in conflicted or len(rows)!=1:continue
        row=rows.iloc[0]
        if row.rejection_reason!='invalid_ohlcv' or not negative_only(row):continue
        repaired=row.drop(labels=['rejection_reason']).to_dict()
        for field in frame.columns:
            if field not in repaired and len(frame):repaired[field]=frame.iloc[0][field]
        repaired.update(volume=0.,candle_source='quarantine_volume_zero',original_volume=float(row.volume))
        added.append(repaired);stamps.add(stamp)
        events.append(dict(timestamp=stamp.isoformat(),method='quarantine_volume_zero',original_volume=float(row.volume),new_volume=0.))
    if added:frame=pd.concat([frame,pd.DataFrame(added)],ignore_index=True).sort_values('timestamp').reset_index(drop=True)
    return frame,events


def fill_isolated(frame, rejected, product):
    events=[];added=[]
    if product not in ('NIFTY','SENSEX','BANKNIFTY'):return frame,events
    blocked=set()
    for _,row in rejected.iterrows():
        if row.get('rejection_reason')=='conflicting_duplicate_timestamp' or (row.get('rejection_reason')=='invalid_ohlcv' and not negative_only(row)):
            blocked.add(row.timestamp)
    for day,g in frame.groupby(frame.timestamp.dt.strftime('%Y-%m-%d')):
        expected=pd.date_range(day+' 09:15',day+' 15:29',freq='min',tz='Asia/Kolkata')
        g=g[g.timestamp.isin(expected)].sort_values('timestamp')
        prices=g[['open','high','low','close','volume']].apply(pd.to_numeric,errors='coerce')
        valid=np.isfinite(prices).all(axis=1)&prices[['open','high','low','close']].gt(0).all(axis=1)&prices.volume.ge(0)
        valid &= prices.high.ge(prices[['open','low','close']].max(axis=1))&prices.low.le(prices[['open','high','close']].min(axis=1))
        if len(g)<370 or not valid.all() or g.timestamp.duplicated().any():continue
        observed=g.set_index('timestamp');missing=expected.difference(observed.index)
        runs=[]
        for stamp in missing:
            if runs and stamp-runs[-1][-1]==pd.Timedelta(minutes=1):runs[-1].append(stamp)
            else:runs.append([stamp])
        for run in runs:
            before=run[0]-pd.Timedelta(minutes=1);after=run[-1]+pd.Timedelta(minutes=1)
            if len(run)>3 or before not in observed.index or after not in observed.index or any(t in blocked for t in run):continue
            previous=observed.loc[before];following=observed.loc[after]
            # Rolling strike changes across a hole cannot identify the missing
            # contract. Do not fabricate a strike or expiry transition.
            if 'strike' in g and previous.strike!=following.strike:continue
            for stamp in run:
                row=previous.to_dict();close=float(previous.close)
                row.update(timestamp=stamp,open=close,high=close,low=close,close=close,volume=0.,candle_source='synthetic_ffill',source_timestamp=before.isoformat())
                added.append(row)
                events.append(dict(timestamp=stamp.isoformat(),method='synthetic_ffill',source_timestamp=before.isoformat(),price=close))
    if added:frame=pd.concat([frame,pd.DataFrame(added)],ignore_index=True).sort_values('timestamp').reset_index(drop=True)
    return frame,events
