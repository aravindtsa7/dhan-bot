# t3_vix_atm_iron_fly

## A. Source identity

Historical T3 protocol VERIFIED full fingerprint
`326773433d7831af3a6d7401577566962174594b0a223ae989efe1a0e035ce7d`:
calendar `src/modules/research/trademind-t3-vix-gated-iron-fly/t3-protocol.ts`,
`t3-rules.ts`, historical backtest and retained runs. Source physical hashes and
dependencies in [source_identity.md](source_identity.md). Forward paper V1_1
`88b90e189ea807c9b0d206cfd360254671ddddb53934e048ba1c6758df87421e`
is a **different bid/ask protocol**, not silently combined. Inspected
`src/tests/t3-forward-paper-collector.ts` imports dotenv, WebSocket/broker clients
and writes journals; it was not executed. Port implements historical T3, no
paper/live strategy activation. Broker historical margin UNKNOWN.

## B. Input contract

Regular375-minuteNIFTY session; exact 09:29 spot and ATM CE/PE closes, historical
IndiaVIX09:29 close, datedowning front expiry and source calendar session distance.
Frozen09:29-only VIX reference snapshot is the retained authority (hash/path in
run fingerprints). VIX absent/nonpositive/nonfinite skips; cannot substitute
dailyclose or nearbyminute. Require at leastone session toexpiry, no expiry day.
No recursive indicator warm-up.

## C. Indicators

No EMA/RSI/ADX. VIX gate15<=VIX<20. ATM nearest50 halfDOWN09:29spot close.
Reference straddle=ATMCEclose+ATMPEclose, positive. Wing width is straddle rounded
UP to next50 using integer paise arithmetic, not a tuned constantwidth.

## D. Signal

Regular session AND VIX gate AND expiry distance>=1 AND all references/identities
available. One attempt per date. Decision uses09:29 completed minute; no momentum
or H30rise/floor25%stop. No simultaneous directional alternatives.

## E. Contract selection

Exactly four same expiry legs: SELL ATMCE, SELL ATMPE, BUY(ATM+wing)CE,
BUY(ATM-wing)PE. Same units all legs. Missing exactwing skips, never intrinsic
modeledwing. DatedGROWWIDs/PIT evidence required. Positive finite quotes and entry
credit strictly between0 andwidth; no₹100 premium floor. Tick identity not
independently certified. No conversion to buy-only.

## F. Orders and exits

Historical allfour09:31OPEN entry, allfour15:01OPEN exit. No target,stop,trailing,
adjustment or re-entry. Entrycredit=shortCE+shortPE-longCE-longPE;
exitdebit=same basket at exit. Grosspoints=credit-debit. Rawexitdebit outside
[-0.000001,width+0.000001] unresolved, raw quotes preserved; no projection.
Open position missing/invalidexit remains exposed and blocks account finality.
This historical endpoint policy does not continuously mark/stop the fly.

## G. Sizing/accounting

Source assumed75, requested boundeddated100% scenario, fresh ₹100,000. Capacity
per unit=width-credit (theoretical maximum loss). Source capital overlay copies
assumed75 per-lot fees timeslots; requested scenario charges actual orderqty once
per leg. Both disclosed. **Executable broker-margin sizing and available
broker-margin cash are NOT_COMPUTABLE** absent historical margin evidence. No
claim that max loss capacity authorizes opening short options. Fee windows as
overview; even a computed theoretical net subtotal is not executable margin P&L.

## H. State machine

IDLE -> VIX/calendar/expiry gate -> four-leg selection ->09:31pending -> four-leg OPEN
->15:01pending -> CLOSED or UNRESOLVED. No partial historical leg fills invented.
Forward paper state machine is deliberately outside this historicalport.
