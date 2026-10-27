# Indian Market Research Lake — Dark Terminal

Start the local application:

```powershell
.venv\Scripts\python -m streamlit run quant_ui.py --server.port 8501
```

Open <http://127.0.0.1:8501>. Backtests is the default page. The sidebar also
contains Overview, Data Lake, Data Quality, Strategy Builder, Reports, and Live
Paper Trading. The former Option Chain tab and legacy optimizer controls are
not part of this interface.

## Configure and run

The two-column form sends the selected instrument, strategy, inclusive date
range, capital, allocation, candle interval, take profit, stop loss, minimum
premium, and exit mode to engine 5.2.0. Saved results retain their original
parameters; editing the form does not recalculate an existing run.

| UI strategy | Engine signal | Allowed side |
| --- | --- | --- |
| Buy CE / Buy PE | EMA / Supertrend / VWAP directional | CE or PE |
| Momentum CE | Opening-range option momentum breakout | CE |
| Reversal PE | Bearish VWAP trend pullback | PE |
| Straddle Breakout | Fixed ATM straddle breakout, mode A | First qualifying leg |
| Expiry Gamma | Expiry-day afternoon gamma momentum | CE or PE |
| MCX Breakout | Evening-session range breakout | Long or short futures |

Periods include the last selected day. The engine stores the following date as
its exclusive end. The signal candle intervals are 1, 3, 5, and 15 minutes;
execution and stop management continue to use minute candles.

Minimum premium is the observed option entry price before slippage. An entry
strictly below the threshold is skipped; equality is allowed. Stop-entry orders
use the higher of the observed open and trigger. The filter applies to each
option leg and does not apply to commodity futures.

| Exit | Behavior |
| --- | --- |
| End of day | Protective fixed stop; no profit target or trailing; mandatory session-end exit |
| Trailing SL | Profit target plus a trailing stop at the selected stop-loss percentage; trail updates apply next minute |
| Target / EOD | Fixed stop and target; any remaining position closes at session end |

Stops win ambiguous intrabar stop/target collisions. All modes square off at
15:20 IST for indices or 23:15 IST for MCX, as in the existing engine.

The terminal allocates whole lots and reserves estimated round-trip fees.
MCX stops and targets use the chosen price percentages, and allocation reserves
100% of notional. Insufficient capital produces an empty result, not an
implicitly leveraged position. Legacy programmatic configurations retain the
previous ATR risk and exit behavior through the default `legacy` settings.

## Runs, saved strategies, and reports

Run Backtest writes the existing Parquet ledger/equity artifacts and result
JSON under `data_lake/backtests/runs/<engine-run-id>/`. Successful runs appear in
Recent Runs with a stable `BT-xxx` label. Re-running identical parameters on the
same engine/data snapshot reuses the deterministic result and label.

Save Strategy persists the exact configuration without running it. If the
configuration already has a completed run, its `BT-xxx` label is linked; running
a previously saved configuration updates this link. Strategy Builder can load
the saved inputs. The run and strategy registry is stored atomically at
`data_lake/backtests/terminal/registry.json` under an OS-owned writer lock.
All these local artifacts remain excluded by `.gitignore`.

Existing result folders are imported into the run library when no registry
exists. They remain inspectable across engine upgrades. Recent Runs and Reports
select the original saved configuration, equity, drawdown, ledger, and replay.
Amber diamonds identify modeled execution candles. CSV ledger/run exports and
JSON configuration downloads are available in the report.

## Data provenance

Market cards show the last stored index close and its change from the preceding
stored session close, with an explicit IST timestamp. LIVE LAKE denotes a
populated local lake; these cards are historical snapshots, not live quotes.
An empty lake displays DEMO DATA with unavailable values rather than fabricated
prices. Coverage comes from `QuantDataLake` committed manifest records. Quality
figures and their check time come from `quality_report.json`.

The current lake contains BANKNIFTY spot history but lacks supported BANKNIFTY
option history and a dated contract schedule. The instrument remains visible;
simulation returns a clear availability error. No other index is substituted.
Rolling-option expiry identity remains unverified, and actual missing-candle
and duplicate counts are shown. Research results retain modeled-price caveats.

## Data Lake and Data Quality workspaces

