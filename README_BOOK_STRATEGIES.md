# Book strategies in Engine 5.4.0

Four intraday research adaptations of Kakushadze & Serur's
[151 Trading Strategies, SSRN 3247865](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3247865)
extend the four existing terminal strategies. The structures and section/equation
references follow the user-specified SSRN edition; filters, sampling, sizing and
exits are explicit intraday adaptations, not claims of exact book replication.

| Key | Structure and trigger |
| --- | --- |
| `book_vertical_spread` | Sections 2.6/2.9, equations 17–20/29–32. Bullish close > session VWAP, EMA9 > EMA21, ADX14 ≥18 buys ATM CE and sells ATM+1 CE. Bearish reverses the inequalities, buys ATM PE and sells ATM-1 PE. MCX futures use long/short with stop distance min(ATR14 × atr_sl, entry × sl_pct / 100), reward_risk target and existing ATR trailing policy. |
| `book_iron_butterfly` | Section 2.44, equations 196–200. After 09:45 IST, ADX14 <22 and spot within 0.25% of VWAP: short ATM CE/PE, long ATM+1 CE and ATM-1 PE. Requires common central strike and equal positive wing widths κ. Entry credit C must satisfy 0 < C < κ; theoretical expiry max loss is κ−C before costs. Exit on credit-value decay by target_pct or expansion by sl_pct, or 15:15 IST. |
| `book_strap_strip` | Sections 2.34/2.35, equations 138–147. Upside ORB or prior-T-bar Donchian break buys 2 ATM CE + 1 ATM PE; downside buys 1 CE + 2 PE. Requires fixed-contract option volume ≥ volume_multiple × mean prior T complete bars. Combined target, stop, stepped trail and EOD. |
| `book_vol_oi_momentum` | Sections 10.3.1/10.4, equations 472–475. Log close returns R; σ is sample standard deviation over T preceding returns, excluding the current return. η is the mean of the last T tanh(R/σ) values. Enter when abs(η) ≥ momentum_threshold, log(V / prior-T mean V) ≥ log(volume_multiple), and log(OI / prior-T mean OI) > 0 when a complete positive OI history exists. Default T=10, threshold=0.25. Sign selects CE/PE or MCX long/short. |

Iron butterfly and strap/strip require NIFTY/SENSEX option streams. The local
MCX lake has futures, so those two structures explicitly reject MCX. Vertical
and momentum support NATURALGAS, CRUDEOIL and GOLDM futures. Index spot volume
is usually zero; session VWAP then uses the existing disclosed TWAP proxy.
Volume/OI activity always uses one fixed option strike, never stitched rolling
strikes. Missing OI is recorded in signal evidence and leaves volume as the
available activity confirmation. Missing volume history rejects the signal.
Momentum return/volatility smoothing resets within each session and requires
roughly 2T+1 complete bars of warm-up. With 15-minute index candles and T=10,
only late-session signals can qualify; complete fixed-strike activity history
further reduces the sample (two NIFTY trades and no SENSEX trades here).

## Atomic baskets and inspection

All strikes resolve at signal close, before next-minute-open execution. Every
entry leg must be priced and the entire structure affordable, or no leg enters.
The basket is one trade and consumes one daily entry. Capital reservation uses
the net debit for long structures or κ−C for the credit butterfly, plus estimated
round-trip fees for every leg. Credit proceeds never increase buying power.

Each distinct leg has two orders, its own signed quantity, adverse slippage,
tick rounding, brokerage, sell-side STT, stamp duty and exchange/GST/SEBI/IPFT
fees. A ratio-2 leg is one order of twice the quantity. The JSON `legs` ledger
field preserves individual fills and fees; net basket P&L is their signed sum
less fees. `gross_before_slippage − slippage_cost − total_cost = net_pnl`.
Historical fee schedules remain configurable research assumptions.

Basket entry/exit values are signed sums (long positive, short negative).
An iron butterfly collecting ₹C has entry value −C; decay moves its value toward
zero and produces profit. The ledger explicitly labels debit/credit, lists
signed leg strikes, and shows BULL CALL SPREAD, BEAR PUT SPREAD, IRON BUTTERFLY,
STRAP (2CE:1PE), or STRIP (1CE:2PE). KPI structure badges and chart titles use
the same labels. Profit/stop thresholds use abs(entry net value).

Basket exits trigger on synchronized minute closes and fill all legs at the
next open. A daily account lock overrides a pending strategy exit. Portfolio
trailing levels become active on the next minute. Combined chart candles show
only synchronized open/close envelopes; individual option OHLC extrema cannot
establish simultaneous executable basket prices. Fallback spread marks are
bounded before execution costs; fees and adverse slippage may exceed theoretical
payoff bounds. Iron butterfly profit targets remain
active in every exit mode; all other baskets follow the selected exit mode.

