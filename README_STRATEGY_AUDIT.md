# Strategy audit — Engine 5.4.0 (2026-11-01)

This audit supersedes valuation and results in `cfd9d90`. Engine 5.4.0 data
eligibility/repair rules, Data Lab, Data Quality, dated lot/fee schedules, saved
results, custom strategies, charts and ledgers are preserved. Run identities now
use `strategy-audit-v4`; old saved results remain available without being reused.

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
vertical stop fills. Impossible observed configurations still fail explicitly.

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
Long baskets reserve net debit plus fees. Expiry wing extrinsic is additionally
multiplied by remaining-session fraction on expiry, with exact quotes unchanged.

Save Result and named Save Strategy workflows remain supported for all nine
strategies. Virtual paper uses fresh real quotes, never wing models; actual
bid/ask remains the fill base where available, plus capped modeled slippage.
Paper entry caps and stop cooldowns follow the updated rules. Broker warm-up
currently provides resolved-contract activity, not a historical rolling ATM
chain; historical/live signal parity is therefore not asserted. Historical risk
is minute sampled; paper risk uses fresh ticks.

## Verification settings and interpretation

The full table covers nine strategies × three instruments × two intervals × two
windows: **108 combinations**, including unsupported cases. Recent means
2026-08-01 through 2026-10-21 inclusive. Full requests 2025-01-01 through
2026-10-31 inclusive; actual eligible coverage is reported below.

Each run starts at **₹1,00,000**, fixed one dated lot, 100% allocation, no
compounding, minimum premium zero, TP25% / SL25%, Target + SL, no trail, configured
slippage 0.50, volume multiple 1.3 and T=6. Daily locks are disabled. Futures
verification explicitly assumes **20% margin** to permit one NATURALGAS lot at
this capital. This is not a broker margin quote; the application's 100% default
is unchanged. Book futures retain ATR-capped stops. MCX uses observed prices;
indices permit disclosed continuity models. Expiry uses calendar BTST and 15:20
cutoff. Parameters were not tuned to improve these results.

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

**342 offline tests passed, zero external network attempts.** All 327 prior tests remain, with 15 added audit regressions. HTTP root and health on port 8501 return 200.

**84 runs completed, 20 combinations are unsupported, and four full-history butterfly runs failed on contradictory observed quotes.** Completed runs reconcile every fill, fee, allocation and P&L and have zero unresolved positions. The four failures publish no partial P&L and are not counted as passed backtests.

| Full butterfly run | First blocking observed pair | Strike width |
| --- | --- | ---: |
| NIFTY 5m | 2025-10-28 15:14 IST open: put pair 50.30 | 50 |
| NIFTY 15m | 2025-02-13 15:15 IST open: put pair 50.05 | 50 |
| SENSEX 5m | 2025-12-24 15:05 IST open: put pair 100.75 | 100 |
| SENSEX 15m | 2025-12-18 15:06 IST open: call pair 100.80 | 100 |

These exact observed pairs exceed the entire wing-width budget before assigning a nonnegative value to the modeled companion pair. Preserving exact quotes and the requested bound is mathematically incompatible on those bars. No observed price is overwritten, session silently omitted, or data eligibility rule relaxed. Completing these four runs requires corrected source quotes or an explicitly different observed-price policy.

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

Expiry coverage counts entry/exit dependency sessions, not every selected calendar day. NATURALGAS has a shorter front-active futures sample. Repaired means repaired-input provenance; modeled means rolling options/model dependence, not verified expiry identity.

