#!/usr/bin/env python3
"""Run one serialized five-session research sweep after a new session appears."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "quant_dashboard.sqlite3"
STATE = ROOT / ".research_scheduler_state.json"


def latest_session() -> str | None:
    with sqlite3.connect(DB) as db:
        value = db.execute("select max(time_key) from candles where symbol='US.ANT' and interval_minutes=1").fetchone()[0]
    return str(value)[:10] if value else None


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
    if previous.get("last_completed_session") == session:
        print(json.dumps({"state": "SKIPPED", "session": session, "reason": "already researched"}), flush=True)
        return 0
    STATE.write_text(json.dumps({"state": "RUNNING", "session": session, "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}, indent=2) + "\n")
    command = [sys.executable, str(ROOT / "scripts/app_minute_interval_sweep.py"), "--start", "2020-01-01", "--end", dt.date.today().isoformat(), "--total-candidates", "100000", "--ai"]
    result = subprocess.run(command, cwd=ROOT, check=False)
    payload = {"state": "COMPLETED" if result.returncode == 0 else "FAILED", "session": session, "exit_code": result.returncode, "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    if result.returncode == 0:
        payload["last_completed_session"] = session
        evolution = subprocess.run([sys.executable, str(ROOT / "scripts" / "autonomous_evolution.py")], cwd=ROOT, text=True, capture_output=True, check=False)
        payload["autonomous_evolution"] = evolution.stdout.strip() or evolution.stderr.strip()
    STATE.write_text(json.dumps(payload, indent=2) + "\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
