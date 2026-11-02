# Rule to source to Python mapping

Source paths below are relative to `C:/TSA/trademind-calendar/src/modules/`. Physical SHA values and dependencies are in [source_identity.md](source_identity.md).

| Rule | Source authority | Python | Verification scope |
|---|---|---|---|
| V2 5m regime/cooldown | adaptive-intraday/services/v2-trend-down-entry-evaluator.service.ts; research-lake/services/v2-replay-strategy-adapter.service.ts | quant_trademind.V2State; trademind_signals.generate_signals | retained TS fixture + frozen full decision sets |
| V4 compression/expansion and 3m-before-5m alignment | adaptive-intraday/services/v4-nifty-momentum-shadow-evaluator.service.ts; research-lake/services/v4-replay-strategy-adapter.service.ts | quant_trademind.V4State; trademind_signals.generate_signals | retained TS fixture + frozen full decision sets |
| V8 interaction/reclaim + prior-session readiness | research/v8-nifty-bullish-reclaim/v8-bullish-reclaim.signal.ts; research-lake/services/v8-replay-strategy-adapter.service.ts | quant_trademind.V8State; trademind_signals.generate_signals | coherent TS fixture + frozen full decision sets; original invalid OHLC fixture explicitly rejected |
| Indicators / seed / bucketing | research-lake/services/incremental-regime-indicators.service.ts; incremental-session-bucketer.service.ts; cross-session warm-up dependencies | quant_trademind.indicator_series; trademind_signals.aggregate | source-executed indicator snapshots; no library substitution |
| Phase-B fills / ambiguity / timeout | research-lake/services/historical-ohlc-fill-policy.service.ts | quant_trademind.phase_b_exit | 6 source-executed fill cases; strict coverage discrepancy regression |
| T3 VIX/ATM/short fly/endpoint fills | research/trademind-t3-vix-gated-iron-fly/t3-rules.ts; t3-protocol.ts | quant_trademind.t3_structure/t3_exit; trademind_backtest.run_observed | 6 retained source four-leg fixtures + actual-source run comparison |
| H30 same-pair rise / floor / 30m close-next-open | research/nifty-ce-pe-rise5m-filter/filter-study.ts; nifty-ce-pe-rise5m-holding/holding-study.ts; floor100 study.ts | quant_trademind.rise5m/close_then_open_exit | source execution fixtures + retained frozen observations |
| H30 tick candles/asks/bids/liquidity/generation | research/nifty-ce-pe-h30-tick-feasibility/tick-candles.ts; tick-execution.ts; tick-feasibility.ts | trademind_ticks.* | 4 source fixture cases; isolated raw capture replay separately reported |
| ORB first close breakout and exact floors | research/nifty-orb-single-option/orb-study.ts | quant_trademind.orb_signal/floor_gate/close_then_open_exit | source execution fixtures + retained frozen observations |
| Dated fixed contract / owning expiry / PIT | research-lake manifests, multi-expiry plans and historical PIT selector rules | trademind_data.RetainedTradeMindLake | raw physical hashes, dated ID/known_at checks; full independent rank replay is a disclosed gap |
| Fees / whole lots / cash reservations | options/services/historical-option-charges-calculator.service.ts; historical-option-charge-rates.config.ts; capital-simulation source | quant_trademind.source_fees/size_lots; trademind_backtest.capital_ledger | source fee fixtures + independent affine sizing/turnover/cash arithmetic |
| Backtest UI / frozen configuration / paper guard | Destination integration (no TradeMind live runner imported) | trademind_ui; quant_terminal; quant_ui; quant_backtester; quant_deployment; paper_engine; step6_paper_trader | UI/session-state tests, registry and paper rejection tests |
