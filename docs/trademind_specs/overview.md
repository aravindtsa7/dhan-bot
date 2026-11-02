# Source authority and shared contracts

This specification applies to the current inspected TradeMind working-tree
bytes, not to an assumed clean Git revision. Discovery included committed,
modified and untracked files in `C:/TSA/trademind-calendar`,
`C:/TSA/trademind` and `C:/TSA/research-lake`, read-only. Environment/credential
files were excluded. No database, collector, broker or download was used.

Calendar HEAD was `dbbe347c3b05773749c90f92a7496fc0f85ad8e3` and sibling
TradeMind HEAD `a1001e31b4be0adb272ad804fbfb5ea38801e9ee`. The research-lake
root was not a Git repository. Full SHA-256 source, dependency, sibling and HEAD
byte identities are in [source_identity.md](source_identity.md) and the local
`source_identity.json`, `protocol_source_bindings.json`, `tsa_source_before.json`.
Rule-to-source-to-Python mapping is in [mapping.md](mapping.md).

`VERIFIED` means checked against the stated local bytes/output, never exchange
verification. `UNKNOWN` means evidence absent. `CONFLICTING` means evidence
disagrees and both are retained. Frozen V2/V4/V8 16-character fingerprints bind
the sorted, compact JSON strategy parameter object with SHA-256 truncated to
16 hexadecimal characters. They do **not** bind every imported source file.
The parameter preimages are in the parity fixture and source capital protocol;
all three recompute exactly. Full original run source-byte provenance is
UNKNOWN where the frozen artifact does not retain code hashes. Current calendar
and sibling differences must not be silently combined. H30/ORB/T3 protocols use
the source canonical JSON serializer and full SHA-256; protocol bindings retain
the physical code hashes. JavaScript exponent formatting matters (`0.000001`
versus `1e-6`); Python default JSON is not a general proof of Node hash equality.

## B. Shared observed input contract

All six use NIFTY, dated options and IST START-labelled one-minute candles. A
candle labelled 09:29 contains the minute ending 09:30. A completed 5m bucket
09:15..09:19 has decision time 09:20. Complete buckets anchor to 09:15; partial
2m end-of-day buckets are discarded. Only information at/before each decision
selects the contract. No future minute fills a missing historical observation.

Retained underlying certificates establish 249 sessions in 2025 and 170 in
2026, through September 8. The source study calendar has six additional 2026
dates, retained as unavailable, including September 9 with no canonical
underlying partition. Dates beyond certification are not invented as exchange
sessions. The October 21, 2025 special session is 13:45..14:44; it cannot supply
regular morning windows. T3 rejects nonregular sessions. Options are GROWW
dated contract IDs; underlying canonical manifests say UPSTOX. Some frozen study
runners label their underlying `REST(Groww)`: provider identity is CONFLICTING,
even when numeric prices agree. These comparisons are provider-specific.

The adapter verifies physical partition SHA, manifest/storage content linkage,
timezone/date/whole-minute uniqueness, positive finite OHLC and ordering.
Underlying certification gaps split eligibility and indicator history. Repaired
composite **observed** sources remain labelled; synthetic/imputed/forward-filled
inputs are rejected. Invalid option rows are preserved and checked where the
strategy consumes them; an irrelevant future bad row cannot reject an earlier
closed trade. Optional invalid volume is retained, not repaired; volume is not
an entry predicate in these six strategies. No library fill, modeled price,
intrinsic-wing fallback or basket projection is available.

Expiry authority is the retained GROWW multi-expiry ownership plan, not guessed
weekday expiry. Ambiguous ownership rejects the date. Contract catalog evidence
must precede selection: a completed intraday candle or prior accepted session
inside that owning expiry lifecycle. IDs remain fixed throughout each trade.
Exact ATM uses integer paise: nearest 50, exact halfway rounded **down**. V2/V4/V8
instead rank PIT-eligible contracts by absolute strike distance to spot, then
lower strike and lexical ID, as their source selector does. This eligibility
ranking is disclosed; other strategies cannot substitute another strike.

## C. Shared indicator equations for V2/V4/V8

No external indicator defaults are used. EMA(p) seeds with the arithmetic mean
of the first p closes, then E[t]=E[t-1]+2/(p+1)*(C[t]-E[t-1]). Periods 15 and 35.
TR=max(H-L,abs(H-prevC),abs(L-prevC)); first TR=H-L. ATR14 seeds mean(first 14TR),
then (13*ATR+TR)/14. RSI14 seeds the first 14 close differences' mean positive
gains/absolute losses, then Wilder averages. RSI=100-100/(1+gain/loss); zero loss
gives100 if gain>0, otherwise50; zero gain/nonzero loss gives0.

For ADX14, +DM is positive high change if greater than positive low decline,
otherwise0; -DM is the analogous strictly greater low decline. First DM=0.
The source seeds smoothed TR/+DM/-DM as **sums** of 14, then updates each as
(13*previous+current)/14 (an unusual sum-seed/average recurrence deliberately
preserved). DI=100*smoothedDM/smoothedTR, or0 for zeroTR. DX=100*abs(+DI--DI)/
(+DI+-DI), or0 for zero denominator. ADX seeds mean(first 14DX), then Wilder14.
Values remain unrounded binary floats. Null/not-ready indicators block signals;
nonfinite/invalid underlying rejects the segment. Zero volume is valid and does
not change these price indicators. Warm-up/history wiring differs per strategy
as specified below; a short generic rolling preload is not substituted.

