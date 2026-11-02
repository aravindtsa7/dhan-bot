# v4_momentum_expansion_pe

## A. Source identity

VERIFIED fingerprint `549aacaca3149ffd`, TREND_DOWN PE Momentum Expansion.
Calendar `src/modules/adaptive-intraday/services/v4-nifty-momentum-shadow-evaluator.service.ts`
and `src/modules/research-lake/services/v4-replay-strategy-adapter.service.ts`,
regime/indicator dependencies, Phase-B fills and capital protocol. Full paths and
SHA identities: [source_identity.md](source_identity.md). Full original frozen
run code custody UNKNOWN; no merging sibling/HEAD variants.

## B. Input contract

Source3m signal and 5m regime from complete09:15-anchored1m buckets. Initial gate35
completed 5m; seed event does not emit. Three-minute callback runs **before** a
simultaneously completed 5m callback; regime uses5m boundaries strictly earlier
than the 3m decision. Current-session compression, prior-session indicator tails.
Missing certified underlying splits/reseeds; no VIX.

## C. Indicators

Overview C equations, but ATR14 is reinitialized from last 84 prior-session 3m
bars plus current-session 3m prefix. Regime reinitialized from last 50 prior5m
bars plus eligible current5m prefix. This deliberately bounded source wiring is
different from V2's continuous seed. No imported library seed or hidden RSI<35.

## D. Signal

Need current plus three prior current-session 3m candles. Compression span
max(prior3HIGH)-min(prior3LOW)<=2*ATR14. Current abs(close-open)>=1*ATR14;
close<open; close<=min(prior3LOW)-0.1*ATR14. Latest eligible primary regime must
be TREND_DOWN. Cooldown>=300,000ms since emitted signal; resets per session as
source adapter. Emit BUY_PE at completed 3m boundary. No daily cap. Missing
ATR/compression/regime blocks. Equality at compression/body/breakout is allowed.

## E. Contract selection

Same PIT-eligible nearest dated PE selection as V2, at signal spot, owning expiry;
ID fixed after selection. No₹100  floor, no fabricated strike/expiry/provider.
Whole dated lots in requested scenario; off-tick source barriers disclosed.

## F. Orders and exits

One BUY PE; Phase-B OPEN strictly later than decision (strict guard+1m), target 5%,
stop 5%, deadline decision+15m capped15:30. Same-bar unresolved, opengap precedence
and timeout CLOSE as overview. No trailing/ordinaryEOD overlay from Dhan.

## G. Sizing/accounting

Shared independent₹100,000 source assumed75 and requested dated100% scenarios.
Overlapping attempts, cash reservation, fee NC and equity freeze per overview.

## H. State machine

Warm-up -> compression/regime eligible -> signal cooldown -> selected/pending/
open/exit/closed; pre-entry blocks and open unresolved differ. No reuse of V2 or
V8 entry state/rules despite shared arithmetic helpers.
