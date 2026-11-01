# Strategy audit — Engine 5.4.0 (2026-11-01)

This audit supersedes the execution valuation and results in `38e6751` (and earlier `cfd9d90`). Engine 5.4.0 data
eligibility/repair rules, Data Lab, Data Quality, dated lot/fee schedules, saved
results, custom strategies, charts and ledgers are preserved. Run identities now
use `stateful-execution-v5`; old saved results remain available without being reused.

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

Save Result and named Save Strategy workflows remain supported for all nine
strategies. Virtual paper uses fresh real quotes, never wing models; actual
bid/ask remains the fill base where available, plus capped modeled slippage.
Paper entry caps and stop cooldowns follow the updated rules. Broker warm-up
currently provides resolved-contract activity, not a historical rolling ATM
chain; historical/live signal parity is therefore not asserted. Historical risk
is minute sampled; paper risk uses fresh ticks.

## Futures risk and strategy defaults

For the nine public strategies on MCX, UI TP/SL percentages represent an assumed
5x margin return. Underlying target distance is
`min(entry * TP% / 500, 2.5 * entry_ATR14)`; stop distance is
`min(entry * SL% / 500, 1.5 * entry_ATR14)`. For a 220 entry, ATR 2 and TP25/SL15,
the target is 5 points and stop 3 points. New UI futures configurations reserve
20% assumed margin plus fees. This is a configurable research funding assumption,
not a dated broker margin schedule. Legacy research aliases retain their old rules.

At +1.2 entry ATR, futures lock +0.2 ATR and advance in 0.25 ATR steps. Directional
options and verticals activate a +10% lock after +18% gain, then add one configured
`trail_jump_pct` (default 5%) for each additional complete step. Explicit option
EOD mode retains its protective stop without this automatic lock. Stops are
monotonic and take effect on the next minute in backtests; paper uses subsequent
fresh ticks. These levels exclude fees and do not guarantee net profit or gap fills.
The activation levels were user-specified; lock offsets/steps are disclosed
implementation choices. Paper and historical futures share the risk helpers.

UI defaults for VWAP, ORB, Supertrend, vertical and momentum are TP30 / SL20.
Expiry Day defaults to TP25 / SL60, minimum premium 70 per leg and allocation 50%.
Butterfly and strap/strip retain TP25 / SL15. MCX evening defaults to TP25 / SL15.
Changing strategy applies its defaults; user edits and loaded saved parameters
remain explicit. Existing saved results are not rewritten.

## Verification settings and interpretation

The full table covers nine strategies × three instruments × two intervals × two
windows: **108 combinations**, including unsupported cases. Recent means
2026-08-01 through 2026-10-21 inclusive. Full requests 2025-01-01 through
2026-10-31 inclusive; actual eligible coverage is reported below.

Each run starts at **INR 100,000**, fixed one dated lot, no compounding,
Target + SL, configured slippage 0.50, volume multiple 1.3 and T=6. Daily account
locks are disabled. Directional keys use TP30/SL20 and the +18% stepped lock.
Butterfly, strap/strip and MCX evening use TP25/SL15. Expiry uses TP25/SL60,
minimum premium 70, 50% allocation, calendar BTST and 15:20 cutoff. Other runs
use 100% allocation and minimum premium zero for coverage (the index UI default
is 50). Futures use 20% margin and the new 5x/ATR risk policy. MCX uses observed
prices; indices permit disclosed continuity models. These fixed settings follow
the requested defaults; results are not a parameter search.

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

**358 offline tests passed, zero external network attempts.** All 342 prior tests remain, updated where requested exit semantics changed; 16 regressions cover continuity, expiry theta, futures/paper risk, projection, repairs and UI defaults. Streamlit root and health return HTTP 200 on port 8501.

**88 runs completed, 20 combinations are unsupported, zero failures.** Every completed run reconciles fill P&L, per-leg fees, slippage and allocation and has zero unresolved positions. All four previously failing full-history butterfly runs now complete. Data eligibility and exclusions remain unchanged.

