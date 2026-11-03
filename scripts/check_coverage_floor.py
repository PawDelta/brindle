#!/usr/bin/env python3
"""Fail when the suite's line+branch coverage of brindle drops below the floor.

The floor was measured on 2026-11-03 after the test-reduction round
(3,226 tests, 84.2%). Speeding tests up must not lower it; raise FLOOR when
coverage grows. Run with ``uv run python scripts/check_coverage_floor.py``.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

FLOOR = 84.0  # percent, line + branch; measured 84.2 with a little slack for run-to-run noise
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "cov.json"
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-n", "auto", "-m", "not serial",
             "--cov=brindle", "--cov-branch", f"--cov-report=json:{report}", "--cov-report="],
            cwd=ROOT,
        )
        if run.returncode != 0:
            print("coverage floor: the suite failed, so coverage was not judged")
            return run.returncode
        percent = json.loads(report.read_text())["totals"]["percent_covered"]
    verdict = "ok" if percent >= FLOOR else "BELOW FLOOR"
    print(f"coverage floor: {percent:.2f}% (floor {FLOOR}%): {verdict}")
    return 0 if percent >= FLOOR else 1


if __name__ == "__main__":
    sys.exit(main())
