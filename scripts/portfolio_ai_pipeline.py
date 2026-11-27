#!/usr/bin/env python3
"""Benchmark 1–10m first, then AI-optimise every configured paper symbol."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_config import active_symbols

CURRENT_PROCESS: subprocess.Popen[str] | None = None


def stop_pipeline(signum: int, _frame: object) -> None:
    """Stop the active child process group so GUI cancellation leaves no workers."""
    process = CURRENT_PROCESS
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    raise SystemExit(128 + signum)


def stream(command: list[str], stage: str, symbol: str | None = None) -> int:
    global CURRENT_PROCESS
    print(json.dumps({"pipeline_stage": stage, "symbol": symbol, "state": "STARTED"}), flush=True)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    CURRENT_PROCESS = process
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
    code = process.wait()
    CURRENT_PROCESS = None
    return code


def main() -> int:
    signal.signal(signal.SIGINT, stop_pipeline)
    signal.signal(signal.SIGTERM, stop_pipeline)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(active_symbols()))
    parser.add_argument("--total-candidates", type=int, default=100_000)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    args = parser.parse_args()
    if args.total_candidates < 10:
        parser.error("--total-candidates must be at least 10 for the 1–10 minute sweep")
    requested = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    if requested != active_symbols():
        parser.error(f"symbols must exactly match configured portfolio {active_symbols()}")

    end = date.today().isoformat()
    start = (date.today() - timedelta(days=30)).isoformat()
    benchmark = [
        sys.executable,
        str(ROOT / "scripts" / "multi_symbol_backtest.py"),
        "--symbols", ",".join(requested),
        "--start", start,
        "--end", end,
        "--workers", str(args.workers),
    ]
    if stream(benchmark, "BENCHMARK_1_TO_10") != 0:
        return 1

    for index, symbol in enumerate(requested, start=1):
        command = [
            sys.executable,
            str(ROOT / "scripts" / "app_minute_interval_sweep.py"),
            "--symbol", symbol,
            "--start", "2026-01-01",
            "--end", end,
            "--intervals", "1,2,3,4,5,6,7,8,9,10",
            "--total-candidates", str(args.total_candidates),
            "--phase", "MANUAL",
            "--ai",
        ]
        attempts = 0
        while True:
            code = stream(command, "AI_OPTIMISE", symbol)
            if code != 75 or attempts >= 20:
                break
            attempts += 1
            print(json.dumps({"pipeline_stage": "WAITING_FOR_RESEARCH_LOCK", "symbol": symbol, "attempt": attempts}), flush=True)
            time.sleep(15)
        if code != 0:
            print(json.dumps({"pipeline_stage": "FAILED", "symbol": symbol, "exit_code": code}), flush=True)
            return code
        print(json.dumps({"pipeline_stage": "SYMBOL_COMPLETE", "symbol": symbol, "index": index, "total_symbols": len(requested)}), flush=True)

    print(json.dumps({"pipeline_stage": "COMPLETE", "symbols": requested, "paper_only": True}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
