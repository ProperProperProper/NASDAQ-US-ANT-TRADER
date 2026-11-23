#!/usr/bin/env python3
"""Check research eligibility every minute without blocking other services."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_INTERVAL_SECONDS = 60


def main() -> int:
    while True:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "research_scheduler.py")],
            cwd=ROOT,
            check=False,
        )
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
