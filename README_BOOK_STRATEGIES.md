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
establish simultaneous executable basket prices. Gaps, fees and slippage may
exceed theoretical expiry payoff bounds. Iron butterfly profit targets remain
active in every exit mode; all other baskets follow the selected exit mode.

Saved strategies deploy to a virtual-only paper runner. It resolves a common
dated expiry, verifies actual leg lot sizes, subscribes to every fixed contract,
and waits for fresh quotes on all legs before atomic entry/exit. Recovery retains
each contract and waits for fresh quotes again. Paper marks are synchronized
fresh LTPs (up to the existing 15-second freshness bound), with bid/ask and adverse
slippage fills; historical signals/exits are minute sampled. No modeled wing
prices or broker order endpoints are used in live paper execution.

## Large-move fixed-strike pricing

1. Use an exact current fixed-strike quote from any stored offset first.
2. With `model_continuity` policy, if the strike lies outside the observed band,
   take the nearest observed wing at the same minute. For each mark, use
   intrinsic(S, K) + min(observed wing extrinsic, one observed strike spacing)
   × exp(−distance from entry strike to wing / spacing), floored at ₹0.05.
   Spot and wing extrema produce a synthetic envelope. No lagged IV, calendar
   expiry, previous option mark, or future quote is needed.
3. Other missing-candle cases retain the existing lagged-IV continuity model.
   Missing required spot/neighbor evidence still fails explicitly.

Trade and per-leg `modeled_wing_fallback` flags, modeled-bar counts, price sources,
KPI disclosure, ledger column, and result limitations expose the estimates.
`strict_observed` still rejects all modeled execution. Synthetic repaired wings
are never treated as observed fallback anchors. A pricing revision in the run
identity prevents reusing old cached results while retaining Engine 5.4.0.
Rolling options still lack verified expiry identity; a priced close is not
proof of a verified contract or a profitable strategy.

## Offline validation

```powershell
.venv\Scripts\python.exe verify_offline_tests.py
.venv\Scripts\python.exe verify_book_strategies.py --start 2025-01-01 --end 2026-10-31
```

External sockets are blocked during these checks. The matrix retains current
Engine 5.4.0 quality/eligibility/repair gates and writes only to ignored
`data/book_verification/`. It verifies per-leg reconciliation, total costs,
slippage accounting, entry caps, and zero unresolved trades. It never registers
verification runs in Reports or changes source partitions.

The 24-row matrix uses ₹1,000,000 capital, one dated lot, 100% capital budget,
25% target / 15% stop, no trail, zero minimum premium, volume multiple 1.3,
09:30–14:30 index entries / 17:00–22:45 MCX entries. End is exclusive; only
available eligible sessions contribute. Results are descriptive in-sample
research, conditioned on stored coverage and including disclosed modeled prices.

### Local-lake checkpoint results (2026-10-31)

| Instrument | Eligible | Excluded | First / last eligible session |
| --- | ---: | ---: | --- |
| NIFTY | 445 | 8 | 2025-01-01 / 2026-10-22 |
| SENSEX | 435 | 19 | 2025-01-01 / 2026-10-22 |
| NATURALGAS | 64 | 37 | 2026-07-09 / 2026-10-29 |

The request window was 2025-01-01 through 2026-10-30 inclusive. Unavailable
dates are not fabricated. All 20 supported runs closed every entered position;
four MCX option-only combinations are explicitly unsupported. No external
network was available. NIFTY runs include disclosed repaired inputs; SENSEX
runs with trades depend on modeled pricing. NATURALGAS uses observed futures
candles under strict-observed policy. Across runs, 3,866 trades used a wing
fallback. Several strategies depleted the capital budget and therefore stopped
entering trades; counts are not identical samples. Losses may exceed starting
capital on the final gapped/costed close. Positive vertical-spread results also
depend heavily on modeled fills and unverified rolling expiry identity.

| Instrument | TF | Strategy | Net ₹ | Win % | Trades | Profit factor |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| NIFTY | 5m | Vertical | 630,628.60 | 46.49 | 869 | 1.737 |
| NIFTY | 5m | Iron butterfly | -999,690.54 | 3.85 | 338 | 0.021 |
| NIFTY | 5m | Strap / strip | -996,541.79 | 15.91 | 176 | 0.054 |
| NIFTY | 5m | Vol/OI momentum | -114,118.40 | 24.60 | 126 | 0.238 |
| NIFTY | 15m | Vertical | 742,868.09 | 44.68 | 799 | 2.187 |
| NIFTY | 15m | Iron butterfly | -1,000,195.80 | 2.82 | 319 | 0.012 |
| NIFTY | 15m | Strap / strip | -328,524.32 | 22.71 | 207 | 0.188 |
| NIFTY | 15m | Vol/OI momentum | -382.45 | 0.00 | 2 | 0.000 |
| SENSEX | 5m | Vertical | 667,740.70 | 46.98 | 860 | 1.573 |
| SENSEX | 5m | Iron butterfly | -1,002,211.56 | 2.87 | 279 | 0.046 |
| SENSEX | 5m | Strap / strip | -997,615.58 | 16.42 | 134 | 0.041 |
| SENSEX | 5m | Vol/OI momentum | -164,371.90 | 25.29 | 87 | 0.096 |
| SENSEX | 15m | Vertical | 1,096,368.00 | 50.19 | 799 | 2.503 |
| SENSEX | 15m | Iron butterfly | -1,000,755.24 | 2.84 | 282 | 0.034 |
| SENSEX | 15m | Strap / strip | -203,644.43 | 27.94 | 68 | 0.102 |
| SENSEX | 15m | Vol/OI momentum | 0.00 | 0.00 | 0 | — |
| NATURALGAS | 5m | Vertical futures equivalent | -169,124.71 | 13.28 | 128 | 0.151 |
| NATURALGAS | 5m | Iron butterfly | N/A | N/A | N/A | N/A |
| NATURALGAS | 5m | Strap / strip | N/A | N/A | N/A | N/A |
| NATURALGAS | 5m | Vol/OI momentum | -67,893.26 | 24.00 | 75 | 0.391 |
| NATURALGAS | 15m | Vertical futures equivalent | -185,392.30 | 18.49 | 119 | 0.204 |
| NATURALGAS | 15m | Iron butterfly | N/A | N/A | N/A | N/A |
| NATURALGAS | 15m | Strap / strip | N/A | N/A | N/A | N/A |
| NATURALGAS | 15m | Vol/OI momentum | -36,793.72 | 25.00 | 48 | 0.532 |

The original 267 regression tests remain, with the catalog assertion updated to
eight keys. New tests cover causal positive/negative book signals, per-leg tax
and slippage arithmetic, theoretical expiry payoff bounds, atomic allocation,
credit/debit exits, portfolio trail/daily locks, large-move fallback, exact-quote
priority, strict pricing, every result/name save path, all four book UI charts,
and fresh-quote paper basket recovery.

The final full offline suite passed **292 tests** (267 existing + 25 new), with
**zero external network attempts**. The real-lake UI smoke check also passed
NIFTY and CRUDEOIL backtests, all six pages, explicit saves and Reports replay.
