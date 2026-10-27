"""Verify stored partitions, catalogue coverage, and measure query latency."""
import json
from pathlib import Path
import time
import pandas as pd
from quant_lake import QuantDataLake,ROOT,summary
from step2_build_data_lake import digest,atomic_json


def audit(root=ROOT):
    root=Path(root)
    with QuantDataLake(root) as lake:
        issues=[];sessions=[];bench=[]
        entries=list(lake.manifest['chunks'].values())
        totals={k:sum(v.get('quality',{}).get(k,0) for v in entries) for k in
                ['received','stored','exact_duplicates','conflicting_duplicate_rows','ambiguous_timestamps','outside_request','outside_market_hours','invalid_ohlcv','missing_internal_minutes']}
        for v in entries:
            if v['status']=='failed':issues.append(dict(instrument=v['instrument'],start=v['start'],issue=v['error']))
            if v['status']=='complete':
                p=root/v['path']
                if not p.exists() or digest(p)!=v['sha256']:issues.append(dict(path=v['path'],issue='checksum mismatch'))
        for dataset,instrument in lake.instruments():
            files=[str(root/v['path']) for v in entries if v['status']=='complete'
                   and (v['dataset'],v['instrument'])==(dataset,instrument)]
            dup=lake.db.execute('SELECT count(*)-count(DISTINCT timestamp) FROM read_parquet(?,union_by_name=true)',[files]).fetchall()[0][0]
            if dup:issues.append(dict(instrument=instrument,issue='duplicate timestamps',count=dup))
            coverage=lake.db.sql('SELECT timestamp::DATE AS session, count(*) AS rows, min(timestamp) AS first_bar, '
               'max(timestamp) AS last_bar FROM read_parquet(?,union_by_name=true) GROUP BY 1 ORDER BY 1',params=[files]).to_arrow_table().to_pandas()
            coverage['instrument']=instrument;coverage['dataset']=dataset
            # Flag short sessions for review, not an assumption that every day has 375 bars.
            coverage['short_session_review']=(coverage.rows<375)&(dataset!='daily')&(not instrument.startswith(('CRUDEOIL','NATURALGAS','GOLDM')))
            sessions.append(coverage)
        for tf in ['1m','3m','5m','15m']:
            t=time.perf_counter();f=lake.query('NIFTY',timeframe=tf)
            bench.append(dict(instrument='NIFTY',timeframe=tf,rows=len(f),milliseconds=round(1000*(time.perf_counter()-t),2)))
        coverage=pd.concat(sessions,ignore_index=True) if sessions else pd.DataFrame()
        coverage.to_parquet(root/'session_coverage.parquet',index=False)
        result=dict(checked_at=pd.Timestamp.now(tz='Asia/Kolkata').isoformat(),issues=issues,quality_counts=totals,
                    statuses={s:sum(v['status']==s for v in entries) for s in ['complete','failed','empty','unavailable']},
                    short_sessions_for_review=int(coverage.short_session_review.sum()) if len(coverage) else 0,
                    notes=['Out-of-hours and invalid OHLCV rows are quarantined, never silently repaired.',
                           'Internal gaps and short sessions require exchange-calendar review; missing bars are not filled.',
                           'Rolling ATM series changes strike/contract; it is not a fixed-contract tradable return series.',
                           'Active MCX contracts do not establish continuous commodity history from 2025.'])
        atomic_json(root/'quality_report.json',result);atomic_json(root/'query_benchmark.json',bench)
        table=summary(root);table.to_csv(root/'summary.csv',index=False)
        print(table.to_string(index=False));print(json.dumps(result,indent=2));print('Query benchmarks:',bench)
        return len(issues)

if __name__=='__main__':raise SystemExit(bool(audit()))
