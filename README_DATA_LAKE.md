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
- `quality_report.json`: current versioned, manifest-bound local audit.
- `audit_history/`: preserved previous audit report bytes.
- `summary.csv`, `session_coverage.parquet`, `query_benchmark.json`: legacy
  artifacts; the current UI does not treat these as current audit evidence.

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

The current seven-page terminal uses **Data Lake** and **Data Quality** for
catalog inspection and local audits. See [README_DARK_TERMINAL.md](README_DARK_TERMINAL.md)
for workflow, export bounds and status definitions. The option-chain module
described below remains a separate API/module; it is no longer a navigation tab.

## Session evidence for quality audits

`session_calendar.json` is optional local evidence, not a downloader. No schedule
is inferred from the expiry calendar, weekdays, candle count, or the first observed
commodity candle. Unsupported dates remain **Coverage not verified**. A sourced
date-specific schedule can represent closures (`windows: []`), special sessions,
breaks, and MCX seasonal closing changes. Each window is opening-inclusive and
closing-exclusive, at one-minute frequency. Windows must be ordered, disjoint,
and within the dated session. Daily data expects one candle on an evidenced open
session; it never receives a 375-minute expectation.

Schema illustration only — these are **not real exchange schedule facts**:

```json
{
  "sessions": [
    {"exchange": "NSE", "date": "2026-01-05", "windows": [["09:15", "15:30"]], "source": "Replace with reviewed exchange notice"},
    {"exchange": "NSE", "date": "2026-01-06", "windows": [], "source": "Replace with reviewed closure notice"}
  ],
  "contracts": {
    "EXACT_COMMODITY_STREAM_NAME": {
      "start": "2026-01-05", "end_exclusive": "2026-02-01",
      "source": "Replace with reviewed contract listing/applicability evidence"
    }
  }
}
```

Only the union of a stream's requested manifest periods is evaluated. Commodity
futures additionally require exact-stream applicability bounds and provenance;
dates outside those bounds are not counted as missing sessions. The source field
records the local evidence supplied by the operator; the audit does not fetch or
authenticate external notices. Review the evidence before treating counts as
exchange-complete. Options identity remains a separate unverified field.

Run `.venv\Scripts\python.exe audit_quant_lake.py --root data_lake` to check local
files only. The command exits nonzero for findings requiring review; a successful
audit execution can correctly produce **Review needed**. It serializes with
ingestion, hashes committed files, checks candles/duplicates and preserves prior
reports. It never rewrites historical partitions, session-coverage artifacts,
strategy settings or credentials.

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

## Buffered sync and transparent repair

Open **Data Lake** in `quant_ui.py` for the instrument inventory, per-row Sync,
Backfill and Repair actions, plus global actions. Row health is automatically
measured and cached against current manifest/audit/file/calendar/quarantine
fingerprints; **Refresh cached health** is optional. Spot and options use their
own stored-date denominators; BANKNIFTY receives spot-only checks. Session and
required-bar percentages include exact counts. The inspection drawer checks arbitrary
inclusive dates and previews/exports a bounded stream/session. Data Quality is
the audit and quarantine/repair inspector. Downloads only begin on explicit
action buttons; opening a page never calls the broker.

Intraday chart calls use at least seven calendar days, expanding backwards from
the exact requested exclusive end. Rolling requests are split into seven stored
days (at most eight queried days); other intraday requests remain under 90 days.
Responses are filtered to the exact stored interval **before** normalization or
writing. This avoids narrow-window requests associated with DH-905; Dhan does
not document a universal seven-day minimum, so it is a defensive workaround,
not proof of the provider error's cause. Rolling `toDate` is non-inclusive per
the official API documentation. No out-of-range response rows are committed.

Forward MCX sync refreshes the current instrument master and selects the nearest
two unexpired recorded/discovered expiries per product. No request extends past
expiry; custom historical requests are clipped to recorded expiry. Historical
availability before listing is not invented. A failed stream is isolated from
healthy streams; authentication/subscription/connectivity errors still stop the
batch. Existing complete partitions survive failed, empty or lossy replacements.

Segment/subscription/access errors are isolated to their stream so healthy index
spot, rolling options and active MCX requests continue. Authentication/connectivity
failures stop further requests explicitly. Each run saves a sanitized per-request
summary under `data_lake/sync_history/`, including unattempted streams; partial
completion never means every dataset synced. Latest sync uses actual Parquet
tail timestamps: a partial response cannot protect the unstored remainder of its
requested partition. A retry preserves the original partition bounds and old
timestamps. Audit refresh rebuilds row health automatically; failed downloads
cannot produce a fabricated fresh coverage percentage.

