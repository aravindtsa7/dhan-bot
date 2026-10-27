# Step 4: observed signals, complete simulated accounting, explicit model risk

```powershell
.venv\Scripts\python run_strategy_research.py
.venv\Scripts\python run_strategy_research.py --report-only
.venv\Scripts\python verify_quant_research.py
.venv\Scripts\python -m streamlit run quant_ui.py
```

The default research window remains **2025-01-01 through 2026-10-22**, inclusive. The new grid contains **252 configurations** (108 each for Nifty/Sensex and 12 each for Crude Oil, Natural Gas and Gold Mini). Earlier Step 3 run directories are retained; the current leaderboard includes only the current engine version.

## What zero unresolved trades means

The original engine already searched all three same-side moneyness streams for the **entered strike**. The 373 unresolved positions were cases where that strike was absent from all three. A spot-rounded strike cannot recover its missing market price, and matching a strike does not recover its expiry ID.

`Config.price_policy` has two explicit modes:

- `model_continuity` (the requested completion mode): use observed fixed-strike candles whenever available, otherwise use the continuity model below. Every entered position must reach a priced stop, target, or square-off. All costs and P&L for those positions enter the results. Every estimated bar and trade is tagged. A required model input failure rejects the run; it never silently censors or drops the position.
- `strict_observed`: if an open position loses its fixed-strike observation, the **entire run fails** and is not ranked. It does not look ahead to reject entry opportunities whose future prices are missing, liquidate retroactively at a last-known quote, or silently discard losing/incomplete trades.

Thus `unresolved_trades == 0` is an accounting invariant on completed runs. It is **not** evidence that modeled prices are historical quotes, that the strategy is profitable in reality, or that the rolling contract expiry has been verified.

`entry_strike` comes only from observed option metadata. Entry prices are never synthesized. Signals and option VWAP/volume/OI never use modeled candles.

## Continuity model

`quant_option_model.py` retains the entered strike, finds the closest same-side observed strike with both current and previous-minute candles and valid **lagged** IV, and estimates delta/gamma with a zero-rate European approximation and calendar-inferred nearest expiry. The option IV unit is explicitly percent.

For each modeled underlying/neighbor OHLC pair:

1. Compute the entered strike's exact intrinsic change from the observed underlying price.
2. Compute its delta/gamma total-price change, subtracting intrinsic change to isolate the extrinsic approximation.
3. Measure the neighboring option's residual price change after its delta/gamma change, and scale that residual by a bounded vega ratio.
4. Add the extrinsic changes to the previous observed/modeled extrinsic value; floor extrinsic at zero and bound total value by intrinsic and standard option bounds.

This provides continuity, including a proxy for theta/IV drift. **It does not recover actual missing OHLC or execution ordering.** The synthetic high/low envelope pairs extrema by call/put direction; those extrema need not have occurred simultaneously. The model can accumulate error over long missing intervals, especially near expiry. Returning observed quotes supersedes the model and can produce jumps.

`stop_paths.parquet` includes observed/model OHLC, `price_source`, `entry_strike`, neighboring strike, lagged IV and assumed expiry. Trade logs include modeled bar counts, `used_modeled_prices`, `exit_price_source`, and verification status. The UI draws modeled candles in purple.

`audit_option_research.py` deliberately withholds observed target-strike candles and predicts them using **other** strikes. MAE/RMSE are saved per index in `option_data_audit.json`; sampled predictions are in `model_holdout_*.parquet`. These are one-step diagnostics, not validation of long gaps or a guarantee of executable fills.

## True option VWAP and confirmations

VWAP is `cumulative((high + low + close)/3 × volume) / cumulative(volume)`, accumulated **per session, side and fixed strike**. It never combines premiums from different strikes. H1 requires continuous observed coverage from 09:15 through its signal timestamp, so a partial-session VWAP cannot silently stand in for the full session. The UI may display an observed-history VWAP for other trades and labels the coverage limitation.

Spot EMA, Supertrend and ATR remain on spot. The zero-volume index chart's reference line is labelled **TWAP proxy**, separate from option VWAP.

Option volume confirmation compares the last completed one-minute candle against the preceding 20 observed minutes of that same strike, requiring at least 1.5× mean volume. OI confirmation requires a configurable nonzero five-minute change (default 0.5%): rising, unwinding, or either. OI is not interpreted as proof of buying vs writing; price/trend alignment supplies separate confirmation. Missing history skips the signal causally.

