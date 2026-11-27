#!/usr/bin/env python3
"""Run serialized five-session research sweeps on fresh or due cached data."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from us_market_calendar import session_phase as market_session_phase
from trading_config import active_symbol, research_dir, strategy_path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "quant_dashboard.sqlite3"
SYMBOL = active_symbol()
STATE = research_dir(SYMBOL) / "scheduler_state.json"
RERUN_INTERVAL = dt.timedelta(minutes=15)
PREMARKET_CANDIDATES = "20000"
PREMARKET_FAST_CANDIDATES = "500"
RTH_CANDIDATES = "2000"
MAX_INTERVAL_MINUTES = 10
ET = ZoneInfo("America/New_York")


def session_phase(now: dt.datetime) -> str:
    """Return the US equity session phase for an aware timestamp."""
    eastern = now.astimezone(ET)
    phase = market_session_phase(eastern.date(), eastern.time())
    return "OFF_HOURS" if phase == "OFF_HOURS" else phase


def latest_session() -> str | None:
    with sqlite3.connect(DB) as db:
        value = db.execute("select max(time_key) from candles where symbol=? and interval_minutes=1", (SYMBOL,)).fetchone()[0]
    return str(value)[:10] if value else None


def active_intervals() -> str:
    """Focus intraday research around the currently published interval."""
    try:
        config = json.loads(strategy_path(SYMBOL).read_text())
        center = int(config.get("interval", 1))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        center = 1
    return ",".join(str(value) for value in range(max(1, center - 2), min(MAX_INTERVAL_MINUTES, center + 2) + 1))


def rerun_due(previous: dict, session: str, now: dt.datetime) -> bool:
    """Return whether the current cached session is due for another sweep."""
    if previous.get("last_completed_session") != session:
        return True
    finished_at = previous.get("finished_at")
    if not finished_at:
        return False
    try:
        finished = dt.datetime.fromisoformat(finished_at)
    except (TypeError, ValueError):
        return False
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=dt.timezone.utc)
    return now - finished >= RERUN_INTERVAL


def main() -> int:
    session = latest_session()
    if not session:
        print(json.dumps({"state": "SKIPPED", "reason": "no 1m cache"}), flush=True)
        return 0
    previous = {}
    if STATE.exists():
        try:
            previous = json.loads(STATE.read_text())
        except (OSError, json.JSONDecodeError):
            previous = {}
    now = dt.datetime.now(dt.timezone.utc)
    phase = session_phase(now)
    if phase in {"OFF_HOURS", "CLOSED"}:
        print(json.dumps({"state": "SKIPPED", "session": session, "phase": phase, "reason": "outside premarket and regular session"}), flush=True)
        return 0
    if not rerun_due(previous, session, now):
        finished_at = previous.get("finished_at")
        print(json.dumps({"state": "SKIPPED", "session": session, "phase": phase, "reason": "rerun not due", "rerun_interval_minutes": 15, "last_finished_at": finished_at}), flush=True)
        return 0
    eastern_now = now.astimezone(ET)
    fast_premarket = phase == "PREMARKET" and eastern_now.time() >= dt.time(9, 15)
    budget = PREMARKET_FAST_CANDIDATES if fast_premarket else (PREMARKET_CANDIDATES if phase == "PREMARKET" else RTH_CANDIDATES)
    STATE.write_text(json.dumps({"state": "RUNNING", "session": session, "phase": phase, "started_at": now.isoformat()}, indent=2) + "\n")
    eastern_date = now.astimezone(ET).date().isoformat()
    command = ["/usr/bin/nice", "-n", "10", sys.executable, str(ROOT / "scripts/app_minute_interval_sweep.py"), "--symbol", SYMBOL, "--start", "2020-01-01", "--end", eastern_date, "--total-candidates", budget, "--phase", phase, "--ai"]
    if phase == "RTH" or fast_premarket:
        command.extend(["--intervals", active_intervals()])
    result = subprocess.run(command, cwd=ROOT, check=False)
    payload = {"state": "COMPLETED" if result.returncode == 0 else "FAILED", "session": session, "phase": phase, "exit_code": result.returncode, "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    if result.returncode == 0:
        payload["last_completed_session"] = session
        evolution = subprocess.run(["/usr/bin/nice", "-n", "10", sys.executable, str(ROOT / "scripts" / "autonomous_evolution.py")], cwd=ROOT, text=True, capture_output=True, check=False)
        payload["autonomous_evolution"] = evolution.stdout.strip() or evolution.stderr.strip()
    STATE.write_text(json.dumps(payload, indent=2) + "\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
