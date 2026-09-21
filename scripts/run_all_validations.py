#!/usr/bin/env python3
"""Run the four progressive validations, then regenerate the summary.

Durations come from spec § 1.2: 1, 2, 4 and 6 minutes. Each run takes a fresh
run number, because audit_log.db retains every run's events and replaying an
already-used number mixes runs together.

Requires the memory engine to be reachable at ENGINE_API_BASE_URL.

Usage:
    python3 scripts/run_all_validations.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

RUNS = [(60, 1), (120, 2), (240, 3), (360, 4)]


def main() -> int:
    for duration_seconds, sequence in RUNS:
        print(f"=== run {sequence}: {duration_seconds}s ===", flush=True)
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_validation.py"),
             str(duration_seconds), str(sequence)],
            cwd=ROOT,
        )
        if result.returncode != 0:
            print(f"run {sequence} failed with exit code {result.returncode}")
            return result.returncode

    return subprocess.run(
        [sys.executable, str(SCRIPTS / "generate_summary.py")], cwd=ROOT
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
