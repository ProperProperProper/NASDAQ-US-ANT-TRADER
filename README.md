# Moomoo Paper Trading Bot & Native Research App

Open-source Moomoo OpenD paper-trading system for one configurable US symbol with a local SQLite
market-data cache, selectable 1–21 minute candle intervals, a causal candle-open
grid-cross strategy, bounded research, risk APIs, audit logs, and a native macOS
app. Designed for research and SIMULATE paper trading on Apple Silicon.

**Keywords:** Moomoo paper trading bot, Moomoo OpenD API, quantitative trading,
algorithmic trading, ANT stock, price grid strategy, candle open crossing, walk-forward
optimization, SQLite market data, native paper trading app, Apple Silicon, Rust.

> Research and paper trading only. This project does not provide financial
> advice and does not guarantee performance. Live trading is intentionally not
> supported by the native app.

Research and backtesting code for a long-only intraday price-grid strategy driven
only by completed-candle open crossings.

## Highlights

- Moomoo OpenD candle ingestion with SQLite persistence.
- Deterministic randomized research over up to the latest five completed sessions.
- Shared strategy, data, risk, audit, and execution interfaces in
  `quant_api.py`.
- Paper execution is restricted to the configured Moomoo SIMULATE account; live
  trading is not enabled.
- Native macOS app for bot controls, status, and research progress.
- Bundled Apple-Silicon-oriented Rust AI gateway in `ai_gateway/`.

## Candle-open grid baseline

The active strategy uses prior completed candles to calculate a rolling top
and bottom. Two evenly spaced grid lines are inserted between them. It plots
only candle open, candle close, and those four price-grid lines:

| Setting | Value |
|---|---:|
| Grid lookback | 45 completed candles |
| Grid levels | Top, 2/4 midpoint, 1/4 midpoint, bottom |
| Entry | Candle open crosses upward through any grid level |
| Exit | Candle open crosses downward through any grid level while long |
| Other signal indicators | None; execution risk exits are handled separately by the bot |
| Slippage | 2 bps per side |
| Position | Long only, one open position maximum across SIMULATE |

This is the research baseline, not a claim about the currently selected paper-bot
interval. The live paper bot defaults to 10-minute candles; the supported
intervals and operational limits are documented in
[`docs/PAPER_BOT_OPERATIONS.md`](docs/PAPER_BOT_OPERATIONS.md).

The 2 bps figure is an execution assumption, not a guarantee. Results depend on the data vendor, fills, commissions, liquidity, and date range.

## Files

- `competition_bot.py` — concurrent portfolio paper-trading process.
- `ai_gateway/src/bin/trading_engine.rs` — Rust supervisor for the paper bot, research, gateway, and MLX workers.
- `market_data.py` — local Moomoo 1-minute ingestion used by native research.
- `DesktopApp.swift` — native macOS AppKit GUI source for runtime status, paper-bot controls, and interval research.
- `ai_gateway/` — bundled Rust Multi-Model AI Gateway and its MCP bridge; the strategy lab no longer depends on a sibling project.
- `docs/AI_ENGINEERING_CONTROLS.md` — provider-neutral AI safety, tool, prompt, evaluation, and release controls.
- `scripts/app_minute_interval_sweep.py` — per-symbol 1m-through-10m research sweep built from each 1m cache and limited to the latest 5 sessions.
- `ops/start_ai_gateway.sh` — starts the bundled gateway on `127.0.0.1:8788`.
- `quant_storage.py` — SQLite candle, run, trade, and event storage.
- `strategy_57pct.py` — strategy and walk-forward evaluation logic.
- `quant_api.py` — shared data, risk, strategy, and audit interfaces.
- `watch_moomoo_paper_stack.sh` — local health check for OpenD and the bot.
- `docs/PAPER_BOT_OPERATIONS.md` — current runtime and troubleshooting reference.

The native app controls the single Rust engine launch agent. The interval sweep
calls the bundled `ai_gateway/ai/mcp_server.py` plugin, which routes bounded
proposal requests through the gateway; the local deterministic evaluator still
owns all scoring and paper-bot configuration changes.

Moomoo OpenD must be running on `127.0.0.1:11111` to fetch fresh candles. Do not commit API keys or credentials; use the macOS Keychain or environment variables locally.

