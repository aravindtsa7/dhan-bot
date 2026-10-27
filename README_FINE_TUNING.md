# Targeted risk-profile fine tuning

```powershell
.venv\Scripts\python.exe -X utf8 fine_tune_strategy.py
.venv\Scripts\python.exe -X utf8 fine_tune_strategy.py --report-only
.venv\Scripts\python.exe -X utf8 fine_tune_strategy.py --rebuild-report
```

The default run declares **224 configurations** before executing them:

| Profile | Market / strategy | Tested parameters |
|---|---|---|
| Steady | Crude Oil H4, 5m | ATR stop 1.5/2; reward/risk 2/3; trailing jump 0.25/0.5 of initial stop distance; daily loss ₹1,000/₹1,500; allocation 25/50%; daily profit lock ₹1,000/₹2,000 |
| Explosive | Nifty H5 3m / Sensex H5 5m | Option SL 20/25%; premium target 50/100/150%; trailing jump 5/10% of entry premium; daily loss ₹1,500/₹2,500; allocation 50/75% |
| Exploratory explosive | Crude Oil / Natural Gas H6, 3m | ATR stop 1.5/2; reward/risk 2/3; trailing jump 0.25/0.5 of initial stop; daily loss ₹1,500/₹2,500; allocation 50/75% |

Capital defaults to ₹1 lakh for options and ₹5 lakh for futures. Futures use an **explicit 20% margin assumption**, not a verified broker margin schedule. Allocation uses whole lots, reserves fees and skips unaffordable orders. Compounding is off for comparability; at most one new trade per session is allowed in these sweeps. Explosive profiles leave the daily upper profit lock disabled to permit larger winning trades; their daily loss lock, per-trade stops/targets and square-off remain active.

Natural Gas uses 0.5 price points of slippage per fill; Crude Oil uses 1 point. Options use 0.5 points. These configurable research assumptions can materially affect results.

Override capital/margin or narrow the sweep:

```powershell
.venv\Scripts\python.exe -X utf8 fine_tune_strategy.py --option-capital 200000 --futures-capital 1000000 --futures-margin-pct 100 --output data_lake/backtests/fine_tuning_fully_funded
.venv\Scripts\python.exe -X utf8 fine_tune_strategy.py --core-only
.venv\Scripts\python.exe -X utf8 fine_tune_strategy.py --instruments CRUDEOIL --output data_lake/backtests/fine_tuning_crude
```

Dates default to `2025-01-01 <= session < 2026-10-23`. Actual futures coverage is shorter; coverage and exclusions are exported per configuration. `--split 2026-09-01` defines development versus later-period reporting. Ranking uses development data only, prioritizes at least ten traded development sessions, then profitable development results, then profile-specific outcomes. The later-period check is **not untouched holdout validation**: these data and original strategy choices were inspected in prior research. Low-trade configurations remain visible and are explicitly flagged.

## Daily protection and accounting

Daily lock checks use realized P&L plus the net liquidation value of open positions, after estimated closing costs. A threshold crossing at a minute close latches the account for the rest of that session, rejects later signals and liquidates open positions at the next minute's open. A rebound never unlocks the account. State resets next session. Execution gaps can exceed a loss cutoff or reduce an attained profit; the engine never clips P&L to the requested amount.

Every new engine run saves `daily_risk.parquet`, including lock type, trigger minute/P&L, blocked entry count, peak and minimum session P&L, maximum within-session drawdown, final net P&L and loss-cutoff overshoot. Zero-trade eligible sessions are retained. Tests cover cost-sensitive triggers, rebounds, overshoots, session resets and independently known drawdowns.

## Report definitions

- **Avg/day:** total net P&L divided by all quality-eligible underlying sessions, including zero-trade days. The CSV also shows average per traded session. Quarantined/excluded days are counted separately, not silently treated as valid zero returns.
- **≥1k%, ≥2k%, 1–2k%:** percentages of eligible sessions whose *final realized net P&L* is respectively at least ₹1,000, at least ₹2,000, or in the inclusive ₹1,000–₹2,000 band. A profit trigger followed by a worse next-open fill does not count as a final target hit.
- **Max day DD:** maximum within-session peak-to-trough loss in net liquidation P&L sampled at minute closes and actual exits, with a zero starting peak. This does not measure unobserved intrabar worst-case drawdown.
- **Worst day:** lowest final daily P&L, a separate metric from drawdown.
- **Expl20/50/100%:** percentages of closed option trades capturing at least 20%, 50% or 100% premium gain with positive net profit. Futures use the same thresholds for net ROI on assumed margin committed, **not** futures price moves. No-trade frequencies are missing/N/A, not manufactured zero success rates. Maximum favorable excursion is not counted as captured profit.

## Outputs and verification

`data_lake/backtests/fine_tuning/` contains:

- `plan.json`: full predeclared grid, capital/margin/cost assumptions, metric definitions, engine version and plan hash.
- `summary.csv`: every configuration, full/development/later metrics, risk parameters and provenance.
- `selected_profiles.csv`: one development-ranked candidate per profile/instrument; selection is not a recommendation or proof of consistent income.
- `summary.txt`: readable terminal report.
- `completion.json`: completion/failure counts and unresolved-trade audit.
- `runs/<run_id>/`: trade ledgers, stop paths, daily risk audits, equity, monthly results, skipped entries and exclusions. Completed runs resume by configuration/engine/data fingerprint.

Each run is checked for zero unresolved positions, reconciled daily/trade totals, no entry after a daily lock and closure by the next minute. Selected candidates' drawdowns are rebuilt separately from trade fills and candle paths. Option continuity estimates remain tagged; rolling option expiry identities remain unverified even when candles are observed.

```powershell
.venv\Scripts\python.exe -m unittest discover -v
```

The files support review and repeatable research. The sweep does not assume that either income or explosive-return targets will be achieved consistently.

## Completed default run

All 224 configurations completed with no failures and zero unresolved trades; 58 unit tests passed. The Streamlit regression check also passed all instruments/modes and a real four-trade custom run. Selected candidates' minute-close drawdowns were independently reconstructed from saved candles and fills.

The development-ranked steady Crude H4 candidate averaged INR 29.06 per eligible session across 65 sessions, with 23.08% of sessions finishing at least INR 1,000 and 18.46% finishing at least INR 2,000. Its maximum single-session minute-close drawdown was INR 3,600.09, and its later-period average was negative. This does not validate consistent INR 1,000?2,000 daily income.

The selected Sensex H5 candidate had only four trades: one captured a premium move above 100%, giving 25% explosive frequency but too little evidence for a consistency claim. The selected Nifty and both H6 candidates lost money over the full window. All selected runs used observed execution candles, while rolling option expiry identity remains unverified. The CSVs retain losing configurations and cutoff overshoots.