Saved strategies deploy to a virtual-only paper runner. It resolves a common
dated expiry, verifies actual leg lot sizes, subscribes to every fixed contract,
and waits for fresh quotes on all legs before atomic entry/exit. Recovery retains
each contract and waits for fresh quotes again. Paper marks are synchronized
fresh LTPs (up to the existing 15-second freshness bound), with bid/ask and adverse
slippage fills; historical signals/exits are minute sampled. No modeled wing
prices or broker order endpoints are used in live paper execution.

## Spread continuity revision (2026-11-01)

The previous cap-only model could convert a losing spread into a width-cap win
when an offset disappeared. Its intrinsic floor and independent wing extrinsic
decay have been removed from vertical and butterfly spread valuation. Those
historical spread results are superseded by the verification below.

Exact fixed-strike observations still take priority. When a spread leg is missing,
the engine values the pair directly, before reconstructing positive per-leg
prices and charging slippage, tick rounding and fees. It never calls the
single-option fallback for that missing spread leg. Entries remain atomic and
require every entry leg to be present.

For each call or put vertical, state starts with the raw entry spread and spot
at entry open. It then advances through that bar's close, the next open, and so
on. Every fully observed pair refreshes this state. With direction +1 for a call
vertical and -1 for a put vertical:

```
Vadj = clamp(ATM premium - outward wing premium, 0.05, width - 0.05)
anchor = clamp(Vadj + direction * 0.20 * (ATM strike - fixed long strike),
               0.05, width - 0.05)
predicted = Vprevious + direction * 0.20 * (spot - previous spot)
correction = clamp(0.25 * (anchor - predicted),
                   -0.05 * abs(spot change), +0.05 * abs(spot change))
V = clamp(predicted + correction, 0.05, width - 0.05)
```

The 0.20 delta and 0.25 sensitivity ceiling are explicit research assumptions,
not universal theoretical bounds on option Greeks. An offset change alone cannot
move the modeled spread: with zero spot change the correction is zero. Before
price bounds, a 20-point move changes the modeled value by 3-5 points; the
maximum for a 50-point move is 12.5 points. The adjacent anchor cannot teleport
the spread to the width cap. No intrinsic-spread floor is applied.

The adjacent ATM/outward pair must be contemporaneously observed, of matching
width, and free of synthetic repair candles. If that preferred pair is unusable,
select the nearest same-width observed pair among the three stored offsets.
For example, SENSEX on 2025-11-12 at 10:58 labels ATM+1 as 85000 while ATM
is 84400. Its observed 84300/84400 pair supplies the required 100-point anchor;
the 600-point difference is never substituted for it. If no valid pair exists,
missing evidence still fails explicitly.
One observed target leg remains unchanged; its partner is reconstructed from V.
With both target legs missing, the lower-intrinsic leg carries its previous time
value with exact intrinsic movement, while the modeled difference remains V.
Both reconstructed premiums must be at least 0.05. Inconsistent exact quotes
that cannot satisfy positivity and continuity are rejected.

The butterfly applies this process to its call and put pairs, then projects the
combined liability into one wing-width budget. Exact observations remain fixed.
If an observed pair consumes the budget, its modeled companion may shrink
further than the standalone delta band, even to zero; the joint no-arbitrage
bound takes priority. The signed butterfly value remains in [-width, 0].

Risk checks use synchronized closes. A pending Stop Loss fills at the next
open using the previous close's spread/spot state. There is no special rule to
force stop exits negative: a genuine observed gap can still change the outcome.
Per-leg fills reconcile to the modeled basket, and all fees remain charged.

`modeled_wing_fallback`, `spread_bounds_applied`, modeled bar counts and per-leg
sources disclose the model. The new sources are `model_spread_wing_continuity`
and `model_spread_continuity`. Strict-observed policy still rejects missing or
synthetic legs. Single-option, strap/strip, Expiry Day and futures pricing paths
are unchanged. Engine 5.4.0 eligibility, repair rules, Data Lab and Data Quality
are unchanged. Run identity now uses `book-spread-continuity-v3`; rerunning a
backtest creates a new result rather than reusing a cap-only cached result.
Existing saved results retain their historical values.

## Offline validation

The complete suite passed **327 tests**: all 320 prior cases plus seven new
continuity regressions, with **zero external network attempts**. The prior
intrinsic-floor test now verifies the replacement continuity requirement;
no test was removed. Fixtures contain only synchronized quote excerpts needed
for the six reported cases, so the regressions do not require a production lake.
Coverage includes both missing legs, calls and puts, exact quote priority,
butterfly pair continuity, mismatched offset spacing, missing/synthetic adjacent
evidence, and independence
of a stop fill from the fill bar's later close. Streamlit was restarted on 8501;
both the page and health endpoint returned HTTP 200.

