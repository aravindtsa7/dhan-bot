# Expiry Intraday

`expiry_intraday` is the separate tenth strategy, supporting NIFTY and SENSEX
weekly index options. MCX is unsupported. Existing `expiry_day` BTST behavior,
defaults, tests and results remain unchanged.

- Default signal: expiry-day 09:20 IST close; atomic CE+PE entry at 09:21 open.
- Signal is configurable from 09:15 through 11:00 using `intraday_entry_signal`.
- Dated exchange expiry rules and holiday shifts determine eligible sessions.
- Select balanced observed CE/PE premiums across ATM, ATM-1 and ATM+1, with a
  default minimum of INR 40 per leg; tie-break by distance to ATM and strike.
- Defaults: TP35%, SL40%, allocation50%; fixed strikes and equal quantities.
- Only combined basket value triggers TP/SL. A losing individual leg never exits
  separately. Target + SL has no automatic trailing. Trailing SL explicitly enables
  combined stepped trailing. Close triggers fill both legs at the next minute open.
- Default cutoff: 15:20 IST (15:15 also available); no overnight scheduling.
- Both legs retain full costs and proportional adverse slippage. Missing fixed
  strikes use the existing stateful continuity model and expiry theta, clearly tagged.

Backtests and Live Paper Trading expose the strategy under its own name. The badge
is **EXPIRY INTRADAY (CE+PE)**. Ledger/chart show both leg premiums, combined debit,
return, signal and execution times. Save Result and named Save Strategy retain the
morning timing. Paper requires fresh real quotes and two contracts expiring that day;
modeled quotes are never used live.

## Verification

The 369-test offline suite passes with zero external network attempts. The complete
120-combination matrix is in [the audit](README_STRATEGY_AUDIT.md#audit-results).
Below: INR 100,000, one dated lot, 50% allocation, TP35/SL40, minimum premium40,
Target + SL, 09:20 signal / 09:21 fill, 15:20 cutoff. Both chart intervals use the
same minute-based schedule and combined risk. Net includes slippage and fees.

| Window | Instrument | Interval | Strategy | Net INR | Win % | Trades | Profit factor | Max DD INR | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| recent | NIFTY | 5m | `expiry_intraday` | 3,209.50 | 58.33 | 12 | 1.3043 | 7,415.32 | modeled |
| recent | NIFTY | 15m | `expiry_intraday` | 3,209.50 | 58.33 | 12 | 1.3043 | 7,415.32 | modeled |
| recent | SENSEX | 5m | `expiry_intraday` | -57.55 | 45.45 | 11 | 0.9954 | 10,928.10 | modeled |
| recent | SENSEX | 15m | `expiry_intraday` | -57.55 | 45.45 | 11 | 0.9954 | 10,928.10 | modeled |
| full | NIFTY | 5m | `expiry_intraday` | -87,998.53 | 38.10 | 84 | 0.5188 | 94,650.42 | repaired |
| full | NIFTY | 15m | `expiry_intraday` | -87,998.53 | 38.10 | 84 | 0.5188 | 94,650.42 | repaired |
| full | SENSEX | 5m | `expiry_intraday` | -55,423.32 | 39.74 | 78 | 0.5857 | 67,187.02 | modeled |
| full | SENSEX | 15m | `expiry_intraday` | -55,423.32 | 39.74 | 78 | 0.5857 | 67,187.02 | modeled |

Full-history losses and drawdowns are material. Modeled rolling WEEK1 results do
not establish performance on contracts with verified expiry identity.
