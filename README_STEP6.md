# Step 6 — live quotes, virtual trades

`step6_paper_trader.py` connects directly to DhanHQ v2's binary WebSocket feed. It shares signal generation, whole-lot sizing, tick rounding and cost calculations with the research engine. **All executions are local simulations. There are no broker order placement, modification or cancellation calls.**

## Implemented scope

| Stream | Use |
| --- | --- |
| Nifty 50 / Sensex | LTP monitoring; index snapshots after market close are marked stale |
| Crude Oil | Default H4: compressed 17:00–17:30 range breakout with ADX/ATR filters, 5-minute completed bars |
| Crude Oil / Natural Gas | Optional H6: ATR expansion, volume surge and momentum breakout, 3-minute completed bars |

Expiry-gamma option execution is not implemented here. One selected futures instrument and one position per account are supported. The nearest future expiring **after** today is selected from the cached Dhan master; an existing position keeps its exact security ID across restarts and contract rolls.

Default virtual account: ₹500,000, 50% allocation ceiling, one fixed lot, assumed 20% futures margin, ₹1,500 daily loss cutoff, ₹2,000 daily profit lock, ATR stop multiplier 2, reward/risk 3, trailing jump 0.5 × initial stop distance. Allocation is a ceiling, not a requirement to deploy that percentage. H4 permits one entry per day. Entry window is 17:30–21:30 IST; square-off is requested at 23:15 IST and the process shuts down at 23:16 IST. Start it each trading day; it is not a daily scheduler. These are research settings, not a promise of daily profit.

## Run in PowerShell

From `C:\Dhan\dhan-bot`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Offline execution demonstration; separate synthetic ledger under paper_demo.
.\.venv\Scripts\python.exe step6_paper_trader.py --mode demo
# Receive live quotes without opening new positions; bounded connectivity check.
.\.venv\Scripts\python.exe step6_paper_trader.py --monitor-only --duration 30
# Live paper strategy: virtual Crude Oil H4 orders only.
.\.venv\Scripts\python.exe step6_paper_trader.py
```

The existing `.env` supplies `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN`. Tokens are never command-line arguments or log output. On an authentication failure, the runner stops and retains any pending paper position. Refresh credentials with the existing `step1_health_check.py` flow and restart with the same account settings. Data API access is required for the candle warmup. No broker account balance or broker margin is used.

Optional H6 account:

```powershell
.\.venv\Scripts\python.exe step6_paper_trader.py --strategy h6 --trade-instrument NATURALGAS --output data_lake/paper_ng
```

Use `--help` for capital, allocation, assumed margin, lots, daily profit/loss limits, ATR stop, reward/risk, slippage and trailing-jump controls. Existing accounts reject changed strategy/risk settings; use a new output directory for a different experiment. Each directory is a **separate** virtual account with independent capital and risk limits.

## Background operation

```powershell
# Hidden process; default Crude Oil H4 virtual account.
# Run these commands directly in PowerShell (no .ps1 execution policy change).
$paperRoot = 'C:\Dhan\dhan-bot'
$paperOutput = Join-Path $paperRoot 'data_lake\paper_trades'
New-Item -ItemType Directory -Force -Path $paperOutput | Out-Null
$paperProcess = Start-Process -FilePath "$paperRoot\.venv\Scripts\python.exe" `
    -ArgumentList '-u', 'step6_paper_trader.py' -WorkingDirectory $paperRoot `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput "$paperOutput\background.stdout.log" `
    -RedirectStandardError "$paperOutput\background.stderr.log"
$paperProcess.Id
# Read live updates (Ctrl+C here stops only this log viewer).
Get-Content .\data_lake\paper_trades\paper_trader.log -Tail 20 -Wait
```

For a bounded background connectivity check, replace the `-ArgumentList` line with:

```powershell
-ArgumentList '-u', 'step6_paper_trader.py', '--monitor-only', '--duration', '30' -WorkingDirectory $paperRoot `
```

Graceful stop from another terminal:

```powershell
New-Item -ItemType File -Force .\data_lake\paper_trades\STOP
Get-Content .\data_lake\paper_trades\last_run.json
```

Wait for a new `stopped_at` in `last_run.json` and `STOPPED` in the log. The runner tries to close an open virtual position on a fresh quote, allowing up to 10 seconds for recovery; network connection/REST timeouts can add shutdown delay. If no fresh quote arrives, the position remains explicitly `OPEN` with `pending_exit` in the durable state. Restarting resumes that exit before allowing another entry. Do not delete the account or force-kill it to make an open trade disappear.

After reviewing the state, remove the stop request before restarting:

```powershell
Remove-Item -LiteralPath .\data_lake\paper_trades\STOP
# Repeat the Start-Process command above with the same risk settings.
```

An OS lock prevents two writers for the same account. Keep the machine awake and connected; closing the terminal does not stop the hidden process, but sleep/reboot does. `paper_trader.log` rotates at 2 MB with three backups. Background stdout/stderr files are replaced on each launch.

## Execution and data semantics

- Stops, targets, trailing steps and net daily locks are checked on every accepted tick. Daily limits include realized P&L plus the open position's estimated liquidation P&L after costs. Once reached, the daily lock is latched. Gaps, spreads and slippage can cause a loss beyond the configured cutoff.
- Virtual market fills use best ask for buys / best bid for sells, plus adverse slippage and tick rounding; missing valid depth falls back to LTP plus slippage, recorded in the ledger. Default slippage is 1 Crude point / 0.5 Natural Gas points per side. Quantity uses economic multipliers 100 / 1,250. This model does not simulate queue priority, depth depletion, partial fills or broker margin changes. It reuses the backtest's brokerage/tax assumptions, not actual contract notes.
- Quotes older than 15 seconds, more than 2 seconds in the future, or out of chronological order are rejected for execution. WebSocket LTT uses Dhan's IST wall-clock epoch convention, normalized by subtracting 19,800 seconds; the raw epoch is retained in status. REST chart timestamps are handled separately as standard epoch seconds. The system clock must be correct.
- Same-day completed 1-minute candles are warmed from the read-only `/v2/charts/intraday` route and refreshed once a minute. Warmup never replays historical entries. Volume is derived from cumulative-volume differences on live ticks. Partial or disconnected minutes are invalidated rather than filled forward. Entries require uninterrupted completed candles from 16:30 through the signal boundary. No complete warmup means no entry.
- A signal is considered only at its current completed-bar boundary, within 15 seconds. Reconnects retry with capped exponential delays. Missing quotes pause entries; an exit is never fabricated. Nifty/Sensex monitoring does not create option trades.

## Files and verification

### Browser monitoring (Step 7)

Open `http://127.0.0.1:8501` and select **📊 Live Paper Trading Room**.
The read-only tab refreshes the default `data_lake/paper_trades` account every
3 seconds, showing lifetime trade counts, open positions, current net liquidation
P&L, closed net profit/loss, win rate, and the full color-coded downloadable ledger.
Historical research sidebar filters do not change this account view.

Bot status combines the `STOP` flag, heartbeat time and shutdown records. An absent
`STOP` file does not prove the bot is running. Heartbeats older than 35 seconds and
quotes older than 15 seconds are marked stale; missing live P&L remains unavailable.
The bot normally writes status every 10 seconds, so refreshing the browser faster
does not create more frequent quotes. Start/restart the updated paper runner to
publish the new per-position valuation snapshots; older snapshots remain readable
but cannot provide identity-verified live position P&L. The tab does not start or
stop the bot, and does not read `.env` or call Dhan.

Start the web UI if needed:

```powershell
.\.venv\Scripts\python.exe -m streamlit run quant_ui.py --server.address 127.0.0.1 --server.port 8501
```

`data_lake/paper_trades/` contains:

| File | Purpose |
| --- | --- |
| `ledger.csv` | Every virtual trade, including explicit OPEN rows; contract, quantity, timestamps, prices, stops, capital, itemized costs, net P&L and exit reason |
| `state.json` | Authoritative restart state, risk policy, daily locks and signal IDs; ledger rebuilt from this state |
| `status.json` | Latest quotes, freshness, connection state, position count, realized and open P&L |
| `last_run.json` | Shutdown time, accepted/fresh tick counts, rejected ticks, warmup count and open positions |
| `paper_trader.log` | Human-readable LTP, entry, stop adjustment, exit and lock updates |
| `process.json` | Last launched PID and time; not a substitute for the OS account lock |

JSON state is atomically replaced before the CSV snapshot; restart repairs a stale CSV from state. Ledger files should not be held open by applications that lock writes.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

The paper tests cover packet decoding, IST normalization, completed-candle causality, H4 positive signals, long/short exits, gap fills, daily net locks, trailing stops, missing-quote square-off, allocation, contract recovery and process locking. Demo P&L is synthetic; a monitor-only live test verifies transport and warmup, not strategy profitability.

Protocol references: [Dhan v2 live feed](https://dhanhq.co/docs/v2/live-market-feed/), [Dhan support's LTT clarification](https://madefortrade.in/t/ticker-packet-structure-in-your-live-market-feed-last-trade-time-ltt-field/57896), [historical candles](https://dhanhq.co/docs/v2/historical-data/), [instrument master](https://dhanhq.co/docs/v2/instruments/).