Data Lake groups the local catalog by **exchange, underlying/product, segment,
and stored interval**. Options streams and futures contracts remain individually
selectable. Daily and minute candles are separate datasets. The four summary
cards count readable committed Parquet rows, distinct physical file bytes, and
available logical datasets. Quarantine, orphan files and generated results are
excluded. Counts represent stored stream observations, not unique tradable
contracts or a continuous futures series.

Filter by exchange, instrument or segment, search a stream name, then use
**Selected dataset** and **Stream / contract**. Details show exact stored dates,
session availability, recorded endpoints, interval derivations, storage and
options-identity limitations. The preview defaults to the selected stream's
latest actual session. Preview CSV contains exactly the displayed 100, 500 or
1,000 rows for that single session; it is not an entire-lake export.
**Open Data Quality** carries the dataset selection across pages.

The expandable ingestion panel shows manifest records and exclusive request-end
dates, including failed, empty and unavailable responses. These are not download
jobs, and no pause/resume, import, or download actions are implied.

Data Quality separates file integrity, stored candle validity, historical session
coverage, and options identity. **Run quality check** reads local partitions and
writes an atomic `quality_report.json`; it makes no broker requests, repairs,
fills or downloads. Prior report bytes, including malformed reports, are retained
under `data_lake/audit_history/`. Filter and export findings as CSV or download
the original report JSON. Legacy report evidence remains visible in the technical
expander but cannot establish a current verification.

| Status | Meaning |
| --- | --- |
| No data | No manifest datasets, or no stored candles for the selected group. |
| Not checked | No supported audit bound to this lake; legacy/unversioned reports are unverified. |
| Audit outdated | Manifest, committed-file state or session evidence changed, or ingestion changed the snapshot during the audit. |
| Review needed | Failed/empty/unavailable requests, integrity/validity findings, insufficient coverage evidence, or unverified options identity require review. |
| Checks passed | The documented checks passed within their evidenced scope; a checksum alone never grants this status. |

Reports carry an audit version, canonical manifest fingerprint, file-state
fingerprint and session-calendar fingerprint. Checksums are read during audits;
file size/mtime changes also invalidate the displayed report. An unchanged
size/mtime is not a fresh cryptographic check. Audits share the ingestion writer
lock and recheck the snapshot before publishing. The existing backtest integrity
`issues` field is retained. Legacy `session_coverage.parquet` and benchmark files
are not rewritten or used to claim current UI coverage.

Exact removed input duplicates, rejected/quarantined conflicting rows, and
unresolved conflicts in stored candles are separate findings. Missing measurements
remain unknown. A one-candle minute session never passes coverage. Without a
verified session schedule, observed minute-spacing holes are described only as
spacing measurements, not exact counts of missing exchange candles.

Expected opening/closing gaps and entirely absent sessions require an optional
local `data_lake/session_calendar.json`. Its schema is documented in
[README_DATA_LAKE.md](README_DATA_LAKE.md#session-evidence-for-quality-audits).
The existing expiry calendar does **not** establish a complete exchange-session
calendar. The current lake has no such session evidence, so historical coverage
remains unverified. Rolling ATM-relative options retain unverified contract/expiry
identity even when their file and candle checks pass.

### Offline checks

```powershell
# Self-contained fixtures; no local market data required
.venv\Scripts\python.exe verify_offline_tests.py test_quant_data_pages
# Full regression suite; includes tests requiring the existing local lake
.venv\Scripts\python.exe verify_offline_tests.py
```

The runner blocks external socket connections while allowing Windows asyncio's
loopback sockets. The existing `test_quant_lake.LakeTests.test_live_lake_query_intraday_bound`
and `test_quant_option_chain.LocalLakeOptionTests` require actual local NIFTY/SENSEX
data; they are not replaced with fixtures or skipped. The full paper-page smoke
test also reads existing local paper artifacts without starting a trader.

### Backtest checks

```powershell
.venv\Scripts\python -m unittest discover -p "test_*.py"
.venv\Scripts\python verify_step5_ui.py
```

The regression suite covers threshold boundaries, pre-slippage filtering,
option sides, percentage futures risk, all exit policies, allocation,
configuration persistence, empty/error states, all seven pages, and report
selection. The UI smoke check additionally executes bounded NIFTY and CRUDEOIL
backtests against the local lake in a temporary output directory.
