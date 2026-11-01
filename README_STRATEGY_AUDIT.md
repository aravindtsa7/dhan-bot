# Strategy audit — Engine 5.4.0 (2026-11-01)

This audit supersedes directional-exit and verification assumptions in `67e23c4`. Expiry Day is preserved exactly; Expiry Intraday is a separate tenth strategy. Engine 5.4.0 data
eligibility/repair rules, Data Lab, Data Quality, dated lot/fee schedules, saved
results, custom strategies, charts and ledgers are preserved. Run identities now
use `explicit-exits-v6` for changed strategies. Expiry Day retains its `stateful-execution-v5` identity; old saved results remain available.

## Signals and warm-up

| Strategy key | Audited signal and limits |
| --- | --- |
| `vwap_ema_pullback` | VWAP and EMA9/21 alignment; EMA9 touch followed by a close beyond the prior bar, or a fresh EMA9 cross. Directional body and rising ADX14 ≥18. Maximum two entries; three selected-timeframe bars of elapsed cooldown after a stop/trailing stop. |
| `orb_volume_breakout` | Existing index 09:15–09:30 / MCX 17:00–17:30 range and RSI confirmation. Current activity exceeds configured multiple of ten complete prior bars. Maximum two entries. |
| `supertrend_adx_trend` | Existing Wilder ATR10 Supertrend ×3, ADX >22, confirmed flip or continuation. Existing portfolio caps and exits preserved. |
| `mcx_evening_breakout` | NATURALGAS/CRUDEOIL only; ten-bar Donchian breakout, EMA21/50 directional alignment, absolute body ≥0.6 ATR14. Signal closes 17:00–22:00 IST; maximum one entry per session. |
| `book_vertical_spread` | Sections 2.6/2.9, equations 17–20/29–32. Same fresh pullback/body/rising-ADX conditions as VWAP strategy, plus activity expansion over ten prior bars. Bull: long ATM CE, short ATM+1 CE. Bear: long ATM PE, short ATM−1 PE. Maximum two entries and three-bar stop cooldown. MCX uses directional futures with ATR-capped percentage stops. |
| `book_iron_butterfly` | Section 2.44, equations 196–200. After 09:45, ADX14 <22 and within 0.25% of VWAP; short ATM CE/PE, long equal-distance wings. Credit must be positive and less than width. Combined target/stop; 15:15 square-off. |
| `book_strap_strip` | Sections 2.34/2.35, equations 138–147. Initial 09:15–09:45 range <0.65% of spot, fresh range break, valid ADX14 ≥20, directional ATM volume >max(1.4, configured multiple) × prior mean. Strap 2CE:1PE; strip 1CE:2PE. Maximum one entry before 13:30. |
| `book_vol_oi_momentum` | Sections 10.3.1/10.4, equations 472–475. Log return normalized by sample volatility of T prior returns; smooth tanh over up to T valid values. Default T=6, first possible signal after T+2 bars; threshold 0.25. Active ATM CE/PE activity confirms direction; positive OI expansion required when complete positive OI history exists. |
| `expiry_intraday` | Separate morning-only expiry strategy: 09:20 close signal / 09:21 open fill, configurable 09:15-11:00; balanced CE+PE, combined-only TP35/SL40, minimum premium 40, allocation 50%, 15:20 EOD. No overnight hold. |
| `expiry_day` | Fixed balanced CE+PE basket at pre-expiry 15:25 signal / 15:26 open; combined-only expiry risk and atomic next-open exits. [Detailed methodology](README_EXPIRY_DAY.md). |

These are intraday research adaptations of Kakushadze & Serur's *151 Trading
Strategies*, following the user-specified section/equation references. Filters and
execution assumptions are adaptations, not claims of exact book replication.

For zero-volume index minutes, synchronized **observed active ATM CE+PE volume**
weights VWAP and activity filters. This is option activity, not spot volume.
Rolling strikes may change in activity history; execution strikes never roll.
Missing/synthetic activity is not invented. Unavailable activity retains a tagged
TWAP fallback. OI uses each completed bar's final minute; missing OI is disclosed
and leaves volume confirmation, while missing volume history rejects entry.

EMA/ATR/ADX warm-up carries across consecutive eligible sessions of the same
contract, resetting after an excluded/missing expected session or contract change.
VWAP resets daily. Momentum uses intraday T+2 warm-up, removing the second full T
smoothing window and the fixed-strike activity-history bottleneck. Required data
dependency windows have not been relaxed. NaN ADX cannot qualify a strap/strip.

