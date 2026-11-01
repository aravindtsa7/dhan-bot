# Book strategies in Engine 5.4.0

The 2026-11-01 deep audit supersedes the results and flat-delta valuation in
`38e6751` and `cfd9d90`. The complete methodology, coverage, all-nine-strategy comparison and
reproduction commands are in [the strategy audit](README_STRATEGY_AUDIT.md).

The four strategies remain research adaptations of Kakushadze & Serur's
*151 Trading Strategies*, following the user-specified section/equation numbering.

| Key | Book structure | Updated implementation |
| --- | --- | --- |
| `book_vertical_spread` | Bull call / bear put, sections 2.6/2.9 | Fresh EMA9 pullback/cross, rising ADX >=18, directional body, VWAP/EMA alignment and rolling ATM activity expansion; two entries and three-bar stop cooldown. MCX remains directional futures with ATR-capped risk. |
| `book_iron_butterfly` | Credit butterfly, section 2.44 | Equal wings, sideways ADX/VWAP regime after 09:45, combined target/stop and 15:15 exit. Reserve full wing width plus round-trip fees, separately from width-minus-credit payoff loss. |
| `book_strap_strip` | 2CE:1PE / 1CE:2PE, sections 2.34/2.35 | Compressed first 30-minute range (<0.65%), fresh breakout, valid ADX >=20, option activity >1.4x baseline, one entry before 13:30. |
| `book_vol_oi_momentum` | Volatility-scaled activity signal, sections 10.3.1/10.4 | Default T=6, T+2 warm-up, active rolling ATM volume/OI history. Entry still resolves a fixed strike at signal close. |

Zero-volume index VWAP uses synchronized observed ATM CE+PE volume. Indicator
warm-up carries only across consecutive eligible sessions of the same contract.
Unavailable activity remains disclosed; no data eligibility/repair rule is relaxed.

Missing vertical/butterfly legs now use an entry-anchored smooth moneyness curve,
0.28-to-0.42 delta, local observed extrinsic scale and a favorable intrinsic-aware
floor. Latest exact observations recalibrate the level; pending fills use only
the next open. Vertical pre-slippage values remain within [0.05, width-0.05].
Butterflies retain a one-wing liability budget and positive legs. Small observed
pair overshoots (up to 2% of width beyond the cap) are projected and tagged;
strict-observed rejects such projections. Entirely repair-tagged bands retain
prior calibration, never using repairs as observed anchors. This is a disclosed sigmoid research model, not recovered IV.

Single-leg momentum options and strap/strip now retain last price/spot state
outside ATM+/-1. Smooth moneyness delta, per-minute theta, an intrinsic floor and
a movement-bounded wing blend prevent boundary time-value collapse. Exact quotes
recalibrate the state. No modeled prices are used for entry.

MCX exits convert UI percentages at 5x leverage, capped at 2.5 ATR target / 1.5 ATR
stop. A stepped lock starts at +1.2 ATR. Directional options/verticals lock +10%
after +18% gain, then step by 5%; explicit EOD mode retains its fixed protection.

Basket slippage is capped per leg per fill at
`min(configured, 0.50, max(0.05, nearest_tick(1.25% of raw premium)))`.
Full brokerage, STT/CTT, GST, exchange, stamp and other configured fees remain on
every leg. All fills, quantities, raw values and costs reconcile to the basket.
All nine strategy selectors, Save Result, named Save Strategy and virtual paper
workflows remain available. Live paper uses actual quotes rather than wing models;
broker historical activity remains resolved-contract rather than rolling-chain.

## Updated results