## H1–H4 and execution

- **H1 Option_VWAP_OI_Momentum:** spot EMA9/EMA21 and Supertrend alignment, ADX ≥20, ATR at least the prior 20-bar mean, complete fixed-strike option VWAP, close above that VWAP and the preceding N completed signal bars' option high, volume and OI confirmation. Sweeps 3m/5m/15m, ATM±1/ATM, two risk templates and rising/either OI.
- **H2 Dual_Leg_Straddle_Gamma_Breakout:** fixed ATM CE/PE anchor strikes at 09:25, 09:30 or 13:30; afternoon anchors apply only on calendar expiry sessions. Spot breaks the pre-anchor compression range, the chosen leg closes ≥18% or ≥25% above its anchor, and volume/OI/ADX/ATR confirm. Entry occurs at the next minute open: using a candle's final volume to fill at its earlier high touch would look ahead. At most one entry per side and two per day; this is the directional breakout variant, not a simultaneous long straddle. Step trailing manages the chosen leg.
- **H3 Morning_ORB_Retest_Sniper:** 09:15–09:30 spot range, a breakout on an earlier completed bar, then directional retest/rejection and a fixed-strike option opening-high breakout with volume/OI confirmation. Entry 09:30–11:15, at most one per day. A +15% premium excursion moves the stop to entry, active next minute; this is premium breakeven, not net-of-cost breakeven. Intrabar sequencing is not known from minute OHLC.
- **H4 MCX_US_Open_Inventory_Breakout:** the 17:00–17:30 range must be compressed relative to 16:30–17:00; breakout, ADX and ATR expansion confirm. Entry strictly 17:30–21:30, one per evening, ATR stop/target and stepped ATR trailing. The name does **not** imply an inventory-release calendar filter: no timestamped inventory calendar is supplied.

