# Moomoo Paper Trading Bot & Walk-Forward Quant Dashboard

Open-source Moomoo OpenD paper-trading system for US.APP with a local SQLite
market-data cache, selectable candle intervals, and a causal daily candle-open grid-cross strategy,
walk-forward-only parameter optimization, risk APIs, audit logs, and a browser
dashboard. Designed for research and paper competitions on macOS Apple Silicon.

**Keywords:** Moomoo paper trading bot, Moomoo OpenD API, quantitative trading,
algorithmic trading, APP stock, price grid strategy, candle open crossing, walk-forward
optimization, SQLite market data, paper trading dashboard, Apple Silicon, Rust.

> Research and paper trading only. This project does not provide financial
> advice and does not guarantee performance. Live trading is intentionally not
> supported by the dashboard.

Research and backtesting code for a long-only daily price-grid strategy driven
only by candle-open crossings.

## Highlights

- Moomoo OpenD candle ingestion with SQLite persistence.
- Strict walk-forward testing: parameters are selected only on prior data and
  scored only on the next unseen fold.
- Shared strategy, data, risk, audit, and execution interfaces in
  `quant_api.py`.
- Paper execution is restricted to the configured Moomoo SIMULATE account; live
  trading is not enabled by the dashboard.
- Local dashboard for charts, trades, optimizer progress, risk decisions, and
  execution status.
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

- `competition_bot.py` — US.APP paper-trading process.
- `quant_dashboard.py` — local Moomoo-backed web console and tester.
- `quant_storage.py` — SQLite candle, run, trade, and event storage.
- `strategy_57pct.py` — strategy and walk-forward evaluation logic.
- `quant_api.py` — shared data, risk, strategy, and audit interfaces.
- `watch_moomoo_paper_stack.sh` — local health check for OpenD, bot, and dashboard.
- `docs/PAPER_BOT_OPERATIONS.md` — current runtime and troubleshooting reference.

Moomoo OpenD must be running on `127.0.0.1:11111` to fetch fresh candles. Do not commit API keys or credentials; use the macOS Keychain or environment variables locally.

## Moomoo paper-competition bot

`competition_bot.py` is the paper-execution boundary. The installed launchd job
runs US.APP against the explicit Moomoo SIMULATE account `COMPETITION_ACCOUNT_ID`, with a
10-minute bar interval by default. The bot does not place live orders.

Start Moomoo OpenD and log in, then identify the US paper account index:

```sh
python3 competition_bot.py --list-accounts
```

Run a signal-only check:

```sh
python3 competition_bot.py APP --acc-id COMPETITION_ACCOUNT_ID --bar-interval 10 --live-paper
```

Use the dashboard or launchd service for normal operation. See
[`docs/PAPER_BOT_OPERATIONS.md`](docs/PAPER_BOT_OPERATIONS.md) for checks and
troubleshooting.

## Moomoo Quant Dashboard

`quant_dashboard.py` combines the tester, optimizer, charts, trade list, and
paper-bot controls in one local dashboard. It binds to loopback only:

```sh
python3 quant_dashboard.py
open http://127.0.0.1:8780
```

The dashboard fetches `US.APP` candles directly from Moomoo OpenD with
`request_history_kline`, caches them in `quant_dashboard.sqlite3`, and uses
those cached candles for the tester. A test is limited to 30 calendar days of
scored data and adds a strategy warm-up period before that window. The Stop
tester button cancels an active run. It stores each run, optimizer result,
trade, and paper execution event in SQLite. The dashboard reports return,
ending equity, maximum drawdown, trades, win rate, profit factor, average
trade, Sharpe approximation, equity curve, monthly returns, optimizer
rankings, and the complete trade list. Paper mode remains SIMULATE-only.
