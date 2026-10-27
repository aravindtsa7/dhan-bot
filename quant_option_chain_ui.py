"""Weekly multi-strike option ladders and observed fixed-strike replay."""
from datetime import date, time, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from quant_lake import IST, ROOT, QuantDataLake
from quant_calendar import CALENDAR_HASH, regular_session

FIRST_DATE = date(2025, 1, 1)
LAST_DATE = date(2026, 10, 31)
CHAIN_COLUMNS = ['CE stream', 'CE LTP', 'CE Volume', 'CE OI', 'CE IV',
                 'Strike Price', 'Spot Index LTP',
                 'PE LTP', 'PE Volume', 'PE OI', 'PE IV', 'PE stream']
MATRIX_COLUMNS = ['CE LTP Chg (%)', 'CE OI', 'CE OI Chg (%)', 'CE Volume', 'CE IV', 'LTP (CE)',
                  'Strike Price', 'LTP (PE)', 'PE IV', 'PE Volume', 'PE OI Chg (%)', 'PE OI', 'PE LTP Chg (%)']


@st.cache_data(ttl=120, show_spinner=False)
def load_expiries(root, revision, instrument, calendar_revision=CALENDAR_HASH):
    with QuantDataLake(root) as lake:
        return lake.option_expiries(instrument)


@st.cache_data(ttl=120, show_spinner=False)
def load_week(root, revision, instrument, expiry, basis, first_session, calendar_revision=CALENDAR_HASH):
    with QuantDataLake(root) as lake:
        end = str(date.fromisoformat(expiry) + timedelta(days=1))
        options = lake.option_history(instrument, expiry, str(first_session), end, basis)
        spot = lake.query(instrument, str(first_session), end)
    return options, spot


def previous_session(day):
    day -= timedelta(days=1)
    while not regular_session(day):
        day -= timedelta(days=1)
    return day


