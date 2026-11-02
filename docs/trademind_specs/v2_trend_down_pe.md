# v2_trend_down_pe

## A. Source identity

VERIFIED parameter fingerprint `f8a0ee53d6fdeb8a`, recorded TREND_DOWN PE.
Authority: `C:/TSA/trademind-calendar/src/modules/adaptive-intraday/services/v2-trend-down-entry-evaluator.service.ts`,
`research-lake/services/v2-replay-strategy-adapter.service.ts` under the same
`src/modules` root, incremental indicators/bucketer, adaptive regime service,
capital-protocol and historical-ohlc-fill-policy. Full physical SHA/dependency
identities: [source_identity.md](source_identity.md). Original full run source
bytes UNKNOWN; fingerprint binds params, not source. Sibling differences are
documented there, never merged. SPEC_COMPLETE refers to these inspected rules.

## B. Input contract

Shared observed contract applies. 5m completed buckets, minimum 36 complete5m
bars from the supplied canonical replay origin; continuous recursive seeds span
sessions within the certificate. Backtest execution range is filtered after
initialization from that origin. A request that supplies a different prefix is
not inherently seed invariant; certification origin is recorded. Explicit
eligibility breaks reseed, then require warm-up again. No VIX input.

## C. Indicators

Exact EMA15/35, RSI14, ADX14, ATR14 and primary regime equations in overview C.
No higher-timeframe filter beyond the completed 5m context, no volume filter.

## D. Signal

All: primary regime==TREND_DOWN; RSI14<35; close<EMA35;
abs(high-EMA35)/EMA35*100<=0.20; prior emitted signal at least600,000ms earlier
(or none). Decision at bucketSTART+5m. Cooldown updates on signal, including an
unaffordable/unfillable selection. No daily cap or generic max-two-trades rule.
Direction BUY_PE only; simultaneous CE conflict does not exist.

## E. Contract selection

Owning front expiry, PE, source PIT eligible nearest strike to decision's last
underlying1m close, tie lower strike then ID. Dated provider ID fixed. No₹100
premium floor, no H30 rule import, no alternate expiry/provider fallback. Missing
PIT catalog produces NO_ELIGIBLE_CONTRACT; exact entry absence is NO_ENTRY.
Tick authority is not established for off-tick barrier prices.

## F. Orders and exits

One BUY PE. Shared Phase-B source policy: next strictly later observed OPEN,
strict baseline decision+1; target+5%, stop-5%, hold decision+15m capped15:30.
Gap and same-bar ambiguity precedence per overview F; no trailing. This is an
explicit candle execution assumption, not a bid/ask assertion.

## G. Sizing/accounting

Shared G; independent₹100,000, source assumed75 vs requested dated100% scenario,
whole lots, cash committed across potentially overlapping signals. No margin
requirement on a fully paid long premium. Fee unsupported periods remain NC.

## H. State machine

Shared lifecycle plus persistent signal cooldown. OPEN ambiguous/missing path
freezes account; idle/no-signal, warm-up, pre-entry skip remain distinct.