**Repair** is an explicit correction operation, separate from a read-only audit:

- A single quarantined row rejected only for negative volume can be restored
  when OHLC is finite, positive and consistent and optional numeric fields are
  valid. Its volume becomes zero; original volume and `candle_source` remain
  recorded. New ingestion applies the same normalization and keeps raw quarantine.
- Dense index sessions (at least 370 valid candles within 09:15–15:29) can receive
  1–3 consecutive missing **interior** minutes. Provider retry comes first when
  credentials are configured. Offline fallback uses the immediately preceding
  close for all four prices and zero volume, labeled `synthetic_ffill`.
- No opening/closing extensions, sparse/short sessions, four-minute-or-longer
  runs, invalid prices, conflicting quarantine timestamps, or rolling-strike
  changes are filled. MCX holes are not synthesized without applicable session
  evidence. Multiple short holes can exist in one dense session.
- Raw quarantine stays unchanged. Original files and records are retained in
  `data_lake/repair_history/<original-sha256>/`; the old manifest is archived in
  `ingestion_history`. Promotion rolls back if the manifest commit fails. The
  shared writer lock excludes concurrent ingestion and audit writers. A fresh
  local audit is written after repair; an audit failure leaves readiness unverified.

Synthetic bars can change signals, volume indicators and execution prices. They
are **not recovered market observations**. Engine 5.4.0 records repaired inputs
in eligibility, results and report exports; synthetic execution bars count as
modeled prices. Strict-observed runs refuse synthetic dependency inputs. Old
engine results remain inspectable and are not reused by current caches.

Full observed special index sessions undergo ordinary required-window checks.
Budget-session evidence: [NSE 1 February 2025](https://nsearchives.nseindia.com/content/circulars/CMTR65729.pdf)
and [NSE 1 February 2026](https://nsearchives.nseindia.com/content/circulars/FAOP72352.pdf).
Other dense non-weekday sessions are explicitly labeled observed/calendar-
unverified; this does not create a holiday calendar or admit short Muhurat
sessions. Options expiry identity remains unverified. A current integrity audit
or zero required-window gaps never proves complete historical exchange coverage.

Offline validation (the full suite includes these focused tests):

```powershell
.venv\Scripts\python.exe verify_offline_tests.py test_quant_repairs
.venv\Scripts\python.exe verify_offline_tests.py
.venv\Scripts\python.exe -m streamlit run quant_ui.py --server.address 127.0.0.1 --server.port 8501
```

Inventory health is independent for each of the eight displayed dataset rows.
Spot uses only that index's stored dates and 09:15–15:20 candles. Rolling options
uses its own observed dates and six required CE/PE streams, without inheriting
spot-only tail dates. BANKNIFTY has ordinary spot completeness checks. The health
pill shows `XX.X% Clean`, with ready days and gap days below; Coverage shows the
valid/expected one-minute bar percentage. The global percentage is an equal-weight
average of populated rows, and `X/8 datasets at latest` compares their tails with
the latest stored date. It does not certify freshness against an exchange calendar.

MCX **inventory** cleanliness measures internal continuity between each observed
contract/session's first and last minute. Partial listing days are not penalized
for an inferred earlier open. All observed contracts must pass for their observed
date to be clean; one candle is insufficient evidence. This measurement cannot
establish missing opening/closing hours or backtest readiness. The date-range
inspector, Data Quality engine summary and backtester retain full engine dependency
windows, warm-up, prior-session selection and integrity checks. Inventory cache
version 2.0 invalidates the previous interpretation; engine results are unchanged.

Rolling sync uses integer interval `1` and preserves the selected weekly expiry,
offset and CALL/PUT identity. For 23–29 October, storage bounds are 23 October to
30 October exclusive; the buffered provider request starts 22 October. Failed
sync results now preserve attempted query bounds, safe rolling parameters and
the provider's error description after credential/URL redaction in `sync_history`.
An empty recent expired-options response is not evidence of successful catch-up;
old partitions remain stored and their tail stays unchanged. No alternative
expiry or fabricated recent candles are silently substituted.
The most recent saved sync outcomes (or explicitly labeled read-only provider
check) are shown after a UI restart, including sanitized provider descriptions.
A provider check is not an ingestion job and never establishes successful sync.
