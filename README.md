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

- `BBI_5m_Backtest/donchian_ema_10m_baseline_backtest.py` — 10-minute baseline backtester.
- `BBI_5m_Backtest/donchian_ema_10m_research.py` — cost sensitivity, holdout, and walk-forward research harness.
- `BBI_5m_Backtest/bbi_bull_switch_backtest.py` — BBI bull-switch comparison backtester.
- `Moomoo_Quant_Donchian_EMA_10m.py` — preserved 6.38% TP Quant baseline.
- `Moomoo_Quant_Donchian_EMA_10m_8pctTP.py` — promoted 8% TP Quant candidate.
- `Moomoo_Quant_Donchian_EMA_10m_GreenYellowRed.py` — exit-only Green/Yellow/Red candidate.
- `EXIT_OPTIMIZATION_RESULTS.md` — frozen-entry exit optimization results.
- `BBI_5m_Backtest/US_APP_donchian_ema_10m_optimized.csv` — saved baseline trades.
- `BBI_5m_Backtest/US_APP_donchian_ema_research.json` — latest robustness report.
- `BBI_5m_Backtest/bhp_profit_lock_daily_backtest.py` — daily ASX BHP backtester for the exact profit-lock/yellow-exit strategy, including optimization.
- `BBI_5m_Backtest/local_quant_tester.py` — local Quant-style engine and self-contained HTML report with price/trade, equity, drawdown, monthly-return, and trade tables.
- `Moomoo_Quant_GridOpenCross_1D.py` — Moomoo daily strategy with only candle open/close and four price-grid levels.
- `BBI_5m_Backtest/local_quant_portal.py` — local browser portal for selecting ASX 200 symbols, rerunning the optimizer, and viewing charts/stats.

Moomoo OpenD must be running on `127.0.0.1:11111` to fetch fresh candles. Do not commit API keys or credentials; use the macOS Keychain or environment variables locally.

## Thomas AI

The native Apple-Silicon AI control plane is in `thomas-ai/`. It uses Rust for
the gateway, routing, UI serving, CLI, NVIDIA Keychain access, and process
orchestration, with MLX-LM retained as the Python runtime boundary. It routes
ordinary prompts to local MLX and difficult prompts to the configured NVIDIA
API model. See [THOMAS_AI.md](THOMAS_AI.md).

## Research note

The original 2 bps baseline remains the profit benchmark. The research harness is for validation and should not automatically replace the baseline with a lower-return parameter set.

The baseline is immutable. Its locked Git tag is `baseline-2bps-6.38tp`; all exit experiments, including green/yellow/red state models, must use separate files and candidate versions.

The promoted 8% TP candidate tested at +53.86% compounded return with approximately -11.10% maximum drawdown over the full test period. It is preserved separately because its Jan–June split was weaker than the 6.38% baseline.

This is research software, not financial advice or a promise of future performance.

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

For an ASX BHP test, export daily candles from Moomoo Quant and run:

```sh
python3 BBI_5m_Backtest/bhp_profit_lock_daily_backtest.py \\
  --csv /path/to/BHP_daily.csv --optimize
```

The script does not alter Moomoo files or place orders.

To generate the local Quant-style report:

```sh
python3 BBI_5m_Backtest/local_quant_tester.py \\
  --csv /path/to/CDA_daily.csv --symbol CDA --optimize
open local_quant_report/report.html
```

For an interactive portal:

```sh
python3 BBI_5m_Backtest/local_quant_portal.py
open http://127.0.0.1:8765
```