def weekly_chain_matrix(options, spot, at, instrument, view='ATM ±10'):
    """One exact-minute CE/PE ladder; prior-session close is the change baseline.

    The strike universe includes every observation of the selected expiry,
    independently of the rolling moneyness stream or snapshot timestamp.
    Session volume is the sum of observed minutes; coverage accompanies it.
    """
    observed = sorted(options.strike.unique()) if not options.empty else []
    exact_spot = spot.loc[spot.timestamp.eq(at), 'close'] if not spot.empty else pd.Series(dtype=float)
    spot_ltp = exact_spot.iloc[0] if not exact_spot.empty else float('nan')
    step = 50 if instrument == 'NIFTY' else 100
    atm = int((spot_ltp + step / 2) // step) * step if pd.notna(spot_ltp) else None
    if atm is None and not options.empty:
        atm_rows = options[options.timestamp.eq(at) & options.moneyness.fillna('').str.split(', ').map(lambda x: 'ATM' in x)]
        if not atm_rows.empty and atm_rows.strike.nunique() == 1:
            atm = float(atm_rows.strike.iloc[0])
    strikes = [atm + offset * step for offset in range(-10, 11)] if view == 'ATM ±10' and atm is not None else observed
    frame = pd.DataFrame(index=pd.Index(strikes, name='Strike Price'))
    prior = previous_session(at.date())
    coverage = []
    for side in ('CE', 'PE'):
        if options.empty:
            for col in MATRIX_COLUMNS:
                if col != 'Strike Price':
                    frame[col] = float('nan')
            continue
        side_rows = options[options.option_type.eq(side)]
        current = side_rows[side_rows.timestamp.eq(at)].set_index('strike')
        # Only a genuine prior-session closing observation is an EOD baseline.
        # A last-seen midday quote is not a previous close.
        baseline = side_rows[(side_rows.timestamp.dt.date == prior)
                             & (side_rows.timestamp.dt.hour == 15)
                             & side_rows.timestamp.dt.minute.isin([29, 30])]
        baseline = baseline.sort_values('timestamp').drop_duplicates('strike', keep='last').set_index('strike')
        day_rows = side_rows[(side_rows.timestamp.dt.date == at.date()) & side_rows.timestamp.le(at)]
        totals = day_rows.groupby('strike').volume.sum(min_count=1)
        counts = day_rows.groupby('strike').close.count()
        frame[f'LTP ({side})'] = current.close
        frame[f'{side} OI'] = current.oi
        frame[f'{side} IV'] = current.iv
        # Suppress volume where there is no snapshot quote; never imply a stale
        # option is currently quoted. Gaps in earlier minutes are disclosed below.
        frame[f'{side} Volume'] = totals.reindex(current.index[current.close.notna()])
        for source, label in [('close', 'LTP'), ('oi', 'OI')]:
            base = baseline[source].where(baseline[source].gt(0))
            frame[f'{side} {label} Chg (%)'] = (current[source] / base - 1) * 100
        for strike in strikes:
            coverage.append(dict(strike=strike, side=side, observed_minutes=int(counts.get(strike, 0)),
                                 expected_minutes=max(0, (at.hour * 60 + at.minute) - (9 * 60 + 15) + 1),
                                 baseline_date=str(prior),
                                 baseline_time=baseline.timestamp.get(strike) if strike in baseline.index else None))
    result = frame.reset_index().reindex(columns=MATRIX_COLUMNS)
    result.attrs.update(atm=atm, spot=spot_ltp, baseline_date=str(prior), coverage=coverage,
                        available_strikes=len(observed))
    return result


def ladder_style(frame):
    """Shade CE/PE wings and make the actual ATM strike unmistakable."""
    atm = frame.attrs.get('atm')
    def colors(row):
        values = ['background-color: #102b2b'] * 6 + ['background-color: #17243b; font-weight: bold'] + ['background-color: #30202a'] * 6
        if row['Strike Price'] == atm:
            values = ['background-color: #384023; font-weight: bold'] * len(row)
        return values
    formats = {name: '{:,.2f}' for name in MATRIX_COLUMNS}
    formats.update({name: '{:,.0f}' for name in MATRIX_COLUMNS if name == 'Strike Price' or name.endswith((' OI', ' Volume'))})
    display = frame.copy()
    display.attrs = {}  # Coverage metadata is rendered separately, not serialized into Arrow.
    return display.style.apply(colors, axis=1).format(formats, na_rep='—')


def selected_strike(frame, rows, fallback):
    """Streamlit selection positions refer to the original dataframe order."""
    if rows and 0 <= rows[0] < len(frame):
        return frame.iloc[rows[0]]['Strike Price']
    return fallback


def chain_snapshot(options, spot, at):
    """Join CE/PE on numeric strike at exactly one minute; never carry quotes."""
    if options.empty:
        return pd.DataFrame(columns=CHAIN_COLUMNS)
    minute = options[options.timestamp.eq(at)]
    strikes = sorted(minute.strike.unique())
    chain = pd.DataFrame(index=pd.Index(strikes, name='Strike Price'))
    for side in ('CE', 'PE'):
        quotes = minute[minute.option_type.eq(side)].set_index('strike')
        for source, label in [('moneyness', 'stream'), ('close', 'LTP'), ('volume', 'Volume'),
                              ('oi', 'OI'), ('iv', 'IV')]:
            chain[f'{side} {label}'] = quotes[source]
    exact_spot = spot[spot.timestamp.eq(at)] if not spot.empty else spot
    chain['Spot Index LTP'] = exact_spot.close.iloc[0] if not exact_spot.empty else float('nan')
    return chain.reset_index().reindex(columns=CHAIN_COLUMNS)


def strike_replay(options, strike, side, start, at, timeframe='1m'):
    """Filter the numeric strike BEFORE aggregation; return explicit minute gaps.

    Five-minute bars require five observed minutes. Sparse or unfinished buckets
    have null OHLCV/OI, so the chart never claims an exact candle from partial data.
    All timestamps label the start of their minute/bucket, matching the lake.
    """
    if timeframe not in ('1m', '5m'):
        raise ValueError('Replay supports 1m and 5m')
    fields = ['open', 'high', 'low', 'close', 'volume', 'oi', 'iv']
    if options.empty:
        quotes = pd.DataFrame(columns=fields, index=pd.DatetimeIndex([], tz=IST, name='timestamp'))
    else:
        quotes = options.loc[options.strike.eq(strike) & options.option_type.eq(side)
                             & options.timestamp.between(start, at)].set_index('timestamp')
    pieces = []
    observed_days = set(quotes.index.date)
    for day in pd.date_range(start.normalize(), at.normalize(), freq='D'):
        if not regular_session(day.date()) and day.date() not in observed_days:
            continue
        lo = max(start, day + pd.Timedelta(hours=9, minutes=15))
        hi = min(at, day + pd.Timedelta(hours=15, minutes=30))
        if hi < lo:
            continue
        frame = quotes.reindex(pd.date_range(lo, hi, freq='min', name='timestamp'))[fields].copy()
        frame['source_rows'] = frame[['open', 'high', 'low', 'close']].notna().all(axis=1).astype(int)
        if timeframe == '5m':
            agg = dict(open='first', high='max', low='min', close='last',
                       volume=lambda s: s.sum(min_count=1), oi=lambda s: s.iloc[-1],
                       iv=lambda s: s.iloc[-1], source_rows='sum')
            frame = frame.resample('5min', origin='start_day', offset='9h15min').agg(agg)
        frame['bar_complete'] = frame.source_rows.eq(5 if timeframe == '5m' else 1)
        frame.loc[~frame.bar_complete, fields] = float('nan')
        pieces.append(frame.reset_index())
    if not pieces:
        return pd.DataFrame({'timestamp': pd.Series([], dtype=f'datetime64[ns, {IST}]'),
                             **{c: pd.Series(dtype=float) for c in fields},
                             'source_rows': pd.Series(dtype=int), 'bar_complete': pd.Series(dtype=bool)})
    return pd.concat(pieces, ignore_index=True)


def replay_figure(frame, title):
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=.04,
                        row_heights=[.62, .19, .19], subplot_titles=[title, 'Volume', 'Open interest'])
    # Explicit local wall-clock axes avoid browser timezone conversion.
    x = frame.timestamp.dt.tz_localize(None)
    fig.add_trace(go.Candlestick(x=x, open=frame.open, high=frame.high, low=frame.low,
                                close=frame.close, name='Observed OHLC',
                                increasing_line_color='#36cfaf', decreasing_line_color='#ff6685'), row=1, col=1)
    colors = ['#36cfaf' if c >= o else '#ff6685' for c, o in zip(frame.close, frame.open)]
    fig.add_trace(go.Bar(x=x, y=frame.volume, name='Volume', marker_color=colors), row=2, col=1)
    oi_x, oi_y, last_day = [], [], None
    for timestamp, oi in zip(x, frame.oi):
        if last_day is not None and timestamp.date() != last_day:
            oi_x.append(None); oi_y.append(None)
        oi_x.append(timestamp); oi_y.append(oi)
        last_day = timestamp.date()
    fig.add_trace(go.Scatter(x=oi_x, y=oi_y, name='OI', mode='lines+markers',
                             connectgaps=False, marker_size=3, line_color='#92a7ff'), row=3, col=1)
    fig.update_xaxes(rangeslider_visible=False, tickformat='%d %b<br>%H:%M', hoverformat='%d %b %Y %H:%M',
                     rangebreaks=[dict(bounds=['sat', 'mon']), dict(bounds=[15 + 31/60, 9.25], pattern='hour')])
    fig.update_xaxes(title_text='Time (IST) · candle start', row=3, col=1)
    fig.update_yaxes(title_text='Premium', row=1, col=1)
    fig.update_layout(template='plotly_dark', paper_bgcolor='#0b101a', plot_bgcolor='#101827',
                      height=700, margin=dict(l=25, r=25, t=50, b=25),
                      legend=dict(orientation='h'), hovermode='x unified')
    return fig