## Stateful single-leg wing valuation

Every open option leg retains its latest price, spot and timestamp. Exact fixed
strike observations recalibrate that state. When the strike leaves the observed
band, signed midpoint moneyness in strike steps sets delta to
`clamp(0.50 + 0.38*tanh(0.65*m), 0.08, 0.92)`. The next price is the prior price
plus signed delta times the spot move, less theta. Normal-day theta is 0.02% of
prior extrinsic per elapsed trading minute. On expiry, theta is prior extrinsic
times elapsed minutes divided by the session minutes remaining at the prior mark.
Overnight elapsed wall-clock hours are not charged as intraday minutes.

The intrinsic / 0.05 floor always applies. A wing-based target receives at most
10% weight, further bounded by 10% of absolute delta movement plus 10% of theta.
A rolling-band change at unchanged spot therefore cannot collapse time value.
The old distance-decayed formula remains only an anchor/standalone diagnostic;
open positions use persistent state. Exact quotes can still differ from estimates.
The source is `model_stateful_wing_continuity`, with `modeled_wing_fallback=True`.
These are explicit research assumptions, not measured option Greeks or market IV.

Single-leg strategies, strap/strip and Expiry Day share this implementation.
Expiry Day applies remaining-session decay once inside the model, never a second
multiplication of the whole modeled extrinsic. Both basket legs still exit together.
Modeled candles are open/close envelopes. Pending fills consume only the open.

## Coupled spread valuation

Exact fixed-strike observations take priority. Missing spread legs are priced as
pairs, never by independently collapsing a wing's extrinsic. Model-dependent
legs/trades remain disclosed. Strict-observed still rejects missing/synthetic
prices. Entries require every leg to be observed and affordable.

Each pair starts at raw entry V0 and spot S0. A smooth moneyness potential
integrates delta from 0.28 near ATM toward 0.42 ITM. Its transition scale blends
entry extrinsic with current observed adjacent same-width extrinsic, bounded
between width/6 and twice width. This is a **sigmoid research model, not recovered
market IV**. The entry curve equals V0 at S0 and includes the favorable floor:

`max(0.05, min(width−0.05, V0 + 0.32 × signed_spot_move, 0.85 × intrinsic_spread))`.

Each incremental update adds the curve's spot-to-spot change to the latest pair
mark. An intervening exact close recalibrates the level, including a stop-triggering
close. The next fill cannot jump back to a stale entry estimate. Continuity takes
priority where that observation conflicts with the old entry-curve floor. Zero
spot movement produces zero modeled vertical spread change. Modeled vertical marks remain
in [0.05, width−0.05], with incremental movement ≤0.42 × absolute spot move.
Individual premiums are reconstructed to retain observed prices and positive legs.

Butterfly pairs share one liability budget: their sum cannot exceed wing width;
signed basket marks remain in [−width, 0]. Costs apply after these bounds. Exact
observations and positive leg prices constrain the projection. If an observed
central option decays below the prior pair liability, a butterfly's individual
spot-delta band yields to positive wing prices and the joint width constraint.
An explicit regression covers this unchanged-spot decay; it is not applied to
vertical stop fills. Small observed pair overshoots of `width - 0.05`, within
`max(0.05, 2% of width)`, are projected to that cap. This covers the four originally
reported bars and the same SENSEX 2025-12-24 15:05 candle's 101.30 close liability.
The adjusted wing is tagged `model_observed_spread_projection`; the trade/trace
records `spread_bounds_applied=True`. Leg metadata retains the raw open and time
of its most recent projection. Joint butterfly liability remains bounded by one
wing width. Projection is an execution assumption and never edits lake quotes.
Larger incompatible quotes still fail explicitly; strict-observed rejects projection.

If all three stored strikes are explicitly tagged synthetic repairs, an already
calibrated spread retains its prior curve scale through that minute, rather than
calibrating to flat repairs. `retained_repair_calibration` discloses this case.
No missing/synthetic quote becomes an observed anchor. This resolves the fully
repaired NIFTY 2026-04-28 11:58 band without changing Engine 5.4.0 eligibility.

