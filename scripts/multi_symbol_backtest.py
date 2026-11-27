#!/usr/bin/env python3
"""Backtest several US symbols across 1–10 minute bars without changing execution."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data import fetch_moomoo_candles
from minute_bars import MINUTE_INTERVALS, aggregate_us_session
from quant_storage import load_candles, save_backtest, save_candles
from strategy_57pct import ALLOCATION, BASELINE, run, stats
from trading_config import normalize_symbol


def parse_symbols(value: str) -> list[str]:
    symbols = []
    for item in value.split(","):
        symbol = normalize_symbol(item)
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise ValueError("provide at least one US symbol")
    return symbols


def parse_intervals(value: str) -> list[int]:
    intervals = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not intervals or any(item not in MINUTE_INTERVALS for item in intervals):
        raise ValueError("intervals must be between 1 and 10 minutes")
    return sorted(set(intervals))


def evaluate(symbol: str, source: pd.DataFrame, interval: int) -> dict:
    bars = aggregate_us_session(source, interval)
    if bars.empty:
        return {"symbol": symbol, "interval": interval, "status": "NO_COMPLETE_BARS"}
    save_candles(bars, symbol, interval, source="local_1m_aggregation")
    _, trades, data = run(bars, dict(BASELINE), 100_000.0, ALLOCATION, 2.0, 0.0)
    result = stats(trades, data, 100_000.0)
    run_id = save_backtest(symbol, interval, str(bars.index.min()), str(bars.index.max()),
                           dict(BASELINE), result, trades, [])
    return {"symbol": symbol, "interval": interval, "status": "COMPLETED",
            "run_id": run_id, **result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, help="comma-separated US symbols")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--intervals", default="1,2,3,4,5,6,7,8,9,10")
    parser.add_argument("--workers", type=int, default=2, choices=range(1, 11))
    parser.add_argument("--no-refresh", action="store_true", help="use only the existing local 1-minute cache")
    parser.add_argument("--output", type=Path, default=ROOT / "multi_symbol_results" / "latest.json")
    args = parser.parse_args()
    symbols = parse_symbols(args.symbols)
    intervals = parse_intervals(args.intervals)
    results = []
    for symbol in symbols:
        print(json.dumps({"state": "FETCHING", "symbol": symbol}), flush=True)
        if not args.no_refresh:
            fetch_moomoo_candles(symbol, args.start, args.end)
        source = load_candles(symbol, 1, args.start, f"{args.end}T23:59:59")
        if source.empty:
            results.append({"symbol": symbol, "status": "NO_DATA"})
            continue
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(evaluate, symbol, source, interval) for interval in intervals]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results.append(result)
                print(json.dumps(result, default=str), flush=True)
    completed = [row for row in results if row.get("status") == "COMPLETED"]
    ranking = sorted(completed, key=lambda row: (float(row.get("total_return_pct", 0)),
                                                  -abs(float(row.get("max_drawdown_pct", 0)))), reverse=True)
    payload = {"created_at": datetime.now(timezone.utc).isoformat(), "paper_only": True,
               "symbols": symbols, "intervals": intervals, "results": results, "ranking": ranking}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    temporary.replace(args.output)
    print(json.dumps({"state": "COMPLETED", "output": str(args.output),
                      "tested": len(completed), "best": ranking[0] if ranking else None}, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
