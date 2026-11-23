# Moomoo Paper Trading Bot & Native Research App

Open-source Moomoo OpenD paper-trading system for US.ANT with a local SQLite
market-data cache, selectable candle intervals, and a causal daily candle-open grid-cross strategy,
walk-forward-only parameter optimization, risk APIs, audit logs, and a native
macOS app. Designed for research and paper competitions on macOS Apple Silicon.

**Keywords:** Moomoo paper trading bot, Moomoo OpenD API, quantitative trading,
algorithmic trading, ANT stock, price grid strategy, candle open crossing, walk-forward
optimization, SQLite market data, native paper trading app, Apple Silicon, Rust.

> Research and paper trading only. This project does not provide financial
> advice and does not guarantee performance. Live trading is intentionally not
> supported by the native app.

Research and backtesting code for a long-only daily price-grid strategy driven
only by candle-open crossings.

## Highlights

- Moomoo OpenD candle ingestion with SQLite persistence.
- Deterministic randomized research over a bounded five-session window.
- Shared strategy, data, risk, audit, and execution interfaces in
  `quant_api.py`.
- Paper execution is restricted to the configured Moomoo SIMULATE account; live
  trading is not enabled.
- Native macOS app for bot controls, status, and research progress.
- Apple-Silicon-oriented Rust control-plane work in `thomas-ai/`.

## Candle-open grid baseline

The active strategy uses the prior completed daily candles to calculate a rolling top
and bottom. Two evenly spaced grid lines are inserted between them. It plots
only candle open, candle close, and those four price-grid lines:

| Setting | Value |
|---|---:|
| Grid lookback | 45 completed daily candles |
| Grid levels | Top, 2/4 midpoint, 1/4 midpoint, bottom |
| Entry | Candle open crosses upward through any grid level |
| Exit | Candle open crosses downward through any grid level while long |
| Other indicators | None; no EMA, ATR, take-profit, stop, or trailing exit |
| Slippage | 2 bps per side |
| Position | Long only, one position |

This is the research baseline, not a claim about the currently selected paper-bot
interval. The live paper bot defaults to 10-minute candles; the supported
intervals and operational limits are documented in
[`docs/PAPER_BOT_OPERATIONS.md`](docs/PAPER_BOT_OPERATIONS.md).

The 2 bps figure is an execution assumption, not a guarantee. Results depend on the data vendor, fills, commissions, liquidity, and date range.

## Files

- `competition_bot.py` — US.ANT paper-trading process.
- `market_data.py` — local Moomoo 1-minute ingestion used by native research.
- `DesktopApp.swift` — native macOS AppKit GUI source for runtime status, paper-bot controls, and interval research.
- `ai_gateway/` — bundled Rust Multi-Model AI Gateway and its MCP bridge; the strategy lab no longer depends on a sibling project.
- `scripts/app_minute_interval_sweep.py` — US.ANT 1m-through-21m research sweep built from the single 1m cache and limited to the latest 5 sessions.
- `ops/start_ai_gateway.sh` — starts the bundled gateway on `127.0.0.1:8788`.
- `quant_storage.py` — SQLite candle, run, trade, and event storage.
- `strategy_57pct.py` — strategy and walk-forward evaluation logic.
- `quant_api.py` — shared data, risk, strategy, and audit interfaces.
- `watch_moomoo_paper_stack.sh` — local health check for OpenD and the bot.
- `docs/PAPER_BOT_OPERATIONS.md` — current runtime and troubleshooting reference.

The native app starts the bundled AI gateway when needed. The interval sweep
calls the bundled `ai_gateway/ai/mcp_server.py` plugin, which routes bounded
proposal requests through the gateway; the local deterministic evaluator still
owns all scoring and paper-bot configuration changes.

Moomoo OpenD must be running on `127.0.0.1:11111` to fetch fresh candles. Do not commit API keys or credentials; use the macOS Keychain or environment variables locally.

## Moomoo paper-competition bot

`competition_bot.py` is the paper-execution boundary. The installed launchd job
runs US.ANT against the explicit Moomoo SIMULATE account `COMPETITION_ACCOUNT_ID`. It uses the
interval and parameters published by the latest completed research sweep (10m
is only the initial fallback). The bot does not place live orders.

Start Moomoo OpenD and log in, then identify the US paper account index:

```sh
python3 competition_bot.py --list-accounts
```

Run a signal-only check:

```sh
python3 competition_bot.py ANT --acc-id COMPETITION_ACCOUNT_ID --bar-interval 2 --live-paper
```

Use the native app or launchd service for normal operation. See
[`docs/PAPER_BOT_OPERATIONS.md`](docs/PAPER_BOT_OPERATIONS.md) for checks and
troubleshooting.

## Research engine

Research fetches only `US.ANT` 1-minute candles from Moomoo OpenD with
`request_history_kline`, caches the source candles in `quant_dashboard.sqlite3`,
and aggregates complete regular-session bars locally for each requested interval
from 1m through 21m. The interval sweep uses the latest 5 completed US sessions.
It stores each run, optimizer result, trade, and paper execution event in SQLite.
The research output reports return,
ending equity, maximum drawdown, trades, win rate, profit factor, average
trade, Sharpe approximation, equity curve, monthly returns, optimizer
rankings, and the complete trade list. Paper mode remains SIMULATE-only.

To compare every locally aggregated interval, run:

```sh
python3 scripts/app_minute_interval_sweep.py --start 2026-01-01 --end 2026-11-23
```

The final `winner` record in `strategy_lab_runs/ANT-MINUTE-*/summary.json` identifies the highest-return tested interval and parameters over the 5-session window. A completed sweep publishes that configuration to the SIMULATE paper bot; no live trading is enabled.

## Native macOS GUI

The desktop app provides the day-to-day control panel without requiring a browser.
It controls the launchd-managed runtime directly, with the SIMULATE-only
execution boundary shared by the bot:

```sh
open -a "/Applications/US.ANT Paper Trader.app"
```

The GUI shows bot health and account, provides refresh/start/stop controls, and runs the 100,000-
candidate 1m-through-21m sweep over 5 sessions in the background. A completed
sweep publishes its highest-return tested interval and parameters to the
SIMULATE paper bot. Closing the GUI does not stop the paper bot.
The evaluator uses four worker processes for the randomized tests.

The installed launchd research job checks continuously, waiting seven minutes
after each check/run completes. It runs only when a new cached US session is
available, prevents overlapping sweeps, and records its state in the scheduler
state file.