```powershell
.venv\Scripts\python.exe verify_offline_tests.py
.venv\Scripts\python.exe verify_spread_continuity.py
.venv\Scripts\python.exe verify_book_strategies.py --instruments NIFTY SENSEX --strategies book_vertical_spread --output data/book_continuity_verification
```

All verification processes block external sockets. Generated JSON, CSV and
Parquet evidence stays under ignored data/ directories; no source partitions
or saved Reports are changed. The book matrix checks spread width bounds and
per-leg/slippage/fee reconciliation. Incomplete runs are marked explicitly,
never assigned partial totals or silently excluded.

### Original NIFTY entries replayed

Saved run `5292ca092a43c63d` supplied the entry identities. Each is replayed with
its original fixed strikes, 25,000 INR budget and original risk settings,
independently of the corrected full portfolio's later capital availability.
Trade numbers in a fresh full run can differ because exits and entry eligibility
change. In the actual saved ledger, #66 is 23050/23000 PE; #73 is 22500/22450 PE.

| Original trade | Entry IST | Contract | Raw exit | Exit after slippage | Net INR | Exit reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 66 | 2026-09-25 09:42 | +23050PE / -23000PE | 14.082500 | 13.05 | -7,716.27 | Stop Loss |
| 70 | 2026-09-29 09:39 | +22600PE / -22550PE | 18.000000 | 17.00 | -6,335.51 | Stop Loss |
| 71 | 2026-09-30 09:27 | +22750CE / -22800CE | 19.774375 | 18.75 | -8,897.39 | Stop Loss |
| 72 | 2026-09-30 10:18 | +22750CE / -22800CE | 21.667500 | 20.65 | -7,072.81 | Stop Loss |
| 73 | 2026-10-01 09:27 | +22500PE / -22450PE | 15.312500 | 14.30 | -7,517.70 | Stop Loss |
| 74 | 2026-10-01 09:30 | +22550PE / -22500PE | 16.407500 | 15.40 | -7,557.63 | Stop Loss |

For #70, the 09:41 observed spread closes at 18.20 and spot at 22618.50.
The 09:42 spot open is 22619.30, a +0.80 move. The put model predicts 18.04;
the bounded adjacent correction is -0.04, giving raw 18.00 and a 17.00 fill
instead of the old raw 36.456028 / fill 35.45 windfall. Its net changes from
+12,860.56 to -6,335.51 INR. #73 now exits at 09:29 at 14.30 rather than 48.95.

### Reported 3-minute window

2026-08-01 inclusive through 2026-10-21 inclusive (end exclusive 2026-10-22).
Settings match the saved run: 100,000 INR capital, 25% allocation, allocation
sizing, minimum long premium 50, TP 25%, SL 15%, no trail, 0.5 slippage per leg,
09:25-14:30 entries. Both completed with zero unresolved positions.

| Instrument | Net INR | Win % | Trades | Profit factor |
| --- | ---: | ---: | ---: | ---: |
| NIFTY | -94,438.27 | 21.250000 | 80 | 0.116740 |
| SENSEX | -85,708.22 | 35.514019 | 107 | 0.490573 |

The NIFTY portfolio loses most of its starting capital; affordability then
reduces the sample. These are in-sample results using modeled prices and
unverified rolling expiry identities, not verified contract performance.

### Long-history vertical verification

Window: 2025-01-01 inclusive to 2026-10-31 exclusive. Same checkpoint settings:
1,000,000 INR capital, one dated lot, 100% budget, TP 25%, SL 15%, no trail,
minimum premium zero, 0.5 slippage per leg, 09:30-14:30 entries. NIFTY has
445 eligible / 8 excluded sessions; SENSEX has 435 eligible / 19 excluded.
Both cover available eligible sessions through 2026-10-22.

| Instrument | Interval | Net INR | Win % | Trades | Profit factor |
| --- | --- | ---: | ---: | ---: | ---: |
| NIFTY | 5m | -228,643.46 | 28.139535 | 860 | 0.233097 |
| NIFTY | 15m | -230,555.58 | 24.494949 | 792 | 0.187618 |
| SENSEX | 5m | -154,427.18 | 28.904429 | 858 | 0.217272 |
| SENSEX | 15m | -148,412.85 | 27.169811 | 795 | 0.201147 |

All four completed with zero unresolved positions and passed spread-width and
per-leg cost/P&L checks. The previously positive cap-only results are superseded.
The SENSEX 15-minute run additionally exercises the observed matching-width
anchor selection described above; the other runs use the preferred pair.
