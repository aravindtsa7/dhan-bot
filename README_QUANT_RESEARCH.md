# Options and commodities research

**Current engine: Step 4.** See [README_STEP4.md](README_STEP4.md) for H1–H4, option VWAP/OI, explicit continuity estimates, dated expiries, and the current 252-run grid. The remainder of this file documents the retained Step 3 baseline and original cost assumptions.

Run from `C:\Dhan\dhan-bot`:

```powershell
.venv\Scripts\python run_strategy_research.py
.venv\Scripts\python -m streamlit run quant_ui.py
# Open http://127.0.0.1:8501
.venv\Scripts\python -m unittest test_quant_backtester -v
```

The default period is 2025-01-01 inclusive through 2026-10-23 exclusive. No broker credentials, authentication, trading endpoints, or orders are used. Dependencies are pinned in `requirements.txt`.

## What the data supports

**These are provisional research results, not production-certified trading returns.** The lake contains actual Dhan OHLC candles, but the rolling options have no expiry date or contract ID. The simulator fixes the entered strike and searches all three same-side rolling streams for that strike. It never substitutes a new ATM strike into a position. Missing fixed-strike prices censor the position: exit price, P&L, and final charges are unknown. A matching strike cannot prove that two observations refer to the same expiry. Censored entries incur real obligations if traded; their P&L and charges are deliberately not represented as zero in the trade log. Rankings and equity include **only closed trades** and are therefore conditional, potentially biased diagnostics.

All index volume in this snapshot is zero. S1/S4 consequently use a session typical-price **TWAP proxy**, explicitly identified in the UI; this is not a traded-volume VWAP. S2 confirms with actual option volume and the fixed strike's opening high. Missing option history prevents the signal.

The active MCX contract sample starts in May–July 2026 depending on the commodity. This is not continuous 2025–2026 history. Each day selects the observed contract with the largest volume on the previous available session. No current-day volume determines selection. Positions stay in that exact contract and are intraday. The sample has active-contract/survivorship selection bias; no continuous-price back-adjustment is attempted.

To produce production-grade option P&L, ingest **expiry-resolved, fixed-contract candles through every exit**, verified historical lot schedules, exchange sessions, and historical fee/margin/liquidity data. UI and artifacts retain these limits rather than concealing missing observations.

## Strategies and sweep

The reproducible default grid has **366 configurations**: 147 each for Nifty and Sensex, plus 24 each for three MCX commodities. S1/S2/S4 cross 3m/5m/15m and ATM/ATM-1/ATM+1. Three risk templates sweep stop/target/trail percentages: 20/40/off, 25/50/20, 30/75/25. S1 also sweeps ADX 15/25; S2 sweeps 15/30-minute opening ranges. S3 tests both modes, 09:25/09:30 anchors, 15/20% breakout thresholds where applicable, and two risk templates. Its stop orders and anchor entries execute on minute candles, independent of the chart timeframe; duplicate chart-timeframe configurations are not added to the grid. S5 sweeps three timeframes, two ATR stop multipliers, two reward/risk ratios, and trailing on/off.

- S1: session EMA9/EMA21, Supertrend(10,3), TWAP proxy and ADX confluence.
- S2: completed spot bar beyond opening range; fixed option strike closes above its own opening high, with the latest minute volume exceeding 1.5 times its preceding 20-minute mean volume. Missing fixed-strike opening/history data skips the signal.
- S3 A: fixed ATM strikes from the minute before the anchor; real high-touch stop orders. If both legs first trigger in the same minute, sequence is unknowable and the session is skipped. After a stopped first leg, the other leg may enter on a later breach. At most one entry per side.
- S3 B: buy both anchor strikes; separate 25% stops and stepped trailing management. Targets are configurable.
- S4: EMA50/TWAP proxy trend, pullback within max(0.25 ATR, 0.03% spot), directional candle closing in the outer 40% of its range.
- S5: fixed 17:00–17:30 IST range, entries 17:30–22:30, ATR stops/targets. Overnight or contract-roll positions are never carried.

EMA/ATR/ADX/Supertrend are causal and reset per session. S1/S4 require 20 completed signal bars of warm-up, so 15m configurations begin late in the session. S2 uses minute-volume confirmation regardless of spot signal timeframe. S5 has its indicator warm-up from daytime candles. These definitions are explicit research variants and should not be interpreted as exact replicas of any broker's indicator implementation.

## Execution and quality

Ordinary signals use completed bars; execution starts at the next minute's open. At entry, the strike is selected from the preceding minute, never the future candle's moneyness. S3 stop triggers are known before the execution minute. Price slippage is adverse at entry and exit. OHLC cannot establish intrabar ordering: a stop wins when stop and target collide. The entry candle also uses this conservative ordering. Gapped stops fill at the worse open. Targets fill at the target with adverse slippage (no favorable gap improvement).

Trailing stops move only after a favorable jump and become active in the next minute. Options use entry-premium percentage distances; futures use ATR distance and quarter-stop-distance jumps. All positions with available candles close at the **15:20 IST open** for indices and **23:15 IST open** for MCX. A missing candle produces censoring, not a fictitious square-off.

The engine requires `quality_report.json` without integrity issues. It uses only clean manifest partitions, excludes flagged short sessions, requires every underlying/option minute in the relevant execution window, rejects conflicting same-strike observations across rolling streams, and never forward-fills gaps. Exclusion logs are saved per run. These filters condition the sample on data availability and can create selection bias.