Pending stop/target exits and scheduled square-offs price **only the fill open**.
A candle's later close cannot price or veto that exit. A new regression covers the
NIFTY 2026-09-08 15:15 butterfly failure. Existing regressions retain all six
reported 3-minute NIFTY transitions, including Trade #70's observed 09:41 stop
followed by its 09:42 modeled fill.

## Slippage, capital and paper workflows

For verticals, butterflies, strap/strip and Expiry Day, adverse slippage per leg
per fill is `min(configured, 0.50, max(0.05, nearest_tick(0.0125 × raw_premium)))`.
Tick ties round up; fills retain adverse tick rounding. Configured zero stays
zero. Premiums ≥₹40 still reach the default ₹0.50 ceiling: this formula does not
guarantee ₹0.05–₹0.25 slippage for every leg. Single-leg/futures slippage is unchanged.

Every leg retains brokerage, sell-side STT/CTT, exchange, GST, SEBI/IPFT and stamp
duty charges. Ratio-2 is one order with twice the quantity. Signed leg fill P&Ls
sum to basket gross; raw gross minus slippage minus fees equals net. Entry/exit is
atomic; combined minute-close risk triggers next-open fills. Chart candles show
open/close envelopes, not simultaneously executable leg extrema.

Butterflies reserve **full width × quantity plus estimated round-trip fees**.
Payoff max loss remains (width−credit) × quantity before fees, in a separate
ledger field. Credit never increases buying power. The ₹25,000 / 100-point SENSEX
fixture cannot open more than 12 twenty-unit lots; fees can reduce this further.
Long baskets reserve net debit plus fees. Observed Expiry Day quotes remain unchanged.

Save Result and named Save Strategy workflows remain supported for all ten
strategies. Virtual paper uses fresh real quotes, never wing models; actual
bid/ask remains the fill base where available, plus capped modeled slippage.
Paper entry caps and stop cooldowns follow the updated rules. Broker warm-up
currently provides resolved-contract activity, not a historical rolling ATM
chain; historical/live signal parity is therefore not asserted. Historical risk
is minute sampled; paper risk uses fresh ticks.

## Futures risk and strategy defaults

For public MCX strategies, UI TP/SL values scale entry ATR. Target distance is
`entry_ATR14 * 2.2 * TP / 30`; stop distance is
`entry_ATR14 * 1.2 * SL / 15`. Defaults TP30/SL15 therefore produce a 2.2 ATR
target / 1.2 ATR stop. This is a **1.83:1 gross ratio before fees**, not a guaranteed
net ratio. Values are ATR scaling controls, not literal underlying percentage moves.
New UI configurations reserve 20% assumed margin plus fees; no dated broker margin
schedule is inferred. Legacy research aliases retain their historical risk rules.

**Target + SL / Target / EOD never activates the automatic profit lock.** Those
modes keep their configured initial stop and target until exit or EOD. Only
explicit **Trailing SL** activates the futures +1.2 ATR / +0.2 ATR lock and
0.25 ATR increments, or directional option +18% / +10% lock with configured
`trail_jump_pct` increments (default 5%). Paper and backtests share the same helper.
A trailing stop applies on subsequent minutes/ticks; costs and gaps remain charged.
Explicit EOD has no target and retains its protective stop.

Directional index defaults remain TP30/SL20. All supported MCX defaults are
TP30/SL15. Butterfly and strap/strip retain TP25/SL15. **Expiry Day defaults and
execution are unchanged:** TP25/SL60, minimum premium 70, allocation 50%, pre-expiry
15:25 signal / 15:26 fill, combined-only overnight basket. Its source module,
existing test file and standalone README remain byte-for-byte unchanged.

**Expiry Intraday** has its own key, timing fields, signal gate and badge
`EXPIRY INTRADAY (CE+PE)`. Dated exchange calendar/holiday rules admit only expiry
sessions. `intraday_entry_signal` accepts 09:15-11:00 (default 09:20), with next-minute
open entry. At signal close, match observed CE/PE premiums across ATM and both wings,
requiring 40 per leg by default. Entry is atomic. The unchanged shared execution
primitive manages only the sum of the two legs, using TP35/SL40, optional combined
trailing and `intraday_exit_cutoff` 15:20 (or 15:15). It is never scheduled overnight.
Live paper requires two fresh real quotes with the same expiry equal to that day.
Save Result, named Save Strategy, restored timing controls, ledger and charts are supported.

