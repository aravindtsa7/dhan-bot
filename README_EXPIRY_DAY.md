# Expiry Day — fixed CE+PE BTST basket

`expiry_day` / **Expiry Day** is the ninth terminal strategy in Engine 5.4.0.
It supports NIFTY and SENSEX; MCX is explicitly unsupported. Data Lab, Data
Quality, repair provenance, dated lot assumptions, and the other eight strategies
retain their workflows; signal and valuation changes are documented in the strategy audit.

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
take priority. Each leg retains its latest price, spot and timestamp, including
the overnight transition. Missing out-of-band strikes use smooth moneyness delta
to update the last mark, retaining an intrinsic floor and a gently bounded wing
anchor. Normal pre-expiry decay is 0.02% of extrinsic per trading minute. On expiry,
theta is prior extrinsic times elapsed minutes divided by session minutes remaining
at the prior mark. Overnight wall-clock minutes are not treated as intraday theta.
There is no second multiplication by the remaining-session fraction. Exact observed
quotes recalibrate state and are never decayed. The leg source is
`model_stateful_wing_continuity`, with `modeled_wing_fallback=True`.
Strict-observed policy still rejects modeled or
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
exchange/GST/SEBI/IPFT charges. Per-leg slippage per fill is capped at
`min(configured, 0.50, max(0.05, nearest_tick(1.25% of raw premium)))`;
configured zero stays zero. Long-leg sale taxes use the actual exit date.
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

## Updated offline validation and lake results

The 2026-11-01 execution audit supersedes the results in `38e6751`.
[The complete audit](README_STRATEGY_AUDIT.md) reports all nine strategies and
all 108 instrument/interval/window combinations, including unsupported cases.

Expiry UI defaults: TP25 / SL60, minimum premium 70 per leg, allocation 50%.
Verification uses these settings with INR 100,000 and fixed one dated lot,
no trail, configured slippage 0.50 with proportional cap,
calendar BTST, 15:25 signal / 15:26 fill, and 15:20 exit cutoff. Both 5m and
15m are verified; these scheduled minute-based entries and exits intentionally
produce the same results at both chart intervals. Full requests 2025-01-01
through 2026-10-31; recent requests 2026-08-01 through 2026-10-21 inclusive.

The account never resets capital mid-run. Depletion can make later pairs
unaffordable; full-window counts therefore need not exceed recent counts.
Missing entry/expiry dependency sessions remain explicit skips. Rolling WEEK1
identity across the overnight hold is still unverified, and modeled results
are not evidence of returns on verified expiry-resolved contracts.

Reproduce with `.venv\Scripts\python.exe verify_offline_tests.py` and
`verify_strategy_audit.py NIFTY` / `verify_strategy_audit.py SENSEX`.
The runners block external sockets. Local JSON/CSV/Parquet stays ignored under
`data/strategy_audit/`; verification does not create user-saved Reports.

<!-- EXPIRY_RESULTS -->

| Window | Instrument | Interval | Strategy | Net INR | Win % | Trades | Profit factor | Max DD INR | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| recent | NIFTY | 5m | `expiry_day` | 9,197.93 | 77.78 | 9 | 2.7351 | 3,790.32 | modeled |
| recent | NIFTY | 15m | `expiry_day` | 9,197.93 | 77.78 | 9 | 2.7351 | 3,790.32 | modeled |
| recent | SENSEX | 5m | `expiry_day` | 6,293.53 | 63.64 | 11 | 1.3188 | 10,223.01 | modeled |
| recent | SENSEX | 15m | `expiry_day` | 6,293.53 | 63.64 | 11 | 1.3188 | 10,223.01 | modeled |
| full | NIFTY | 5m | `expiry_day` | 36,670.76 | 56.58 | 76 | 1.2003 | 57,164.84 | repaired |
| full | NIFTY | 15m | `expiry_day` | 36,670.76 | 56.58 | 76 | 1.2003 | 57,164.84 | repaired |
| full | SENSEX | 5m | `expiry_day` | 68,735.89 | 62.92 | 89 | 1.3628 | 56,927.40 | modeled |
| full | SENSEX | 15m | `expiry_day` | 68,735.89 | 62.92 | 89 | 1.3628 | 56,927.40 | modeled |

Both intervals agree because entry and combined risk use the same minute schedule. The 358-test suite passes with zero external connections. Returns remain model-dependent and full-history drawdowns are substantial.