The complete 108-row recent/full comparison (Net INR, Win %, Trades, Profit
Factor and Max Drawdown) is in [the audit results](README_STRATEGY_AUDIT.md#audit-results).
Settings are INR 100,000, fixed one dated lot, 5m/15m, 100% allocation,
minimum premium zero and 20% assumed futures margin. Vertical and momentum use
TP30/SL20; butterfly and strap/strip use TP25/SL15. Results include all fees,
slippage and the applicable stepped locks. Earlier checkpoint numbers are superseded.

<!-- BOOK_RESULTS -->

All supported book-strategy runs complete with zero unresolved positions, including all four full-history butterflies.

| Window | Instrument | Interval | Strategy | Net INR | Win % | Trades | Profit factor | Max DD INR | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| recent | NIFTY | 5m | `book_vertical_spread` | -9,894.31 | 27.78 | 36 | 0.1725 | 10,141.82 | modeled |
| recent | NIFTY | 5m | `book_iron_butterfly` | -11,058.64 | 16.67 | 42 | 0.1219 | 11,207.26 | modeled |
| recent | NIFTY | 5m | `book_strap_strip` | -2,044.43 | 40.00 | 20 | 0.9300 | 16,988.61 | modeled |
| recent | NIFTY | 5m | `book_vol_oi_momentum` | -23,616.09 | 46.07 | 89 | 0.6221 | 33,634.36 | modeled |
| recent | NIFTY | 15m | `book_vertical_spread` | -3,278.00 | 0.00 | 9 | 0.0000 | 3,278.00 | modeled |
| recent | NIFTY | 15m | `book_iron_butterfly` | -7,220.00 | 27.59 | 29 | 0.1271 | 7,582.56 | modeled |
| recent | NIFTY | 15m | `book_strap_strip` | -6,703.07 | 28.57 | 7 | 0.5271 | 8,311.89 | modeled |
| recent | NIFTY | 15m | `book_vol_oi_momentum` | -9,958.26 | 45.45 | 33 | 0.5576 | 13,859.82 | modeled |
| recent | SENSEX | 5m | `book_vertical_spread` | -5,183.71 | 28.95 | 38 | 0.2493 | 5,522.87 | modeled |
| recent | SENSEX | 5m | `book_iron_butterfly` | -512.42 | 52.08 | 48 | 0.8810 | 1,838.70 | modeled |
| recent | SENSEX | 5m | `book_strap_strip` | -3,677.55 | 29.41 | 17 | 0.8708 | 23,107.98 | modeled |
| recent | SENSEX | 5m | `book_vol_oi_momentum` | -6,031.09 | 48.86 | 88 | 0.8742 | 17,165.80 | modeled |
| recent | SENSEX | 15m | `book_vertical_spread` | -1,526.90 | 25.00 | 8 | 0.2346 | 1,526.90 | modeled |
| recent | SENSEX | 15m | `book_iron_butterfly` | 575.36 | 64.52 | 31 | 1.2334 | 631.52 | modeled |
| recent | SENSEX | 15m | `book_strap_strip` | -7,364.49 | 20.00 | 5 | 0.2739 | 7,364.49 | modeled |
| recent | SENSEX | 15m | `book_vol_oi_momentum` | -1,610.67 | 40.91 | 44 | 0.9335 | 14,984.45 | modeled |
| recent | NATURALGAS | 5m | `book_vertical_spread` | -29,558.72 | 10.00 | 20 | 0.0493 | 29,558.72 | observed |
| recent | NATURALGAS | 5m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 5m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 5m | `book_vol_oi_momentum` | -30,492.51 | 8.33 | 24 | 0.0478 | 30,492.51 | observed |
| recent | NATURALGAS | 15m | `book_vertical_spread` | -28,591.05 | 5.56 | 18 | 0.0006 | 28,591.05 | observed |
| recent | NATURALGAS | 15m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| recent | NATURALGAS | 15m | `book_vol_oi_momentum` | -29,807.66 | 15.00 | 20 | 0.0531 | 29,807.66 | observed |
| full | NIFTY | 5m | `book_vertical_spread` | -84,481.73 | 25.78 | 287 | 0.1707 | 84,481.73 | repaired |
| full | NIFTY | 5m | `book_iron_butterfly` | -53,286.44 | 41.99 | 412 | 0.4637 | 53,751.79 | repaired |
| full | NIFTY | 5m | `book_strap_strip` | -87,749.34 | 20.75 | 53 | 0.2589 | 92,162.80 | repaired |
| full | NIFTY | 5m | `book_vol_oi_momentum` | -97,722.52 | 47.49 | 577 | 0.7707 | 98,156.64 | repaired |
| full | NIFTY | 15m | `book_vertical_spread` | -22,287.62 | 12.31 | 65 | 0.0902 | 22,287.62 | repaired |
| full | NIFTY | 15m | `book_iron_butterfly` | -42,353.68 | 41.58 | 291 | 0.4327 | 43,352.94 | repaired |
| full | NIFTY | 15m | `book_strap_strip` | -59,940.63 | 20.83 | 48 | 0.3618 | 59,940.63 | repaired |
| full | NIFTY | 15m | `book_vol_oi_momentum` | -20,743.92 | 49.39 | 328 | 0.8961 | 25,876.14 | repaired |
| full | SENSEX | 5m | `book_vertical_spread` | -57,136.75 | 21.68 | 286 | 0.1733 | 57,475.91 | modeled |
| full | SENSEX | 5m | `book_iron_butterfly` | 8,472.25 | 63.69 | 504 | 1.1776 | 4,368.05 | modeled |
| full | SENSEX | 5m | `book_strap_strip` | -95,219.00 | 25.74 | 101 | 0.4698 | 95,219.00 | modeled |
| full | SENSEX | 5m | `book_vol_oi_momentum` | -75,675.98 | 48.27 | 723 | 0.8428 | 92,108.48 | modeled |
| full | SENSEX | 15m | `book_vertical_spread` | -16,416.60 | 11.84 | 76 | 0.1031 | 16,456.39 | modeled |
| full | SENSEX | 15m | `book_iron_butterfly` | 13,529.01 | 69.75 | 324 | 1.4900 | 2,330.58 | modeled |
| full | SENSEX | 15m | `book_strap_strip` | -5,150.95 | 33.93 | 56 | 0.9289 | 15,950.64 | modeled |
| full | SENSEX | 15m | `book_vol_oi_momentum` | -32,141.50 | 43.71 | 334 | 0.8381 | 39,548.22 | modeled |
| full | NATURALGAS | 5m | `book_vertical_spread` | -35,539.09 | 13.04 | 23 | 0.0512 | 35,539.09 | observed |
| full | NATURALGAS | 5m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 5m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 5m | `book_vol_oi_momentum` | -32,537.61 | 12.50 | 24 | 0.0381 | 33,555.99 | observed |
| full | NATURALGAS | 15m | `book_vertical_spread` | -33,805.02 | 9.52 | 21 | 0.0294 | 34,814.24 | observed |
| full | NATURALGAS | 15m | `book_iron_butterfly` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `book_strap_strip` | -- | -- | -- | -- | -- | unsupported |
| full | NATURALGAS | 15m | `book_vol_oi_momentum` | -33,889.03 | 11.76 | 17 | 0.0463 | 33,889.03 | observed |

The full 358-test offline suite passes. Quote projections and repaired calibration are disclosed in the audit; negative results are retained.