`strategy_defaults` remains an explicit public function in `quant_terminal.py`.
The UI can fall back to independent `quant_strategy_defaults.py` when a stale
module lacks the export. Restarting both old port-8501 Python processes clears
cached imports. The live page is tested with an isolated headless Edge browser,
including selecting Expiry Intraday; rendered text and accessibility output are
checked for ImportError, tracebacks and Streamlit exception elements.

## Verification settings and interpretation

The full table covers ten strategies × three instruments × two intervals × two
windows: **120 combinations**, including unsupported cases. Recent means
2026-08-01 through 2026-10-21 inclusive. Full requests 2025-01-01 through
2026-10-31 inclusive; actual eligible coverage is reported below.

Each run starts at **INR 100,000**, fixed one dated lot, no compounding,
Target + SL (no trailing), configured slippage 0.50, volume multiple 1.3 and T=6.
Daily account locks are disabled. Directional index keys use TP30/SL20; index
single-leg VWAP, ORB, Supertrend and momentum require minimum premium 50, matching
the UI. Other book index runs retain minimum premium zero for comparison.
Butterfly and strap/strip use TP25/SL15. MCX uses TP30/SL15, 20% margin, and
2.2/1.2 ATR target/stop distances. Expiry Day remains TP25/SL60, minimum premium 70,
50% allocation, calendar BTST and 15:20 cutoff. Expiry Intraday uses TP35/SL40,
minimum premium 40, 50% allocation, 09:20 signal and 15:20 cutoff. Other strategies
use 100% allocation. MCX uses observed prices; indices permit disclosed continuity
models. Settings follow the requested defaults, not a parameter search.

Net and profit factor include fees/slippage. Max drawdown is the existing **daily
realized-equity peak-to-trough drawdown in rupees**, not worst intrabar loss.
Local JSON also records minute-close intraday drawdown. Capital depletion skips
unaffordable signals; full-window counts can be smaller than recent-window counts.
Unsupported rows are not zero-trade backtests. WEEK1 expiry identity remains
unverified, so modeled results do not establish performance on expiry-resolved
contracts. The equity losses are reported without optimizing them away.

```powershell
.venv\Scripts\python.exe verify_offline_tests.py
.venv\Scripts\python.exe verify_strategy_audit.py NIFTY
.venv\Scripts\python.exe verify_strategy_audit.py SENSEX
.venv\Scripts\python.exe verify_strategy_audit.py NATURALGAS
```

The runners block external sockets. JSON, per-run trade Parquet and CSV remain
ignored under `data/strategy_audit/`, without registering user-saved Reports.

<!-- AUDIT_RESULTS -->

## Audit results

**369 offline tests passed; zero external network attempts.** All 358 existing tests remain, with expectations updated for the requested catalog/exit changes. The existing Expiry Day test file is unchanged. Eleven new regressions cover the import fallback, new morning strategy, combined exits, explicit-only trailing, UI/save workflows and live paper behavior.

**120 combinations: 96 completed, 24 unsupported, zero failures.** Every completed run has zero unresolved positions and reconciles P&L, fees, slippage and allocation. All Target + SL matrix trade ledgers contain zero trailing-stop exits. Unsupported rows are explicitly marked, not zero-trade backtests.

**Expiry Day preservation verified:** all eight NIFTY/SENSEX recent/full 5m/15m runs have exactly identical run IDs, every metric, and complete trade ledgers compared with `67e23c4`. `quant_expiry.py`, `test_quant_expiry.py` and `README_EXPIRY_DAY.md` are unchanged. New intraday settings are excluded from old strategy identities and paper account policy hashes.

**Rendered application verified:** stale port-8501 Python processes were stopped; a fresh Streamlit server returns HTTP 200 at root and health. An isolated headless Edge browser selected Expiry Intraday and verified its 09:20 timing control, with zero Streamlit exception elements and zero ImportError/traceback text in the rendered page/accessibility output. Rendered artifacts remain ignored in `data/`.

