# Candle-open grid-cross strategy

`GRID_OPEN_CROSS_1D` replaces the previous EMA/Donchian and profit-lock entry
and exit logic in the active dashboard/API path.

## What is plotted

Only six price series are used:

- candle open
- candle close
- rolling grid top
- rolling grid bottom
- a line one quarter of the range above the bottom
- a line two quarters of the range above the bottom

The default range is the highest high and lowest low from the previous 45
completed daily candles. The current candle is excluded from its own grid, which
prevents look-ahead bias.

## Signals

For each candle, the strategy compares the current open with the previous open
and the corresponding prior grid line:

- upward crossing of any grid line: buy when flat
- downward crossing of any grid line: close the long position

The close line is visual context only. It does not create a signal. There are no
EMA, ATR, take-profit, stop-loss, trailing-stop, session-colour, or regime
filters in the active implementation. It is long-only and never opens a short.

The research engine fills at the current candle open. The Moomoo strategy reads
the completed indicator value (`select=1`) and submits at the current market
price, so live/paper execution can differ by one bar or by spread.

## Files

- `strategy_57pct.py`: active Python research/API implementation.
- `quant_api.py`: public strategy name and service boundary.
- `quant_dashboard.py`: walk-forward dashboard integration.
- `BBI_5m_Backtest/local_quant_tester.py`: chart renderer; it plots only the
  grid and candle lines for dashboard results.
- `Moomoo_Quant_GridOpenCross_1D.py`: importable Moomoo OpenD daily strategy.

## Current validation

The existing local cache contains 19,488 intraday US.ANT candles, not daily
candles. The dashboard now requests and stores `interval_minutes=1440` daily
candles from Moomoo OpenD. A daily backtest should be run after that fetch;
intraday results must not be treated as daily results. Keep the strategy in
research or simulated paper mode until an out-of-sample daily replay supports
it.
