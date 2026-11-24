#!/usr/bin/env python3
"""Research US.ANT on synthetic 1m..21m candles built from a 1m Moomoo cache.

The sweep tests the full 5-session window, publishes the highest-return tested
interval and parameters without interrupting the configured SIMULATE paper bot.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import multiprocessing
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RANDOM_HISTORY = ROOT / ".randomized_param_history.json"
MAX_RESEARCH_SESSIONS = 5
# Keep research below the CPU threshold used by MLX Sentinel so local AI and
# the paper bot remain responsive while sweeps run asynchronously.
RESEARCH_WORKERS = 1
# Candidate generation currently uses the full research window. Until a
# chronological train/validation/test split is implemented, never promote a
# full-window winner automatically into the live paper bot.
AUTO_PROMOTION_ENABLED = False
_WORK_FRAME = None


def _init_evaluator(frame: pd.DataFrame) -> None:
    global _WORK_FRAME
    _WORK_FRAME = frame


def _evaluate_params(params: dict) -> dict:
    return evaluate(_WORK_FRAME, params)
sys.path.insert(0, str(ROOT))
from quant_storage import load_candles
from minute_bars import aggregate_us_session
from market_data import fetch_moomoo_candles
from strategy_57pct import BASELINE, run, stats


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
        raise RuntimeError("No 1-minute US.ANT candles are cached")
    frame = frame.sort_index()[["open", "high", "low", "close", "volume"]].astype(float)
    session_days = pd.Index(frame.index.normalize().unique()).sort_values()
    if len(session_days) > MAX_RESEARCH_SESSIONS:
        session_days = session_days[-MAX_RESEARCH_SESSIONS:]
    frame = frame[frame.index.normalize().isin(session_days)]
    print(json.dumps({"research_window": {"sessions": len(session_days), "start": str(session_days[0].date()), "end": str(session_days[-1].date())}}), flush=True)
    return frame


aggregate = aggregate_us_session


def evaluate(frame: pd.DataFrame, params: dict) -> dict:
    _, trades, data = run(frame, params, 100_000.0, 1.0, 2.0, 0.0)
    result = stats(trades, data, 100_000.0)
    result["selection_score"] = round(result["total_return_pct"] - .35 * abs(result["max_drawdown_pct"]), 4)
    return {"params": dict(params), "stats": result}


def param_key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def known_successful(interval: int) -> list[dict]:
    """Return prior profitable parameters before generating new random candidates."""
    found = {}
    config_path = ROOT / "paper_bot_live_strategy.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if int(config.get("interval")) == interval and config.get("mode") == "SIMULATE":
                found[param_key(config.get("params", {}))] = dict(config["params"])
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    for path in sorted(ROOT.glob("strategy_lab_runs/ANT-MINUTE-*/profitable_params.jsonl")):
        try:
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row.get("interval") == f"{interval}m" and row.get("stats", {}).get("total_return_pct", 0) > 0:
                    params = row.get("params", {})
                    found[param_key(params)] = params
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return list(found.values())


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
        "Return a JSON array of up to 3 bounded parameter objects for a US.ANT "
        f"{interval}-minute candle-open grid strategy. Use only grid_lookback "
        "integer 5..120, grid_intervals integer 2..20, entry_mode in any_grid_cross/top_breakout/upper_half_cross/"
        "lower_half_cross, and exit_mode in any_grid_cross_down/top_breakdown/"
        "bottom_breakdown/lower_half_breakdown. Also propose trailing_activation_pct "
        "and trailing_stop_pct as decimals in 0.0..0.04; use both zero to disable. No prose."
    )
    # Structured candidate extraction needs a stronger instruction-following
    # backend than the tiny local model; keep ordinary chat on auto routing,
    # but use the verified NVIDIA free endpoint for this bounded JSON task.
    mcp_request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "gateway_chat", "arguments": {"prompt": prompt, "provider": "nvidia", "max_tokens": 700}}}
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
    """Publish a tested winner without interrupting the running paper bot."""
    interval = int(winner["interval"].removesuffix("m"))
    params = dict(winner["params"])
    strategy_path = ROOT / "paper_bot_live_strategy.json"
    payload = {
        "symbol": "US.ANT", "interval": interval, "params": params,
        "run_id": winner["run_id"], "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "mode": "SIMULATE",
    }
    temporary = strategy_path.with_name(strategy_path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(strategy_path)

    # The live bot reloads compatible parameters at each new candle. Do not
    # restart launchd here: an automatic research promotion must never
    # interrupt an active paper order or reset in-memory execution state.
    return payload


def promotion_gate(winner: dict | None, completed_sessions: int) -> tuple[bool, str]:
    """Allow hot-swapping only after a minimally credible validation window."""
    if winner is None:
        return False, "no eligible winner"
    if completed_sessions < 2:
        return False, f"only {completed_sessions} completed sessions; require at least 2"
    stats = winner.get("best", {}).get("stats", {})
    if winner.get("evidence") != "NORMAL":
        return False, "winner has weak trade-count evidence"
    if int(stats.get("trades", 0)) < 2:
        return False, "winner has fewer than 2 trades"
    return True, "minimum promotion gate passed"


def run_main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="US.ANT", choices=["US.ANT"], help="active symbol (fixed to US.ANT)")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default=pd.Timestamp.now().strftime("%Y-%m-%d"))
    ap.add_argument("--refresh-1m", action="store_true")
    ap.add_argument("--candidates", type=int, default=250)
    ap.add_argument("--total-candidates", type=int, default=None, help="Total randomized candidates shared across the 21 intervals")
    ap.add_argument("--ai", action="store_true", help="Add bounded AI proposals from the local gateway")
    ap.add_argument("--phase", choices=["PREMARKET", "RTH", "MANUAL"], default="MANUAL", help="Research phase for scheduling and audit records")
    ap.add_argument("--run-dir", type=Path, default=ROOT / "strategy_lab_runs")
    args = ap.parse_args()
    try:
        ensure_one_minute_cache(args.symbol, args.start, args.end, args.refresh_1m)
    except Exception as exc:
        print(json.dumps({"state": "ERROR", "stage": "cache", "error": str(exc)}), flush=True)
        return 1
    base = load_one_minute(args.symbol)
    completed_sessions = int(base.index.normalize().nunique())
    run_dir = args.run_dir / ("ANT-MINUTE-" + time.strftime("%Y%m%d-%H%M%S"))
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.total_candidates is not None and args.total_candidates < 21:
        raise ValueError("--total-candidates must be at least 21")
    candidate_budget = args.candidates
    remainder = 0
    if args.total_candidates is not None:
        candidate_budget, remainder = divmod(args.total_candidates, 21)
    profitable_path = run_dir / "profitable_params.jsonl"
    profitable_count = 0
    tested_randomized = 0
    total_randomized = args.total_candidates or args.candidates * 21
    ai_count = 0
    results = []
    randomized_history = load_random_history()
    randomized_before = len(randomized_history)
    for interval in range(1, 22):
        try:
            frame = aggregate(base, interval)
            if len(frame) < 80:
                result = {"symbol": args.symbol, "interval": f"{interval}m", "rows": len(frame), "status": "INSUFFICIENT_COMPLETE_CANDLES"}
            else:
                interval_budget = candidate_budget + (1 if interval <= remainder else 0)
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
        (run_dir / "progress.json").write_text(json.dumps({"state": "RUNNING", "randomized_candidates": args.total_candidates or args.candidates * 21, "ai_proposals": ai_count, "profitable_saved": profitable_count, "results": results}, indent=2, default=str) + "\n")
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
    promoted = None
    promotion_allowed, promotion_reason = promotion_gate(winner, completed_sessions)
    if winner is not None:
        winner["best"]["run_id"] = run_dir.name
        if promotion_allowed and AUTO_PROMOTION_ENABLED:
            promoted = promote_winner(winner["best"])
        else:
            reason = promotion_reason if not AUTO_PROMOTION_ENABLED else "automatic promotion disabled"
            if not AUTO_PROMOTION_ENABLED:
                reason = "automatic promotion disabled pending out-of-sample validation"
            promoted = {"state": "HELD", "reason": reason}
    randomized_actual = len(randomized_history) - randomized_before
    summary = {"state": "COMPLETED", "symbol": args.symbol, "phase": args.phase, "base_interval": "1m", "completed_sessions": completed_sessions, "max_research_sessions": MAX_RESEARCH_SESSIONS, "randomized_candidates_requested": args.total_candidates or args.candidates * 21, "randomized_candidates": randomized_actual, "randomized_unique": True, "known_successful_tested_first": True, "ai_enabled": args.ai, "ai_proposals": ai_count, "profitable_params_file": str(profitable_path), "profitable_saved": profitable_count, "tested_intervals": [f"{n}m" for n in range(1, 22)], "winner": winner_record, "promotion_gate": {"allowed": promotion_allowed, "reason": promotion_reason}, "promoted": promoted, "results": results}
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
