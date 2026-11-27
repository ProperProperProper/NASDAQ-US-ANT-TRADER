#!/usr/bin/env python3
"""Research the active symbol on synthetic 1m..10m candles from a 1m cache.

The sweep tests the full 5-session window, publishes the highest-return tested
interval and parameters without interrupting the configured SIMULATE paper bot.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import multiprocessing
import os
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_config import active_symbol, active_symbols, normalize_symbol, promotion_path, research_dir, strategy_path, research_workers
ACTIVE_SYMBOL = active_symbol()
RANDOM_HISTORY = research_dir(ACTIVE_SYMBOL) / "randomized_param_history.json"
MAX_RESEARCH_SESSIONS = 5
MAX_INTERVAL_MINUTES = 10
MAX_KNOWN_SUCCESSFUL_PER_INTERVAL = 5
# Keep research below the CPU threshold used by MLX Sentinel so local AI and
# the paper bot remain responsive while sweeps run asynchronously.
# Use configured evaluator workers so research finishes promptly while
# remaining well below the machine-wide resource ceiling on this Mac.
RESEARCH_WORKERS = max(1, min(10, int(os.environ.get("RESEARCH_WORKERS", str(research_workers())))))
# Eligible candidates are promoted automatically through the paper bot's
# broker-verified flat-position gate; never publish directly from research.
AUTO_PROMOTION_ENABLED = True
MIN_PROMOTION_SESSIONS = 2
MIN_PROMOTION_TRADES = 2
PENDING_PROMOTION_PATH = promotion_path(ACTIVE_SYMBOL)
_WORK_FRAME = None
_KNOWN_SUCCESSFUL_BY_INTERVAL: dict[int, dict[str, dict]] | None = None


def _init_evaluator(frame: pd.DataFrame) -> None:
    global _WORK_FRAME
    _WORK_FRAME = frame


def _evaluate_params(params: dict) -> dict:
    return evaluate(_WORK_FRAME, params)
from quant_storage import load_candles
from minute_bars import aggregate_us_session
from market_data import fetch_moomoo_candles
from strategy_57pct import ALLOCATION, BASELINE, run, stats, validate_strategy_params


def ensure_one_minute_cache(symbol: str, start: str, end: str, refresh: bool) -> None:
    with sqlite3.connect(ROOT / "quant_dashboard.sqlite3") as db:
        minimum, maximum, count = db.execute(
            "SELECT min(time_key), max(time_key), count(*) FROM candles WHERE symbol=? AND interval_minutes=1",
            (symbol,),
        ).fetchone()
    covers_range = bool(count and minimum and maximum and str(minimum)[:10] <= start and str(maximum)[:10] >= end)
    if covers_range and not refresh:
        print(json.dumps({"cache": "covered", "start": start, "end": end}), flush=True)
        return
    if count and maximum and not refresh:
        cached_end = pd.Timestamp(str(maximum)[:10])
        requested_end = pd.Timestamp(end)
        # A Friday-to-Monday gap (or a short exchange holiday) is expected.
        # Do not refetch the entire history when no new completed session can
        # exist yet; the cached candles are the correct research boundary.
        if requested_end >= cached_end and (requested_end - cached_end).days <= 3:
            print(json.dumps({"cache": "using_latest_completed_session", "cached_end": str(cached_end.date()), "requested_end": end}), flush=True)
            return
    print(json.dumps({"cache": "fetching", "start": start, "end": end}), flush=True)
    fetch_moomoo_candles(symbol, start, end)


def load_one_minute(symbol: str) -> pd.DataFrame:
    frame = load_candles(symbol, 1, "1900-01-01", "2999-12-31")
    if frame.empty:
        raise RuntimeError(f"No 1-minute {symbol} candles are cached")
    frame = frame.sort_index()[["open", "high", "low", "close", "volume"]].astype(float)
    session_days = pd.Index(frame.index.normalize().unique()).sort_values()
    if len(session_days) > MAX_RESEARCH_SESSIONS:
        session_days = session_days[-MAX_RESEARCH_SESSIONS:]
    frame = frame[frame.index.normalize().isin(session_days)]
    print(json.dumps({"research_window": {"sessions": len(session_days), "start": str(session_days[0].date()), "end": str(session_days[-1].date())}}), flush=True)
    return frame


aggregate = aggregate_us_session


def evaluate(frame: pd.DataFrame, params: dict) -> dict:
    _, trades, data = run(frame, params, 100_000.0, ALLOCATION, 2.0, 0.0)
    result = stats(trades, data, 100_000.0)
    result["selection_score"] = round(result["total_return_pct"] - .35 * abs(result["max_drawdown_pct"]), 4)
    return {"params": dict(params), "stats": result}


def param_key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def known_successful(interval: int) -> list[dict]:
    """Return prior profitable parameters before generating new random candidates."""
    global _KNOWN_SUCCESSFUL_BY_INTERVAL
    if _KNOWN_SUCCESSFUL_BY_INTERVAL is None:
        _KNOWN_SUCCESSFUL_BY_INTERVAL = {}
        # The sweep evaluates only the latest five completed sessions. Reuse
        # successful candidates from the five newest result files rather than
        # reparsing hundreds of MB of stale, out-of-window history.
        result_files = sorted(
            (research_dir(ACTIVE_SYMBOL) / "runs").glob("*/profitable_params.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:5]
        for path in result_files:
            try:
                for line in path.read_text().splitlines():
                    row = json.loads(line)
                    if row.get("stats", {}).get("total_return_pct", 0) <= 0:
                        continue
                    interval_text = str(row.get("interval", ""))
                    if not interval_text.endswith("m"):
                        continue
                    interval_value = int(interval_text[:-1])
                    params = row.get("params", {})
                    _KNOWN_SUCCESSFUL_BY_INTERVAL.setdefault(interval_value, {})[param_key(params)] = params
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue

    found = dict(_KNOWN_SUCCESSFUL_BY_INTERVAL.get(interval, {}))
    config_path = strategy_path(ACTIVE_SYMBOL)
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if int(config.get("interval")) == interval and config.get("mode") == "SIMULATE":
                found[param_key(config.get("params", {}))] = dict(config["params"])
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return list(found.values())[:MAX_KNOWN_SUCCESSFUL_PER_INTERVAL]


def candidates(interval: int, count: int, excluded: set[str]) -> list[dict]:
    rng = random.SystemRandom()
    result = []
    attempts = 0
    while len(result) < count and attempts < max(1000, count * 20):
        attempts += 1
        params = dict(BASELINE)
        params["grid_lookback"] = max(5, min(240, int(round(BASELINE["grid_lookback"] + rng.gauss(0, 30)))))
        params["grid_intervals"] = rng.randint(2, 20)
        params["entry_mode"] = rng.choice(("any_grid_cross", "top_breakout", "upper_half_cross", "lower_half_cross"))
        params["exit_mode"] = rng.choice(("any_grid_cross_down", "top_breakdown", "bottom_breakdown", "lower_half_breakdown"))
        params["trailing_activation_pct"] = rng.choice((0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04))
        params["trailing_stop_pct"] = rng.choice((0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04))
        params["take_profit_pct"] = rng.choice((0.008, 0.012, 0.015, 0.02, 0.025, 0.0274, 0.03, 0.035, 0.04, 0.05, 0.06, 0.08))
        params["open_delay_minutes"] = rng.choice((0, 2, 5, 8, 10, 12, 15, 20, 25, 30, 35, 40, 45))
        if params["trailing_activation_pct"] == 0.0:
            params["trailing_stop_pct"] = 0.0
        key = f"{interval}m:{param_key(params)}"
        if key not in excluded:
            excluded.add(key)
            result.append(params)
    return result


def load_random_history() -> set[str]:
    try:
        value = json.loads(RANDOM_HISTORY.read_text())
        return set(value if isinstance(value, list) else [])
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()


def save_random_history(history: set[str]) -> None:
    temporary = RANDOM_HISTORY.with_name(RANDOM_HISTORY.name + ".tmp")
    temporary.write_text(json.dumps(sorted(history), separators=(",", ":")) + "\n")
    temporary.replace(RANDOM_HISTORY)


def ai_candidates(interval: int) -> list[dict]:
    prompt = (
        f"Return a JSON array of up to 3 bounded parameter objects for {ACTIVE_SYMBOL} "
        f"{interval}-minute candle-open grid strategy. Use only grid_lookback "
        "integer 5..120, grid_intervals integer 2..20, entry_mode in any_grid_cross/top_breakout/upper_half_cross/"
        "lower_half_cross, and exit_mode in any_grid_cross_down/top_breakdown/"
        "bottom_breakdown/lower_half_breakdown. Also propose trailing_activation_pct "
        "and trailing_stop_pct as decimals in 0.0..0.04; use both zero to disable. No prose."
    )
    # Prefer native local inference, while allowing the gateway to fall back to
    # the configured hosted backend when the resource governor pauses MLX
    # during a four-core research batch.
    mcp_request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "gateway_chat", "arguments": {"prompt": prompt, "provider": "auto", "max_tokens": 256}}}
    completed = subprocess.run(
        [sys.executable, str(ROOT / "ai_gateway/ai/mcp_server.py")],
        input=json.dumps(mcp_request) + "\n", capture_output=True, text=True, timeout=90, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "MCP AI plugin failed")
    response = json.loads(completed.stdout.strip().splitlines()[-1])
    if "error" in response:
        raise RuntimeError(str(response["error"].get("message", response["error"])))
    text = response.get("result", {}).get("content", [{}])[0].get("text", "")
    decoder = json.JSONDecoder()
    raw = None
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            if isinstance(value, dict):
                raw = [value] if raw is None else raw + [value]
            elif isinstance(value, list):
                raw = value if raw is None else raw + value
        except json.JSONDecodeError:
            continue
    if not isinstance(raw, list):
        return []
    entries = []
    entry_modes = {"any_grid_cross", "top_breakout", "upper_half_cross", "lower_half_cross"}
    exit_modes = {"any_grid_cross_down", "top_breakdown", "bottom_breakdown", "lower_half_breakdown"}
    for item in raw:
        if not isinstance(item, dict):
            continue
        params = dict(BASELINE)
        try:
            if "grid_lookback" in item:
                params["grid_lookback"] = max(5, min(120, int(item["grid_lookback"])))
            if "grid_intervals" in item:
                params["grid_intervals"] = max(2, min(20, int(item["grid_intervals"])))
            if "open_delay_minutes" in item:
                params["open_delay_minutes"] = max(0, min(60, int(item["open_delay_minutes"])))
            if "take_profit_pct" in item:
                params["take_profit_pct"] = max(0.005, min(0.20, float(item["take_profit_pct"])))
        except (TypeError, ValueError):
            continue
        for name in ("trailing_activation_pct", "trailing_stop_pct"):
            try:
                if name in item:
                    params[name] = max(0.0, min(0.04, float(item[name])))
            except (TypeError, ValueError):
                continue
        if params["trailing_activation_pct"] == 0.0:
            params["trailing_stop_pct"] = 0.0
        if item.get("entry_mode") in entry_modes:
            params["entry_mode"] = item["entry_mode"]
        if item.get("exit_mode") in exit_modes:
            params["exit_mode"] = item["exit_mode"]
        if params not in entries:
            entries.append(params)
    return entries


def promote_winner(winner: dict) -> dict:
    """Queue a tested winner for the paper bot's broker-verified flat gate."""
    interval = int(winner["interval"].removesuffix("m"))
    if interval not in range(1, MAX_INTERVAL_MINUTES + 1):
        raise ValueError("promotion interval must be between 1 and 10 minutes")
    params = validate_strategy_params(winner["params"])
    if "take_profit_pct" in params:
        params["take_profit_pct"] = round(float(params["take_profit_pct"]) * 0.70, 6)
    payload = {
        "symbol": ACTIVE_SYMBOL, "interval": interval, "params": params,
        "run_id": winner["run_id"], "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "mode": "SIMULATE",
    }
    temporary = PENDING_PROMOTION_PATH.with_name(PENDING_PROMOTION_PATH.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(PENDING_PROMOTION_PATH)
    return {"state": "QUEUED_BROKER_CHECK", "reason": "paper bot applies when SIMULATE broker is flat with no pending order", "pending_path": str(PENDING_PROMOTION_PATH), **payload}


def promotion_gate(winner: dict | None, current_stats: dict | None = None, completed_sessions: int = 0) -> tuple[bool, str]:
    """Allow a profitable, strictly better paper-only candidate to activate."""
    if winner is None:
        return False, "no eligible winner"
    stats = winner.get("best", {}).get("stats", {})
    if completed_sessions < MIN_PROMOTION_SESSIONS:
        return False, f"requires at least {MIN_PROMOTION_SESSIONS} completed sessions"
    if int(stats.get("trades", 0) or 0) < MIN_PROMOTION_TRADES:
        return False, f"requires at least {MIN_PROMOTION_TRADES} completed trades"
    if float(stats.get("total_return_pct", 0)) <= 0:
        return False, "candidate is not profitable after costs"
    if current_stats is None:
        return False, "current paper strategy comparison unavailable"
    if float(stats.get("total_return_pct", 0)) <= float(current_stats.get("total_return_pct", 0)):
        return False, "candidate does not beat current active parameters"
    return True, "profitable candidate beats current active parameters"


def parse_intervals(value: str | None) -> list[int]:
    if not value:
        return list(range(1, MAX_INTERVAL_MINUTES + 1))
    values = sorted({int(item) for item in value.split(",") if item.strip()})
    if not values or any(item < 1 or item > MAX_INTERVAL_MINUTES for item in values):
        raise ValueError(f"--intervals must contain comma-separated values from 1 to {MAX_INTERVAL_MINUTES}")
    return values


def run_main() -> int:
    global ACTIVE_SYMBOL, RANDOM_HISTORY, PENDING_PROMOTION_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=ACTIVE_SYMBOL, help="configured active US symbol")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default=pd.Timestamp.now().strftime("%Y-%m-%d"))
    ap.add_argument("--refresh-1m", action="store_true")
    ap.add_argument("--candidates", type=int, default=250)
    ap.add_argument("--total-candidates", type=int, default=None, help="Total randomized candidates shared across the 1m..10m intervals")
    ap.add_argument("--ai", action="store_true", help="Add bounded AI proposals from the local gateway")
    ap.add_argument("--phase", choices=["PREMARKET", "RTH", "MANUAL"], default="MANUAL", help="Research phase for scheduling and audit records")
    ap.add_argument("--intervals", default=None, help="Comma-separated intervals to test; defaults to 1..10")
    ap.add_argument("--run-dir", type=Path, default=research_dir(ACTIVE_SYMBOL) / "runs")
    args = ap.parse_args()
    args.symbol = normalize_symbol(args.symbol)
    if args.symbol not in active_symbols():
        ap.error(f"symbol must belong to configured portfolio {active_symbols()}")
    ACTIVE_SYMBOL = args.symbol
    RANDOM_HISTORY = research_dir(args.symbol) / "randomized_param_history.json"
    PENDING_PROMOTION_PATH = promotion_path(args.symbol)
    if args.run_dir == research_dir(active_symbol()) / "runs":
        args.run_dir = research_dir(args.symbol) / "runs"
    intervals = parse_intervals(args.intervals)
    try:
        ensure_one_minute_cache(args.symbol, args.start, args.end, args.refresh_1m)
    except Exception as exc:
        print(json.dumps({"state": "ERROR", "stage": "cache", "error": str(exc)}), flush=True)
        return 1
    base = load_one_minute(args.symbol)
    completed_sessions = int(base.index.normalize().nunique())
    run_dir = args.run_dir / (args.symbol.replace(".", "-") + "-MINUTE-" + time.strftime("%Y%m%d-%H%M%S"))
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.total_candidates is not None and args.total_candidates < len(intervals):
        raise ValueError(f"--total-candidates must be at least {len(intervals)}")
    candidate_budget = args.candidates
    remainder = 0
    if args.total_candidates is not None:
        candidate_budget, remainder = divmod(args.total_candidates, len(intervals))
    profitable_path = run_dir / "profitable_params.jsonl"
    profitable_count = 0
    tested_randomized = 0
    total_randomized = args.total_candidates or args.candidates * len(intervals)
    ai_count = 0
    results = []
    randomized_history = load_random_history()
    randomized_before = len(randomized_history)
    for interval_index, interval in enumerate(intervals):
        try:
            frame = aggregate(base, interval)
            if len(frame) < 80:
                result = {"symbol": args.symbol, "interval": f"{interval}m", "rows": len(frame), "status": "INSUFFICIENT_COMPLETE_CANDLES"}
            else:
                interval_budget = candidate_budget + (1 if interval_index < remainder else 0)
                full_population = []
                known_params = known_successful(interval)
                randomized_excluded = set(randomized_history)
                for params in known_params:
                    randomized_excluded.add(f"{interval}m:{param_key(params)}")
                    evaluated = evaluate(frame, params)
                    full_population.append(evaluated)
                    if evaluated["stats"]["total_return_pct"] > 0:
                        with profitable_path.open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps({"interval": f"{interval}m", "stage": "known_successful", "params": evaluated["params"], "stats": evaluated["stats"]}) + "\n")
                        profitable_count += 1
                full_population.append(evaluate(frame, dict(BASELINE)))
                proposed = []
                if args.ai:
                    try:
                        proposed = ai_candidates(interval)
                    except Exception as exc:
                        print(json.dumps({"ai": "unavailable", "interval": f"{interval}m", "error": str(exc)}), flush=True)
                ai_count += len(proposed)
                for params in proposed:
                    evaluated = evaluate(frame, params)
                    full_population.append(evaluated)
                    if evaluated["stats"]["total_return_pct"] > 0:
                        with profitable_path.open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps({"interval": f"{interval}m", "stage": "ai_full_5_session_window", "params": evaluated["params"], "stats": evaluated["stats"]}) + "\n")
                        profitable_count += 1
                random_params = candidates(interval, interval_budget, randomized_excluded)
                randomized_history.update(f"{interval}m:{param_key(params)}" for params in random_params)
                save_random_history(randomized_history)
                context = multiprocessing.get_context("fork")
                with context.Pool(processes=RESEARCH_WORKERS, initializer=_init_evaluator, initargs=(frame,)) as pool:
                    evaluated_candidates = pool.imap(_evaluate_params, random_params, chunksize=32)
                    for evaluated in evaluated_candidates:
                        full_population.append(evaluated)
                        tested_randomized += 1
                        if tested_randomized % 250 == 0:
                            print(json.dumps({"progress": round(tested_randomized / total_randomized, 6), "tested": tested_randomized, "total": total_randomized}), flush=True)
                        if evaluated["stats"]["total_return_pct"] > 0:
                            with profitable_path.open("a", encoding="utf-8") as handle:
                                handle.write(json.dumps({"interval": f"{interval}m", "stage": "full_5_session_window", "params": evaluated["params"], "stats": evaluated["stats"]}) + "\n")
                            profitable_count += 1
                full_population.sort(key=lambda item: item["stats"]["total_return_pct"], reverse=True)
                selected = full_population[0]
                selected["interval"] = f"{interval}m"
                result = {
                    "symbol": args.symbol, "interval": f"{interval}m", "rows": len(frame), "base_rows": len(base),
                    "best": selected,
                    "evidence": "WEAK" if selected["stats"]["trades"] < 20 else "NORMAL",
                }
        except Exception as exc:
            result = {"symbol": args.symbol, "interval": f"{interval}m", "rows": len(frame) if "frame" in locals() else 0, "status": "ERROR", "error": str(exc)}
        results.append(result)
        (run_dir / "progress.json").write_text(json.dumps({"state": "RUNNING", "phase": args.phase, "intervals": intervals, "randomized_candidates": total_randomized, "ai_proposals": ai_count, "profitable_saved": profitable_count, "results": results}, indent=2, default=str) + "\n")
        print(json.dumps({"interval": result["interval"], "rows": result["rows"], "status": result.get("status", "COMPLETE"), "return_pct": result.get("best", {}).get("stats", {}).get("total_return_pct"), "ending_equity": result.get("best", {}).get("stats", {}).get("ending_equity"), "trades": result.get("best", {}).get("stats", {}).get("trades"), "profitable_saved": profitable_count, "progress": round(tested_randomized / total_randomized, 6), "tested": tested_randomized, "total": total_randomized}), flush=True)
    eligible = [item for item in results if item.get("best", {}).get("stats")]
    winner = max(eligible, key=lambda item: item["best"]["stats"]["total_return_pct"], default=None)
    winner_record = None if winner is None else {
        "interval": winner["interval"],
        "params": winner["best"]["params"],
        "total_return_pct": winner["best"]["stats"]["total_return_pct"],
        "max_drawdown_pct": winner["best"]["stats"]["max_drawdown_pct"],
        "trades": winner["best"]["stats"]["trades"],
        "ending_equity": winner["best"]["stats"]["ending_equity"],
        "evidence": winner["evidence"],
    }
    active_interval = None
    active_params = None
    try:
        active_config = json.loads(strategy_path(args.symbol).read_text())
        active_interval = int(active_config.get("interval"))
        active_params = {**BASELINE, **dict(active_config.get("params", {}))}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    active_result = next((item for item in results if item.get("interval") == f"{active_interval}m" and item.get("best", {}).get("stats")), None)
    current_stats = None
    if active_result is not None and active_params is not None:
        current_stats = evaluate(aggregate(base, active_interval), active_params)["stats"]
    promotion_candidate = winner
    promoted = None
    promotion_allowed, promotion_reason = promotion_gate(promotion_candidate, current_stats, completed_sessions)
    if promotion_candidate is not None:
        promotion_candidate["best"]["run_id"] = run_dir.name
        if promotion_allowed and AUTO_PROMOTION_ENABLED:
            promoted = promote_winner(promotion_candidate["best"])
        else:
            promoted = {"state": "HELD", "reason": promotion_reason}
    randomized_actual = len(randomized_history) - randomized_before
    summary = {"state": "COMPLETED", "symbol": args.symbol, "phase": args.phase, "base_interval": "1m", "completed_sessions": completed_sessions, "max_research_sessions": MAX_RESEARCH_SESSIONS, "randomized_candidates_requested": total_randomized, "randomized_candidates": randomized_actual, "randomized_unique": True, "known_successful_tested_first": True, "ai_enabled": args.ai, "ai_proposals": ai_count, "profitable_params_file": str(profitable_path), "profitable_saved": profitable_count, "tested_intervals": [f"{n}m" for n in intervals], "winner": winner_record, "active_interval": active_interval, "current_active_stats": current_stats, "promotion_gate": {"allowed": promotion_allowed, "reason": promotion_reason, "scope": "SIMULATE any tested interval", "policy_owner": "user_only"}, "promoted": promoted, "results": results}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    (run_dir / "progress.json").write_text(json.dumps({"state": "COMPLETED", "randomized_candidates": summary["randomized_candidates"], "ai_proposals": ai_count, "profitable_saved": profitable_count, "results": results}, indent=2, default=str) + "\n")
    print(json.dumps({"winner": winner_record}, default=str), flush=True)
    return 0


if __name__ == "__main__":
    lock_path = ROOT / ".research_sweep.lock"
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"state": "BUSY", "message": "another research sweep is already running"}), flush=True)
            raise SystemExit(75)
        raise SystemExit(run_main())