| Window | Instrument | Dependency basis | Eligible | Excluded | Actual first / last eligible |
| --- | --- | --- | ---: | ---: | --- |
| recent | NIFTY | intraday | 55 | 0 | 2026-08-03 / 2026-10-21 |
| recent | NIFTY | expiry dependencies | 24 | 0 | 2026-08-03 / 2026-10-19 |
| recent | SENSEX | intraday | 55 | 0 | 2026-08-03 / 2026-10-21 |
| recent | SENSEX | expiry dependencies | 22 | 0 | 2026-08-05 / 2026-10-15 |
| recent | NATURALGAS | intraday | 50 | 7 | 2026-08-03 / 2026-10-21 |
| full | NIFTY | intraday | 445 | 8 | 2025-01-01 / 2026-10-22 |
| full | NIFTY | expiry dependencies | 188 | 4 | 2025-01-01 / 2026-10-19 |
| full | SENSEX | intraday | 435 | 19 | 2025-01-01 / 2026-10-22 |
| full | SENSEX | expiry dependencies | 185 | 7 | 2025-01-02 / 2026-10-22 |
| full | NATURALGAS | intraday | 64 | 37 | 2026-07-09 / 2026-10-29 |

Expiry Day coverage counts entry/exit dependency sessions. Expiry Intraday loads the ordinary eligible intraday window but enters only on dated expiry sessions. NATURALGAS has a shorter front-active sample. Repaired/model-dependent labels are retained; WEEK1 expiry identity is not verified. Max DD below is realized daily equity drawdown.

