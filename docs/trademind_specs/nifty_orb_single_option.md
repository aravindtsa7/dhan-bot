# nifty_orb_single_option

## A. Source identity

VERIFIED frozen protocol
`57a727a64782b3b3879a2e0405de1177d5f79a6a577b33d2e6130ca50e8e937d`.
Calendar `src/modules/research/nifty-orb-single-option/orb-study.ts` and
`src/tests/nifty-orb-single-option-run.ts`, source charge/ATM/calendar dependencies;
full SHA in [source_identity.md](source_identity.md). This source actually uses
15 minutes; duration is extracted, not a generic replacement. Prior poor results
are preserved as references. Underlying provider label conflict in overview applies.

## B. Input contract

Complete09:15..09:29underlying1mrange; completedclose scan09:30..10:59. Any missing
requiredscan minute failsclosed ratherthan skippinga potentialfirst signal. Exact
signal close and following entry OPEN for selected dated option, then contiguous required
management/exitpath. Noindicator warm-up orVIXinput.

## C. Indicators

RangeHigh=max09:15..09:29HIGH, RangeLow=minLOW. No EMA, ATR or tuned breakout buffer.
Option exit threshold arithmetic usesinteger paiseJSround (floor(x+0.5)).

## D. Signal

First completed CLOSE strictly>RangeHigh ->BUYCE; strictly<RangeLow ->BUYPE.
Equality notbreakout. One attempt per date: rejected premium/contract/entry doesnot
allow later breakout substitution. Lastsignal10:59START, entry11:00OPEN. No generic
15m high-touchintrabar entry or breakout retry.

## E. Contract selection

ATM nearest50 exact half DOWN at signal underlying CLOSE; exact signalled CEorPEonly,
owning expiry, dated fixedGROWWID/PIT. Signal option CLOSE>100 AND next OPEN>100,
strictly; missing/floor failure skips without buyingboth or switching strike.
Dated lot authority shared; no currentmaster extrapolation.

## F. Orders and exits

OneBUY atsignal minute+1OPEN. Each completed option CLOSE fromentry untiltimeout:
rounded close paise*100>=rounded entry paise*105 target; <=*75 stop. Next OPEN exit,
not barrierprice. Timeout entry+30minutesexact OPEN. Target first, positive price
thresholds disjoint. Gaps may execute beyond threshold at actual next OPEN; required
missingcandle unresolved. No trailing or intrabarHIGH/LOW assumption.

## G. Sizing/accounting

Source baseline100%available cashwhole lots, fresh ₹100,000 per year, source compounding;
separately named requested 100% maycoincide. Entry fees reserved supported netwindow,
no borrowing/short margin. CostNC and equityfreeze rules in overview. No tuningof
stop 25%, floor100, timeout 30 or openingrange despite poor historical outcomes.

## H. State machine

IDLE/rangebuilding -> scan -> first signal selected ->entry pending ->OPEN ->close
target/stop orscheduled timeout ->exit pending ->CLOSED. First attempt rejection ends
session SKIPPED; missing open management endsUNRESOLVED, not asecondattempt.
