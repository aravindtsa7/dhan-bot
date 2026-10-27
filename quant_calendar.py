"""Dated index expiry rules; calendar dates do not prove rolling contract identity."""
from datetime import date, timedelta
from functools import lru_cache
import hashlib
import json
from pathlib import Path

SOURCES = {
    'nse_expiry': 'https://nsearchives.nseindia.com/content/circulars/FAOP68747.pdf',
    'bse_2025_january': 'https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=20241128-70',
    'bse_2025_september': 'https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=20250623-59',
    'holidays_2025': 'https://nsearchives.nseindia.com/content/circulars/FAOP65588.pdf',
    'holidays_2026': 'https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf',
    'january_2026_addendum': 'https://nsearchives.nseindia.com/content/circulars/CMTR72260.pdf',
    'bse_january_2026_addendum': 'https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=20260112-8',
}
# Regular daytime closures, not settlement-only holidays. Muhurat sessions do
# not count as regular expiry sessions. The BSE calendar is aligned to this list;
# recorded as a calendar assumption, not expiry-resolved instrument metadata.
HOLIDAYS = set('''2025-02-26 2025-03-14 2025-03-31 2025-04-10 2025-04-14
2025-04-18 2025-05-01 2025-08-15 2025-08-27 2025-10-02 2025-10-21
2025-10-22 2025-11-05 2025-12-25 2026-01-15 2026-01-26 2026-03-03
2026-03-26 2026-03-31 2026-04-03 2026-04-14 2026-05-01 2026-05-28
2026-06-26 2026-09-14 2026-10-02 2026-10-20 2026-11-10 2026-11-24
2026-12-25'''.split())
CALENDAR_VERSION = '2025-2026-rules-1'
CALENDAR_HASH = hashlib.sha256(json.dumps([CALENDAR_VERSION,sorted(HOLIDAYS),SOURCES],sort_keys=True).encode()).hexdigest()[:16]


def regular_session(day):
    day=date.fromisoformat(str(day)[:10])
    return day.weekday()<5 and day.isoformat() not in HOLIDAYS


@lru_cache(maxsize=2)
def expiries(instrument):
    if instrument not in ('NIFTY','SENSEX'): return ()
    result=set()
    for offset in range((date(2027,1,1)-date(2025,1,1)).days):
        d=date(2025,1,1)+timedelta(days=offset)
        weekday=(3 if d<date(2025,9,1) else 1) if instrument=='NIFTY' else (4 if d<=date(2025,1,3) else 1 if d<date(2025,9,1) else 3)
        if d.weekday()!=weekday: continue
        adjusted=d
        while not regular_session(adjusted): adjusted-=timedelta(days=1)
        result.add(adjusted.isoformat())
    return tuple(sorted(result))


def next_expiry(instrument, session):
    session=str(session)[:10]
    dates=[d for d in expiries(instrument) if d>=session]
    if not dates: raise ValueError('Expiry calendar is restricted to 2025-2026')
    return dates[0]


def is_expiry_day(instrument, session):
    return str(session)[:10] in expiries(instrument)


def export_calendar(root):
    import pandas as pd
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    rows=[dict(instrument=i,date=d,is_expiry_day=True,basis='dated rules + holiday adjustment; contract identity unverified')
          for i in ('NIFTY','SENSEX') for d in expiries(i)]
    pd.DataFrame(rows).to_csv(root/'expiry_calendar.csv',index=False)
    (root/'expiry_calendar_sources.json').write_text(json.dumps(dict(version=CALENDAR_VERSION,sources=SOURCES,holidays=sorted(HOLIDAYS),
        limitations=['BSE regular holidays aligned to NSE list and BSE January election addendum.',
                     'Calendar labels are underlying expiry sessions, not confirmed expiry of rolling candles.',
                     'EOD extrinsic/IV diagnostics are retrospective and never used in intraday signals.']),indent=2))


if __name__=='__main__':
    export_calendar(Path(__file__).parent/'data_lake')
