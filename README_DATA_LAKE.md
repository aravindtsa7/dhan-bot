# Quant research data lake

Run commands from this directory with the local virtual environment:

```powershell
.\.venv\Scripts\python.exe -u step2_build_data_lake.py
.\.venv\Scripts\python.exe audit_quant_lake.py
.\.venv\Scripts\python.exe step2_build_data_lake.py --summary-only
.\.venv\Scripts\python.exe -m unittest test_step1_health_check test_quant_lake
```

The default interval is **2025-01-01 inclusive to today IST exclusive**, so the
current session is excluded even after market close. `--end 2026-10-23` includes
22 October 2026. Use `--only spot`, `--only options`, or `--only mcx` to resume one
family. Re-running checks file hashes and skips complete partitions. Failed
partitions retry; genuinely empty successful responses are rechecked after 24h.
MCX DH-907 responses are tracked separately as `unavailable`, with the provider
code retained and a 24-hour recheck window; they are not successful downloads.
The same lake must retain its original start date. A separate date universe can
use a separate `--root`.

## Storage and provenance

- `spot_1m/<instrument>/<chunk-start>.parquet`: indices and individually named MCX futures.
- `options_rolling_1m/<underlying>_<CE|PE>_<ATM|ATM+1|ATM-1>_WEEK1/`: nearest weekly rolling series.
- `daily/<MCX contract>/`: available daily bars for each active contract.
- `scrip_master/`: public Dhan master, source checksum, selected active contracts.
- `quarantine/`: out-of-hours or invalid OHLCV rows with rejection reasons.
- `ingestion_manifest.json`: exact request payload, time window, status, checksum,
  file location, row count, actual coverage, and validation counts per partition.
- `summary.csv`, `quality_report.json`, `session_coverage.parquet`,
  `query_benchmark.json`: produced by the audit command.

Parquet is Zstandard-compressed with timezone-aware IST timestamps. Stable
28-day storage partitions can grow at the tail without creating overlapping
files. Options are fetched in seven-day subrequests to avoid gateway timeouts.
Twelve I/O workers share a global rate limiter (at most about 2.86 requests/s).
429/server/network failures receive bounded exponential backoff and jitter.
Only one writer process can own the lake; the OS releases its lock on crashes.
Files and manifest are atomically replaced. An interrupted write can leave an
unreferenced file, which is ignored until the next ingestion verifies/replaces it.
Readers select only committed files from the manifest. Run `refresh()` on an
existing reader after ingestion. For concurrent Windows reader/writer workloads,
retry a failed replacement after readers release their files.

## Query API for the strategy UI

```python
from quant_lake import QuantDataLake

with QuantDataLake() as lake:
    print(lake.instruments())  # (dataset, instrument) tuples
    print(lake.catalog())     # availability, row counts and chunk statuses for the UI
    bars = lake.query('NIFTY', '2025-01-01', '2026-10-23', timeframe='5m')
    calls = lake.query('NIFTY_CE_ATM_WEEK1', '2026-01-01', '2026-02-01',
                       timeframe='3m', dataset='options_rolling_1m')
    lake.query('SENSEX', '2026-10-01', '2026-10-23', csv='sensex_october.csv')
    # MCX instrument names include product, expiry, and security ID.
    # Daily bars require dataset='daily', timeframe='1d'.
```

Date arguments are inclusive start/exclusive end. Naive inputs mean IST.
Queries prune files by date before DuckDB reads them. DuckDB performs OHLCV
aggregation; supported 3/5/15-minute bars align with 09:15 for indices and 09:00
for MCX. No forward-filling occurs across gaps or sessions. Resampled bars expose
`source_rows`, `bar_complete` and, for options, `strike_changes`. OHLC uses
first/max/min/last, volume sums, and OI/IV/spot/strike use the last observation.
A `QuantDataLake` instance owns a DuckDB connection; create one per UI worker.

## Research limitations that must remain visible in the UI

### Step 9: weekly option chain and strike replay

Run `.venv\Scripts\python.exe -m streamlit run quant_ui.py --server.address=127.0.0.1 --server.port=8501`
and open **📊 Option Chain & Strike Replay**. Its controls are independent of the
backtest sidebar. Select Nifty or Sensex, a **weekly expiry date**, and a trading
date/time within its Monday-to-expiry week (January 2025 through October 2026).
Choose **ATM ±10** for a 21-row ladder centered on spot, or **All available
strikes** for every numeric strike observed for that expiry. CE metrics are on
the left, the highlighted ATM strike is in the center, and PE metrics are on
the right. Click any row, then choose CE or PE. The strike dropdown provides
the same selection for keyboard users.

