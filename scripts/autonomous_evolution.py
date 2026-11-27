#!/usr/bin/env python3
"""Autonomously propose, review, test, merge, and hot-swap paper strategy code.

AI may change only the active root strategy source. Every candidate is isolated
in a temporary worktree, reviewed through the bundled MCP gateway, compiled,
and imported before it can reach main. Failures leave the active bot untouched.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_config import active_symbol, research_dir, strategy_path
SYMBOL = active_symbol()
MCP = ROOT / "ai_gateway" / "ai" / "mcp_server.py"
RUNS = research_dir(SYMBOL) / "evolution"
# The AI mutation worker may change strategy implementation only. Promotion
# policy, execution, account, and scheduler files are user-owned controls.
ALLOWED = re.compile(r"^strategy_57pct\.py$")
FORBIDDEN = ("LIVE", "REAL", "place_order", "submit_order", "trade_env")


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def ask(prompt: str) -> str:
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "gateway_chat", "arguments": {"prompt": prompt, "provider": "local", "max_tokens": 128}
    }}
    payload = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize"}) + "\n" + json.dumps(request) + "\n"
    result = subprocess.run([sys.executable, str(MCP)], input=payload, text=True, capture_output=True, cwd=ROOT, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "MCP gateway failed")
    replies = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    response = next((item for item in replies if item.get("id") == 1), {})
    if "error" in response:
        raise RuntimeError(response["error"].get("message", "AI request failed"))
    result_data = response.get("result", {})
    return result_data.get("structuredContent", {}).get("text") or result_data.get("content", [{}])[0].get("text", "")


def extract_patch(text: str) -> str:
    fence = chr(96) * 3
    match = re.search(re.escape(fence) + r"diff\s*(.*?)" + re.escape(fence), text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip() + "\n"
    start = text.find("diff --git ")
    if start >= 0:
        return text[start:].strip() + "\n"
    raise ValueError("AI did not return a unified diff")


def safety_check(worktree: Path, patch: str) -> None:
    changed = [line[6:] for line in patch.splitlines() if line.startswith("+++ b/")]
    if not changed or any(not ALLOWED.fullmatch(path) for path in changed):
        raise ValueError(f"candidate changes outside strategy allowlist: {changed}")
    if any(token.upper() in patch.upper() for token in FORBIDDEN):
        raise ValueError("candidate contains a live-trading or credential-sensitive token")
    if len(patch.splitlines()) > 700:
        raise ValueError("candidate diff is too large")
    if not (worktree / "strategy_57pct.py").exists():
        raise ValueError("canonical strategy file disappeared")


def tests(worktree: Path) -> None:
    commands = [
        [sys.executable, "-m", "compileall", "-q", "scripts"],
        [sys.executable, "scripts/app_minute_interval_sweep.py", "--help"],
        [sys.executable, "-c", "import strategy_57pct; required={'grid_lookback','grid_intervals','entry_mode','exit_mode'}; assert required <= set(strategy_57pct.BASELINE), 'active strategy schema mismatch'"],
        ["git", "diff", "--check"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=worktree, text=True, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(f"test failed: {' '.join(command)}\n{result.stdout}\n{result.stderr}")
    config = json.loads(strategy_path(SYMBOL).read_text())
    if config.get("mode") != "SIMULATE":
        raise RuntimeError("active symbol strategy is not SIMULATE")


def profitability_gate(worktree: Path) -> dict:
    """Require the mutated implementation to beat the active one on identical data."""
    baseline_source = subprocess.check_output(
        ["git", "show", "HEAD:strategy_57pct.py"], cwd=worktree, text=True
    )
    baseline_path = worktree / "baseline_strategy_for_gate.py"
    baseline_path.write_text(baseline_source)
    script = r"""
import importlib.util, json, sqlite3, sys
from pathlib import Path
import pandas as pd
from minute_bars import aggregate_us_session
import strategy_57pct as candidate
spec = importlib.util.spec_from_file_location("baseline_strategy", sys.argv[2])
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)
config = json.loads(Path(sys.argv[3]).read_text())
with sqlite3.connect(sys.argv[1]) as db:
    frame = pd.read_sql_query("select time_key,open,high,low,close,volume from candles where symbol=? and interval_minutes=1 order by time_key", db, params=(sys.argv[4],), parse_dates=["time_key"])
frame = frame.set_index("time_key").sort_index()
days = sorted(frame.index.normalize().unique())[-5:]
frame = frame[frame.index.normalize().isin(days)]
params = config["params"]
def score(module, bars, parameters):
    _, trades, data = module.run(bars, parameters, 100_000.0, 0.95, 2.0, 0.0)
    return module.stats(trades, data, 100_000.0)
results = {}
for interval in range(1, 11):
    bars = aggregate_us_session(frame, interval)
    old = score(baseline, bars, params)
    new = score(candidate, bars, params)
    results[f"{interval}m"] = {"baseline": old, "candidate": new}
