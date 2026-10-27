"""Parquet/DuckDB query foundation. Dates are inclusive start, exclusive end, in IST."""
from pathlib import Path
import json
import time
import duckdb
import pandas as pd
import pyarrow.parquet as pq
from quant_calendar import expiries

IST = 'Asia/Kolkata'
ROOT = Path(__file__).resolve().parent / 'data_lake'

class QuantDataLake:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.db = duckdb.connect()
        self.db.execute("SET TimeZone='Asia/Kolkata'")
        self.refresh()

    def refresh(self):
        p = self.root / 'ingestion_manifest.json'
        self.manifest = json.loads(p.read_text()) if p.exists() else {'chunks': {}}

    def instruments(self):
        return sorted({(v['dataset'], v['instrument']) for v in self.manifest['chunks'].values()
                       if v['status'] == 'complete'})

    def catalog(self):
        """UI metadata, including requested instruments with no available bars."""
        groups={}
        for v in self.manifest['chunks'].values():
            key=(v['dataset'],v['instrument'])
            item=groups.setdefault(key,dict(dataset=key[0],instrument=key[1],rows=0,
                                           complete_chunks=0,empty_chunks=0,failed_chunks=0,unavailable_chunks=0))
            item['rows']+=v.get('rows',0)
            field=v['status']+'_chunks'
            if field in item:item[field]+=1
        return list(groups.values())

    def query(self, instrument, start='2025-01-01', end=None, timeframe='1m',
              dataset='spot_1m', csv=None):
        if timeframe not in ('1m','3m','5m','15m','1d'):
            raise ValueError('Supported timeframes: 1m, 3m, 5m, 15m, 1d')
        if dataset == 'daily' and timeframe != '1d':
            raise ValueError('Daily source requires timeframe=1d')
        if dataset != 'daily' and timeframe == '1d':
            raise ValueError('Use dataset=daily for daily candles')
        lo = pd.Timestamp(start)
        lo = lo.tz_localize(IST) if lo.tzinfo is None else lo.tz_convert(IST)
        hi = pd.Timestamp(end) if end else pd.Timestamp.now(tz=IST).normalize()
        hi = hi.tz_localize(IST) if hi.tzinfo is None else hi.tz_convert(IST)
        if hi <= lo:
            raise ValueError('end must follow start (end is exclusive)')
        files = [str(self.root / v['path']) for v in self.manifest['chunks'].values()
                 if v['status']=='complete' and v['dataset']==dataset and v['instrument']==instrument
                 and pd.Timestamp(v['end'],tz=IST) > lo and pd.Timestamp(v['start'],tz=IST) < hi]
        if not files:
            return pd.DataFrame()
        source='SELECT * FROM read_parquet(?, union_by_name=true) WHERE timestamp >= ? AND timestamp < ?'
        sql=source+' ORDER BY timestamp'
        if timeframe not in ('1m','1d'):
            minutes=int(timeframe[:-1])
            names=pq.read_schema(files[0]).names
            aggregates=['first(open ORDER BY timestamp) AS open','max(high) AS high',
                        'min(low) AS low','last(close ORDER BY timestamp) AS close','sum(volume) AS volume',
                        'count(*) AS source_rows',f'count(*)={minutes} AS bar_complete']
            aggregates += [f'last({c} ORDER BY timestamp) AS {c}' for c in ('oi','iv','spot','strike') if c in names]
            aggregates += [f'first({c} ORDER BY timestamp) AS {c}' for c in
                ('instrument','security_id','exchange_segment','underlying_security_id','underlying_exchange_segment','instrument_type','contract_expiry','option_type','moneyness','expiry_flag','expiry_code') if c in names]
            if 'strike' in names:aggregates+=['count(DISTINCT strike)>1 AS strike_changes']
            # All supported minute intervals divide 15, aligning both 09:00 and 09:15.
            sql=(f"SELECT time_bucket(INTERVAL '{minutes} minutes',timestamp) AS timestamp, "
                 + ', '.join(aggregates)+f' FROM ({source}) GROUP BY 1 ORDER BY 1')
        frame = self.db.sql(sql,params=[files,lo.to_pydatetime(),hi.to_pydatetime()]).to_arrow_table().to_pandas()
        if frame.empty:
            return frame
        frame['timestamp'] = pd.to_datetime(frame.timestamp, utc=True).dt.tz_convert(IST)
        if frame.timestamp.duplicated().any():
            raise ValueError('Overlapping lake partitions; run integrity verification')
        if csv:
            frame.to_csv(csv, index=False)
        return frame

    def close(self):
        self.db.close()

    def _option_files(self, instrument, start, end):
        """All committed option streams, including wings and fixed contracts."""
        if instrument not in ('NIFTY', 'SENSEX'):
            raise ValueError('Option chain supports NIFTY and SENSEX')
        return [str(self.root / v['path']) for v in self.manifest['chunks'].values()
                if v['status'] == 'complete' and v['dataset'] in
                   ('options_rolling_1m', 'options_contract_1m', 'options_1m')
                and v['instrument'].startswith(instrument + '_')
                and pd.Timestamp(v['end']).date() > pd.Timestamp(start).date()
                and pd.Timestamp(v['start']).date() < pd.Timestamp(end).date()]

    def _option_source(self, files):
        # Older rolling partitions deliberately have no resolved expiry date.
        names = set().union(*(pq.read_schema(f).names for f in files))
        expiry = 'CAST(CAST(contract_expiry AS DATE) AS VARCHAR)' if 'contract_expiry' in names else 'NULL::VARCHAR'
        optional = {'expiry_flag': 'NULL::VARCHAR', 'expiry_code': 'NULL::INTEGER',
                    'moneyness': "'Fixed strike'", 'oi': 'NULL::DOUBLE',
                    'iv': 'NULL::DOUBLE', 'spot': 'NULL::DOUBLE'}
        defaults = ''.join(f', {value} AS {name}' for name, value in optional.items() if name not in names)
        return f'SELECT *, {expiry} AS expiry_date{defaults} FROM read_parquet(?, union_by_name=true)'

    def option_sessions(self, instrument, start='2025-01-01', end='2026-11-01'):
        """Observed trading dates and expiry identities, never calendar-inferred."""
        files = self._option_files(instrument, start, end)
        if not files:
            return pd.DataFrame(columns=['trading_date', 'expiry_flag', 'expiry_code', 'expiry_date'])
        sql = f'''SELECT DISTINCT timestamp::DATE AS trading_date,
                         expiry_flag, expiry_code, expiry_date
                  FROM ({self._option_source(files)})
                  WHERE timestamp::DATE >= ?::DATE AND timestamp::DATE < ?::DATE
                  ORDER BY 1, 2, 3, 4'''
        return self.db.sql(sql, params=[files, str(start), str(end)]).to_arrow_table().to_pandas()

    def option_minutes(self, instrument, trading_date, expiry_flag='WEEK', expiry_code=1,
                       expiry_date=None):
        """Observed numeric strikes across all ATM streams for one session/expiry.

        Identical stream overlaps count once. Conflicting quotes are retained as
        explicit gaps, with their measurements nulled rather than guessed.
        """
        lo = pd.Timestamp(trading_date).tz_localize(IST).normalize()
        hi = lo + pd.Timedelta(days=1)
        files = self._option_files(instrument, lo, hi)
        if not files:
            return pd.DataFrame()
        fields = ('open', 'high', 'low', 'close', 'volume', 'oi', 'iv', 'spot')
        measures = ', '.join(f'min({c}) AS {c}' for c in fields)
        sql = f'''SELECT timestamp, strike, option_type, {measures},
                         string_agg(DISTINCT moneyness, ', ' ORDER BY moneyness) AS moneyness,
                         count(DISTINCT row({', '.join(fields)})) > 1 AS quote_conflict
                  FROM ({self._option_source(files)})
                  WHERE timestamp >= ? AND timestamp < ? AND expiry_flag = ? AND expiry_code = ?
                    AND expiry_date IS NOT DISTINCT FROM ? AND strike > 0
                    AND option_type IN ('CE', 'PE')
                  GROUP BY timestamp, strike, option_type ORDER BY timestamp, strike, option_type'''
        frame = self.db.sql(sql, params=[files, lo.to_pydatetime(), hi.to_pydatetime(),
                                        expiry_flag, int(expiry_code), expiry_date]).to_arrow_table().to_pandas()
        frame['timestamp'] = pd.to_datetime(frame.timestamp, utc=True).dt.tz_convert(IST)
        frame.loc[frame.quote_conflict, list(fields)] = float('nan')
        return frame

    def weekly_option_sessions(self, instrument, start='2025-01-01', end='2026-11-01'):
        """Resolve weekly groups, keeping recorded and calendar-derived dates separate.

        A calendar mapping is a research grouping, NOT proof of a rolling
        contract's identity. Never persist inferred dates into source Parquet.
        """
        sessions = self.option_sessions(instrument, start, end)
        calendar = expiries(instrument)
        rows = []
        for row in sessions.itertuples():
            if row.expiry_flag != 'WEEK':
                continue
            day = pd.Timestamp(row.trading_date).date()
            recorded = None if pd.isna(row.expiry_date) else str(row.expiry_date)
            if recorded:
                expiry, basis = recorded, 'recorded'
            else:
                # The lake's WEEK1/2/3 streams are relative expiry selections.
                # Unrecognized or missing codes cannot be assigned a calendar date.
                if pd.isna(row.expiry_code) or row.expiry_code not in (1, 2, 3):
                    continue
                candidates = [d for d in calendar if d >= day.isoformat()]
                code = int(row.expiry_code)
                if len(candidates) < code:
                    continue
                expiry, basis = candidates[code - 1], 'calendar-derived'
            if day > pd.Timestamp(expiry).date():
                continue
            rows.append(dict(trading_date=day, expiry_flag=row.expiry_flag,
                             expiry_code=row.expiry_code, source_expiry=recorded,
                             expiry_date=expiry, expiry_basis=basis))
        return pd.DataFrame(rows, columns=['trading_date', 'expiry_flag', 'expiry_code',
                                           'source_expiry', 'expiry_date', 'expiry_basis'])

    def option_expiries(self, instrument, start='2025-01-01', end='2026-11-01'):
        """Expiry choices with actual coverage and an explicit identity basis."""
        sessions = self.weekly_option_sessions(instrument, start, end)
        if sessions.empty:
            return pd.DataFrame(columns=['expiry_date', 'expiry_basis', 'first_session', 'last_session', 'sessions'])
        return (sessions.groupby(['expiry_date', 'expiry_basis'], as_index=False)
                .agg(first_session=('trading_date', 'min'), last_session=('trading_date', 'max'),
                     sessions=('trading_date', 'nunique')).sort_values(['expiry_date', 'expiry_basis']))

    def option_history(self, instrument, expiry_date, start, end, expiry_basis='recorded',
                       strike=None, option_type=None):
        """All observed strikes for one dated weekly group, across sessions.

        Dates are inclusive/exclusive IST; numeric strike/side filtering occurs
        in DuckDB before deduplication or later candle aggregation. Expiries,
        identity bases and underlying instruments can never bleed into one another.
        """
        if expiry_basis not in ('recorded', 'calendar-derived'):
            raise ValueError('expiry_basis must be recorded or calendar-derived')
        if option_type is not None and option_type not in ('CE', 'PE'):
            raise ValueError('option_type must be CE or PE')
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        lo = lo.tz_localize(IST) if lo.tzinfo is None else lo.tz_convert(IST)
        hi = hi.tz_localize(IST) if hi.tzinfo is None else hi.tz_convert(IST)
        if hi <= lo:
            raise ValueError('end must follow start (end is exclusive)')
        # Catalog queries include the final partial day when bounds are intraday.
        catalog_end = hi.ceil('D') if hi != hi.normalize() else hi
        files = self._option_files(instrument, lo, catalog_end)
        if not files:
            return pd.DataFrame()
        mapping = self.weekly_option_sessions(instrument, lo.date(), catalog_end.date())
        mapping = mapping[mapping.expiry_date.eq(str(expiry_date)) & mapping.expiry_basis.eq(expiry_basis)]
        if mapping.empty:
            return pd.DataFrame()
        fields = ('open', 'high', 'low', 'close', 'volume', 'oi', 'iv', 'spot')
        values = ', '.join(f'min(q.{c}) AS {c}' for c in fields)
        condition, params = '', [files, lo.to_pydatetime(), hi.to_pydatetime()]
        if strike is not None:
            condition += ' AND q.strike = ?'
            params.append(float(strike))
        if option_type is not None:
            condition += ' AND q.option_type = ?'
            params.append(option_type)
        sql = f'''SELECT q.timestamp, q.strike, q.option_type, {values},
                         string_agg(DISTINCT q.moneyness, ', ' ORDER BY q.moneyness) AS moneyness,
                         count(DISTINCT row({', '.join('q.' + c for c in fields)})) > 1 AS quote_conflict
                  FROM ({self._option_source(files)}) q JOIN selected_option_sessions s
                    ON q.timestamp::DATE = s.trading_date::DATE
                    AND q.expiry_flag = s.expiry_flag
                    AND q.expiry_code IS NOT DISTINCT FROM s.expiry_code
                    AND q.expiry_date IS NOT DISTINCT FROM s.source_expiry
                  WHERE q.timestamp >= ? AND q.timestamp < ? AND q.strike > 0
                    AND q.option_type IN ('CE', 'PE') {condition}
                  GROUP BY q.timestamp, q.strike, q.option_type
                  ORDER BY q.timestamp, q.strike, q.option_type'''
        self.db.register('selected_option_sessions', mapping)
        try:
            frame = self.db.sql(sql, params=params).to_arrow_table().to_pandas()
        finally:
            self.db.unregister('selected_option_sessions')
        frame['timestamp'] = pd.to_datetime(frame.timestamp, utc=True).dt.tz_convert(IST)
        frame.loc[frame.quote_conflict, list(fields)] = float('nan')
        frame['expiry_date'], frame['expiry_basis'] = str(expiry_date), expiry_basis
        return frame

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def resample(frame, minutes):
    """Session anchored, left-labelled bars; no forward-fill across gaps or days.

    bar_complete and strike_changes let the UI reject sparse/rolling-mixed bars.
    An OHLC rolling series is not a fixed contract's executable P&L history.
    """
    pieces=[]
    for _, group in frame.groupby(frame.timestamp.dt.date):
        group=group.set_index('timestamp')
        offset='9h' if group.exchange_segment.iloc[0]=='MCX_COMM' else '9h15min'
        agg={'open':'first','high':'max','low':'min','close':'last','volume':'sum'}
        agg.update({c:'last' for c in ('oi','iv','spot','strike') if c in group})
        agg.update({c:'first' for c in ('instrument','security_id','exchange_segment','option_type','moneyness','expiry_flag','expiry_code') if c in group})
        r=group.resample(f'{minutes}min',origin='start_day',offset=offset)
        bars=r.agg(agg)
        bars['source_rows']=r.size()
        bars['bar_complete']=bars.source_rows.eq(minutes)
        if 'strike' in group:
            bars['strike_changes']=r['strike'].nunique().gt(1)
        pieces.append(bars[bars.source_rows.gt(0)].reset_index())
    return pd.concat(pieces,ignore_index=True)


def summary(root=ROOT):
    root=Path(root)
    with QuantDataLake(root) as lake:
        rows=[]
        for item in lake.catalog():
            dataset,instrument=item['dataset'],item['instrument']
            entries=[v for v in lake.manifest['chunks'].values() if v['status']=='complete'
                     and (v['dataset'],v['instrument'])==(dataset,instrument)]
            files=[str(root/v['path']) for v in entries]
            if not files:
                rows.append(dict(dataset=dataset,instrument=instrument,earliest='N/A',latest='N/A',
                                 trading_days=0,rows=0,size_mb=0,path=f'{dataset}/{instrument}'))
                continue
            result=lake.db.execute("SELECT min(timestamp)::DATE, max(timestamp)::DATE, "
                "count(DISTINCT timestamp::DATE), count(*) FROM read_parquet(?,union_by_name=true)", [files]).fetchall()[0]
            rows.append(dict(dataset=dataset,instrument=instrument,earliest=str(result[0]),latest=str(result[1]),
                             trading_days=result[2],rows=result[3],size_mb=round(sum(Path(f).stat().st_size for f in files)/1024**2,2),
                             path=str(Path(entries[0]['path']).parent)))
        return pd.DataFrame(rows)
