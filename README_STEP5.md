# Step 5: configurable capital and growth research

Launch locally:

```powershell
.venv\Scripts\python.exe -m streamlit run quant_ui.py --server.port 8501
```

Open http://127.0.0.1:8501. Choose an instrument, dates, mode, capital, allocation percentage, and daily limits in the sidebar. Configure the strategy and press **Run & save backtest**. Saved results keep their original dates and capital; changing controls does not rescale an existing backtest. The Step 4 archive remains available separately.

## Capital and daily controls

- `capital` is initial account equity. `capital_usage_pct` caps committed capital across concurrent positions in the selected instrument/run. Separate saved runs are independent accounts, not a combined multi-instrument portfolio.
- `sizing_mode='allocation'` buys whole lots within the budget, up to `max_lots`. `fixed_lots` either funds all requested lots or skips the entry. Skips are saved in `skipped_entries.parquet`.
- Options commit their entry premium. Futures commit entry notional times `futures_margin_pct` (100% by default). A lower margin percentage is an explicit leverage assumption, not a verified historical broker margin. Economic point multipliers remain distinct from lot counts.
- Estimated round-trip fees at the entry price are reserved in addition to principal. Actual closing fees, taxes, slippage and adverse tick rounding are reconciled at exit. Exit costs can differ from the reserve. Futures gaps can lose more than the margin committed.
- With `compound=True`, already-realized profits can increase subsequent allocations. Open profits never finance new positions; open losses reduce buying power. With compounding off, allocation stays capped at initial capital and shrinks if account equity falls below it.
- `daily_profit_target` / `daily_loss_limit` are rupee limits; `daily_profit_pct` / `daily_loss_pct` use start-of-session equity. Zero disables a limit. If both absolute and percentage limits are set, the nearer threshold applies.
- Limits monitor realized P&L plus open liquidation P&L after costs at each minute close. Crossing a limit locks entries for the day and closes remaining positions at the **next minute's open**. Gaps can overshoot a goal or loss limit. Existing price stops/targets take priority within a candle; mandatory square-off remains 15:20 / 23:15.
- Simultaneous legacy straddle legs share one ceiling and each gets at most half. Both must be affordable before either executes. The daily entry cap counts legs; this mode requires two.

Growth mode offers daily percentage goals of 10–200%; these are configurable exit triggers, not an expected or guaranteed return. It selects aggressive strategy defaults. `strategy_mode` is a descriptive label in the engine; explicit risk parameters determine behavior.

## New signals