| Window | Instrument | Interval | Strategy | Net INR | Win % | Trades | Profit factor | Max DD INR | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| recent | NIFTY | 5m | `vwap_ema_pullback` | -19,031.30 | 31.48 | 54 | 0.5871 | 23,718.93 | modeled |
| recent | NIFTY | 5m | `orb_volume_breakout` | -28,461.26 | 27.27 | 55 | 0.4479 | 28,625.73 | modeled |
| recent | NIFTY | 5m | `supertrend_adx_trend` | -15,149.44 | 39.13 | 92 | 0.8007 | 40,262.35 | modeled |
| recent | NIFTY | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | NIFTY | 5m | `book_vertical_spread` | -10,533.63 | 29.73 | 37 | 0.2307 | 10,875.59 | modeled |
| recent | NIFTY | 5m | `book_iron_butterfly` | -11,058.64 | 16.67 | 42 | 0.1219 | 11,207.26 | modeled |
| recent | NIFTY | 5m | `book_strap_strip` | -2,044.43 | 40.00 | 20 | 0.9300 | 16,988.61 | modeled |
| recent | NIFTY | 5m | `book_vol_oi_momentum` | -23,112.93 | 34.67 | 75 | 0.6635 | 29,365.36 | modeled |
| recent | NIFTY | 5m | `expiry_day` | 9,197.93 | 77.78 | 9 | 2.7351 | 3,790.32 | modeled |
| recent | NIFTY | 5m | `expiry_intraday` | 3,209.50 | 58.33 | 12 | 1.3043 | 7,415.32 | modeled |
| recent | NIFTY | 15m | `vwap_ema_pullback` | -3,706.42 | 35.71 | 28 | 0.8190 | 13,931.22 | modeled |
| recent | NIFTY | 15m | `orb_volume_breakout` | -16,995.09 | 22.22 | 18 | 0.1639 | 16,995.09 | observed_strike_expiry_unverified |
| recent | NIFTY | 15m | `supertrend_adx_trend` | -552.33 | 39.68 | 63 | 0.9891 | 18,942.86 | modeled |
| recent | NIFTY | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | NIFTY | 15m | `book_vertical_spread` | -3,366.27 | 0.00 | 9 | 0.0000 | 3,366.27 | modeled |
| recent | NIFTY | 15m | `book_iron_butterfly` | -7,220.00 | 27.59 | 29 | 0.1271 | 7,582.56 | modeled |
| recent | NIFTY | 15m | `book_strap_strip` | -6,703.07 | 28.57 | 7 | 0.5271 | 8,311.89 | modeled |
| recent | NIFTY | 15m | `book_vol_oi_momentum` | -9,975.50 | 37.04 | 27 | 0.5804 | 16,363.65 | modeled |
| recent | NIFTY | 15m | `expiry_day` | 9,197.93 | 77.78 | 9 | 2.7351 | 3,790.32 | modeled |
| recent | NIFTY | 15m | `expiry_intraday` | 3,209.50 | 58.33 | 12 | 1.3043 | 7,415.32 | modeled |
| recent | SENSEX | 5m | `vwap_ema_pullback` | -11,293.79 | 43.06 | 72 | 0.7792 | 16,885.69 | modeled |
| recent | SENSEX | 5m | `orb_volume_breakout` | 17,152.56 | 52.78 | 72 | 1.5190 | 11,440.82 | modeled |
| recent | SENSEX | 5m | `supertrend_adx_trend` | 10,552.69 | 45.26 | 95 | 1.1748 | 15,894.95 | modeled |
| recent | SENSEX | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | SENSEX | 5m | `book_vertical_spread` | -3,611.38 | 40.54 | 37 | 0.4420 | 4,528.02 | modeled |
| recent | SENSEX | 5m | `book_iron_butterfly` | -512.42 | 52.08 | 48 | 0.8810 | 1,838.70 | modeled |
| recent | SENSEX | 5m | `book_strap_strip` | -3,677.55 | 29.41 | 17 | 0.8708 | 23,107.98 | modeled |
| recent | SENSEX | 5m | `book_vol_oi_momentum` | -610.15 | 41.18 | 85 | 0.9885 | 20,973.34 | modeled |
| recent | SENSEX | 5m | `expiry_day` | 6,293.53 | 63.64 | 11 | 1.3188 | 10,223.01 | modeled |
| recent | SENSEX | 5m | `expiry_intraday` | -57.55 | 45.45 | 11 | 0.9954 | 10,928.10 | modeled |
| recent | SENSEX | 15m | `vwap_ema_pullback` | -19,237.93 | 23.68 | 38 | 0.4122 | 25,217.24 | modeled |
| recent | SENSEX | 15m | `orb_volume_breakout` | 822.48 | 51.35 | 37 | 1.0411 | 12,605.78 | modeled |
| recent | SENSEX | 15m | `supertrend_adx_trend` | -11,970.46 | 37.66 | 77 | 0.7888 | 25,791.47 | modeled |
| recent | SENSEX | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | SENSEX | 15m | `book_vertical_spread` | -1,526.90 | 25.00 | 8 | 0.2346 | 1,526.90 | modeled |
| recent | SENSEX | 15m | `book_iron_butterfly` | 575.36 | 64.52 | 31 | 1.2334 | 631.52 | modeled |
| recent | SENSEX | 15m | `book_strap_strip` | -7,364.49 | 20.00 | 5 | 0.2739 | 7,364.49 | modeled |
| recent | SENSEX | 15m | `book_vol_oi_momentum` | 2,810.30 | 37.21 | 43 | 1.1141 | 12,792.59 | modeled |
| recent | SENSEX | 15m | `expiry_day` | 6,293.53 | 63.64 | 11 | 1.3188 | 10,223.01 | modeled |
| recent | SENSEX | 15m | `expiry_intraday` | -57.55 | 45.45 | 11 | 0.9954 | 10,928.10 | modeled |
| recent | NATURALGAS | 5m | `vwap_ema_pullback` | -30,025.73 | 0.00 | 22 | 0.0000 | 30,025.73 | observed |
| recent | NATURALGAS | 5m | `orb_volume_breakout` | -33,470.76 | 8.00 | 25 | 0.0333 | 33,470.76 | observed |
| recent | NATURALGAS | 5m | `supertrend_adx_trend` | -33,118.54 | 4.17 | 24 | 0.0153 | 33,118.54 | observed |
| recent | NATURALGAS | 5m | `mcx_evening_breakout` | -32,460.23 | 11.54 | 26 | 0.0661 | 32,460.23 | observed |
| recent | NATURALGAS | 5m | `book_vertical_spread` | -30,291.60 | 4.76 | 21 | 0.0167 | 30,291.60 | observed |
| recent | NATURALGAS | 5m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 5m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 5m | `book_vol_oi_momentum` | -29,651.21 | 4.55 | 22 | 0.0211 | 29,651.21 | observed |
| recent | NATURALGAS | 5m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 5m | `expiry_intraday` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `vwap_ema_pullback` | -25,536.56 | 7.14 | 14 | 0.0472 | 25,536.56 | observed |
| recent | NATURALGAS | 15m | `orb_volume_breakout` | -34,645.69 | 13.64 | 22 | 0.0839 | 34,645.69 | observed |
| recent | NATURALGAS | 15m | `supertrend_adx_trend` | -28,690.68 | 10.53 | 19 | 0.1132 | 29,229.31 | observed |
| recent | NATURALGAS | 15m | `mcx_evening_breakout` | -30,826.26 | 10.53 | 19 | 0.0793 | 30,826.26 | observed |
| recent | NATURALGAS | 15m | `book_vertical_spread` | -29,715.84 | 11.11 | 18 | 0.0676 | 29,715.84 | observed |
| recent | NATURALGAS | 15m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `book_vol_oi_momentum` | -31,025.50 | 13.64 | 22 | 0.1026 | 31,025.50 | observed |
| recent | NATURALGAS | 15m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `expiry_intraday` | -- | -- | -- | -- | -- | unsupported |
| full | NIFTY | 5m | `vwap_ema_pullback` | -97,037.08 | 36.97 | 376 | 0.7403 | 102,174.83 | repaired |
| full | NIFTY | 5m | `orb_volume_breakout` | -95,543.51 | 38.31 | 449 | 0.7757 | 111,797.49 | repaired |
| full | NIFTY | 5m | `supertrend_adx_trend` | -96,666.49 | 38.97 | 585 | 0.8468 | 101,084.10 | repaired |
| full | NIFTY | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | NIFTY | 5m | `book_vertical_spread` | -89,929.63 | 24.48 | 286 | 0.1892 | 90,024.08 | repaired |
| full | NIFTY | 5m | `book_iron_butterfly` | -53,286.44 | 41.99 | 412 | 0.4637 | 53,751.79 | repaired |
| full | NIFTY | 5m | `book_strap_strip` | -87,749.34 | 20.75 | 53 | 0.2589 | 92,162.80 | repaired |
| full | NIFTY | 5m | `book_vol_oi_momentum` | -97,122.02 | 38.68 | 393 | 0.7563 | 103,439.34 | repaired |
| full | NIFTY | 5m | `expiry_day` | 36,670.76 | 56.58 | 76 | 1.2003 | 57,164.84 | repaired |
| full | NIFTY | 5m | `expiry_intraday` | -87,998.53 | 38.10 | 84 | 0.5188 | 94,650.42 | repaired |
| full | NIFTY | 15m | `vwap_ema_pullback` | -15,863.50 | 38.22 | 191 | 0.9027 | 27,972.72 | repaired |
| full | NIFTY | 15m | `orb_volume_breakout` | -74,796.89 | 32.93 | 167 | 0.5343 | 74,796.89 | repaired |
| full | NIFTY | 15m | `supertrend_adx_trend` | -97,362.12 | 39.40 | 434 | 0.7883 | 101,197.71 | repaired |
| full | NIFTY | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | NIFTY | 15m | `book_vertical_spread` | -23,232.80 | 13.85 | 65 | 0.0973 | 23,232.80 | repaired |
| full | NIFTY | 15m | `book_iron_butterfly` | -42,353.68 | 41.58 | 291 | 0.4327 | 43,352.94 | repaired |
| full | NIFTY | 15m | `book_strap_strip` | -59,940.63 | 20.83 | 48 | 0.3618 | 59,940.63 | repaired |
| full | NIFTY | 15m | `book_vol_oi_momentum` | -25,985.59 | 38.96 | 231 | 0.8730 | 37,271.97 | repaired |
| full | NIFTY | 15m | `expiry_day` | 36,670.76 | 56.58 | 76 | 1.2003 | 57,164.84 | repaired |
| full | NIFTY | 15m | `expiry_intraday` | -87,998.53 | 38.10 | 84 | 0.5188 | 94,650.42 | repaired |
| full | SENSEX | 5m | `vwap_ema_pullback` | -40,472.43 | 40.59 | 542 | 0.9000 | 48,366.44 | modeled |
| full | SENSEX | 5m | `orb_volume_breakout` | 28,412.44 | 44.01 | 584 | 1.0725 | 26,618.95 | modeled |
| full | SENSEX | 5m | `supertrend_adx_trend` | -19,582.16 | 42.57 | 754 | 0.9682 | 70,948.15 | modeled |
| full | SENSEX | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | SENSEX | 5m | `book_vertical_spread` | -55,722.46 | 28.07 | 285 | 0.2304 | 56,639.10 | modeled |
| full | SENSEX | 5m | `book_iron_butterfly` | 8,472.25 | 63.69 | 504 | 1.1776 | 4,368.05 | modeled |
| full | SENSEX | 5m | `book_strap_strip` | -95,219.00 | 25.74 | 101 | 0.4698 | 95,219.00 | modeled |
| full | SENSEX | 5m | `book_vol_oi_momentum` | -56,766.11 | 39.63 | 704 | 0.8948 | 88,841.05 | modeled |
| full | SENSEX | 5m | `expiry_day` | 68,735.89 | 62.92 | 89 | 1.3628 | 56,927.40 | modeled |
| full | SENSEX | 5m | `expiry_intraday` | -55,423.32 | 39.74 | 78 | 0.5857 | 67,187.02 | modeled |
| full | SENSEX | 15m | `vwap_ema_pullback` | -16,058.38 | 37.39 | 238 | 0.9086 | 34,212.53 | modeled |
| full | SENSEX | 15m | `orb_volume_breakout` | -23,318.27 | 39.31 | 290 | 0.8726 | 35,097.67 | modeled |
| full | SENSEX | 15m | `supertrend_adx_trend` | -14,596.79 | 42.58 | 573 | 0.9671 | 50,166.75 | modeled |
| full | SENSEX | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | SENSEX | 15m | `book_vertical_spread` | -16,957.78 | 17.11 | 76 | 0.1320 | 16,997.57 | modeled |
| full | SENSEX | 15m | `book_iron_butterfly` | 13,529.01 | 69.75 | 324 | 1.4900 | 2,330.58 | modeled |
| full | SENSEX | 15m | `book_strap_strip` | -5,150.95 | 33.93 | 56 | 0.9289 | 15,950.64 | modeled |
| full | SENSEX | 15m | `book_vol_oi_momentum` | -16,119.02 | 37.38 | 313 | 0.9221 | 28,355.92 | modeled |
| full | SENSEX | 15m | `expiry_day` | 68,735.89 | 62.92 | 89 | 1.3628 | 56,927.40 | modeled |
| full | SENSEX | 15m | `expiry_intraday` | -55,423.32 | 39.74 | 78 | 0.5857 | 67,187.02 | modeled |
| full | NATURALGAS | 5m | `vwap_ema_pullback` | -31,897.22 | 0.00 | 23 | 0.0000 | 31,897.22 | observed |
| full | NATURALGAS | 5m | `orb_volume_breakout` | -34,160.17 | 4.17 | 24 | 0.0591 | 34,160.17 | observed |
| full | NATURALGAS | 5m | `supertrend_adx_trend` | -35,052.31 | 0.00 | 24 | 0.0000 | 35,052.31 | observed |
| full | NATURALGAS | 5m | `mcx_evening_breakout` | -34,285.43 | 0.00 | 23 | 0.0000 | 34,285.43 | observed |
| full | NATURALGAS | 5m | `book_vertical_spread` | -34,772.92 | 4.17 | 24 | 0.0146 | 34,772.92 | observed |
| full | NATURALGAS | 5m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 5m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 5m | `book_vol_oi_momentum` | -33,559.78 | 0.00 | 23 | 0.0000 | 33,559.78 | observed |
| full | NATURALGAS | 5m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 5m | `expiry_intraday` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `vwap_ema_pullback` | -28,380.26 | 6.25 | 16 | 0.0427 | 28,380.26 | observed |
| full | NATURALGAS | 15m | `orb_volume_breakout` | -36,340.55 | 9.52 | 21 | 0.0337 | 37,608.88 | observed |
| full | NATURALGAS | 15m | `supertrend_adx_trend` | -36,186.24 | 9.09 | 22 | 0.0372 | 36,186.24 | observed |
| full | NATURALGAS | 15m | `mcx_evening_breakout` | -34,143.92 | 17.39 | 23 | 0.1178 | 36,045.26 | observed |
| full | NATURALGAS | 15m | `book_vertical_spread` | -33,055.14 | 14.29 | 21 | 0.0810 | 33,814.36 | observed |
| full | NATURALGAS | 15m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `book_vol_oi_momentum` | -32,935.03 | 18.18 | 22 | 0.0942 | 32,935.03 | observed |
| full | NATURALGAS | 15m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `expiry_intraday` | -- | -- | -- | -- | -- | unsupported |

Expiry Intraday's recent results do not generalize to the full sample: full-history NIFTY and SENSEX runs are negative, with substantial drawdowns. The original Expiry Day results remain unchanged. Directional and MCX outcomes are reported without further parameter tuning; a positive gross ATR reward/risk does not guarantee net profitability after costs.
