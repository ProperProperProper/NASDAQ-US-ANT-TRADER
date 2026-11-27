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
CHECK_INTERVAL_SECONDS = 60


def main() -> int:
    parent_engine = int(os.environ.get("PARENT_ENGINE_PID", "0"))
    while True:
        if parent_engine and os.getppid() != parent_engine:
            return 0
        for symbol in active_symbols():
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "research_scheduler.py"), "--symbol", symbol],
                cwd=ROOT,
                check=False,
            )
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