old_best = max(item["baseline"]["total_return_pct"] for item in results.values())
new_best = max(item["candidate"]["total_return_pct"] for item in results.values())
old_mean = sum(item["baseline"]["total_return_pct"] for item in results.values()) / len(results)
new_mean = sum(item["candidate"]["total_return_pct"] for item in results.values()) / len(results)
improved = sum(item["candidate"]["total_return_pct"] > item["baseline"]["total_return_pct"] for item in results.values())
print(json.dumps({"sessions": 5, "intervals": list(results), "results": results, "improved_intervals": improved, "best_baseline_return_pct": old_best, "best_candidate_return_pct": new_best, "mean_baseline_return_pct": old_mean, "mean_candidate_return_pct": new_mean}, default=str))
if improved < 6:
    raise SystemExit("candidate does not improve a majority of the 1m-10m intervals")
if new_mean <= old_mean:
    raise SystemExit("candidate does not improve mean return across the 1m-10m interval competition")
if new_best < old_best:
    raise SystemExit("candidate reduces the best result across the 1m-10m interval competition")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "quant_dashboard.sqlite3"),
         str(baseline_path), str(strategy_path(SYMBOL)), SYMBOL],
        cwd=worktree, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"profitability gate failed: {result.stderr.strip()}")
    return json.loads(result.stdout.strip())


def main() -> int:
    dirty = [line for line in git("status", "--porcelain").splitlines()
             if not line.startswith("?? archive/")]
    if dirty:
        print(json.dumps({"state": "SKIPPED", "reason": "main worktree is not clean"}))
        return 0
    run_id = "AUTO-EVOLVE-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    branch = "autonomous/" + run_id.lower()
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    current = (ROOT / "strategy_57pct.py").read_text()
    prompt = f"""You are the strategy mutation worker for a paper-only {SYMBOL} system.
The research contract is fixed: fetch/cache only 1-minute candles, locally aggregate
and test every interval from 1m through 10m, and use only the latest five completed
US sessions. The deterministic evaluator owns PnL; the AI must not invent results.
The safe search space includes grid_lookback integers 5..240, grid_intervals
integers 2..20, four supported entry modes, and four supported exit modes.
Propose one small, measurable improvement to the active deterministic grid-strategy implementation.
Return ONLY a unified git diff. Change only strategy_57pct.py at the repository root. Do not alter account, execution,
paper/live mode, market-data source, or safety boundaries. Preserve public function
signatures. The candidate must pass Python compile/import checks.

Current strategy source:
{current}
"""
    try:
        proposed = extract_patch(ask(prompt))
        (run_dir / "proposal.diff").write_text(proposed)
        review = ask(f"""Review this proposed strategy-code diff for a paper-only {SYMBOL} bot.
The research contract is 1-minute source candles, local aggregation across every 1m
through 10m interval, and exactly the latest five completed sessions. Check correctness,
regression risk, look-ahead bias, unsafe execution changes, and whether it can improve
the mean return and a majority of the 1m-10m intervals without reducing the best result.
Reply with exactly APPROVE or REJECT followed by reasons.
DIFF:
{proposed}""")
        (run_dir / "review.txt").write_text(review)
        if not review.strip().upper().startswith("APPROVE"):
            raise RuntimeError("AI reviewer rejected candidate")
        with tempfile.TemporaryDirectory(prefix="usant-evolution-") as temp:
            worktree = Path(temp) / "repo"
            git("worktree", "add", "--detach", str(worktree), "HEAD")
            try:
                candidate = worktree / "candidate.diff"
                candidate.write_text(proposed)
                safety_check(worktree, proposed)
                applied = subprocess.run(["git", "apply", "--index", str(candidate)], cwd=worktree, text=True, capture_output=True, check=False)
                if applied.returncode:
                    raise RuntimeError(applied.stderr.strip() or "git apply failed")
                tests(worktree)
                gate = profitability_gate(worktree)
                (run_dir / "profitability.json").write_text(json.dumps(gate, indent=2) + "\n")
                git("switch", "-c", branch, cwd=worktree)
                git("commit", "-m", f"autonomous strategy evolution {run_id}", cwd=worktree)
                git("merge", "--ff-only", branch, cwd=ROOT)
            finally:
                git("worktree", "remove", "--force", str(worktree), check=False)
        deploy = subprocess.run([str(ROOT / "ai_gateway/target/release/trading_engine"), "bot-restart"], text=True, capture_output=True, check=False)
        if deploy.returncode:
            raise RuntimeError(f"paper bot hot-swap failed: {deploy.stderr.strip()}")
        (run_dir / "result.json").write_text(json.dumps({"state": "DEPLOYED", "run_id": run_id, "branch": branch, "paper_only": True}, indent=2) + "\n")
        print(json.dumps({"state": "DEPLOYED", "run_id": run_id, "paper_only": True}))
        return 0
    except Exception as exc:
        (run_dir / "result.json").write_text(json.dumps({"state": "REJECTED", "run_id": run_id, "error": str(exc), "paper_only": True}, indent=2) + "\n")
        git("branch", "-D", branch, check=False)
        print(json.dumps({"state": "REJECTED", "run_id": run_id, "error": str(exc), "paper_only": True}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