Regime: not-ready if any required indicator is absent/nonfinite; SIDEWAYS if
ADX<20 or abs(EMA15-EMA35)/close*100<=0.05. Otherwise TREND_DOWN requires
EMA15<EMA35 and RSI<50; TREND_UP requires EMA15>EMA35 and RSI>50; else SIDEWAYS.
Volatility regime constants do not add an entry filter to the frozen strategies.

## F. Shared Phase-B candle policy (V2/V4/V8 only)

`HISTORICAL_OHLC_FILL_V1` buys at the OPEN of the first observed candle strictly
after the decision timestamp. The observed port requires exactly decision+1m.
Deadline=min(decision+15m,15:30). During entry..deadline-1: OPEN<=stop exits OPEN
as STOP_GAP; OPEN>=target exits at target as TARGET_GAP (favorable gap capped).
If both LOW<=stop and HIGH>=target in a candle, PNL_UNRESOLVED: order is unknown.
Otherwise stop/target uses the barrier price, not a claimed observed tick. Last
deadline-1 candle CLOSE is TIME_EXIT/EOD_EXIT. Stored START exit timestamp can
precede the actual close availability by a minute; preserve and disclose this
legacy timing ambiguity. No trailing, spread projection, price rounding or extra
slippage overlay. Strict missing management candles invalidate the open path.
The labelled legacy diagnostic can traverse holes solely to demonstrate old
semantics; real observed backtests never do so. Barrier prices may be off the
dated option tick grid: execution realism and historical tick authority are
UNKNOWN, not rounded away to manufacture a fill.

## G. Shared independent-account accounting

The destination API enforces an annual-only half-open request: `[start, end)`
must lie within one calendar year, allowing January 1 of the next year as the
exclusive end. Multi-year requests fail validation before data access; they are
not truncated. Separate annual calls initialize from the configured capital.
This API/account boundary does not change any extracted signal, fill, barrier,
sizing, fee or T3 margin assumption.

Fresh and rescenarioed results share summary derivation. Unit execution status
is separate from account affordability and account uncertainty. An unresolved
net account has unknown net P&L and `net_status=UNRESOLVED`; unsupported
full-request cost coverage is separately disclosed by `net_cost_coverage`.

Fresh ₹100,000 per strategy/year/scenario; whole lots and compounding are the
source baseline, not a parameter search. `source_baseline` for V2/V4/V8/T3
preserves assumed lot 75. `allocation_100` uses the bounded dated source schedule:
25 for the January 30,2025 monthly contract;75 for eligible2025  weekly cycles;
65 for January 6..September 8,2026 expiries. A contract must be within7 calendar
days of expiry. Outside the retained mapping, quantity is unresolved; a current
instrument master is not historical authority. H30/ORB already use 100% cash and
dated lots in the source, so their two named scenarios may coincide.

Long-only capacity includes entry brokerage/taxes in the supported-net account.
Cash is committed per position; an exit at the exact same timestamp cannot fund
another entry OPEN (source event convention). There is no implicit one-position
limit on V2/V4/V8. Realized exit-order equity is not intraday MTM. An entered
unresolved path freezes subsequent equity, rather than dropping the trade.
This stricter accounting differs from the source capital overlay that omitted
unresolved trades; report source replication separately, not as exact port cash
parity. T3 maximum-loss capacity and broker margin are separate concepts.

Source STANDARD fees: ₹20 per executed leg-side order; STT sell turnover*0.001
in March 2026 and*0.0015 from April; exchange both turnover*0.0003553; SEBI both
turnover*0.000001; stamp buy turnover*0.00003; GST0.18*(brokerage+exchange).
Source does not round each component. There is no additional candle slippage;
do not deduct bid/ask spread twice in tick variants. Historical fee evidence
before March 1,2026 is absent. Full requested-year net is NOT_COMPUTABLE. The
March 1..September 8 supported-net account starts afresh at₹100,000, independent
of the full gross account. Profit factor without losing trades has a separate
infinite flag. Unknown equity/drawdown/PF is never silently zero.

## H. Shared state and disclosure

IDLE -> SELECTED -> ENTRY_PENDING -> OPEN -> EXIT_PENDING -> CLOSED. Pre-entry
missing identities/windows/prices produce SKIPPED/UNAVAILABLE with reason. An
entered missing/invalid/ambiguous exit produces UNRESOLVED, retaining contract,
raw entry/exit evidence and exposure; it cannot become a favorable substitute.
Affordability is a separately recorded capital skip. Financial account freezes
do not delete unit signals/trades. Every known source date is retained in
eligibility; no-signal and warm-up differ from missing data. T3 adds VIX/expiry
gates; V8 adds armed/interacted state; H30 tick execution retains partial exits.

Parity tests compare source-executed expectations, not Python self-outputs.
Legacy source invalid-OHLC acceptance, missing-candle traversal, off-tick barriers,
same-bar ambiguity and unrealizable minute-OPEN timing are disclosed, not fixed
silently under a parity label. Independent market matching remains NOT VERIFIED.
