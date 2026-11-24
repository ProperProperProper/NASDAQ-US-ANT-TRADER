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

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "quant_dashboard.sqlite3"
STATE = ROOT / ".research_scheduler_state.json"
RERUN_INTERVAL = dt.timedelta(minutes=15)
PREMARKET_CANDIDATES = "20000"
RTH_CANDIDATES = "2000"
ET = ZoneInfo("America/New_York")


def session_phase(now: dt.datetime) -> str:
    """Return the US equity session phase for an aware timestamp."""
    eastern = now.astimezone(ET)
    clock = eastern.time()
    if dt.time(7, 0) <= clock < dt.time(9, 30):
        return "PREMARKET"
    if dt.time(9, 30) <= clock < dt.time(16, 0):
        return "RTH"
    return "OFF_HOURS"


def latest_session() -> str | None:
    with sqlite3.connect(DB) as db:
        value = db.execute("select max(time_key) from candles where symbol='US.ANT' and interval_minutes=1").fetchone()[0]
    return str(value)[:10] if value else None


def active_intervals() -> str:
    """Focus intraday research around the currently published interval."""
    try:
        config = json.loads((ROOT / "paper_bot_live_strategy.json").read_text())
        center = int(config.get("interval", 1))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        center = 1
    return ",".join(str(value) for value in range(max(1, center - 2), min(21, center + 2) + 1))


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
    if phase == "OFF_HOURS":
        print(json.dumps({"state": "SKIPPED", "session": session, "phase": phase, "reason": "outside premarket and regular session"}), flush=True)
        return 0
    if not rerun_due(previous, session, now):
        finished_at = previous.get("finished_at")
        print(json.dumps({"state": "SKIPPED", "session": session, "phase": phase, "reason": "rerun not due", "rerun_interval_minutes": 15, "last_finished_at": finished_at}), flush=True)
        return 0
    budget = PREMARKET_CANDIDATES if phase == "PREMARKET" else RTH_CANDIDATES
    STATE.write_text(json.dumps({"state": "RUNNING", "session": session, "phase": phase, "started_at": now.isoformat()}, indent=2) + "\n")
    eastern_date = now.astimezone(ET).date().isoformat()
    command = ["/usr/bin/nice", "-n", "10", sys.executable, str(ROOT / "scripts/app_minute_interval_sweep.py"), "--start", "2020-01-01", "--end", eastern_date, "--total-candidates", budget, "--phase", phase, "--ai"]
    if phase == "RTH":
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