No index entry is allowed in `[11:30, 13:15)` IST. The H3/H4 one-trade cap is enforced even if the UI requests two. Ordinary signals fill next minute; ambiguous stop/target collisions use stop first. Trailing stops move in whole jumps: `floor(favorable excursion / jump) × jump`, with the configured trailing distance and no retreat. Overshoot is retained for the next complete jump. Changes affect the next minute; minute OHLC cannot reproduce tick-level broker sequencing. Dhan documents the [trailingJump price increment](https://dhanhq.co/docs/v2/super-order/); exact live fill parity is not claimed. Mandatory square-offs use 15:20 and 23:15 opens.

Fills include adverse configurable slippage, then adverse tick rounding (options ₹0.05; Crude Oil/Gold Mini ₹1; Natural Gas ₹0.10; overridable). Logs reconcile `gross_before_slippage - slippage_cost - total_cost = net_pnl`; tick rounding is included in the measured slippage effect. Brokerage is ₹20/order, with STT/CTT, exchange, SEBI/IPFT, GST and stamp as documented in the Step 3 fee model. Model values are not rounded into apparent observed quotes.

Lot quantities now use the **inferred expiry** to handle the January 2025 monthly-contract transition and December 31, 2025 trades in January 2026 contracts. This improves the schedule approximation, but missing expiry identity still prevents claiming exact historical quantities for every rolling candle.

## Dated expiry calendar and retrospective evidence

`quant_calendar.py` records source URLs and daytime holidays, exporting `expiry_calendar.csv` and `expiry_calendar_sources.json`:

- Nifty: Thursday through August 2025, Tuesday from September 2025.
- Sensex: the January 3, 2025 Friday contract, then Tuesday through August 2025, Thursday from September 2025.
- Scheduled expiries on holidays move to the preceding regular trading session, including the January 15, 2026 election closure and October 20, 2026 holiday.

Primary references: [NSE dated expiry circular](https://nsearchives.nseindia.com/content/circulars/FAOP68747.pdf), [BSE January change](https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=20241128-70), [BSE September change](https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=20250623-59), [NSE 2025 holidays](https://nsearchives.nseindia.com/content/circulars/FAOP65588.pdf), [2026 holidays](https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf), [January 2026 addendum](https://nsearchives.nseindia.com/content/circulars/CMTR72260.pdf), [BSE election notice](https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=20260112-8). The BSE regular holiday list is aligned to the NSE calendar and BSE addendum; this assumption is recorded. Exceptional exchange changes beyond these sources require updating the calendar. No data-driven expiry weekday is invented.

Expiry diagnostics compare morning vs 15:20–15:29 ATM extrinsic and IV. An empirical-support flag requires average EOD extrinsic ≤max(₹5, 25% of morning extrinsic) and EOD/morning IV <0.75. This heuristic is **retrospective corroboration only**. Failure does not overwrite the calendar; success does not prove rolling-contract expiry. These diagnostics never determine intraday signals, quantities, or prices. Source/calendar changes alter run IDs.

## Leaderboards and verification scope

`leaderboard.csv` contains all completed current-engine research runs, with zero unresolved counts and explicit modeled-trade counts. `verified_leaderboard.csv` contains only observed fixed-contract-price runs with reconciled simulated accounting. Missing option expiry IDs mean index-option runs cannot qualify for this latter set even if all their prices happened to be observed.

`top15_verified.csv` and `top10.csv` contain up to 15/10 **profitable qualifying** configurations. They may have fewer rows or only headers. The terminal prints the actual count and never pads the table with losing, zero-trade, or modeled configurations. `top15_research.csv` separately shows the best research configurations, including explicitly unverified/model-dependent options.

“Verified-price” is deliberately narrow: observed fixed-contract candles and accounting checks. It does not verify liquidity, depth, real broker fills, margin, contract-note aggregation, future performance, or statistical significance. Costs remain configurable research assumptions. MCX has only the active-contract history actually available in the lake, not all of 2025–2026.

Metrics include all entered trades, fee/slippage breakdowns, daily realized drawdown, Sharpe, ROI, profit factor and yearly 2025/2026 net P&L. Realized daily drawdown is not intraday mark-to-market risk. The whole-window grid is in-sample; the 2026 column is a descriptive split, not an untouched validation set. No minimum profitable count is promised and no parameters are tuned after observing the leaderboard.

`verify_quant_research.py` checks every run's accounting identities, finite fills, complete closure, strike retention, daily caps, entry windows, modeled-bar counts, price-source eligibility, and all UI tabs/filters. `test_quant_backtester.py` checks dated expiry transitions, fixed-strike VWAP completeness, future-data invariance, model tagging and strict failure, stop handling and breakeven timing.

## Completed run evidence (engine 4.1.0)

- 252/252 configurations completed; 1,648 simulated trade instances across configurations; **zero unresolved**. These are not unique trades across the grid.
- 89 trade instances used 1,140 modeled candles and remain explicitly unverified. All 39 unit tests, per-run artifact reconciliation, the four-tab UI checks and a custom-form smoke test passed.
- Four profitable configurations qualified for the observed-price/accounting-checked table, all Crude Oil H4 5m variants. The leader (`efd396632a49587d`) used ATR stop 2.0, reward/risk 2.5, compression ratio 1.0, one lot, one trade/day, ₹1-point slippage per fill, and trailing enabled. Net ₹15,485.32, 34 trades, 47.06% win rate, PF 1.49, realized max drawdown ₹10,291.27, Sharpe 1.67.
- The winning commodity sample covers **2026-07-22–2026-10-22**, 65 eligible sessions. Its 2025 P&L aggregate of zero means **no 2025 source coverage**, not a tested flat year. None of the MCX samples establish full 2025 history.
- 158 configurations generated no qualifying trades. H3 generated no trades in either index under these filters. Positive-path fixtures confirm it can trigger; filters were not loosened after observing the results.
- One-minute pseudo-missing prediction MAE/RMSE: Nifty 0.656/1.408 points (9,996 observations); Sensex 2.152/5.250 points (9,669 observations). These errors do not validate long extrapolations.
- Empirical extrinsic/IV crush corroborated 56/86 eligible Nifty calendar-expiry sessions and 61/93 Sensex sessions. It was never substituted for the dated calendar or used as an intraday signal.

Inspect `data_lake/backtests/verification.json`, `research_summary.json`, `top15_verified.csv`, `top15_research.csv`, `leaderboard.csv` and `option_data_audit.json` for the corresponding saved evidence. `top10.csv` intentionally contains only the four qualifying profitable configurations.
