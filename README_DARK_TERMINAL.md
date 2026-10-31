# Indian Market Research Lake — Dark Terminal

```powershell
.venv\Scripts\python.exe -m streamlit run quant_ui.py --server.port 8501
```

Open <http://127.0.0.1:8501>. The six pages are Overview, Data Lake,
Data Quality, Backtests (default), Reports, and Live Paper Trading. Strategy
selection and tuning live directly on Backtests. The compact ticker shows the
last stored index close/change, with its historical timestamp in the tooltip;
DhanHQ / Parquet / IST pills identify the infrastructure, not a live quote feed.
An empty lake shows unavailable values. No demo prices or strategies are supplied.

## Configure, simulate, save

**▶ Run Backtest** simulates into Streamlit session memory (`persist=False`). It
immediately renders the complete dashboard, and writes no run files or Reports
history. Editing the form does not change an already computed result. Closing
the session loses an unsaved result.

**💾 Save Result**, below the configuration, writes the existing simulation's
Parquet tables and result JSON under `data_lake/backtests/runs/<run-id>/` and adds
an explicit-save entry to the version-two terminal registry. Its optional note
appears in Reports. Identical engine/config/data runs reuse their deterministic
ID, so repeated saves do not duplicate history. The library ignores version-one
auto-save registries and never scans/imports old result folders. Existing files
remain on disk; demo/legacy runs do not populate the default interface.

Enter a custom **Strategy Name**, then **📌 Save Strategy**, to persist the exact
engine configuration plus the requested public parameter names into
`data/saved_strategies.json`. A name is mandatory. Saving the same name updates
that named configuration; different names can share identical logic. This action
does not simulate or save a result. Saved configurations can be loaded beneath
the Backtests form. Current user-named records are the sole source of the Live
Paper Trading selector.

Periods include the selected last date; engine dates use an exclusive end.
Signals use complete 1m / 3m / 5m / 15m candles and enter at the next minute open.
Initial instrument defaults are 25% allocation, ₹100,000 for index options and
₹2,000,000 / ₹4,000,000 / ₹8,000,000 for NATURALGAS / CRUDEOIL / GOLDM; options use 15% stop / 25% target and ₹50 minimum premium,
MCX uses 5% stop / 5% target. The conservative allocation cap reserves fees and
whole lots. It limits committed capital, not potential loss. These are transparent
research assumptions, not optimized claims of profitability.

| Strategy key | Close-confirmed entry rules |
| --- | --- |
| `vwap_ema_pullback` | Previous candle touches EMA9; close clears its high/low, EMA9/21 alignment, price above/below session VWAP, ADX14 ≥18. 09:25–14:30 IST on indices and MCX. |
| `orb_volume_breakout` | Index 09:15–09:30 / MCX 17:00–17:30 range, >1.3× ten prior complete bars' volume, RSI14 55–78 bullish / 22–45 bearish. Maximum two completed entries per session. |
| `supertrend_adx_trend` | True Wilder ATR10 Supertrend ×3 flip or continuation beyond previous high/low, ADX14 >22; supports Trailing SL. |
| `mcx_evening_breakout` | NATURALGAS / CRUDEOIL only, 17:00–22:45 IST, preceding 12-bar Donchian high/low, EMA50, candle body >1.2× Wilder ATR14. |
| `book_vertical_spread` | ADX14 ≥18, close/VWAP and EMA9/21 alignment: buy ATM call and sell upper call, or buy ATM put and sell lower put. MCX directional ATR-capped futures. |
| `book_iron_butterfly` | After 09:45, ADX14 <22 and close within 0.25% of VWAP: short ATM CE/PE with long equidistant wings. Credit decay/expansion exits; 15:15 square-off. Index options only. |
| `book_strap_strip` | ORB or preceding 10-bar Donchian break and fixed-contract volume ≥1.3× preceding 10 bars: long 2CE:1PE strap or 1CE:2PE strip. Index options only. |
| `book_vol_oi_momentum` | Mean tanh(log return / lagged 10-bar return volatility), absolute signal ≥0.25; log volume expansion and positive log OI activity where OI is available. Options or MCX futures. |