| Full butterfly instrument | Interval | Trades with quote projection | Trades retaining repair calibration | Net INR | Trades |
| --- | --- | ---: | ---: | ---: | ---: |
| NIFTY | 5m | 0 | 1 | -53,286.44 | 412 |
| NIFTY | 15m | 0 | 1 | -42,353.68 | 291 |
| SENSEX | 5m | 1 | 0 | 8,472.25 | 504 |
| SENSEX | 15m | 0 | 0 | 13,529.01 | 324 |

Projection counts refer to trades, not modified lake records. Observed-only strict runs reject these adjustments. Repaired rows retain their provenance; neither those quotes nor WEEK1 identity become verified market data.

The new strategy defaults change holding periods, so the matrix does not encounter every original failing bar while a position is open. Regression fixtures independently cover all four reported pair overshoots; the counts above include only projections reached by the new runs.

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

Expiry coverage counts entry/exit dependency sessions. NATURALGAS has a shorter front-active futures sample. Repaired means repaired-input provenance; modeled means option model dependence, not verified expiry identity. Max DD below is realized daily equity drawdown, not worst intrabar loss.

| Window | Instrument | Interval | Strategy | Net INR | Win % | Trades | Profit factor | Max DD INR | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| recent | NIFTY | 5m | `vwap_ema_pullback` | -17,200.12 | 43.08 | 65 | 0.6060 | 23,043.61 | modeled |
| recent | NIFTY | 5m | `orb_volume_breakout` | -20,772.99 | 44.29 | 70 | 0.5798 | 22,158.79 | modeled |
| recent | NIFTY | 5m | `supertrend_adx_trend` | -17,761.10 | 50.00 | 102 | 0.7388 | 32,069.77 | modeled |
| recent | NIFTY | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | NIFTY | 5m | `book_vertical_spread` | -9,894.31 | 27.78 | 36 | 0.1725 | 10,141.82 | modeled |
| recent | NIFTY | 5m | `book_iron_butterfly` | -11,058.64 | 16.67 | 42 | 0.1219 | 11,207.26 | modeled |
| recent | NIFTY | 5m | `book_strap_strip` | -2,044.43 | 40.00 | 20 | 0.9300 | 16,988.61 | modeled |
| recent | NIFTY | 5m | `book_vol_oi_momentum` | -23,616.09 | 46.07 | 89 | 0.6221 | 33,634.36 | modeled |
| recent | NIFTY | 5m | `expiry_day` | 9,197.93 | 77.78 | 9 | 2.7351 | 3,790.32 | modeled |
| recent | NIFTY | 15m | `vwap_ema_pullback` | -4,649.15 | 45.00 | 40 | 0.7982 | 15,296.58 | modeled |
| recent | NIFTY | 15m | `orb_volume_breakout` | -18,892.37 | 29.63 | 27 | 0.1741 | 19,112.06 | observed_strike_expiry_unverified |
| recent | NIFTY | 15m | `supertrend_adx_trend` | -9,278.39 | 50.63 | 79 | 0.8096 | 18,602.72 | modeled |
| recent | NIFTY | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | NIFTY | 15m | `book_vertical_spread` | -3,278.00 | 0.00 | 9 | 0.0000 | 3,278.00 | modeled |
| recent | NIFTY | 15m | `book_iron_butterfly` | -7,220.00 | 27.59 | 29 | 0.1271 | 7,582.56 | modeled |
| recent | NIFTY | 15m | `book_strap_strip` | -6,703.07 | 28.57 | 7 | 0.5271 | 8,311.89 | modeled |
| recent | NIFTY | 15m | `book_vol_oi_momentum` | -9,958.26 | 45.45 | 33 | 0.5576 | 13,859.82 | modeled |
| recent | NIFTY | 15m | `expiry_day` | 9,197.93 | 77.78 | 9 | 2.7351 | 3,790.32 | modeled |
| recent | SENSEX | 5m | `vwap_ema_pullback` | -10,007.39 | 50.67 | 75 | 0.7894 | 16,565.62 | modeled |
| recent | SENSEX | 5m | `orb_volume_breakout` | 15,182.55 | 61.84 | 76 | 1.4935 | 8,644.68 | modeled |
| recent | SENSEX | 5m | `supertrend_adx_trend` | 14,888.00 | 60.82 | 97 | 1.3093 | 9,212.03 | modeled |
| recent | SENSEX | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | SENSEX | 5m | `book_vertical_spread` | -5,183.71 | 28.95 | 38 | 0.2493 | 5,522.87 | modeled |
| recent | SENSEX | 5m | `book_iron_butterfly` | -512.42 | 52.08 | 48 | 0.8810 | 1,838.70 | modeled |
| recent | SENSEX | 5m | `book_strap_strip` | -3,677.55 | 29.41 | 17 | 0.8708 | 23,107.98 | modeled |
| recent | SENSEX | 5m | `book_vol_oi_momentum` | -6,031.09 | 48.86 | 88 | 0.8742 | 17,165.80 | modeled |
| recent | SENSEX | 5m | `expiry_day` | 6,293.53 | 63.64 | 11 | 1.3188 | 10,223.01 | modeled |
| recent | SENSEX | 15m | `vwap_ema_pullback` | -17,908.97 | 39.47 | 38 | 0.3431 | 22,190.76 | modeled |
| recent | SENSEX | 15m | `orb_volume_breakout` | -417.92 | 61.54 | 39 | 0.9772 | 11,662.83 | modeled |
| recent | SENSEX | 15m | `supertrend_adx_trend` | -12,330.57 | 49.38 | 81 | 0.7470 | 21,295.40 | modeled |
| recent | SENSEX | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | SENSEX | 15m | `book_vertical_spread` | -1,526.90 | 25.00 | 8 | 0.2346 | 1,526.90 | modeled |
| recent | SENSEX | 15m | `book_iron_butterfly` | 575.36 | 64.52 | 31 | 1.2334 | 631.52 | modeled |
| recent | SENSEX | 15m | `book_strap_strip` | -7,364.49 | 20.00 | 5 | 0.2739 | 7,364.49 | modeled |
| recent | SENSEX | 15m | `book_vol_oi_momentum` | -1,610.67 | 40.91 | 44 | 0.9335 | 14,984.45 | modeled |
| recent | SENSEX | 15m | `expiry_day` | 6,293.53 | 63.64 | 11 | 1.3188 | 10,223.01 | modeled |
| recent | NATURALGAS | 5m | `vwap_ema_pullback` | -30,772.86 | 0.00 | 22 | 0.0000 | 30,772.86 | observed |
| recent | NATURALGAS | 5m | `orb_volume_breakout` | -34,078.28 | 11.54 | 26 | 0.0664 | 34,078.28 | observed |
| recent | NATURALGAS | 5m | `supertrend_adx_trend` | -33,633.08 | 4.35 | 23 | 0.0187 | 33,633.08 | observed |
| recent | NATURALGAS | 5m | `mcx_evening_breakout` | -32,356.49 | 12.00 | 25 | 0.0421 | 32,356.49 | observed |
| recent | NATURALGAS | 5m | `book_vertical_spread` | -29,558.72 | 10.00 | 20 | 0.0493 | 29,558.72 | observed |
| recent | NATURALGAS | 5m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 5m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 5m | `book_vol_oi_momentum` | -30,492.51 | 8.33 | 24 | 0.0478 | 30,492.51 | observed |
| recent | NATURALGAS | 5m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `vwap_ema_pullback` | -27,533.98 | 0.00 | 14 | 0.0000 | 27,533.98 | observed |
| recent | NATURALGAS | 15m | `orb_volume_breakout` | -35,552.02 | 10.00 | 20 | 0.0078 | 35,552.02 | observed |
| recent | NATURALGAS | 15m | `supertrend_adx_trend` | -34,477.45 | 0.00 | 17 | 0.0000 | 34,477.45 | observed |
| recent | NATURALGAS | 15m | `mcx_evening_breakout` | -30,326.26 | 15.79 | 19 | 0.0523 | 30,326.26 | observed |
| recent | NATURALGAS | 15m | `book_vertical_spread` | -28,591.05 | 5.56 | 18 | 0.0006 | 28,591.05 | observed |
| recent | NATURALGAS | 15m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `book_vol_oi_momentum` | -29,807.66 | 15.00 | 20 | 0.0531 | 29,807.66 | observed |
| recent | NATURALGAS | 15m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |
| full | NIFTY | 5m | `vwap_ema_pullback` | -85,854.95 | 48.42 | 568 | 0.7956 | 91,836.99 | repaired |
| full | NIFTY | 5m | `orb_volume_breakout` | -96,986.30 | 47.45 | 548 | 0.7551 | 105,635.73 | repaired |
| full | NIFTY | 5m | `supertrend_adx_trend` | -98,711.25 | 49.54 | 652 | 0.8261 | 98,711.25 | repaired |
| full | NIFTY | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | NIFTY | 5m | `book_vertical_spread` | -84,481.73 | 25.78 | 287 | 0.1707 | 84,481.73 | repaired |
| full | NIFTY | 5m | `book_iron_butterfly` | -53,286.44 | 41.99 | 412 | 0.4637 | 53,751.79 | repaired |
| full | NIFTY | 5m | `book_strap_strip` | -87,749.34 | 20.75 | 53 | 0.2589 | 92,162.80 | repaired |
| full | NIFTY | 5m | `book_vol_oi_momentum` | -97,722.52 | 47.49 | 577 | 0.7707 | 98,156.64 | repaired |
| full | NIFTY | 5m | `expiry_day` | 36,670.76 | 56.58 | 76 | 1.2003 | 57,164.84 | repaired |
| full | NIFTY | 15m | `vwap_ema_pullback` | -27,535.11 | 48.78 | 246 | 0.8252 | 48,967.66 | repaired |
| full | NIFTY | 15m | `orb_volume_breakout` | -90,814.18 | 39.69 | 257 | 0.4882 | 90,814.18 | repaired |
| full | NIFTY | 15m | `supertrend_adx_trend` | -97,679.46 | 49.74 | 587 | 0.7939 | 100,466.51 | repaired |
| full | NIFTY | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | NIFTY | 15m | `book_vertical_spread` | -22,287.62 | 12.31 | 65 | 0.0902 | 22,287.62 | repaired |
| full | NIFTY | 15m | `book_iron_butterfly` | -42,353.68 | 41.58 | 291 | 0.4327 | 43,352.94 | repaired |
| full | NIFTY | 15m | `book_strap_strip` | -59,940.63 | 20.83 | 48 | 0.3618 | 59,940.63 | repaired |
| full | NIFTY | 15m | `book_vol_oi_momentum` | -20,743.92 | 49.39 | 328 | 0.8961 | 25,876.14 | repaired |
| full | NIFTY | 15m | `expiry_day` | 36,670.76 | 56.58 | 76 | 1.2003 | 57,164.84 | repaired |
| full | SENSEX | 5m | `vwap_ema_pullback` | -7,837.86 | 51.15 | 565 | 0.9768 | 28,197.10 | modeled |
| full | SENSEX | 5m | `orb_volume_breakout` | 22,811.10 | 54.59 | 621 | 1.0644 | 52,707.21 | modeled |
| full | SENSEX | 5m | `supertrend_adx_trend` | -24,372.20 | 52.77 | 775 | 0.9541 | 62,091.51 | modeled |
| full | SENSEX | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | SENSEX | 5m | `book_vertical_spread` | -57,136.75 | 21.68 | 286 | 0.1733 | 57,475.91 | modeled |
| full | SENSEX | 5m | `book_iron_butterfly` | 8,472.25 | 63.69 | 504 | 1.1776 | 4,368.05 | modeled |
| full | SENSEX | 5m | `book_strap_strip` | -95,219.00 | 25.74 | 101 | 0.4698 | 95,219.00 | modeled |
| full | SENSEX | 5m | `book_vol_oi_momentum` | -75,675.98 | 48.27 | 723 | 0.8428 | 92,108.48 | modeled |
| full | SENSEX | 5m | `expiry_day` | 68,735.89 | 62.92 | 89 | 1.3628 | 56,927.40 | modeled |
| full | SENSEX | 15m | `vwap_ema_pullback` | -26,682.72 | 47.52 | 242 | 0.8237 | 33,676.53 | modeled |
| full | SENSEX | 15m | `orb_volume_breakout` | -17,659.49 | 49.84 | 309 | 0.8934 | 35,411.82 | modeled |
| full | SENSEX | 15m | `supertrend_adx_trend` | -17,074.61 | 52.61 | 595 | 0.9558 | 43,805.83 | modeled |
| full | SENSEX | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | SENSEX | 15m | `book_vertical_spread` | -16,416.60 | 11.84 | 76 | 0.1031 | 16,456.39 | modeled |
| full | SENSEX | 15m | `book_iron_butterfly` | 13,529.01 | 69.75 | 324 | 1.4900 | 2,330.58 | modeled |
| full | SENSEX | 15m | `book_strap_strip` | -5,150.95 | 33.93 | 56 | 0.9289 | 15,950.64 | modeled |
| full | SENSEX | 15m | `book_vol_oi_momentum` | -32,141.50 | 43.71 | 334 | 0.8381 | 39,548.22 | modeled |
| full | SENSEX | 15m | `expiry_day` | 68,735.89 | 62.92 | 89 | 1.3628 | 56,927.40 | modeled |
| full | NATURALGAS | 5m | `vwap_ema_pullback` | -32,131.01 | 4.17 | 24 | 0.0158 | 32,131.01 | observed |
| full | NATURALGAS | 5m | `orb_volume_breakout` | -34,232.31 | 5.00 | 20 | 0.0075 | 34,232.31 | observed |
| full | NATURALGAS | 5m | `supertrend_adx_trend` | -36,095.61 | 0.00 | 21 | 0.0000 | 36,095.61 | observed |
| full | NATURALGAS | 5m | `mcx_evening_breakout` | -36,269.06 | 4.17 | 24 | 0.0174 | 36,269.06 | observed |
| full | NATURALGAS | 5m | `book_vertical_spread` | -35,539.09 | 13.04 | 23 | 0.0512 | 35,539.09 | observed |
| full | NATURALGAS | 5m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 5m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 5m | `book_vol_oi_momentum` | -32,537.61 | 12.50 | 24 | 0.0381 | 33,555.99 | observed |
| full | NATURALGAS | 5m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `vwap_ema_pullback` | -28,535.72 | 0.00 | 14 | 0.0000 | 28,535.72 | observed |
| full | NATURALGAS | 15m | `orb_volume_breakout` | -35,360.72 | 10.00 | 20 | 0.0476 | 37,129.05 | observed |
| full | NATURALGAS | 15m | `supertrend_adx_trend` | -37,828.13 | 9.52 | 21 | 0.0568 | 37,828.13 | observed |
| full | NATURALGAS | 15m | `mcx_evening_breakout` | -32,392.48 | 26.09 | 23 | 0.0998 | 33,418.80 | observed |
| full | NATURALGAS | 15m | `book_vertical_spread` | -33,805.02 | 9.52 | 21 | 0.0294 | 34,814.24 | observed |
| full | NATURALGAS | 15m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `book_vol_oi_momentum` | -33,889.03 | 11.76 | 17 | 0.0463 | 33,889.03 | observed |
| full | NATURALGAS | 15m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |

The requested Expiry Day configuration is positive in both recent and full samples under these assumptions, with substantial full-window drawdowns. Many directional and butterfly runs remain negative after costs. The execution fixes do not establish a profitable live strategy, and these descriptive samples are not untouched out-of-sample tests.