`QuantDataLake.option_expiries(instrument)` discovers available weekly expiry
groups. `option_history(instrument, expiry_date, start, end,
expiry_basis='recorded', strike=None, option_type=None)` queries every matching
strike through DuckDB, pruning committed files by underlying/date. Optional
numeric strike/side filters run before deduplication. It supports
`options_rolling_1m`, `options_contract_1m`, and `options_1m` manifest datasets;
instrument names must start with `NIFTY_` or `SENSEX_`. Fixed-contract partitions
must contain `contract_expiry` and `expiry_flag='WEEK'`; moneyness, expiry code,
OI, IV and spot are optional. Identical observations count once and conflicting
measurements become explicit gaps.

The current lake has rolling WEEK1 metadata without explicit contract dates.
Those rows use `expiry_basis='calendar-derived'` and the dated calendar in
`quant_calendar.py`. This is an **unverified research grouping**, not proof that
the observations belong to the claimed contract. Recorded dates are labeled
separately; inferred and recorded groups never merge, and post-expiry minutes
never leak into an earlier expiry. Source Parquet metadata is not rewritten.

The matrix shows exact-minute close as LTP, recorded OI/IV, and observed session
volume summed through the snapshot. Volume can be partial; the coverage
expander reports observed/expected minutes per strike and side. LTP/OI change
is `(current / prior_close - 1) * 100` for the **same expiry, strike and side**.
Only 15:29/15:30 quotes from the prior regular trading session qualify as close
baselines. Missing, zero or unavailable baselines produce blank percentages.
No previous-day or stale intraday quote is presented as a current quote.

**Full expiry week** shows Monday morning through expiry close, independently
of the table snapshot. **Through selected date/time** limits the chart for
chronological replay. Plotly synchronizes OHLC, volume and OI panels, with dated
IST axes and overnight/weekend breaks. Resampling runs separately per session;
five-minute candles require five observed minutes. Sparse/unfinished candles
remain gaps and OI takes the final observation rather than a sum. Chain/replay
CSV exports include the displayed values; replay also includes strike, side,
expiry basis and source-row/completeness metadata. Cache invalidation follows
manifest changes, expiry-calendar revision, a two-minute TTL or manual refresh.

**Wider data ingestion:** The original lake downloaded only ATM/ATM±1. A wider
ladder cannot supply unrecorded quotes. To backfill actual ATM−10 through ATM+10
data for the existing date universe, run:

```powershell
.venv\Scripts\python.exe -u step2_build_data_lake.py --only options --option-wing 10 --end 2026-10-23
```

This explicitly downloads the missing wings for both indices (42 streams per
index), retains the original lake start date, and reuses complete matching
partitions. It can make thousands of rate-limited API calls. The default wing
remains 1 for compatibility. Dhan documents ATM±10 for near-expiry index options:
https://dhanhq.co/docs/v2/expired-options-data/. The UI automatically includes
new wings after refresh. No broad broker backfill is run just by opening the tab.

Run the query and UI checks with:
`.venv\Scripts\python.exe -m unittest test_quant_weekly_chain test_quant_option_chain test_quant_lake test_quant_paper_ui`.

A successful request proves retrieval, not exchange-calendar completeness.
Exact duplicate records are deduplicated. Conflicting records sharing a timestamp
are all quarantined; the lake does not guess which price is correct. This creates
explicit gaps that must be considered when backtesting.
The audit flags short sessions and reports missing internal minutes. Special
exchange sessions outside configured normal hours are retained in quarantine;
review exchange circulars before promoting these rows. MCX energy/metals close
uses US DST: 23:30 during DST, 23:55 otherwise. Exceptional exchange schedules
still require review. Index hours accept minute stamps from 09:15 through 15:30.

The current master lists active contracts. Each MCX future's earliest available
bar is contract-specific; these files do not form a continuous futures series
back to January 2025. No expired MCX identifiers or roll adjustments are invented.

Rolling options are relative-to-spot selections, not an unchanging security.
`strike` is stored per minute and can change. Expiry mode is explicitly WEEK,
expiry code 1. Do not compute executable strategy P&L by blindly holding this
rolling series across strike/expiry changes. Contract-level execution history,
roll policy, fees and slippage belong in the backtesting layer.

Credentials remain in `.env`, which is ignored by Git. Neither tokens, TOTP,
PINs, nor raw authentication responses are logged. Data and virtual environments
are also ignored. Token fallback uses the existing Step 1 authentication helper.

Primary API references:
- https://dhanhq.co/docs/v2/historical-data/
- https://dhanhq.co/docs/v2/expired-options-data/
- https://dhanhq.co/docs/v2/instruments/