**H5 — Expiry_Day_Gamma_Explosion:** dated, holiday-adjusted expiry sessions only; 13:00–14:30 entries (an explicit exception to the other strategies' midday exclusion). A completed spot candle must break the preceding 30-minute compression box, with ADX/ATR conditions and observed option high, volume and OI confirmation. Only ATM or OTM strikes qualify: ATM+1 for calls / ATM-1 for puts. The chosen moneyness remains configurable, so a directional ITM candidate is rejected. Active step trailing is required; target and stop percentages remain configurable.

H5 retains the Step 4 exchange calendar, including rule changes during 2025. It does not assume that Nifty is always Thursday or Sensex is always Friday. Calendar classification still cannot prove the expiry identity of rolling WEEK1 candles.

**H6 — Natural_Gas_Crude_High_Beta_Momentum:** Crude Oil and Natural Gas only, 17:30–21:30. Completed-bar ATR must be at least 1.5 times its causal baseline (or the configured higher threshold), volume must exceed the configured multiple of the preceding 20 bars, and price must break the preceding N-bar high/low. It uses ATR stops, configurable reward/risk, step trailing and the account's daily limits. Gold Mini retains S5/H4.

Both modules use completed observed candles for signals and next-minute fills. Missing option execution candles still use tagged continuity estimates or fail an entire strict-observed run. There is no unresolved-trade fallback that silently drops an open trade.

## Ledger and charts

The ledger reports trade ID, instrument, strategy, entry/exit timestamps, fixed strike/contract, committed capital, usage percentage, prices, net P&L, trade ROI and exit reason. Trade ROI is net P&L divided by premium/assumed margin committed; usage percentage divides committed capital by account equity at entry. Raw records also retain the fee reserve, allocation budget, quantity, signal evidence and modeled-price flags. Old results show missing allocation fields as N/A.

Equity and underwater charts use realized daily P&L. Sharpe uses returns on the previous day's equity. Monthly heatmaps continue to show contribution relative to initial capital, not compounded monthly returns. The two-panel replay includes observed and explicitly tagged modeled candles, entry/exit markers and stop paths.

## Reproducible verification

### Run data selection and eligibility (engine 5.3.0)

MCX contract selection uses the immediately preceding **observed** session's
stored total volume, including context before the requested start. Weekends
and missing calendar dates are not assumed to be sessions. Equal volumes keep
the lexical contract-name tie break. No execution-day final volume selects
that day's contract; an unavailable selected contract is excluded rather than
replaced using today's volume. Only requested execution dates enter results.
The earliest observed date without prior volume is explicitly excluded.

Run prerequisites are inclusive IST windows: index underlying and all six
rolling option streams **09:15–15:20**; selected MCX underlying **09:00–23:15**.
These are dependency windows, not entry hours. Indicators reset each day but
consume the morning history (EMA/ATR/ADX and cumulative VWAP); MCX evening
entries also use that history. Opening ranges, option confirmations, position
management and mandatory square-off remain unchanged. The shared loader keeps
its six-stream prerequisite because fixed-strike signals and the continuity
model use both sides and neighboring offsets; it does not optimize eligibility
to the candles a particular trade happened to consume.

Required opening/closing candles, internal gaps, invalid records and conflicting
same-strike quotes are checked. Legacy whole-session short flags are re-measured
against stored timestamps instead of automatically excluding the date. A gap
after 15:20 alone cannot exclude an index session. Historical flags and audit
reports are retained. Rejected invalid/conflicting aggregates require readable,
accounted-for quarantine timestamps; unavailable evidence fails closed.
Recorded checksums are rechecked for execution and selection-context files;
missing/changed files and the existing audit integrity gate still block loading.

Each new run saves `eligibility.json` beside `result.json` under its run directory.
The result JSON also embeds these records: date, instrument, selected contract,
selection-evidence session, required windows, eligible/excluded status, reasons,
streams and known timestamps. All-excluded observed requests save zero-execution
results with their exclusions. `ResearchData.eligibility` exposes the same checks
without running or persisting a backtest.

Eligibility is distinct from whole-session health and verified option history.
The existing regular-index calendar policy remains; no new special sessions or
MCX calendar completeness claims are added. Entirely unobserved MCX dates are
not invented as expected sessions. Prior volume describes the stored sample,
not verified full-session volume; unchanged partial prior sessions remain the
existing selection evidence. Rolling expiry identity and modeled fixed-strike
continuity limitations remain. Request-boundary invariance concerns selected
contracts and execution inputs, not portfolio P&L across different trade histories.

Version 5.3.0 changes the hashed run identity; existing 5.2.0 directories remain
inspectable and are not reused or included in the current-engine leaderboard.
Eligibility evidence is part of the data fingerprint. A cached completion also
requires the eligibility artifact.

Self-contained loader checks, with external sockets blocked:

```powershell
.venv\Scripts\python.exe verify_offline_tests.py test_quant_run_data
```

The full offline suite includes these fixtures (do not add the focused count
again). Existing local-lake tests still require the installed historical lake.

```powershell
.venv\Scripts\python.exe -m unittest discover -v
.venv\Scripts\python.exe -X utf8 verify_step5.py
.venv\Scripts\python.exe -X utf8 verify_step5_ui.py
```

`verify_step5.py` runs 11 representative configurations over all available history across five instruments, reconciles persisted allocations/ROI/costs/equity/square-offs, and writes `data_lake/backtests/step5_verification.json`. It is not a comprehensive parameter sweep or a profitability guarantee. Step 4 CSV rankings are archived before updating current-engine results; old run directories are preserved.

For an optional new discovery sweep, use `run_strategy_research.py --include-growth` (H1–H6) or `--growth-only` (H5/H6). These runs are in-sample research and retain the data-quality and modeling limitations from Step 4.

## Verification completed

- 50 unit tests passed, including concurrent capital, insufficient funding, daily rupee/percentage limits, compounding, both new signals and existing pricing/accounting checks.
- 11 representative full-available-history runs produced 195 closed trades and zero unresolved trades. These particular runs used no modeled candles; index option expiry identities remain unverified.
- Gold Mini's two fully funded configurations correctly produced no fills because their capital ceilings were insufficient; skipped candidate entries remain inspectable.
- Streamlit checks exercised all five instruments, both modes, four tabs and a real custom run with four closed trades. HTTP health at port 8501 returned 200 OK.
- Outcomes were mixed, including substantial losses in the tested Natural Gas configurations. H5/H6 are research modules, not established profitable strategies.
