#!/usr/bin/env python3
"""Exhaustive joint optimization of market-open waiting delay and take-profit targets.

Searches delay values: [0, 2, 5, 8, 10, 15, 20, 25, 30, 40] minutes
and take-profit targets: [1.0%, 1.5%, 2.0%, 2.74%, 3.5%, 4.5%, 6.0%]
Across multi-session historical candle data with causal risk modeling.
Applies the 70% TP rule automatically for SIMULATE paper trading.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from datetime import time as dt_time
import pandas as pd

from quant_storage import load_candles
from strategy_57pct import add_features, run, stats, validate_strategy_params
from trading_config import active_symbols, normalize_symbol, strategy_path


DELAY_CANDIDATES = [0, 2, 5, 8, 10, 15, 20, 25, 30, 40]
TP_CANDIDATES = [0.010, 0.015, 0.020, 0.0274, 0.035, 0.045, 0.060]


def evaluate_combo(candles: pd.DataFrame, base_params: dict, delay: int, tp: float) -> dict:
    params = validate_strategy_params({**base_params, "open_delay_minutes": delay, "take_profit_pct": tp})
    _, trades, equity = run(candles, params, capital=100000.0, slippage_bps=2.0)
    result = stats(trades, equity, 100000.0)
    
    ret = result.get("total_return_pct", 0.0)
    dd = abs(result.get("max_drawdown_pct", 0.0))
    pf = result.get("profit_factor", 0.0)
    # Calmar-weighted score with profit factor reinforcement
    score = round(ret - 0.45 * dd + min(pf or 0.0, 10.0), 3)
    
    return {
        "delay": delay,
        "tested_tp": tp,
        "paper_tp": round(tp * 0.70, 6),
        "trades": result.get("trades", 0),
        "win_rate": round(result.get("win_rate_pct", 0.0), 1),
        "total_return_pct": round(ret, 2),
        "max_drawdown_pct": round(result.get("max_drawdown_pct", 0.0), 2),
        "profit_factor": round(pf, 2) if pf is not None else 0.0,
        "score": score,
    }


def optimize_symbol(symbol: str, apply_winner: bool = True) -> dict:
    norm_sym = normalize_symbol(symbol)
    strat_file = strategy_path(norm_sym)
    payload = json.loads(strat_file.read_text())
    interval = int(payload.get("interval", 10))
    params = payload.get("params", {})
    
    candles = load_candles(norm_sym, interval, "2026-10-01", "2026-11-28")
    if candles.empty:
        raise RuntimeError(f"no candles for {norm_sym}")

    all_results = []
    for delay in DELAY_CANDIDATES:
        for tp in TP_CANDIDATES:
            all_results.append(evaluate_combo(candles, params, delay, tp))
            
    best = max(all_results, key=lambda x: x["score"])
    
    if apply_winner:
        payload["params"]["open_delay_minutes"] = best["delay"]
        payload["params"]["take_profit_pct"] = best["paper_tp"]
        payload["updated_at"] = pd.Timestamp.now(tz="UTC").isoformat()
        strat_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    
    return {
        "symbol": norm_sym,
        "interval": interval,
        "best": best,
        "all_results": all_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Jointly optimize open delay and take profit")
    parser.add_argument("--apply", action="store_true", default=True, help="Apply best params to strategy configs")
    args = parser.parse_args()

    symbols = active_symbols()
    summary = []
    
    print("=" * 115)
    print(f"| {'Symbol':<8} | {'Interval':<8} | {'Best Delay':<10} | {'Tested TP':<9} | {'Paper TP (70%)':<14} | {'Trades':<6} | {'Win Rate':<8} | {'Return %':<10} | {'Drawdown %':<10} | {'PF':<6} |")
    print("=" * 115)

    for sym in symbols:
        res = optimize_symbol(sym, apply_winner=args.apply)
        best = res["best"]
        summary.append(res)
        d_str = f"{best['delay']}m"
        ttp_str = f"{best['tested_tp']*100:.2f}%"
        ptp_str = f"{best['paper_tp']*100:.3f}%"
        print(f"| {res['symbol']:<8} | {res['interval']:>2}m       | {d_str:>10} | {ttp_str:>9} | {ptp_str:>14} | {best['trades']:>6} | {best['win_rate']:>7.1f}% | {best['total_return_pct']:>9.2f}% | {best['max_drawdown_pct']:>9.2f}% | {best['profit_factor']:>6.2f} |")
    
    print("-" * 115)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
