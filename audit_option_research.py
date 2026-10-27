"""Inspectable Step 4 checks. EOD diagnostics never feed strategy signals."""
import numpy as np
import pandas as pd
from quant_backtester import ROOT, OUT, ResearchData, intrinsic
from quant_calendar import is_expiry_day
from quant_option_model import continuation_candle, MissingObservedPrice


def audit(data,output=OUT):
    output.mkdir(parents=True,exist_ok=True)
    expiry_rows=[]; model_errors=[]
    for date,day in data.days.items():
        parts={}
        for side in ('CE','PE'):
            for label,lo,hi in [('morning',560,600),('eod',920,929)]:
                extra=[]; iv=[]
                for at in range(lo,hi+1):
                    strike=day['rolling'][side,'ATM'].get(at)
                    p=day['options'].get((at,side,strike)); meta=day['option_meta'].get((at,side,strike))
                    if p and meta:
                        extra.append(p[3]-intrinsic(meta[2],strike,side)); iv.append(meta[1])
                parts[f'{side}_{label}_extrinsic']=float(np.median(extra)) if extra else np.nan
                parts[f'{side}_{label}_iv']=float(np.median(iv)) if iv else np.nan
        morning=np.mean([parts['CE_morning_extrinsic'],parts['PE_morning_extrinsic']])
        end=np.mean([parts['CE_eod_extrinsic'],parts['PE_eod_extrinsic']])
        iv_ratio=np.mean([parts['CE_eod_iv'],parts['PE_eod_iv']])/max(1e-9,np.mean([parts['CE_morning_iv'],parts['PE_morning_iv']]))
        expiry_rows.append(dict(instrument=data.instrument,date=date,is_expiry_day=is_expiry_day(data.instrument,date),
            empirical_crush_support=bool(end<=max(5,morning*.25) and iv_ratio<.75),extrinsic_eod=end,
            iv_eod_to_morning=iv_ratio,diagnostic_only=True,contract_expiry_verified=False,**parts))
        # Deterministic one-step pseudo-missing observations, including multiple
        # moneyness values. Not a test of long missing paths or execution prices.
        for at in (605,735,865,915):
            for side in ('CE','PE'):
                for mon in ('ATM','ATM-1','ATM+1'):
                    strike=day['rolling'][side,mon].get(at)
                    prev=day['options'].get((at-1,side,strike)); actual=day['options'].get((at,side,strike))
                    if prev is None or actual is None: continue
                    try: pred,source=continuation_candle(data,day,at,side,strike,prev)
                    except MissingObservedPrice: continue
                    model_errors.append(dict(instrument=data.instrument,date=date,minute=at,side=side,strike=strike,
                        actual_close=actual[3],modeled_close=pred[3],error=pred[3]-actual[3],neighbor=source['model_neighbor_strike']))
    ef=pd.DataFrame(expiry_rows); mf=pd.DataFrame(model_errors)
    ef.to_parquet(output/f'expiry_diagnostics_{data.instrument}.parquet',index=False)
    mf.to_parquet(output/f'model_holdout_{data.instrument}.parquet',index=False)
    stats=dict(instrument=data.instrument,sessions=len(ef),calendar_expiry_sessions=int(ef.is_expiry_day.sum()),
               empirical_crush_on_calendar_expiry=int((ef.is_expiry_day & ef.empirical_crush_support).sum()),
               held_out_prices=len(mf),model_mae=float(mf.error.abs().mean()) if len(mf) else None,
               model_rmse=float(np.sqrt((mf.error**2).mean())) if len(mf) else None,
               note='Retrospective diagnostics; empirical crush is not expiry identity proof. One-step model errors do not validate long extrapolations.')
    return stats


if __name__=='__main__':
    from quant_backtester import atomic_json
    reports=[audit(ResearchData(name)) for name in ('NIFTY','SENSEX')]
    atomic_json(OUT/'option_data_audit.json',reports)
    print(pd.DataFrame(reports).to_string(index=False))
