# Expiry Day — fixed CE+PE BTST basket

`expiry_day` / **Expiry Day** is the ninth terminal strategy in Engine 5.4.0.
It supports NIFTY and SENSEX; MCX is explicitly unsupported. Data Lab, Data
Quality, repair provenance, dated lot assumptions, and the other eight strategies
retain their existing behavior.

## Entry and expiry session selection

The default uses the repository's dated exchange calendar, including historical
weekday changes and preceding-session holiday adjustments. Entry is the trading
session immediately before that expiry. An unavailable or excluded predecessor
is not silently replaced by an earlier session. Full recorded special sessions
can be predecessors, subject to the normal eligibility checks.

The Period selects **expiry exit dates**. By default a single expiry-date request
also loads the preceding entry session, even outside the Period. Disable
`expiry_include_prior_session` to require both dates inside the window.
`expiry_entry_mode='expiry_intraday'` instead enters on the expiry session
(09:15 signal / 09:16 fill).

BTST entry defaults to the **15:25 IST close signal and 15:26 open fill**.
`expiry_entry_signal` accepts 15:25–15:28; every fill is the following minute.
Both entry-session underlying and six option streams must pass the existing full
09:15–15:20 checks, extended through **15:29** because the basket is marked
through that close. Expiry-session dependencies retain the Engine 5.4.0 window.
Prepared snapshots must use `ResearchData(..., expiry_config=config)`; an ordinary
15:20-audited snapshot cannot certify a late entry.

At signal close, evaluate the nine available CE/PE strike pairs across ATM±1.
Each leg must meet `minimum_premium`. Minimize the absolute premium difference,
then total distance from ATM, then maximum distance, then strikes for deterministic
ties. Entries use only signal-close information. Entry fills must exist for both
selected fixed strikes and the complete basket must be affordable; otherwise
neither leg enters. No modeled entry fills are created.

`expiry_calendar_mode='observed_reset'` accepts explicitly recorded dated expiry
fields or `weekly_reset` evidence. A reset marks the preceding observed session
as expiry; this mode is **retrospective inference**, disclosed in the ledger.
An ATM strike change alone is never treated as an expiry reset. The current lake
has `expiry_flag=WEEK` and `expiry_code=1`, which describe a relative stream and
provide no dated expiry/reset evidence. Use the default calendar mode for this
lake. Neither mode proves the identity of rolling WEEK1 contracts across nights.

## Combined risk and accounting

Both legs are long, with equal quantities. Debit D0 is the sum of their entry
fills times quantity. Combined return is `(combined value / D0 − 1) × 100`.
**No individual CE or PE target, stop, or trailing stop exists.** A CE loss of
40% and PE gain of 90%, from equal entry premiums, is a combined gain of 25%.

No strategy TP/SL/trail is applied during the pre-expiry session. On expiry,
every minute is priced from 09:15 to the chosen cutoff. Exact fixed-strike quotes
take priority; missing out-of-band strikes use the existing tagged wing model
under `model_continuity`. Strict-observed policy still rejects modeled or
synthetic fills. A combined close crossing TP/SL/trail triggers an atomic exit
of both legs at the next minute open. No individual option OHLC high/low is used
to invent a basket trigger. Gaps can exceed thresholds.

Exit modes are **Target + SL**, **Trailing SL**, and **EOD**. EOD disables the
profit target and retains the combined protective stop. Stepped trailing levels
use combined value and activate on the next minute. `expiry_exit_cutoff` is
15:20 by default, or 15:15. Both legs exit together at that open if still held.
Configured account daily cutoffs remain whole-basket safety overrides, including
the entry session; they can shorten an overnight hold.

Debit plus estimated round-trip fees is reserved across the overnight hold.
Each leg has its own adverse tick rounding, slippage, brokerage, STT, stamp duty,
exchange/GST/SEBI/IPFT charges. Long-leg sale taxes use the actual exit date.
Gross basket return is before fees, after fill slippage; net P&L and allocated
capital ROI include fees. Realized equity books the trade on its exit date.

