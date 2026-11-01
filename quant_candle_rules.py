"""Storage validity, separate from tradable option-price requirements.

Underlying spot/futures/daily OHLC must be strictly positive. Option premiums
may be zero in provider candles (e.g. worthless contracts), but never negative;
zero is not an executable entry quote. Option underlying references and strikes
must be positive when present. Volume/OI/IV may be zero, never negative/nonfinite.
No function here changes a stored value or fills a missing candle.
"""
import numpy as np
import pandas as pd

CANDLE_VALIDATION_VERSION='positive-underlying-v1'


def invalid_candles(frame, *, option_prices=False):
    values=frame[['open','high','low','close','volume']].apply(pd.to_numeric,errors='coerce')
    prices=values[['open','high','low','close']]
    bad=~np.isfinite(values).all(axis=1) | values.volume.lt(0)
    bad |= prices.lt(0).any(axis=1) if option_prices else prices.le(0).any(axis=1)
    bad |= values.high.lt(prices.max(axis=1)) | values.low.gt(prices.min(axis=1))
    for field in ('oi','iv'):
        if field in frame:
            number=pd.to_numeric(frame[field],errors='coerce')
            bad |= ~np.isfinite(number) | number.lt(0)
    if option_prices:
        for field in ('spot','strike'):
            if field in frame:
                number=pd.to_numeric(frame[field],errors='coerce')
                bad |= ~np.isfinite(number) | number.le(0)
    return bad
