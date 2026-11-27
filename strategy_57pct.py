"""Intraday candle-open price-grid strategy used by the bot and native research app."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

import numpy as np
import pandas as pd

from us_market_calendar import session_exit_time


BASELINE = {
    "grid_lookback": 45,
    "grid_intervals": 4,
    "entry_mode": "any_grid_cross",
    "exit_mode": "any_grid_cross_down",
    "take_profit_pct": 0.0274,
    "stop_loss_pct": 0.1324,
    "trailing_activation_pct": 0.0,
    "trailing_stop_pct": 0.0,
    "open_delay_minutes": 5,
}
GRID_BASELINE = BASELINE
ALLOCATION = 0.95
ENTRY_MODES = {"any_grid_cross", "top_breakout", "upper_half_cross", "lower_half_cross"}
EXIT_MODES = {"any_grid_cross_down", "top_breakdown", "bottom_breakdown", "lower_half_breakdown"}


def validate_strategy_params(params: dict | None) -> dict:
    """Return a complete, finite, bounded strategy configuration or fail closed."""
    if params is not None and not isinstance(params, dict):
        raise ValueError("strategy params must be an object")
    merged = {**BASELINE, **(params or {})}
    try:
        merged["grid_lookback"] = int(merged["grid_lookback"])
        merged["grid_intervals"] = int(merged["grid_intervals"])
        merged["open_delay_minutes"] = int(merged.get("open_delay_minutes", 5))
        for key in ("take_profit_pct", "stop_loss_pct", "trailing_activation_pct", "trailing_stop_pct"):
            merged[key] = float(merged[key])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("strategy parameters must be numeric") from exc
    if not 2 <= merged["grid_lookback"] <= 240:
        raise ValueError("grid_lookback must be between 2 and 240")
    if not 2 <= merged["grid_intervals"] <= 20:
        raise ValueError("grid_intervals must be between 2 and 20")
    if not 0 <= merged["open_delay_minutes"] <= 60:
        raise ValueError("open_delay_minutes must be between 0 and 60")
    if merged["entry_mode"] not in ENTRY_MODES:
        raise ValueError(f"unsupported entry_mode: {merged['entry_mode']}")
    if merged["exit_mode"] not in EXIT_MODES:
        raise ValueError(f"unsupported exit_mode: {merged['exit_mode']}")
    for key in ("take_profit_pct", "stop_loss_pct", "trailing_activation_pct", "trailing_stop_pct"):
        if not math.isfinite(merged[key]) or not 0.0 <= merged[key] <= 1.0:
            raise ValueError(f"{key} must be finite and between 0 and 1")
    return merged


@dataclass
class Trade:
    signal_time: str
    entry_time: str
    entry_price: float
    qty: int
    exit_time: str
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    return_pct: float
    reason: str


def _validate_candles(candles: pd.DataFrame) -> None:
    if not candles.index.is_monotonic_increasing:
        raise ValueError("candles must be sorted chronologically")
    if candles.index.has_duplicates:
        raise ValueError("duplicate candle timestamps are not allowed")
    missing = {"open", "high", "low", "close"}.difference(candles.columns)
    if missing:
        raise ValueError(f"missing candle columns: {sorted(missing)}")


def _validate_trades(trades) -> None:
    previous_exit = None
    seen = set()
    for trade in trades:
        key = (trade.signal_time, trade.entry_time, trade.exit_time)
        if key in seen:
            raise ValueError("duplicate trade detected")
        seen.add(key)
        if not (trade.signal_time <= trade.entry_time <= trade.exit_time):
            raise ValueError("invalid trade chronology detected")
        if previous_exit is not None and trade.entry_time < previous_exit:
            raise ValueError("overlapping trades detected")
        previous_exit = trade.exit_time


def add_features(candles: pd.DataFrame, params: dict = BASELINE) -> pd.DataFrame:
    """Create only OHLC and four causal price-grid levels."""
    params = validate_strategy_params(params)
    x = candles.copy().sort_index()
    lookback = max(2, int(params.get("grid_lookback", 45)))
    intervals = max(2, min(20, int(params.get("grid_intervals", 4))))
    prior_high = x["high"].astype(float).shift(1).rolling(lookback, min_periods=lookback).max()
    prior_low = x["low"].astype(float).shift(1).rolling(lookback, min_periods=lookback).min()
    span = prior_high - prior_low
    fractions = [0.0, 0.25, 0.50, 1.0]
    if intervals < 4:
        fractions = [round(i / (intervals - 1), 6) for i in range(intervals)]
    elif intervals > 4:
        fractions = sorted(set(fractions + [round(i / intervals, 6) for i in range(1, intervals)]))
    levels = [prior_low + span * fraction for fraction in fractions]
    x["grid_bottom"] = levels[0]
    x["grid_lower_mid"] = prior_low + span * 0.25
    x["grid_upper_mid"] = prior_low + span * 0.50
    x["grid_top"] = levels[-1]
    lines = [f"grid_level_{index}" for index in range(len(levels))]
    for name, level in zip(lines, levels):
        x[name] = level
    x["open_line"] = x["open"].astype(float)
    x["close_line"] = x["close"].astype(float)
    previous_open = x["open_line"].shift(1)
    x["open_cross_up"] = False
    x["open_cross_down"] = False
    for line in lines:
        x["open_cross_up"] |= (x["open_line"] > x[line]) & (previous_open <= x[line].shift(1))
        x["open_cross_down"] |= (x["open_line"] < x[line]) & (previous_open >= x[line].shift(1))
    entry_mode = params.get("entry_mode", "any_grid_cross")
    exit_mode = params.get("exit_mode", "any_grid_cross_down")
    up_by_line = {
        "any_grid_cross": x["open_cross_up"],
        "top_breakout": (x["open_line"] > x["grid_top"]) & (previous_open <= x["grid_top"].shift(1)),
        "upper_half_cross": ((x["open_line"] > x["grid_top"]) & (previous_open <= x["grid_top"].shift(1))) | ((x["open_line"] > x["grid_upper_mid"]) & (previous_open <= x["grid_upper_mid"].shift(1))),
        "lower_half_cross": ((x["open_line"] > x["grid_lower_mid"]) & (previous_open <= x["grid_lower_mid"].shift(1))) | ((x["open_line"] > x["grid_bottom"]) & (previous_open <= x["grid_bottom"].shift(1))),
    }
    down_by_line = {
        "any_grid_cross_down": x["open_cross_down"],
        "top_breakdown": (x["open_line"] < x["grid_top"]) & (previous_open >= x["grid_top"].shift(1)),
        "bottom_breakdown": (x["open_line"] < x["grid_bottom"]) & (previous_open >= x["grid_bottom"].shift(1)),
        "lower_half_breakdown": ((x["open_line"] < x["grid_lower_mid"]) & (previous_open >= x["grid_lower_mid"].shift(1))) | ((x["open_line"] < x["grid_bottom"]) & (previous_open >= x["grid_bottom"].shift(1))),
    }
    bullish_candle = x["close_line"] >= x["open_line"]
    x["buy_signal"] = up_by_line.get(entry_mode, up_by_line["any_grid_cross"]) & bullish_candle
    x["close_signal"] = down_by_line.get(exit_mode, down_by_line["any_grid_cross_down"])
    x.attrs["entry_mode"] = entry_mode
    x.attrs["exit_mode"] = exit_mode
    x.attrs["params"] = dict(params)
    return x.dropna(subset=lines)


def strategy_signal(frame: pd.DataFrame, params: dict = BASELINE) -> dict:
    """Return the signal for the latest supplied candle in ``frame``.

    The live bot may supply the latest partial candle because its opening price
    is already known. Research and backtests continue to supply complete bars.
    """
    data = frame.copy()
    if "time_key" in data.columns:
        data["time_key"] = pd.to_datetime(data["time_key"])
        data = data.set_index("time_key")
    x = add_features(data, params)
    if len(x) < 2:
        raise ValueError("not enough candles for the grid")
    bar = x.iloc[-1]
    open_delay_minutes = max(0, int(params.get("open_delay_minutes", 5)))
    delay_h = (30 + open_delay_minutes) // 60
    delay_m = (30 + open_delay_minutes) % 60
    from datetime import time as dt_time
    earliest_entry = dt_time(9 + delay_h, delay_m)
    bar_dt = pd.Timestamp(x.index[-1])
    bar_time = bar_dt.time()
    if dt_time(9, 30) <= bar_time < dt_time(10, 30):
        entry_allowed = bar_time >= earliest_entry
    else:
        entry_allowed = True
    return {"bar": str(x.index[-1]), "open": float(bar.open), "close": float(bar.close),
            "grid_top": float(bar.grid_top), "grid_upper_mid": float(bar.grid_upper_mid),
            "grid_lower_mid": float(bar.grid_lower_mid), "grid_bottom": float(bar.grid_bottom),
            "buy_signal": bool(bar.buy_signal), "close_signal": bool(bar.close_signal),
            "long_signal": bool(bar.buy_signal and entry_allowed), "exit_signal": bool(bar.close_signal),
            "entry_mode": str(x.attrs.get("entry_mode", params.get("entry_mode", "any_grid_cross"))),
            "exit_mode": str(x.attrs.get("exit_mode", params.get("exit_mode", "any_grid_cross_down"))),
            "open_delay_minutes": open_delay_minutes,
            "entry_allowed": bool(entry_allowed), "session_exit": False}


def run(candles: pd.DataFrame, params: dict = BASELINE, capital: float = 100000.0,
        allocation: float = ALLOCATION, slippage_bps: float = 2.0,
        commission: float = 0.0, evaluation_start=None):
    """Run long-only grid crossings with causal open-based risk exits."""
    x = add_features(candles, params)
    stamps = x.index
    n = len(stamps)
    opens = x["open"].to_numpy(dtype=float)
    highs = x["high"].to_numpy(dtype=float)
    closes = x["close"].to_numpy(dtype=float)
    buys = x["buy_signal"].to_numpy(dtype=bool)
    closes_sig = x["close_signal"].to_numpy(dtype=bool)

    cash = float(capital); position = None; trades = []; equity_rows = []
    slip = slippage_bps / 10000.0
    take_profit_pct = max(0.0, float(params.get("take_profit_pct", 0.0274)))
    stop_loss_pct = max(0.0, float(params.get("stop_loss_pct", 0.1324)))
    trailing_activation_pct = max(0.0, float(params.get("trailing_activation_pct", 0.0)))
    trailing_stop_pct = max(0.0, float(params.get("trailing_stop_pct", 0.0)))
    open_delay_minutes = max(0, int(params.get("open_delay_minutes", 5)))
    delay_h = (30 + open_delay_minutes) // 60
    delay_m = (30 + open_delay_minutes) % 60
    from datetime import time as dt_time
    earliest_entry = dt_time(9 + delay_h, delay_m)
    peak_price = 0.0
    times = [s.time() for s in stamps]
    dates = [s.date() for s in stamps]

    for i in range(n):
        stamp = stamps[i]
        if evaluation_start is not None and stamp < pd.Timestamp(evaluation_start):
            continue
        price = opens[i]
        high = highs[i]
        close = closes[i]
        bar_t = times[i]
        if position:
            take_profit = take_profit_pct > 0 and price >= position["entry"] * (1.0 + take_profit_pct)
            stop_loss = stop_loss_pct > 0 and price <= position["entry"] * (1.0 - stop_loss_pct)
            trailing_armed = (
                trailing_activation_pct > 0
                and trailing_stop_pct > 0
                and peak_price >= position["entry"] * (1.0 + trailing_activation_pct)
            )
            trailing_stop = trailing_armed and price <= peak_price * (1.0 - trailing_stop_pct)
            exit_at = session_exit_time(dates[i])
            session_exit = bool(exit_at and bar_t >= exit_at)
            if take_profit or stop_loss or trailing_stop or session_exit or bool(closes_sig[i]):
                reason = (
                    "TAKE_PROFIT" if take_profit else
                    "STOP_LOSS" if stop_loss else
                    "TRAILING_STOP" if trailing_stop else
                    "SESSION_EXIT_30M_BEFORE_CLOSE" if session_exit else
                    "OPEN_CROSSED_GRID_DOWN"
                )
                exit_price = price * (1.0 - slip)
                gross = (exit_price - position["entry"]) * position["qty"]
                costs = commission * 2.0; net = gross - costs
                cash += position["qty"] * exit_price - costs
                trades.append(Trade(position["signal_time"].isoformat(), position["entry_time"].isoformat(), position["entry"], position["qty"], stamp.isoformat(), exit_price, gross, costs, net, net / (position["entry"] * position["qty"]) * 100.0, reason))
                position = None
                peak_price = 0.0
            else:
                peak_price = max(peak_price, high)
        if dt_time(9, 30) <= bar_t < dt_time(10, 30):
            entry_allowed = bar_t >= earliest_entry
        else:
            entry_allowed = True
        if position is None and buys[i] and entry_allowed:
            entry = price * (1.0 + slip)
            qty = math.floor(cash * allocation / entry)
            if qty > 0:
                cash -= qty * entry + commission
                position = {"signal_time": stamp, "entry_time": stamp, "entry": entry, "qty": qty}
                peak_price = entry
        mark = cash + (position["qty"] * close if position else 0.0)
        equity_rows.append({"date": stamp, "equity": mark, "cash": cash, "position": position["qty"] if position else 0})
    if position and n > 0:
        stamp = stamps[-1]; exit_price = closes[-1] * (1.0 - slip)
        gross = (exit_price - position["entry"]) * position["qty"]
        costs = commission * 2.0; net = gross - costs
        cash += position["qty"] * exit_price - costs
        trades.append(Trade(position["signal_time"].isoformat(), position["entry_time"].isoformat(), position["entry"], position["qty"], stamp.isoformat(), exit_price, gross, costs, net, net / (position["entry"] * position["qty"]) * 100.0, "END_OF_DATA"))
        if equity_rows:
            equity_rows[-1]["equity"] = cash
            equity_rows[-1]["cash"] = cash
            equity_rows[-1]["position"] = 0
    equity = pd.DataFrame(equity_rows).set_index("date") if equity_rows else pd.DataFrame(columns=["equity"])
    if not equity.empty:
        equity["drawdown"] = equity.equity / equity.equity.cummax() - 1.0
    return x, trades, {"equity": equity, "ending_cash": cash}


def _stats(trades, equity, capital):
    returns = np.array([t.net_pnl / (t.entry_price * t.qty) for t in trades]) if trades else np.array([])
    gains = returns[returns > 0].sum() if len(returns) else 0.0; losses = -returns[returns < 0].sum() if len(returns) else 0.0
    daily = equity["equity"].resample("1D").last().pct_change(fill_method=None).dropna() if not equity.empty else pd.Series(dtype=float)
    ending_equity = float(equity["equity"].iloc[-1]) if not equity.empty else float(capital)
    running_peak = equity["equity"].cummax().clip(lower=float(capital)) if not equity.empty else pd.Series([float(capital)])
    drawdown = equity["equity"] / running_peak - 1.0 if not equity.empty else pd.Series([0.0])
    return {"starting_capital": round(float(capital), 2), "ending_equity": round(ending_equity, 2),
            "total_return_pct": round(float((ending_equity / capital - 1.0) * 100.0), 3),
            "max_drawdown_pct": round(float(drawdown.min() * 100.0), 3),
            "trades": len(trades), "wins": int((returns > 0).sum()),
            "win_rate_pct": round(float((returns > 0).mean() * 100.0), 2) if len(returns) else 0.0,
            "profit_factor": round(float(gains / losses), 3) if losses else None,
            "average_trade_pct": round(float(returns.mean() * 100.0), 3) if len(returns) else 0.0,
            "sharpe_approx": round(float(daily.mean() / daily.std() * np.sqrt(252)), 3) if len(daily) > 1 and daily.std() else 0.0}


def stats(trades, data, capital):
    return _stats(trades, data["equity"] if isinstance(data, dict) else data, capital)


def optimize(candles, base=BASELINE, capital=100000.0, allocation=ALLOCATION, slippage_bps=2.0, commission=0.0, progress=None, trials=12000, seed=20261112):
    rng = random.Random(seed); results = []
    for done in range(1, int(trials) + 1):
        params = {"grid_lookback": rng.randint(10, 120), "grid_intervals": 4}
        _, trades, data = run(candles, params, capital, allocation, slippage_bps, commission)
        result = _stats(trades, data["equity"], capital)
        if result["trades"] >= 3:
            result["selection_score"] = round(result["total_return_pct"] - 0.35 * abs(result["max_drawdown_pct"]), 3)
            results.append({"params": params, "stats": result})
        if progress and (done % 100 == 0 or done == trials): progress(done, int(trials), f"Grid lookback trial {done:,}/{int(trials):,}")
    return sorted(results, key=lambda row: row["stats"]["selection_score"], reverse=True)[:25]


def walk_forward_optimize(candles, base=BASELINE, capital=100000.0, allocation=ALLOCATION, slippage_bps=2.0, commission=0.0, progress=None, trials=12000, seed=20261112, folds=3):
    frame = candles.sort_index(); n = len(frame); per_fold = max(1, int(trials) // folds); results = []
    for fold in range(folds):
        train_end = int(n * (0.50 + fold * (0.50 / folds))); test_end = int(n * (0.50 + (fold + 1) * (0.50 / folds)))
        if test_end > n: continue
        callback = (lambda done, total, message, f=fold: progress(f * per_fold + done, trials, f"Daily grid fold {f + 1}/{folds} · {message}") if progress else None)
        candidates = optimize(frame.iloc[:train_end], base, capital, allocation, slippage_bps, commission, callback, per_fold, seed + fold)
        if not candidates: continue
        params = candidates[0]["params"]; test_start = frame.index[train_end]
        _, trades, data = run(frame.iloc[:test_end], params, capital, allocation, slippage_bps, commission, evaluation_start=test_start)
        result = _stats(trades, data["equity"], capital); result.update({"fold": fold + 1, "test_start": str(test_start), "test_end": str(frame.index[test_end - 1])})
        results.append({"params": params, "stats": result})
    if progress: progress(trials, trials, f"Daily grid walk-forward complete · {len(results)} unseen folds")
    return results


def walk_forward_replay(candles, fold_results, capital=100000.0, allocation=ALLOCATION, slippage_bps=2.0, commission=0.0):
    frame = candles.sort_index(); all_trades = []; equity_parts = []; fold_capital = float(capital)
    for row in fold_results:
        start = pd.Timestamp(row["stats"]["test_start"]); end = pd.Timestamp(row["stats"]["test_end"])
        _, trades, data = run(frame.loc[:end], row["params"], fold_capital, allocation, slippage_bps, commission, evaluation_start=start)
        all_trades.extend(trades)
        if not data["equity"].empty: equity_parts.append(data["equity"])
        returns = np.array([t.net_pnl / (t.entry_price * t.qty) for t in trades])
        if len(returns): fold_capital *= float(np.prod(1.0 + returns))
    equity = pd.concat(equity_parts).sort_index() if equity_parts else pd.DataFrame(columns=["equity", "drawdown"])
    if not equity.empty:
        equity = equity[~equity.index.duplicated(keep="last")]; equity["drawdown"] = equity.equity / equity.equity.cummax() - 1.0
    return add_features(frame, BASELINE), all_trades, {"equity": equity, "ending_cash": fold_capital}
