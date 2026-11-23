#!/usr/bin/env python3
"""Keep research checks seven minutes apart after each run completes."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTERVAL_SECONDS = 7 * 60


def main() -> int:
    while True:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "research_scheduler.py")],
            cwd=ROOT,
            check=False,
        )
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
