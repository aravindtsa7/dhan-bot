# Book strategies in Engine 5.4.0

The 2026-11-01 deep audit supersedes the results and flat-delta valuation in
`cfd9d90`. The complete methodology, coverage, all-nine-strategy comparison and
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
Butterflies preserve observed quotes and positive wings within one wing-width
liability budget. This is a disclosed sigmoid research model, not recovered IV.

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
Settings are INR 100,000, fixed one dated lot, 5m/15m, TP25/SL25, no trail,
100% allocation and an explicit 20% research margin assumption for NATURALGAS.
The application default futures margin remains 100%. Results are after all fees.
Old numbers from the earlier cap-only/flat-delta checkpoints are superseded.

<!-- BOOK_RESULTS -->

All eight index vertical runs completed with zero unresolved trades.

| Window | Instrument | Interval | Strategy | Net INR | Win % | Trades | Profit factor | Max DD INR | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| recent | NIFTY | 5m | `book_vertical_spread` | -11,749.09 | 29.73 | 37 | 0.1787 | 11,996.60 | modeled |
| recent | NIFTY | 15m | `book_vertical_spread` | -2,738.07 | 22.22 | 9 | 0.0905 | 2,951.33 | modeled |
| recent | SENSEX | 5m | `book_vertical_spread` | -5,448.20 | 39.47 | 38 | 0.3015 | 6,171.98 | modeled |
| recent | SENSEX | 15m | `book_vertical_spread` | -1,721.91 | 25.00 | 8 | 0.2137 | 1,721.91 | modeled |
| full | NIFTY | 5m | `book_vertical_spread` | -91,213.84 | 29.12 | 285 | 0.1996 | 91,213.84 | repaired |
| full | NIFTY | 15m | `book_vertical_spread` | -23,939.59 | 20.00 | 65 | 0.1003 | 24,152.85 | repaired |
| full | SENSEX | 5m | `book_vertical_spread` | -64,066.44 | 29.12 | 285 | 0.1782 | 64,790.22 | modeled |
| full | SENSEX | 15m | `book_vertical_spread` | -17,097.48 | 21.33 | 75 | 0.1361 | 17,137.27 | modeled |

Four full-history butterfly runs fail on incompatible observed pair quotes; see the exact coordinates in the audit. Their P&L is intentionally not reported. The 342-test offline suite passes with external sockets blocked.