| Window | Instrument | Interval | Strategy | Net INR | Win % | Trades | Profit factor | Max DD INR | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| recent | NIFTY | 5m | `vwap_ema_pullback` | -47,015.13 | 37.50 | 64 | 0.3588 | 47,015.13 | modeled |
| recent | NIFTY | 5m | `orb_volume_breakout` | -38,797.99 | 40.00 | 65 | 0.4219 | 40,164.51 | modeled |
| recent | NIFTY | 5m | `supertrend_adx_trend` | -89,260.30 | 42.86 | 98 | 0.3606 | 89,270.49 | modeled |
| recent | NIFTY | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | NIFTY | 5m | `book_vertical_spread` | -11,749.09 | 29.73 | 37 | 0.1787 | 11,996.60 | modeled |
| recent | NIFTY | 5m | `book_iron_butterfly` | -9,703.24 | 19.51 | 41 | 0.1398 | 9,851.86 | modeled |
| recent | NIFTY | 5m | `book_strap_strip` | -83,777.30 | 25.00 | 20 | 0.1220 | 83,777.30 | modeled |
| recent | NIFTY | 5m | `book_vol_oi_momentum` | -54,351.23 | 42.05 | 88 | 0.4616 | 61,010.62 | modeled |
| recent | NIFTY | 5m | `expiry_day` | -14,273.19 | 33.33 | 12 | 0.3599 | 14,273.19 | modeled |
| recent | NIFTY | 15m | `vwap_ema_pullback` | -38,144.43 | 30.77 | 39 | 0.2652 | 38,144.43 | modeled |
| recent | NIFTY | 15m | `orb_volume_breakout` | -27,990.36 | 22.22 | 27 | 0.1209 | 27,990.36 | modeled |
| recent | NIFTY | 15m | `supertrend_adx_trend` | -71,890.91 | 36.84 | 76 | 0.3434 | 71,890.91 | modeled |
| recent | NIFTY | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | NIFTY | 15m | `book_vertical_spread` | -2,738.07 | 22.22 | 9 | 0.0905 | 2,951.33 | modeled |
| recent | NIFTY | 15m | `book_iron_butterfly` | -6,261.16 | 32.14 | 28 | 0.1487 | 6,623.72 | modeled |
| recent | NIFTY | 15m | `book_strap_strip` | -55,747.16 | 14.29 | 7 | 0.0259 | 55,747.16 | modeled |
| recent | NIFTY | 15m | `book_vol_oi_momentum` | -41,196.05 | 33.33 | 33 | 0.1930 | 43,771.65 | modeled |
| recent | NIFTY | 15m | `expiry_day` | -14,273.19 | 33.33 | 12 | 0.3599 | 14,273.19 | modeled |
| recent | SENSEX | 5m | `vwap_ema_pullback` | -98,777.10 | 36.73 | 49 | 0.1232 | 98,777.10 | modeled |
| recent | SENSEX | 5m | `orb_volume_breakout` | -96,345.63 | 37.10 | 62 | 0.1826 | 99,194.87 | modeled |
| recent | SENSEX | 5m | `supertrend_adx_trend` | -98,339.46 | 30.19 | 53 | 0.1438 | 98,715.26 | modeled |
| recent | SENSEX | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | SENSEX | 5m | `book_vertical_spread` | -5,448.20 | 39.47 | 38 | 0.3015 | 6,171.98 | modeled |
| recent | SENSEX | 5m | `book_iron_butterfly` | -512.42 | 52.08 | 48 | 0.8810 | 1,838.70 | modeled |
| recent | SENSEX | 5m | `book_strap_strip` | -89,431.25 | 0.00 | 13 | 0.0000 | 89,431.25 | modeled |
| recent | SENSEX | 5m | `book_vol_oi_momentum` | -97,737.34 | 33.33 | 54 | 0.1577 | 97,737.34 | modeled |
| recent | SENSEX | 5m | `expiry_day` | -20,043.38 | 18.18 | 11 | 0.1821 | 20,043.38 | modeled |
| recent | SENSEX | 15m | `vwap_ema_pullback` | -93,950.15 | 19.44 | 36 | 0.0610 | 93,950.15 | modeled |
| recent | SENSEX | 15m | `orb_volume_breakout` | -53,624.72 | 41.03 | 39 | 0.2062 | 53,685.22 | modeled |
| recent | SENSEX | 15m | `supertrend_adx_trend` | -98,085.77 | 24.14 | 58 | 0.1194 | 98,085.77 | modeled |
| recent | SENSEX | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| recent | SENSEX | 15m | `book_vertical_spread` | -1,721.91 | 25.00 | 8 | 0.2137 | 1,721.91 | modeled |
| recent | SENSEX | 15m | `book_iron_butterfly` | 575.36 | 64.52 | 31 | 1.2334 | 631.52 | modeled |
| recent | SENSEX | 15m | `book_strap_strip` | -21,534.00 | 20.00 | 5 | 0.1186 | 21,534.00 | modeled |
| recent | SENSEX | 15m | `book_vol_oi_momentum` | -87,302.66 | 27.27 | 44 | 0.1417 | 92,526.74 | modeled |
| recent | SENSEX | 15m | `expiry_day` | -20,043.38 | 18.18 | 11 | 0.1821 | 20,043.38 | modeled |
| recent | NATURALGAS | 5m | `vwap_ema_pullback` | -29,090.34 | 38.89 | 18 | 0.3582 | 41,261.93 | observed |
| recent | NATURALGAS | 5m | `orb_volume_breakout` | -27,992.81 | 25.00 | 16 | 0.2968 | 30,762.22 | observed |
| recent | NATURALGAS | 5m | `supertrend_adx_trend` | -27,805.64 | 30.00 | 20 | 0.3263 | 31,325.03 | observed |
| recent | NATURALGAS | 5m | `mcx_evening_breakout` | -34,392.98 | 34.78 | 23 | 0.3258 | 34,392.98 | observed |
| recent | NATURALGAS | 5m | `book_vertical_spread` | -29,823.47 | 5.26 | 19 | 0.0408 | 29,823.47 | observed |
| recent | NATURALGAS | 5m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 5m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 5m | `book_vol_oi_momentum` | -30,649.82 | 4.55 | 22 | 0.0396 | 30,649.82 | observed |
| recent | NATURALGAS | 5m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `vwap_ema_pullback` | -14,052.77 | 46.15 | 13 | 0.3862 | 16,269.59 | observed |
| recent | NATURALGAS | 15m | `orb_volume_breakout` | -33,449.45 | 26.32 | 19 | 0.2337 | 33,449.45 | observed |
| recent | NATURALGAS | 15m | `supertrend_adx_trend` | -28,006.65 | 33.33 | 15 | 0.1472 | 30,776.06 | observed |
| recent | NATURALGAS | 15m | `mcx_evening_breakout` | -37,358.83 | 17.65 | 17 | 0.1292 | 37,358.83 | observed |
| recent | NATURALGAS | 15m | `book_vertical_spread` | -29,107.02 | 11.76 | 17 | 0.0872 | 29,107.02 | observed |
| recent | NATURALGAS | 15m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `book_vol_oi_momentum` | -30,182.71 | 15.00 | 20 | 0.1433 | 30,182.71 | observed |
| recent | NATURALGAS | 15m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |
| full | NIFTY | 5m | `vwap_ema_pullback` | -99,028.49 | 36.96 | 46 | 0.1932 | 99,622.17 | repaired |
| full | NIFTY | 5m | `orb_volume_breakout` | -99,710.94 | 28.85 | 52 | 0.1379 | 99,710.94 | repaired |
| full | NIFTY | 5m | `supertrend_adx_trend` | -99,227.24 | 38.98 | 59 | 0.1623 | 99,227.24 | repaired |
| full | NIFTY | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | NIFTY | 5m | `book_vertical_spread` | -91,213.84 | 29.12 | 285 | 0.1996 | 91,213.84 | repaired |
| full | NIFTY | 5m | `book_iron_butterfly` | -- | -- | -- | -- | -- | FAILED (quotes) |
| full | NIFTY | 5m | `book_strap_strip` | -92,905.75 | 26.09 | 23 | 0.1237 | 92,905.75 | repaired |
| full | NIFTY | 5m | `book_vol_oi_momentum` | -99,540.54 | 19.44 | 36 | 0.1040 | 99,540.54 | repaired |
| full | NIFTY | 5m | `expiry_day` | -96,614.31 | 14.63 | 41 | 0.1287 | 96,614.31 | repaired |
| full | NIFTY | 15m | `vwap_ema_pullback` | -98,910.87 | 40.00 | 95 | 0.2748 | 98,910.87 | repaired |
| full | NIFTY | 15m | `orb_volume_breakout` | -97,865.01 | 39.90 | 203 | 0.4418 | 98,209.69 | repaired |
| full | NIFTY | 15m | `supertrend_adx_trend` | -99,139.00 | 31.58 | 38 | 0.1219 | 102,364.49 | repaired |
| full | NIFTY | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | NIFTY | 15m | `book_vertical_spread` | -23,939.59 | 20.00 | 65 | 0.1003 | 24,152.85 | repaired |
| full | NIFTY | 15m | `book_iron_butterfly` | -- | -- | -- | -- | -- | FAILED (quotes) |
| full | NIFTY | 15m | `book_strap_strip` | -93,481.83 | 14.29 | 21 | 0.0822 | 93,481.83 | repaired |
| full | NIFTY | 15m | `book_vol_oi_momentum` | -98,777.20 | 43.48 | 138 | 0.3628 | 100,296.63 | repaired |
| full | NIFTY | 15m | `expiry_day` | -96,614.31 | 14.63 | 41 | 0.1287 | 96,614.31 | repaired |
| full | SENSEX | 5m | `vwap_ema_pullback` | -99,444.80 | 14.71 | 34 | 0.0468 | 99,444.80 | modeled |
| full | SENSEX | 5m | `orb_volume_breakout` | -99,466.23 | 21.43 | 28 | 0.0603 | 99,466.23 | modeled |
| full | SENSEX | 5m | `supertrend_adx_trend` | -99,443.94 | 22.58 | 31 | 0.0140 | 99,443.94 | modeled |
| full | SENSEX | 5m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | SENSEX | 5m | `book_vertical_spread` | -64,066.44 | 29.12 | 285 | 0.1782 | 64,790.22 | modeled |
| full | SENSEX | 5m | `book_iron_butterfly` | -- | -- | -- | -- | -- | FAILED (quotes) |
| full | SENSEX | 5m | `book_strap_strip` | -95,712.59 | 27.27 | 22 | 0.0934 | 95,712.59 | modeled |
| full | SENSEX | 5m | `book_vol_oi_momentum` | -99,517.12 | 31.82 | 44 | 0.0875 | 99,517.12 | modeled |
| full | SENSEX | 5m | `expiry_day` | -96,362.56 | 7.41 | 27 | 0.1070 | 96,362.56 | modeled |
| full | SENSEX | 15m | `vwap_ema_pullback` | -98,991.89 | 30.61 | 49 | 0.0840 | 98,991.89 | modeled |
| full | SENSEX | 15m | `orb_volume_breakout` | -99,403.93 | 30.91 | 55 | 0.1583 | 99,403.93 | modeled |
| full | SENSEX | 15m | `supertrend_adx_trend` | -99,374.76 | 22.22 | 36 | 0.0705 | 99,374.76 | modeled |
| full | SENSEX | 15m | `mcx_evening_breakout` | -- | -- | -- | -- | -- | unsupported |
| full | SENSEX | 15m | `book_vertical_spread` | -17,097.48 | 21.33 | 75 | 0.1361 | 17,137.27 | modeled |
| full | SENSEX | 15m | `book_iron_butterfly` | -- | -- | -- | -- | -- | FAILED (quotes) |
| full | SENSEX | 15m | `book_strap_strip` | -96,246.57 | 17.65 | 17 | 0.0317 | 96,246.57 | modeled |
| full | SENSEX | 15m | `book_vol_oi_momentum` | -99,389.78 | 26.67 | 45 | 0.0555 | 99,389.78 | modeled |
| full | SENSEX | 15m | `expiry_day` | -96,362.56 | 7.41 | 27 | 0.1070 | 96,362.56 | modeled |
| full | NATURALGAS | 5m | `vwap_ema_pullback` | -27,891.44 | 47.83 | 23 | 0.4415 | 41,261.93 | observed |
| full | NATURALGAS | 5m | `orb_volume_breakout` | -35,585.41 | 30.00 | 20 | 0.3706 | 45,594.74 | observed |
| full | NATURALGAS | 5m | `supertrend_adx_trend` | -35,946.11 | 38.10 | 21 | 0.3334 | 35,946.11 | observed |
| full | NATURALGAS | 5m | `mcx_evening_breakout` | -38,990.93 | 41.18 | 34 | 0.4753 | 51,899.37 | observed |
| full | NATURALGAS | 5m | `book_vertical_spread` | -35,804.86 | 9.09 | 22 | 0.0505 | 35,804.86 | observed |
| full | NATURALGAS | 5m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 5m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 5m | `book_vol_oi_momentum` | -33,114.79 | 14.81 | 27 | 0.0998 | 34,633.19 | observed |
| full | NATURALGAS | 5m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `vwap_ema_pullback` | -30,995.52 | 41.18 | 17 | 0.2222 | 30,995.52 | observed |
| full | NATURALGAS | 15m | `orb_volume_breakout` | -30,925.56 | 31.82 | 22 | 0.4263 | 45,854.63 | observed |
| full | NATURALGAS | 15m | `supertrend_adx_trend` | -38,228.83 | 30.00 | 10 | 0.1414 | 38,228.83 | observed |
| full | NATURALGAS | 15m | `mcx_evening_breakout` | -35,360.47 | 23.53 | 17 | 0.1514 | 36,997.43 | observed |
| full | NATURALGAS | 15m | `book_vertical_spread` | -33,575.61 | 15.00 | 20 | 0.1104 | 34,959.84 | observed |
| full | NATURALGAS | 15m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `book_vol_oi_momentum` | -34,074.60 | 19.05 | 21 | 0.1482 | 34,074.60 | observed |
| full | NATURALGAS | 15m | `expiry_day` | -- | -- | -- | -- | -- | unsupported |
