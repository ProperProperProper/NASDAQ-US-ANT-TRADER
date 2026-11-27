#!/usr/bin/env python3
"""Check research eligibility every minute without blocking other services."""
from __future__ import annotations

import subprocess
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_config import active_symbols
CYCLE_COOLDOWN_SECONDS = 30 * 60  # 30 minutes after previous sweep completed


def main() -> int:
    parent_engine = int(os.environ.get("PARENT_ENGINE_PID", "0"))
    while True:
        if parent_engine and os.getppid() != parent_engine:
            return 0
        for symbol in active_symbols():
            subprocess.run(
                ["/usr/sbin/taskpolicy", "-b", "-c", "background", "/usr/bin/nice", "-n", "15", sys.executable, str(ROOT / "scripts" / "research_scheduler.py"), "--symbol", symbol],
                cwd=ROOT,
                check=False,
            )
        # Sleep for 30 minutes after the previous sweep completed
        time.sleep(CYCLE_COOLDOWN_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
