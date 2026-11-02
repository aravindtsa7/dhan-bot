# v8_bullish_reclaim_ce

## A. Source identity

VERIFIED parameter fingerprint `c815f819acd71e98`. Calendar
`src/modules/research/v8-nifty-bullish-reclaim/v8-bullish-reclaim.signal.ts`,
`v8-train-freeze.ts`, shadow service and
`src/modules/research-lake/services/v8-replay-strategy-adapter.service.ts`.
Original train JSON is absent (source explicitly recovers the frozen preimage);
full original code custody UNKNOWN. Current recovered params recompute exactly.
Sibling exports/train/signal bytes differ; [source_identity.md](source_identity.md)
records both. Retained source test contains HIGH<CLOSE; strict Python rejects
that original fixture. A distinct coherent synthetic case executes original TS
and matches Python; it does not replace/conceal the invalid fixture.

## B. Input contract

2m complete buckets, nearest prior PRESENT session must have all 375 minute bars
09:15..15:29. Incomplete nearest prior cannot be replaced by an older day.
Current-session prefix must be continuous from 09:15. Missing prior/current
history produces explicit unavailable/warm-up. No VIX and NO_REGIME_FILTER.

## C. Indicators

RSI14 seeded from last 125 prior-session 2m bars plus current2m prefix; ATR14 from
current-session 2m bars only. Exact overview equations. PDH=max(all prior-session
1m highs). Threshold PDH+0.1*current ATR. ATR must be positive; RSI must be>50.
No use of later daily high, no library resampling/forward fill.

## D. Signal

Per-session state armed=false. With ATR/PDH ready and RSI>50, interaction
LOW<=threshold AND CLOSE<=threshold arms. It cannot reclaim on the same candle
that first arms. A later candle must CLOSE>OPEN, CLOSE>=threshold, bullish body
>=0.25*ATR. Cooldown>=5m since signal; blocked cooldown consumes armed episode.
Successful BUY_CE signal also consumes episode. RSI<=50/notready blocks that
evaluation; no added trend filter,₹100  floor or daily cap. Decision=2m end.

## E. Contract selection

Source nearest PIT-eligible dated CE to spot, owning expiry, tie lower strike/ID.
No fallback to another expiry/provider; no premium100 rule. Contract stays fixed
through the trade. Source assumedlot75 and dated scenario are distinct.

## F. Orders and exits

One BUY CE. Phase-B target+6%, stop-5%, deadline decision+15m capped15:30. Entry
strictly laterOPEN (+1m strict guard), same-bar ambiguity unresolved, timeout CLOSE,
gap precedence in overview. No trailing or reclaim-based exit.

## G. Sizing/accounting

Fresh ₹100,000 per year/scenario; source75 vs dated100% whole-lotcash. Shared fee
limitations and account freeze. Signal parity is separate from account history.

## H. State machine

Readiness -> unarmed -> interaction armed -> later reclaim -> cooldown; resulting
trade independently selected/pending/open/closed or unavailable/unresolved.
Incomplete prior-session readiness resets rather than picking favorable history.