## Moomoo SIMULATE paper bot

`competition_bot.py` is the paper-execution boundary. The installed Rust engine
supervises it against the locally configured standard Moomoo SIMULATE account. It uses the
interval and parameters published by the latest completed research sweep (10m
is only the initial fallback). The bot does not place live orders.

Start Moomoo OpenD and log in, then identify the US paper account index:

```sh
python3 competition_bot.py --list-accounts
```

Run a paper-only check (omit `--live-paper` for signal-only mode):

```sh
PAPER_ACCOUNT_ID='<local SIMULATE account ID>' python3 competition_bot.py US.ANT --bar-interval 2
```

The active portfolio is stored in `trading_system.json`. Change it through the
guarded engine command with a comma-separated symbol list:

```sh
ai_gateway/target/release/trading_engine portfolio-set US.AAPL,US.ADP,US.ALRM,US.ADMA
```

The engine stops the paper and research workers, verifies the standard SIMULATE
account, validates every Moomoo quote, and restarts with isolated per-symbol
state. It refuses the change when a current or requested symbol has a position
or active order. Each symbol has separate strategy, promotion, and research data.

The native app also accepts a comma-separated list such as `US.ANT,US.AAPL`
for research. **Test symbols 1–10m** fetches each symbol's 1-minute candles,
tests every interval from 1 through 10 with four evaluator workers, stores the
individual runs in SQLite, and writes a ranked local summary under
`multi_symbol_results/`. Testing does not change the active execution symbol.

Use the native app or launchd service for normal operation. See
[`docs/PAPER_BOT_OPERATIONS.md`](docs/PAPER_BOT_OPERATIONS.md) for checks and
troubleshooting.

## Research engine

Research fetches each configured portfolio symbol's 1-minute candles from Moomoo OpenD with
`request_history_kline`, caches the source candles in `quant_dashboard.sqlite3`,
and aggregates complete regular-session bars locally for each requested interval
from 1m through 10m. The interval sweep uses up to the latest 5 completed US
sessions; a newly listed symbol may have fewer sessions available.
It stores each run, optimizer result, trade, and paper execution event in SQLite.
The research output reports return,
ending equity, maximum drawdown, trades, win rate, profit factor, average
trade, Sharpe approximation, equity curve, monthly returns, optimizer
rankings, and the complete trade list. Paper mode remains SIMULATE-only.

To compare every locally aggregated interval, run:

```sh
python3 scripts/app_minute_interval_sweep.py --start 2026-01-01 --end 2026-11-23
```

The final `winner` record under `research/<symbol>/runs/` identifies the highest-return tested interval and parameters over the available session window. A completed sweep queues a same-symbol configuration only after at least two completed sessions and at least two trades support a normal-evidence result; otherwise the candidate is held for research and the existing SIMULATE configuration remains active. No live trading is enabled.

## Native macOS GUI

The desktop app provides the day-to-day control panel without requiring a browser.
It controls the launchd-managed runtime directly, with the SIMULATE-only
execution boundary shared by the bot:

```sh
open -a "/Applications/Moomoo Paper Trader.app"
```

The GUI shows bot health and account, provides refresh/start/stop controls, maps each tested
interval to return, ending equity, and trade count, and runs the 1m-through-10m sweep in the
background. The scheduler runs a larger AI-assisted parameter sweep during US pre-market and
smaller fine-tuning sweeps every 15 minutes during regular trading hours. Grid interval count,
lookback, entry/exit mode, and bounded trailing parameters are tested. An eligible winner is
automatically queued for the SIMULATE paper bot, which applies it only when the broker is flat
with no pending order. Closing the GUI does not stop the paper bot. Each research sweep uses up to four
evaluator workers at low priority; the engine keeps paper execution in a separate worker.

The engine's research worker checks continuously every minute. It runs
for a new cached US session immediately, then reruns on the same cached data 15
minutes after the previous successful sweep completes. Research runs at low
CPU priority in its own process. Promotion requires at least two completed
sessions, two trades, positive returns after costs, and a better result than
the current paper strategy on the tested data. It prevents overlapping sweeps and records its state in the scheduler state
file.
