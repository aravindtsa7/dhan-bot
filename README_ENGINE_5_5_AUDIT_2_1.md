# Engine 5.5.0 / Audit 2.1 checkpoint

This checkpoint records the confirmed validation and persistence fixes, including
the Paper and Backtest UI integration. Earlier audit tables and saved results
remain historical evidence; they are not corrected Engine 5.5.0 results.

## Corrected behavior

- Underlying OHLC must be finite and strictly positive during ingestion, audit
  and required-window eligibility. Option premiums may be zero in stored
  candles, but cannot be negative; their underlying references and strikes must
  be positive. Validation does not alter stored candles.
- Relevant raw vertical/butterfly observations are validated at entry, during
  management, at exit and during observed continuity calibration. Invalid
  observed quotes are not projected in either pricing policy. Invalid evidence
  while open invalidates the simulation, retaining raw legs and undefined whole
  run P&L rather than substituting a favorable trade. Structural raw bounds do
  not constrain adverse execution prices or fees. Contract identity remains
  explicitly uncertain for rolling options.
- Iron Butterfly explicit `end_of_day` suppresses ordinary target exits while
  retaining protective stops, in backtest and paper logic. The separate Expiry
  Day behavior and strategy thresholds are preserved.
- Public recursive indicators use canonical contiguous eligible same-contract
  history, clipped to validated dependency windows: index 09:15–15:20 and futures
  09:00–23:15 IST, inclusive. Missing/excluded predecessor sessions or contract
  changes reset initialization; insufficient history is not filled. Requested
  dates constrain executions. Legacy daily features and Expiry Day dependencies
  remain separate. Account histories can still change affordability.
- Data promotion/rollback and joint paper state/ledger publication retain durable
  journals and immutable recovery copies. Nonterminal publications block guarded
  readers; restart recovery runs under writer/account locks. Primary and secondary
  failures remain evidence. Bounded Windows replacement retries supplement this
  protocol; they do not provide atomicity.
- Paper UI reads verify terminal generation hashes and recheck state, ledger,
  journal and invalidation evidence across the read. Interrupted, malformed or
  changing publications expose no current P&L/win rate or mixed ledger download.
  UI reads never recover or mutate account files. Invalidation intents freeze
  valuation and exports while preserving open liabilities. Intentional legacy
  accounts remain explicitly unverified views.
- A new backtest attempt clears the previous session result before configuration
  validation/execution. Rejected or invalidated attempts cannot leave old charts,
  save actions or success notices presented as the new result. Saved historical
  reports remain deliberately accessible.

## Identities and report snapshot

Engine identity is `5.5.0`; non-Expiry-Day pricing identity is
`validated-window-baskets-v8`. Expiry Day retains `stateful-execution-v5` within
the updated engine/data identity. Audit, inventory-health and required-window
versions are `2.1`, `2.1` and `1.3`; canonical initialization policy is
`canonical-validated-segment-v2`. Earlier cached results cannot be reused as
corrected results under these identities.

The intentionally tracked [quality report](data_lake/quality_report.json) was
regenerated locally on 2026-11-01 at 20:31 IST using existing data only. It covers
60 streams with a stable snapshot, zero hard integrity issues and 711 retained
findings. Overall status remains **Review needed**. Its SHA-256 is
`47ff84918c9c00370cc4283511da4b614f0b075cde21b1ac12b61b14f09ba417`.
The original report was archived locally before replacement. Historical candle
partitions and the ingestion manifest were not modified. The actual loader and
Data Quality UI read the refreshed report successfully.

The report is a fingerprint-bound local snapshot, not a substitute for the
excluded lake. A different local manifest, partition state or calendar requires
its own local audit. Only this explicit report is tracked under `data_lake/`;
partitions, manifest, caches, locks, archives and review evidence remain local.

## Validation and limits

The verified source passed 450 offline test methods, including 16 Paper UI and
backtest failure-path regressions. The relevant paper/publication/UI/Expiry
batch passed 153 methods. External network attempts were zero. Subtests are not
counted as additional methods. Reproduce the full check from the repository root:

```powershell
.venv\Scripts\python.exe verify_offline_tests.py
```

Synthetic fixtures cover interrupted writes, denied replacements, restart
recovery, malformed journals, concurrent generation changes, frozen accounts,
legacy viewing and failed backtest presentation. The actual lake and existing
paper accounts were not repaired or migrated. Local detailed review evidence is
excluded from Git along with credentials, logs, caches and generated runs.

Independent market matching remains **NOT VERIFIED**. Hash/generation checks
establish local consistency, not market truth or executable fills. Dated option
identity, calendar applicability, independent full indicator/trailing/model
reconstruction and Windows hardware power-loss behavior remain verification
gaps. Raw filesystem readers must honor publication journals; recovery backups
currently have no automatic pruning.