All eight appear in the selector and support explicit Save Result and custom-name
Save Strategy, including virtual paper deployment. See
[book models and verification](README_BOOK_STRATEGIES.md) for basket accounting,
pricing provenance, and the reproducible local-lake matrix.

The first three support NIFTY, SENSEX, CRUDEOIL, NATURALGAS and GOLDM. Options buy
ATM CE / PE; futures go long / short. BANKNIFTY remains spot-only and returns an
availability error for simulation. Historical strategy APIs remain available
for existing research callers and regression tests, but are absent from the UI.

Wilder indicators carry causal prior-session warm-up within the same underlying /
selected MCX contract; session VWAP resets daily. Morning MCX bars are retained.
Zero-volume index spot cannot supply true VWAP or a volume surge: VWAP is explicitly
labeled as a TWAP proxy, and ORB instead requires eleven complete **fixed-strike
option** bars (signal plus ten prior bars). Missing history rejects a signal;
rolling strikes are never summed as if they were one contract. Rolling option
expiry identity remains unverified. Missing execution candles follow the existing
strict-observed or disclosed continuity-model policy. If neither can price a
position, the entire run fails with the date/time/side/strike; no partial return
is presented as a complete result.

For a fixed strike outside the currently observed ATM±1 band, modeled-price
policy first uses a deterministic intrinsic + bounded wing time-value estimate
without requiring lagged IV. Exact observed quotes always win. Affected trades
carry `modeled_wing_fallback=True`; strict-observed policy still rejects this
estimate. Eligibility and repair rules remain those of Engine 5.4.0.

| Exit | Behavior |
| --- | --- |
| End of day | Fixed protective stop, no target or trail, mandatory square-off |
| Trailing SL | Target plus trailing stop; complete jumps apply on the next minute |
| Target / EOD | Fixed protective stop and target, then mandatory square-off |

Indices square off at 15:20 IST; MCX at 23:15 IST. Stops win ambiguous intrabar
collisions. Gaps, adverse tick rounding, slippage, brokerage and taxes remain in
the existing engine. MCX allocation reserves 100% of notional; no implicit margin
leverage is introduced. Slippage is ₹0.50 per unit per fill for indices / NATURALGAS and ₹1.00 for CRUDEOIL / GOLDM. Fees and dated lot sizes are research assumptions.

## Results and paper deployment

Eight summary cards show net return, win rate/count, profit factor, daily realized
drawdown, CE/long and PE/short results, average win/loss, and total costs. The trade
selector defaults to the highest-profit trade. Its two-panel Plotly chart shows
the underlying session with VWAP/EMA/Supertrend/ORB overlays and the fixed execution
contract on its own price scale. Entry/exit markers use exact fill coordinates;
entry, target and stop levels span only the holding interval. Amber diamonds flag
modeled candles. Equity/drawdown, monthly P&L, full ledger and CSV export follow.
Ledger gross P&L is before slippage; costs include fees + slippage, so gross minus
costs equals net. Return uses allocated capital, and balance uses realized exits.
Eligibility, rejected entries and repair/model disclosures remain inspectable.

On Live Paper Trading, select a named strategy and click **▶ Deploy to Paper**.
This explicitly launches `step6_paper_trader.py --saved-strategy <id>` using the
exact saved policy (only the session dates change). Each strategy/policy has an
isolated account folder and OS lock. Current master records resolve actual dated
ATM CE/PE contracts and lot sizes; an entered option never rolls. All execution
is virtual, and the transport has no broker order/cancel routes. The read-only
feed requires the existing Dhan credentials. Index ORB candidates need observed
fixed-strike option volume confirmation before entry. New strategies retain
prior-session warm-up; protective exits respect the saved percentage/exit policy.
The Stop button requests a clean virtual close on a fresh quote. Logs and account
health appear on the page. No runner starts merely by selecting or saving.

## Research references and offline verification

