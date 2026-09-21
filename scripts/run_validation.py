#!/usr/bin/env python3
"""Run one progressive validation run and write its report.

Every number here is derived from the ValidationReport the run produced. The
earlier per-duration scripts asserted ``"user_isolation": "PASS"`` and
``isolation_pass_rate: 1.0`` as literals regardless of what the run found,
which is why VALIDATION_RESULTS.md disagreed with its own report files.

Run numbers are allocated fresh rather than reused. audit_log.db keeps every
run's events forever, so replaying an already-used run number mixes this run's
facts with historical ones and manufactures cross-user findings.

Usage:
    python3 scripts/run_validation.py <duration_seconds> <sequence>
"""

import asyncio
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from config import settings  # noqa: E402
from simulation import (  # noqa: E402
    AuditLogger,
    EngineClient,
    MonitoringService,
    ScenarioGenerator,
    SimulationRunner,
    ValidatorService,
)

REPORTS_DIR = ROOT / "reports"

#: Outcomes that evidence each success criterion (spec § 1.4).
CRITERION_G_OUTCOMES = ("audit_event_logged", "audit_trail_complete")
CRITERION_H_OUTCOMES = ("user_isolation_ok",)


def _tally(report, outcomes) -> Dict[str, int]:
    counts = Counter()
    for result in report.results:
        if result.outcome in outcomes:
            counts[result.status] += 1
    return {
        "passed": counts.get("passed", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
    }


def next_run_number(audit_db_path: str) -> int:
    """First run number with no events already recorded against it."""
    try:
        conn = sqlite3.connect(audit_db_path)
        try:
            row = conn.execute("SELECT MAX(run_number) FROM audit_events").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 1
    return int(row[0]) + 1 if row and row[0] is not None else 1


def _verdict(tally: Dict[str, int]) -> str:
    """PASS only on evidence: at least one check ran and none failed."""
    if tally["failed"]:
        return "FAIL"
    if tally["passed"]:
        return "PASS"
    return "NO EVIDENCE"


async def run(duration_seconds: int, run_number: int) -> Dict[str, Any]:
    generator = ScenarioGenerator(seed=42)
    scenarios = generator.generate(
        num_scenarios=50, max_duration_seconds=duration_seconds
    )

    async with EngineClient(base_url=settings.engine_api_base_url) as engine_client:
        audit_logger = AuditLogger(
            db_path=settings.audit_db_path, run_number=run_number, auto_init=True
        )
        monitoring = MonitoringService(audit_logger=audit_logger, run_number=run_number)
        validator = ValidatorService(engine_client, audit_logger)
        runner = SimulationRunner(
            engine_client=engine_client,
            monitoring_service=monitoring,
            audit_logger=audit_logger,
            validator=validator,
            run_number=run_number,
            duration_seconds=float(duration_seconds),
            configured_profile_cache_ttl_seconds=settings.profile_cache_ttl_seconds,
        )

        started = time.time()
        result = await runner.run(
            scenarios=scenarios,
            duration_seconds=duration_seconds,
            run_number=run_number,
        )
        ended = time.time()

        report = result.validation_report
        if report is None:
            payload = {
                "run_number": run_number,
                "duration_seconds": duration_seconds,
                "status": result.status,
                "error": "run produced no validation report",
                "notes": list(result.notes),
            }
            audit_logger.close()
            return payload

        scenarios_passed = [s for s in report.scenarios.values() if s.passed]
        g_tally = _tally(report, CRITERION_G_OUTCOMES)
        h_tally = _tally(report, CRITERION_H_OUTCOMES)
        extraction_gaps = sum(
            1 for rec in result.fact_records if not rec.get("memory_ids")
        )

        payload = {
            "run_number": run_number,
            "duration_seconds": duration_seconds,
            "start_time": started,
            "end_time": ended,
            "status": result.status,
            "scenario_summary": {
                "total_scenarios": len(report.scenarios),
                "scenarios_passed": len(scenarios_passed),
                "scenarios_failed": len(report.scenarios) - len(scenarios_passed),
                "facts_played": result.events_played,
                "facts_skipped": result.events_skipped,
                "errors": len(result.errors),
            },
            "checks": {
                "total": report.checks_total,
                "passed": report.checks_passed,
                "failed": report.checks_failed,
                "skipped": report.checks_skipped,
            },
            "criterion_g_audit": {
                **g_tally,
                "verdict": _verdict(g_tally),
                "basis": f"outcomes {list(CRITERION_G_OUTCOMES)} in this run's report",
            },
            "criterion_h_isolation": {
                **h_tally,
                "verdict": _verdict(h_tally),
                "basis": f"outcomes {list(CRITERION_H_OUTCOMES)} in this run's report",
            },
            "cross_validation_errors": len(report.cross_validation_errors),
            "extraction_gaps": {
                "utterances_played": len(result.fact_records),
                "utterances_yielding_no_fact": extraction_gaps,
                "note": (
                    "The engine's extractor returns no fact for some utterances; "
                    "presence checks skip those rather than reporting a storage failure."
                ),
            },
            "metrics": result.metrics or {},
            "notes": list(result.notes),
        }
        audit_logger.close()
        return payload


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    duration_seconds = int(sys.argv[1])
    sequence = int(sys.argv[2])
    run_number = next_run_number(settings.audit_db_path)
    print(f"run {sequence}: {duration_seconds}s as run_number={run_number}")

    payload = asyncio.run(run(duration_seconds, run_number))
    payload["sequence"] = sequence

    REPORTS_DIR.mkdir(exist_ok=True)
    minutes = duration_seconds // 60
    path = REPORTS_DIR / f"run_{sequence}_{minutes}min.json"
    path.write_text(json.dumps(payload, indent=2))
    print(f"wrote {path.relative_to(ROOT)}")
    print(json.dumps(payload.get("checks", {}), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