The ledger and chart show **EXPIRY DAY (CE+PE)**, signal and execution dates,
each leg's entry/exit premium, total combined entry/exit debit, and combined
return. Charts include both sessions. Candle interval affects chart aggregation;
the scheduled entry and risk checks remain minute based. Save Result and custom
Save Strategy preserve all settings.

## Live paper trading

The virtual runner resolves real dated contracts from the master, retrieves
signal-minute prices for the six candidate streams, and applies the same balanced
selection. Both chosen contracts must share an expiry and lot size, and every
fill requires fresh quotes on both legs. No wing model is used live.
Live risk checks use fresh combined LTPs; historical risk checks sample minute
closes, so the two execution paths can exit at different prices and times.

Normal intraday square-off and overnight-recovery exits are bypassed for a valid
Expiry Day hold. The runner retains the basket and stays available overnight,
pauses feed/warm-up work outside the daytime window, reconnects on expiry, and
resumes the same fixed contracts after restart. Expiry cutoff and overdue
recovery close the entire basket. Explicit Stop/shutdown requests remain exit
requests and can wait for fresh quotes. Existing paper policy hashes ignore the
new expiry settings when running any of the previous strategies.

## Offline validation and exact lake results

```powershell
.venv\Scripts\python.exe verify_offline_tests.py
.venv\Scripts\python.exe verify_expiry_strategy.py
```

The full suite passed **312 tests**: all 292 previous tests plus 20 new expiry
tests, with **zero external network attempts**. Tests cover combined-only target,
stop and trail, overnight entry/exit dates, balanced selection, holiday shifts,
single-date context, strict pricing and wing fallback, late dependency audits,
paper recovery, all four chart intervals, and UI/result/custom-strategy saves.
Catalog-size assertions now expect nine strategies; no previous test was removed.

The subsequent spread-bound checkpoint passed **320 offline tests** (the same
312 plus eight spread regressions), again with zero external network attempts.
Its joint vertical/butterfly projection does not apply to the long CE+PE
Expiry Day basket. See [the corrected book matrix](README_BOOK_STRATEGIES.md).

Verification requested 2025-01-01 inclusive to 2026-10-31 exclusive. Settings:
₹1,000,000 capital, fixed one dated lot, 100% capital budget, minimum premium 0,
5-minute chart interval, 0.5 points slippage per leg per fill, no trail,
Target + SL, calendar expiry mode, 15:25 signal / 15:26 fill, 15:20 cutoff.
Each instrument had 96 scheduled expiry pairs and seven skipped pairs. NIFTY had
188 eligible dependency sessions / four excluded; SENSEX 185 / seven. Missing
late-window/expiry evidence is reported, never filled with invented sessions.

| Instrument | TP / SL | Net ₹ | Win % | Trades | Profit factor | Wing-modeled trades |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| NIFTY | 25% / 25% | -159,921.19 | 19.101124 | 89 | 0.3455484304 | 74 |
| NIFTY | 50% / 30% | -155,335.77 | 19.101124 | 89 | 0.4141968474 | 77 |
| SENSEX | 25% / 25% | -244,683.44 | 12.359551 | 89 | 0.1663878175 | 78 |
| SENSEX | 50% / 30% | -266,491.01 | 11.235955 | 89 | 0.1733234418 | 80 |

All four runs had **zero unresolved positions** and reconciled every leg's fees,
slippage and P&L to the combined ledger. NIFTY runs include disclosed repaired
inputs; SENSEX is model dependent. These are in-sample, coverage-conditioned
research results, and all four configurations lost money. The rolling lake does
not verify that pre-expiry and expiry observations refer to the same expiry;
large-move modeled valuations affect most trades. These results do not establish
performance on verified expiry-resolved contracts.

Local verification JSON, trade Parquet and CSV files stay under ignored
`data/expiry_verification/`; they are not registered as user-saved Reports.