Indicator definitions follow [TradingView Supertrend](https://www.tradingview.com/support/solutions/43000634738-supertrend/)
and [Wilder DMI/ADX](https://www.tradingview.com/support/solutions/43000502250-directional-movement-dmi/).
The entry thresholds/combinations above are the requested research hypotheses;
these references do not establish positive expectancy. Contract resolution uses
[Dhan instrument fields](https://dhanhq.co/docs/v2/instruments/) and
[exchange segments](https://dhanhq.co/docs/v2/annexure/).

```powershell
.venv\Scripts\python.exe verify_offline_tests.py
.venv\Scripts\python.exe verify_strategy_workflow.py --output data/strategy_verification_defaults
```

The second command blocks all sockets, loads the actual lake once per product,
and compares all applicable strategies at 5m/15m with the defaults and one risk
sensitivity setting. Outputs include summary CSV, original configurations,
metrics, fingerprints, eligibility and explicit execution failures. It never
adds runs to Reports. Results include all entered trades with full costs;
unsupported index/MCX-only combinations are marked not applicable. These are
in-sample comparisons, not untouched out-of-sample validation or a promise of
profitability. Check modeled-price and repaired-input disclosures before using
any number.

## Data Lake and Data Quality workspaces

### Check a range, then sync or backfill

Data Lake opens **Data Lab — Historical 1m Inventory & Backfill Engine**.
Eight inventory rows separate index spot, rolling options and commodity futures.
The four cards count actual populated instruments, committed 1m rows and storage;
daily candles, raw quarantine and original repair backups are excluded. Use
Row health is measured automatically on load and after local audits, with a
persisted `data_lake/inventory_health.json` cache bound to manifest, audit,
file/calendar/quarantine evidence and metric/engine versions. **Refresh cached
health** is optional. SPOT and OPTIONS rows have independent observed-date
denominators and show session percentage plus unique valid required minutes /
expected minutes. BANKNIFTY receives a real spot-only completeness check; it
still has no supported options backtester. Missing/stale evidence shows an
actionable verification message rather than a guessed percentage.

Index windows are 09:15–15:20 inclusive (366 minutes); MCX is 09:00–23:15
inclusive (856 minutes). The options row requires the six engine streams;
additional wings remain inspectable. MCX counts each product/date once using
the preceding observed session's contract selection. Entirely absent historical
days require separate calendar evidence. Percentages are rounded; counts are
exact. A nonzero missing-bar count is never rounded up to a 100% claim.

**Sync All to Latest**, or a row's **Sync**, appends missing tails. **Custom
Backfill**, or a row's **Backfill**, opens explicit date-range controls. A row's
**Repair** or **Repair All Gaps** attempts provider retries when credentials are
configured, then applies conservative, labeled repairs. Actions make changes only
on a click. The expandable **Inspect Date-Range & Candle Preview** drawer retains
filters, exact stream selection, monthly eligibility and bounded candle exports.

Data Quality focuses on the **Audit Inspector**, actionable findings, historical
evidence and **Quarantine / Repair Log**. It has no second inventory table.
Its optional date-range inspector retains the Data Lake selection. The repair
log exports timestamp, instrument, method, original checksum and recorded time.
The summary strip separates file integrity, combined instrument/date readiness,
restored/filled candles and unresolved required inputs. Validity passes when
required candles are valid and localized rejected inputs are resolved or outside
the window; repaired streams are labeled. Coverage percentages describe observed
required-window checks, while the historical-calendar column retains unverified
holiday/coverage evidence. Original findings and expiry disclosures remain visible.
See [repair policies](README_DATA_LAKE.md#buffered-sync-and-transparent-repair)
before interpreting a repaired session as usable.

Both pages share **Check Date-Range Coverage**: select a product or recorded
stream, choose inclusive From/To dates, and click **Check coverage**. The
inspector calls the engine 5.4.0 loader one month at a time, including
MCX prior-session context. It shows stored underlying sessions, exact eligible
and excluded sessions, monthly totals, and a CSV of date/stream/timestamp reasons.
Eligibility always means the complete product's engine bundle, not that a single
option stream or daily source can substitute for every intraday dependency.
Missing/invalid audits show **Not verified**, never zero gaps. Entirely absent
regular index sessions are shown separately using the existing engine calendar.
MCX whole-session absence and exact trading-day freshness require sourced session
and contract-applicability evidence; unknown calendars remain explicitly unknown.

**Sync Latest Data / Add Historical Data** offers explicit download buttons.
Planning and page navigation are offline. Latest sync resumes each recorded
stream after its last stored date, up to a completed-session boundary in IST
(with a five-minute publication buffer). Index boundaries use the existing
regular-session calendar; special sessions are not newly enabled. MCX uses sourced
local schedules when present, otherwise a clearly labeled completed-weekday
candidate using the ingestion pipeline's seasonal close. It does not invent a
commodity holiday calendar or an exact missing-trading-day count.

Choose a target group and inclusive historical dates to backfill. Fast incremental
mode skips checksum-valid completed partitions. It does not fill gaps *inside*
an existing partition: disable **Skip already cached chunks** to explicitly
re-fetch those dates. An overlapping refresh downloads the original partition's
full bounds, shown in the request preview, to preserve neighboring stored dates.

Downloads use the existing Dhan ingestion API/normalization and its token-expiry
fallback from the local `.env`; no credentials are displayed. Public current MCX
contract metadata is looked up only during an explicit download action. Current
masters cannot recover unknown expired contract IDs, and providers may not offer
expired-contract history. Neither BANKNIFTY options nor MCX options are invented.

Successful downloads are staged, checksum-checked, promoted under the ingestion
writer lock, then audited before the lock is released. Failed or empty forced
replacements retain existing complete data. File/manifest promotion failures roll
back; previous manifests are retained under `data_lake/ingestion_history/`.
Nonempty replacements must also retain every previously stored timestamp;
truncated responses are rejected. Network/authentication/subscription failures
stop remaining requests after the existing pipeline's retry budget, rather than
repeating a failing batch indefinitely.
Partial successes and failures are shown separately. If auditing fails, the UI
does not claim readiness: run a local quality check before backtesting. Sync never
places orders or starts traders. The summary counts **instrument-sessions** once
per product/date, with a separate **stream-sessions** diagnostic. Neither count
is a number of portfolio trades or a proof that newly stored dates are eligible.

Data Quality separates **Actionable Backtest Gaps & Conflicts** from **Handled
Ingestion Notes & Provider Limits**. Unlocalized historical findings stay in a
separate investigation expander; an aggregate rejection or spacing count is not
promoted to a confirmed run exclusion. Removed duplicates/out-of-hours input and
calendar/identity/provider disclosures do not by themselves invalidate a session
that passed the engine. Empty MCX requests are not called proven pre-listing
periods without applicability evidence. Canonical audit statuses remain intact;
**Backtest Usability** shows results for the checked date range.

Offline workflow regressions:

```powershell
.venv\Scripts\python.exe verify_offline_tests.py test_quant_data_workflows
```

Tests mock downloads and public-master discovery on temporary lakes. They do not
use live credentials or alter the installed historical lake.

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

Findings, the finding inspector and filtered CSV exports identify the recorded
**Stream / contract** as well as its logical dataset. Missing identifiers are
shown as **Lake-level** or **Not recorded**; unresolvable recorded IDs remain
visible rather than being guessed. **Count** and **Unit** distinguish input rows,
stored candles, sessions, unverified dates and observed minute-spacing holes.
Unknown counts remain **Not measured**, and unknown units **Not recorded**.

The backtest loader uses the shared audit validation. A missing, malformed,
unsupported or outdated audit blocks loading with an actionable local-check
instruction. Current coverage/identity warnings remain distinct from an invalid
audit and do not alone prove or prevent eligibility for a particular run.

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
configuration persistence, empty/error states, all six pages, and report
selection. The UI smoke check additionally executes bounded NIFTY and CRUDEOIL
backtests against the local lake in a temporary output directory.