@st.fragment
def render_option_chain(root=ROOT):
    st.subheader('📊 Option Chain & Strike Replay')
    st.caption('WEEKLY EXPIRY · MULTI-STRIKE LADDER · FIXED-STRIKE HISTORY · IST')
    try:
        _render_option_chain(Path(root))
    except (OSError, ValueError, duckdb.Error) as exc:
        st.error(f'Unable to read option-chain data: {exc}')


def _render_option_chain(root):
    manifest = root / 'ingestion_manifest.json'
    revision = manifest.stat().st_mtime_ns if manifest.exists() else 0
    a, b = st.columns([4, 1])
    instrument = a.selectbox('Chain instrument', ['NIFTY', 'SENSEX'],
        format_func=lambda v: 'Nifty' if v == 'NIFTY' else 'Sensex', key='chain_instrument')
    if b.button('Refresh chain data', key='chain_refresh'):
        load_expiries.clear(); load_week.clear(); st.rerun()
    catalog = load_expiries(str(root), revision, instrument, CALENDAR_HASH)
    if catalog.empty:
        st.info('No weekly option observations for this instrument from January 2025 through October 2026.')
        return
    choices = list(zip(catalog.expiry_date, catalog.expiry_basis))
    # Default to the latest expiry with observations in its Monday-to-expiry week.
    eligible = [i for i, row in enumerate(catalog.itertuples())
                if pd.Timestamp(row.last_session).date() >=
                (date.fromisoformat(row.expiry_date) - timedelta(days=date.fromisoformat(row.expiry_date).weekday()))]
    a, b = st.columns([2, 1])
    expiry, basis = a.selectbox('Weekly expiry date', choices, index=eligible[-1] if eligible else len(choices)-1,
        format_func=lambda v: f'{v[0]} · {"recorded expiry" if v[1] == "recorded" else "calendar-derived; unverified"}',
        key=f'chain_weekly_expiry_{instrument}')
    view = b.radio('Strike ladder', ['ATM ±10', 'All available strikes'], horizontal=True, key='chain_ladder')
    metadata = catalog[catalog.expiry_date.eq(expiry) & catalog.expiry_basis.eq(basis)].iloc[0]
    options, spot = load_week(str(root), revision, instrument, expiry, basis, metadata.first_session, CALENDAR_HASH)
    expiry_day = date.fromisoformat(expiry)
    monday = max(FIRST_DATE, expiry_day - timedelta(days=expiry_day.weekday()))
    last_day = min(expiry_day, LAST_DATE)
    context = f'{instrument}_{expiry}_{basis}'
    week = options[options.timestamp.dt.date.between(monday, last_day)] if not options.empty else options
    if basis != 'recorded':
        st.info('Expiry dates are calendar-derived groups of rolling WEEK streams, not verified contract identities. '
                'The chart follows the same numeric strike within that group; exact-contract expiry verification requires dated source records.')
    if week.empty:
        st.info('No candles are stored from Monday through this expiry close. Select an earlier expiry with recorded sessions.')
        return
    days = sorted(week.timestamp.dt.date.unique())
    a, b, c = st.columns([2, 1, 1])
    day = a.date_input('Trading date', days[-1], min_value=monday, max_value=last_day, key=f'chain_date_{context}')
    common = week.timestamp[(week.timestamp.dt.date == day) & week.timestamp.isin(spot.timestamp)] if not spot.empty else pd.Series(dtype='object')
    initial_time = common.max().time() if not common.empty else time(15, 29)
    snapshot_time = b.time_input('Snapshot time (IST)', initial_time, step=60, key=f'chain_time_{context}_{day}')
    timeframe = c.selectbox('Replay candle interval', ['1m', '5m'], key='chain_timeframe')
    if not time(9, 15) <= snapshot_time <= time(15, 30):
        st.error('Snapshot time must be between 09:15 and 15:30 IST.')
        return
    at = pd.Timestamp.combine(day, snapshot_time).tz_localize(IST).floor('min')
    chain = weekly_chain_matrix(options, spot, at, instrument, view)
    atm = chain.attrs['atm']
    a, b, c, d = st.columns(4)
    a.metric('Spot index', f"{chain.attrs['spot']:,.2f}" if pd.notna(chain.attrs['spot']) else 'Unavailable')
    b.metric('ATM strike', f'{atm:,.0f}' if atm is not None else 'Unavailable')
    c.metric('Strikes in this expiry', str(chain.attrs['available_strikes']))
    quoted = chain[['LTP (CE)', 'LTP (PE)']].notna().any(axis=1).sum()
    d.metric('Quoted ladder rows', f'{quoted} / {len(chain)}')
    st.caption(f'Snapshot {at:%d %b %Y %H:%M} IST · Changes vs {chain.attrs["baseline_date"]} close · '
               f'History window {monday} → {expiry_day} · {len(days)} observed sessions')
    st.caption('LTP is the minute close. Volume sums observed minutes in the selected session and may be partial. '
               'OI is the current level; IV uses source units. Missing quotes and previous-close baselines remain blank.')
    if not quoted:
        st.info('No option quotes at this exact minute. The expiry strike ladder remains available; earlier quotes are not carried forward.')
    if atm is None:
        st.caption('No current spot or unique ATM observation; displaying available strikes without an ATM highlight.')
    st.markdown('**CALLS (CE)**  ·  Click a strike row to replay  ·  **PUTS (PE)**')
    strike_key = f'chain_selected_strike_{context}'
    strikes = chain['Strike Price'].tolist()
    preferred = atm if atm in strikes else strikes[0]
    if st.session_state.get(strike_key) not in strikes:
        st.session_state[strike_key] = preferred
    table_key = f'chain_matrix_{context}_{day}_{snapshot_time}_{view}'
    def on_strike_row():
        state = st.session_state.get(table_key, {})
        rows = state.get('selection', {}).get('rows', [])
        st.session_state[strike_key] = selected_strike(chain, rows, st.session_state[strike_key])
    columns = {name: st.column_config.NumberColumn(name, help=(
        'Change from the same expiry and strike at the previous regular session close.' if 'Chg' in name else
        'Observed session volume; see coverage details for missing minutes.' if 'Volume' in name else
        'Current at-the-money strike is highlighted.' if name == 'Strike Price' else None)) for name in MATRIX_COLUMNS}
    st.dataframe(ladder_style(chain), hide_index=True, width='stretch', height=min(820, 38 + 31 * len(chain)),
                 column_config=columns, key=table_key, on_select=on_strike_row, selection_mode='single-row')
    with st.expander('Quote coverage and change baselines'):
        st.write('The ladder includes ATM±10 slots or every strike observed for this expiry. '
                 'A wider ladder does not create missing historical prices. '
                 'Only 15:29/15:30 observations from the prior regular trading session qualify as closing baselines. '
                 'Calendar-derived and recorded expiry identities are kept separate.')
        st.dataframe(pd.DataFrame(chain.attrs['coverage']), hide_index=True, width='stretch')
        st.caption(f'{int(options.quote_conflict.sum())} conflicting strike/minute observations excluded. '
                   'Missing or conflicting OHLC candles are not modeled or forward-filled.')
    st.download_button('Export option chain CSV', chain.to_csv(index=False),
                       f'{instrument}_{expiry}_{day}_{snapshot_time:%H%M}_chain.csv', 'text/csv', key='chain_matrix_export')
    st.divider()
    a, b, c = st.columns([2, 1, 2])
    strike = a.selectbox('Strike to replay', strikes, format_func=lambda v: f'{v:g}', key=strike_key)
    side = b.radio('Option side', ['CE', 'PE'], horizontal=True, key='chain_side')
    mode = c.radio('Chart range', ['Full expiry week', 'Through selected date/time'], key='chain_chart_range')
    start = pd.Timestamp.combine(monday, time(9, 15)).tz_localize(IST)
    cutoff = pd.Timestamp.combine(last_day, time(15, 30)).tz_localize(IST) if mode == 'Full expiry week' else at
    replay = strike_replay(options, strike, side, start, cutoff, timeframe)
    complete = int(replay.bar_complete.sum())
    gaps = len(replay) - complete
    st.subheader(f'{instrument} · {strike:g} {side} · expiry {expiry}')
    st.caption(f'{mode}: {start:%d %b %H:%M} → {cutoff:%d %b %H:%M} IST · '
               f'{complete} observed {timeframe} candles · {gaps} missing / incomplete intervals. '
               'Five-minute candles require five observed minutes. OI is the last minute, never a sum.')
    if mode == 'Full expiry week':
        st.caption('Full-week review includes observations after the table snapshot. Select Through selected date/time for chronological replay.')
    if not complete:
        st.info('No complete candles for this strike, side and interval in the selected week.')
    st.plotly_chart(replay_figure(replay, f'{instrument} · {strike:g} {side} · {expiry} expiry · {timeframe}'), width='stretch')
    replay['strike'], replay['option_type'] = strike, side
    replay['expiry_date'], replay['expiry_basis'] = expiry, basis
    with st.expander('Inspect replay candles'):
        st.dataframe(replay, hide_index=True, width='stretch')
    st.download_button('Export strike replay CSV', replay.to_csv(index=False),
                       f'{instrument}_{expiry}_{strike:g}_{side}_{timeframe}.csv', 'text/csv', key='chain_export')
