# ce_pe_rise5m_h30

## A. Source identity

Primary frozen candidate **A / 11:01 / P5 / H30**, not a tuned variant.
Holding fingerprint `0b02c26487fc8ad7b7bf46f63106fe946cdf97d55bae7921b7e83e52d0376eb3`;
filter `173d6b89e0923daa1969fdbe87f1455c9e8dc98bed4da8263dedab9f2a5b3035`;
floor `3bacd67a380205af12de5f79c98885685d4e5f9f853ffd987659fa865f7fd062`;
premium-path `641246f17aefa6d2b553f027b28171956641f9bc83a0e591f1f547e9745aabd1`.
Calendar `src/modules/research/nifty-ce-pe-rise5m-{filter,holding}`, floor100
study, candidate-audit and tick-feasibility source/runners inspected; SHA/code
bindings in [source_identity.md](source_identity.md). Source wrappers are not
executed. Older superseded floor protocol has a runner hash conflict, retained
explicitly; current primary protocol bindings match. Tick protocols separately
bind their dated raw input sets; a stale latest pointer is not authority for a
newer run.

## B. Input contract

Exact 11:00 NIFTY close; the same dated ATM CE+PE pair is selected when that minute completes.
All six START closes, 10:55 through 11:00, for both legs, plus 11:01 OPEN and contiguous required
management/exit candles. Special calendar windows used, no fabricated morning.
Tick variant is separate observed Upstox bid/ask/receive/source clocks and locked
universe, no candles substituted for missing tick evidence.

## C. Indicators

No recursive indicators. Cnow = CE 11:00 close + PE 11:00 close;
Cold = **same pair** CE 10:55 close + PE 10:55 close. Feature available only with all six
valid close points per leg. Not change in current ATM across timestamps.

## D. Signal

Both 11:00 premiums strictly > ₹100 AND Cnow > Cold; both 11:01 entry OPENs strictly > ₹100.
One attempt per date; no retry another pair. Equality fails floor/rise. No RSI/VIX
gate. The combined executed entry debit anchors the +5% target and -25% stop.

## E. Contract selection

ATM nearest 50, exact half DOWN, using 11:00 spot close; owning expiry, exact CE+PE dated IDs;
no alternate premium-ranked pair, nearby strike, expiry, provider or forward fill.
Both BUY, equal whole-lot quantity. Dated lots source authority shared in overview.

## F. Orders and exits

Candle variant: BUY both 11:01OPEN. First combined completed CLOSE>=1.05*entry debit
target, <=0.75*entry debit stop, execute both next-minute OPEN. Target checked first;
these close conditions cannot coincide for positive entry. Never mix leg HIGH/LOW.
Scheduled timeout 11:31OPEN, capped only by genuine historical session last minute;
no11:31CLOSE or post-timeout trigger. Missing monitor/next OPEN leaves UNRESOLVED.

Tick variant: source trade-evidence classification builds minute START candles;
source/receive clocks must arrive by minute end+2s settle. Decision11:01:02,
order eligible+250ms, execution wait2s, quote age<=2s, same generation/post-decision,
uncrossed spread<=5%, positive ask/bid quantity. Both entry asks>100, all-or-none entry at
asks, qty limited by cash and displayed top quantity. Trigger on combined final CLOSE against
**executed ask**debit; lastsignal11:30, timeout decision11:31:02 plus latency.
Exits at bids independently; unchanged(price,qty) cannot regenerate liquidity.
Partial exits retain exposed unsold quantity, no LTP valuation. Spread cost informational,
already present in ask/bid P&L; no duplicate deduction. Collector classification
can be source-executed without claiming independent tick decoder parity.

## G. Sizing/accounting

Source baseline already100% cash, fresh ₹100,000 each year/scenario, dated whole lots,
entry fees reserved in supported net account, source compounding. Requested 100% is
separately labelled, can coincide exactly. Tick research source resets ₹100,000
per day; keep that variant separate from annual candle compounding. Cost windows as
overview, no invented margin and no ₹100floor/stop imported into other strategies.

## H. State machine

IDLE ->11:00pairselected -> six-close feature/floorgate ->11:01entry pending ->OPEN
->close-trigger/timeout ->next OPENexit pending ->CLOSED/UNRESOLVED. Tick variant
adds generation/window failures and partial exit exposure. Every rejection has reason;
unavailable price is never zero P&L.
