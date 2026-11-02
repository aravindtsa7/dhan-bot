# TradeMind observed-data research ports

Six independent NIFTY strategies are registered for **backtest research only**.
The existing ten strategies, including Expiry Day and Expiry Intraday, retain
their own rules. Paper account creation, paper launch and the paper CLI reject
all six TradeMind IDs before creating an account directory. No live activation
is provided.

The specifications in [docs/trademind_specs](docs/trademind_specs/overview.md)
define the source authority, indicators, selection, execution, accounting and
state transitions. Frozen constants are not optimization controls. The native
intervals are V2 5m, V4 3m, V8 2m and T3/H30/ORB 1m, calculated from START-labelled
one-minute observations in Asia/Kolkata.

The default Dhan data choice is explicitly **DATA_UNAVAILABLE**: rolling WEEK1
option labels do not establish dated fixed-contract continuity. The separate
retained TradeMind choice reads manifest-bound UPSTOX underlying and GROWW option
Parquet locally. A source runner's `REST(Groww)` underlying label conflicts with
that canonical manifest label; these inputs are never represented as Dhan or as
independently verified market prices. No broker or database is accessed.

Each strategy/year/scenario starts with a fresh ₹100,000. Source-baseline sizing
and requested 100% allocation are separately named, whole-lot, cash-constrained
scenarios. T3 capacity is theoretical maximum loss, **not broker margin**.
The TradeMind API requires `[start, end)` to stay within one calendar year.
`[2025-01-01, 2026-01-01)` is valid because `end` is exclusive. A combined
2025–2026 request is rejected before reading data; callers must run the years
separately with fresh configured capital. Dates are never silently truncated.
Historical executable T3 margin is **NOT_COMPUTABLE**. Fees before 2026-03-01 are
not established by the retained source schedule; full-year net values therefore
remain unavailable. A separate fresh account reports only the supported
2026-03-01 through 2026-09-08 fee window. No unsupported-period gross profits are
carried into that net account.

The observed port retains source OHLC barrier assumptions for V2/V4/V8, but adds
an explicit contiguous-path evidence guard. It freezes account equity after an
entered unresolved path. The source's practice of skipping missing minutes or
omitting unresolved trades from cash is not silently called exact parity. The
legacy fill diagnostic is separately labelled and is never a real-data fallback.
H30 tick feasibility uses bid/ask, latency, quantity and generation checks as a
separate variant; candle fills are not evidence of executable bid/ask fills.

Lifecycle fields are independent: `SPEC_COMPLETE`, `IMPLEMENTED`,
`PARITY_VERIFIED` (with stated scope), `DATA_UNAVAILABLE`, and
`BACKTEST_COMPLETED`. A completed computation may have `UNRESOLVED` financial
status. Missing data does not make a known specification incomplete; missing
frozen source bytes does limit historical-byte provenance and parity claims.

Run the offline suite with `.venv\Scripts\python.exe verify_offline_tests.py`.
Source-executed fixture expectations are in
`tests/fixtures/trademind/source_parity.json`; they were obtained from inspected,
isolated TypeScript copies with filesystem/network access denied. Source runners
and collectors are not executed. The local evidence directory
`data/review_evidence/20261102_trademind_port/` contains complete source hashes,
frozen protocols, resumable matrix manifests, raw leg evidence, discrepancy and
reconciliation reports, logs and preservation checks. Generated evidence stays
ignored and is not part of the source checkpoint.

Independent exchange/broker market matching: **NOT VERIFIED**.

Fresh and rescenarioed results use the same account-summary derivation.
`strategy_unit_status` describes the unchanged unit execution outcomes;
affordability skips belong to the account ledger. An unresolved net account
reports `net_status=UNRESOLVED` and `net_pnl=None`. Full-period cost coverage is
reported separately, and T3 executable broker-margin results remain unavailable.
The versioned summary identity prevents old summaries being reused as corrected
results. The original annual matrix is preserved as historical evidence; its
unchanged file hashes do not establish unchanged behavior under newer code.