Default sizing is one lot and fixed ₹1,000,000 research capital, no compounding. Options check entry-premium affordability (each straddle leg receives at most half the research capital). Futures margin and capital depletion are not modeled; fixed-notional research may continue after cumulative losses. No market depth, partial fills, price limits, exchange halts, or liquidity impact model is present.

## Costs and sizing assumptions

All completed legs include two executed orders at ₹20 each. Long straddles have two legs and thus four orders. Fees apply to slipped execution prices; slippage is already in gross P&L and is not deducted twice. GST is 18% of brokerage + exchange + SEBI + IPFT. STT/CTT and stamp are rounded to nearest rupee; other fee components to paise. Per-leg rounding may differ from broker aggregation on a contract note.

| Component | Default research model |
|---|---|
| NSE option exchange | 0.03503% of premium turnover, both sides |
| BSE Sensex option exchange | 0.0325% of premium turnover, both sides |
| MCX futures exchange | 0.0021% of turnover, both sides |
| SEBI | 0.0001% of turnover |
| NSE options IPFT | 0.0005% of premium turnover |
| Option STT | Sell premium × 0.10% through 2026-03-31; 0.15% from 2026-04-01 |
| MCX non-agri futures CTT | Sell turnover × 0.01% |
| Stamp | Buy side: options 0.003%, futures 0.002% |
| Nifty units/lot | 75 in 2025, 65 from 2026-01-01; expiry-transition approximation |
| Sensex units/lot | 20 throughout this research window |
| MCX P&L multiplier | Crude oil 100; Natural gas 1,250; Gold mini 10 |

MCX scrip-master `LOT_SIZE=1` does **not** establish the rupee point value. Gold mini is a 100g contract quoted per 10g, giving a 10× price multiplier. UI lot-size override means option units or futures price-point multiplier per lot. Charges and quantities are research assumptions, not an assertion that every historical contract note is reproduced. Exchange/SEBI/IPFT/GST/brokerage are configurable in `Config`; date-dependent STT/CTT/stamp and lot schedules live in `fees()` / `quantity()`.

Primary references consulted:

- [Dhan charges](https://dhan.co/pricing/)
- [NSE October 2024 transaction charges](https://nsearchives.nseindia.com/content/circulars/FA64232.pdf)
- [NSE IPFT contribution](https://archives.nseindia.com/content/circulars/FA56129.pdf)
- [BSE September 2024 Sensex option fee announcement](https://nsearchives.nseindia.com/corporate/BSE_27092024184037_NSEintimation.pdf)
- [NSE STT schedule](https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax)
- [MCX clearing CTT](https://www.mcxccl.com/clearing-settlement/commodities-transaction-tax)
- [Nifty lot revision, October 2024](https://nsearchives.nseindia.com/content/circulars/FAOP64625.pdf) and [October 2025](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf)
- [MCX crude oil](https://www.mcxindia.com/products/energy/crude-oil), [natural gas](https://classic.mcxindia.com/products/energy/natural-gas), [gold mini](https://classic.mcxindia.com/products/bullion/gold-mini)

## Metrics, artifacts, and resume

Every run ID hashes its entire config, engine version, ingestion manifest and quality report; a supplied expiry calendar also changes the hash. Completion metadata is written last after Parquet artifacts. Completed runs with all artifacts present are reused. `research_plan.json` records the exact grid and `research_summary.json` records successes/failures. `leaderboard.csv` is refreshed periodically and at completion. A new engine version yields new IDs; old runs are retained for audit.

Each `data_lake/backtests/runs/<run_id>/` contains:

- `result.json`: configuration, metrics, coverage, source fingerprint, side/expiry splits, limitations.
- `trades.parquet`: closed **and censored** entries, fixed strike/contract, prices, reason, individual fee components.
- `stop_paths.parquet`: minute-by-minute active stops, targets and observed marks.
- `equity.parquet`: daily closed-trade P&L, equity, realized peak and drawdown.
- `monthly.parquet`: monthly net P&L and returns on fixed research capital.
- `excluded_sessions.parquet`: quality rejection dates and reasons.

ROI uses fixed starting capital. Win rate and expectancy use closed legs, not multi-leg packages. Profit factor is positive closed-leg P&L divided by absolute negative P&L; zero-loss positive runs are explicitly flagged infinite. Max drawdown is **realized daily** drawdown including the initial-capital peak, not intraday mark-to-market risk. Sharpe uses daily P&L / fixed capital and √252; observed zero-trade/excluded sessions remain in the curve. Calmar uses annualized total growth divided by realized drawdown/start capital. MCX limited history can make annualization unstable.

2025 and 2026 P&Ls are provided separately. Because the full-period grid is ranked after observing both years, the latter is not an untouched holdout. No statistical significance or multiple-testing-adjusted discovery is claimed.

Expiry classification remains `unknown` unless a verified `data_lake/expiry_calendar.csv` is supplied with columns `instrument,date` (ISO dates). It identifies underlying expiry sessions, not the expiry of a particular rolling candle. The calendar must cover the entire requested window; incomplete calendars would mislabel other dates as non-expiry. Re-run after adding it; its hash is part of run identity.

The Streamlit app is local-only on `127.0.0.1:8501`, with no account login or order-placement controls. Tabs provide filters and custom runs, conditional analytics, linked candlestick replay of fixed-strike prices, and raw lake/quality inspection. Plotly figure times are displayed in IST. CSV export is available in the UI; Parquet remains the canonical artifact format.
