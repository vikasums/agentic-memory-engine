#!/usr/bin/env python3
"""Track T1: measure what the pre-fix checker faults cost, one fault at a time.

The v0.3.0 validation pass rate went 6% -> 12% -> 82% -> 86% across commits
2132dc8, b779619 and 8a80d91, and none of them changed the engine. This script
demonstrates that rather than asserting it: it plays the same 50 scenarios
against the same running engine several times, changing only which pre-fix
measurement fault the validator is running with, and writes every arm's numbers
to one report.

Arms, by default:

* ``baseline`` — the current checker, no faults.
* one arm per harness fault, that fault alone.
* ``all_harness_faults`` — every fault this script can set without restarting
  the engine.
* ``baseline_repeat`` — the baseline again, last, so a reader can see the
  run-to-run variance the per-fault deltas have to clear.

``assume_long_profile_cache`` is excluded from the default arms because it was
the engine's configuration, not harness logic. To measure it, restart the engine
with ``PROFILE_CACHE_TTL_SECONDS=3600`` and run:

    python3 scripts/run_checker_ab.py --arms assume_long_profile_cache \\
        --engine-profile-cache-ttl 3600 --out reports/checker_ttl_fault.json

The value passed there is recorded as *declared by the operator*: this script
cannot read the engine's own TTL, and it does not pretend to.

Usage:
    python3 scripts/run_checker_ab.py [--duration 60] [--arms a,b,c]
                                      [--out reports/checker_bug_ab.json]
"""

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from config import settings  # noqa: E402
from simulation import (  # noqa: E402
    ALL_FAULTS,
    LEGACY_PROFILE_CACHE_TTL_SECONDS,
    FAULT_COMMITS,
    FAULT_DESCRIPTIONS,
    AuditLogger,
    EngineClient,
    LegacyCheckerConfig,
    MonitoringService,
    ScenarioGenerator,
    SimulationRunner,
    ValidatorService,
    legacy_criterion_payload,
)

REPORTS_DIR = ROOT / "reports"
DEFAULT_OUT = REPORTS_DIR / "checker_bug_ab.json"

CRITERION_G_OUTCOMES = ("audit_event_logged", "audit_trail_complete")
CRITERION_H_OUTCOMES = ("user_isolation_ok",)

#: The one fault that is a deployment setting rather than harness logic, so it
#: needs the engine restarted and is never part of a default run.
ENGINE_SIDE_FAULTS = ("assume_long_profile_cache",)

HARNESS_FAULTS = tuple(f for f in ALL_FAULTS if f not in ENGINE_SIDE_FAULTS)

SCENARIO_SEED = 42
SCENARIO_COUNT = 50


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


def _verdict(tally: Dict[str, int]) -> str:
    """PASS only on evidence: at least one check ran and none failed."""
    if tally["failed"]:
        return "FAIL"
    if tally["passed"]:
        return "PASS"
    return "NO EVIDENCE"


def next_run_number(audit_db_path: str) -> int:
    try:
        conn = sqlite3.connect(audit_db_path)
        try:
            row = conn.execute("SELECT MAX(run_number) FROM audit_events").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 1
    return int(row[0]) + 1 if row and row[0] is not None else 1


async def run_arm(
    arm: str,
    faults: Sequence[str],
    duration_seconds: int,
    run_number: int,
) -> Dict[str, Any]:
    """Play the scenario set once with ``faults`` enabled and report the numbers."""
    legacy = LegacyCheckerConfig.from_names(faults)
    # Pre-fix, config.py and the engine agreed on 3600s, so the harness planned
    # the run's cache expectations against that TTL too. Every other arm uses
    # whatever the current configuration says.
    configured_ttl = (
        legacy.engine_ttl_seconds
        if legacy.engine_ttl_seconds is not None
        else settings.profile_cache_ttl_seconds
    )
    generator = ScenarioGenerator(seed=SCENARIO_SEED)
    scenarios = generator.generate(
        num_scenarios=SCENARIO_COUNT, max_duration_seconds=duration_seconds
    )

    async with EngineClient(base_url=settings.engine_api_base_url) as engine_client:
        audit_logger = AuditLogger(
            db_path=settings.audit_db_path, run_number=run_number, auto_init=True
        )
        monitoring = MonitoringService(audit_logger=audit_logger, run_number=run_number)
        validator = ValidatorService(engine_client, audit_logger, legacy=legacy)
        runner = SimulationRunner(
            engine_client=engine_client,
            monitoring_service=monitoring,
            audit_logger=audit_logger,
            validator=validator,
            run_number=run_number,
            duration_seconds=float(duration_seconds),
            configured_profile_cache_ttl_seconds=configured_ttl,
        )

        started = time.time()
        result = await runner.run(
            scenarios=scenarios,
            duration_seconds=duration_seconds,
            run_number=run_number,
        )
        ended = time.time()
        report = result.validation_report
        audit_logger.close()

    payload: Dict[str, Any] = {
        "arm": arm,
        "faults": list(legacy.enabled_faults),
        "fault_details": legacy.describe(),
        "run_number": run_number,
        "duration_seconds": duration_seconds,
        "wall_clock_seconds": round(ended - started, 3),
        "harness_assumed_profile_cache_ttl_seconds": configured_ttl,
        "status": result.status,
    }

    if report is None:
        payload["error"] = "run produced no validation report"
        payload["notes"] = list(result.notes)
        return payload

    scenarios_passed = [s for s in report.scenarios.values() if s.passed]
    g_tally = _tally(report, CRITERION_G_OUTCOMES)
    h_tally = _tally(report, CRITERION_H_OUTCOMES)
    extraction_gaps = sum(1 for rec in result.fact_records if not rec.get("memory_ids"))

    payload.update(
        {
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
            },
            "failed_outcomes": dict(
                Counter(r.outcome for r in report.results if r.failed)
            ),
            "notes": list(result.notes),
        }
    )

    # What the pre-fix report writer would have published from these same
    # tallies, when that fault is one of the arm's faults.
    literal = legacy_criterion_payload(
        legacy,
        audit_tally=g_tally,
        isolation_tally=h_tally,
        events_played=result.events_played,
    )
    if literal:
        payload["report_writer_would_have_published"] = literal

    return payload


def attribute(arms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-fault delta against the baseline arm, plus the baseline's own spread."""
    by_arm = {arm["arm"]: arm for arm in arms}
    baseline = by_arm.get("baseline")
    if baseline is None or "checks" not in baseline:
        return []

    def _delta(arm: Dict[str, Any], section: str, key: str) -> Optional[int]:
        if section not in arm:
            return None
        return arm[section][key] - baseline[section][key]

    rows = []
    for arm in arms:
        if arm["arm"] == "baseline" or "checks" not in arm:
            continue
        faults = arm["faults"]
        rows.append(
            {
                "arm": arm["arm"],
                "faults": faults,
                "commits": sorted({FAULT_COMMITS[f] for f in faults}),
                "descriptions": [FAULT_DESCRIPTIONS[f] for f in faults],
                "scenarios_passed": arm["scenario_summary"]["scenarios_passed"],
                "scenarios_passed_delta": _delta(
                    arm, "scenario_summary", "scenarios_passed"
                ),
                "checks_failed": arm["checks"]["failed"],
                "checks_failed_delta": _delta(arm, "checks", "failed"),
                "checks_passed_delta": _delta(arm, "checks", "passed"),
                "checks_skipped_delta": _delta(arm, "checks", "skipped"),
                "cross_validation_errors": arm["cross_validation_errors"],
                "cross_validation_errors_delta": (
                    arm["cross_validation_errors"]
                    - baseline["cross_validation_errors"]
                ),
                "criterion_h_verdict": arm["criterion_h_isolation"]["verdict"],
                "criterion_g_verdict": arm["criterion_g_audit"]["verdict"],
            }
        )
    return rows


def resolve_arms(requested: Optional[str]) -> List[Dict[str, Any]]:
    """``[{"arm": name, "faults": [...]}, ...]`` in the order they will run."""
    if requested:
        names = [n.strip() for n in requested.split(",") if n.strip()]
        arms = []
        for name in names:
            if name in ("baseline", "baseline_repeat"):
                arms.append({"arm": name, "faults": []})
            elif name == "all_harness_faults":
                arms.append({"arm": name, "faults": list(HARNESS_FAULTS)})
            elif name == "all":
                arms.append({"arm": "all_faults", "faults": list(ALL_FAULTS)})
            elif name in ALL_FAULTS:
                arms.append({"arm": name, "faults": [name]})
            else:
                raise SystemExit(
                    f"unknown arm {name!r}; use baseline, baseline_repeat, "
                    f"all_harness_faults, all, or one of {list(ALL_FAULTS)}"
                )
        return arms

    arms = [{"arm": "baseline", "faults": []}]
    arms += [{"arm": fault, "faults": [fault]} for fault in HARNESS_FAULTS]
    arms.append({"arm": "all_harness_faults", "faults": list(HARNESS_FAULTS)})
    arms.append({"arm": "baseline_repeat", "faults": []})
    return arms


async def main_async(args: argparse.Namespace) -> int:
    arms_to_run = resolve_arms(args.arms)

    async with EngineClient(base_url=settings.engine_api_base_url) as probe:
        try:
            metrics = await probe.get_metrics()
        except Exception as exc:
            print(f"engine at {settings.engine_api_base_url} is not reachable: {exc}")
            return 1

    engine_state = getattr(metrics, "raw", None) or getattr(metrics, "__dict__", {})
    print(
        f"engine {settings.engine_api_base_url} reachable; "
        f"{len(arms_to_run)} arms of {args.duration}s each"
    )

    needs_engine_ttl = [
        arm["arm"] for arm in arms_to_run if "assume_long_profile_cache" in arm["faults"]
    ]
    if needs_engine_ttl and args.engine_profile_cache_ttl != LEGACY_PROFILE_CACHE_TTL_SECONDS:
        print(
            f"arm(s) {needs_engine_ttl} reintroduce the engine's 3600s profile "
            "cache TTL, which is a deployment setting this script cannot change. "
            "Restart the engine with PROFILE_CACHE_TTL_SECONDS=3600 and pass "
            f"--engine-profile-cache-ttl {LEGACY_PROFILE_CACHE_TTL_SECONDS:g} to "
            "confirm, or the arm would record a fault it never applied."
        )
        return 1

    results: List[Dict[str, Any]] = []
    for position, arm in enumerate(arms_to_run, start=1):
        legacy = LegacyCheckerConfig.from_names(arm["faults"])
        run_number = legacy.run_number_for(next_free=next_run_number(settings.audit_db_path))
        print(
            f"[{position}/{len(arms_to_run)}] arm={arm['arm']} "
            f"faults={arm['faults'] or ['none']} run_number={run_number}"
        )
        payload = await run_arm(
            arm=arm["arm"],
            faults=arm["faults"],
            duration_seconds=args.duration,
            run_number=run_number,
        )
        summary = payload.get("scenario_summary", {})
        checks = payload.get("checks", {})
        print(
            f"    scenarios_passed={summary.get('scenarios_passed')}/"
            f"{summary.get('total_scenarios')} "
            f"checks passed={checks.get('passed')} failed={checks.get('failed')} "
            f"skipped={checks.get('skipped')}"
        )
        results.append(payload)

    report = {
        "generated_at": time.time(),
        "purpose": (
            "Track T1: the pre-fix validation numbers were produced by the "
            "harness, not the engine. Every arm below plays the same 50 "
            "scenarios against the same running engine; only the checker's "
            "faults differ."
        ),
        "duration_seconds": args.duration,
        "scenarios": {"count": SCENARIO_COUNT, "seed": SCENARIO_SEED},
        "engine": {
            "base_url": settings.engine_api_base_url,
            "harness_assumed_profile_cache_ttl_seconds": settings.profile_cache_ttl_seconds,
            "operator_declared_profile_cache_ttl_seconds": args.engine_profile_cache_ttl,
            "metrics_at_start": engine_state,
            "note": (
                "The engine process is not restarted between arms, so every arm "
                "reads the same store. Facts from earlier arms therefore remain "
                "present; scenario keys are reused, so repeated arms overwrite "
                "rather than accumulate."
            ),
        },
        "excluded_faults": {
            fault: (
                "deployment setting, not harness logic: needs the engine "
                "restarted with PROFILE_CACHE_TTL_SECONDS=3600"
            )
            for fault in ENGINE_SIDE_FAULTS
            if not any(fault in arm["faults"] for arm in arms_to_run)
        },
        "arms": results,
        "attribution": attribute(results),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration", type=int, default=60, help="seconds per arm (default 60)"
    )
    parser.add_argument(
        "--arms",
        default=None,
        help=(
            "comma-separated arms to run; default is baseline, every harness "
            "fault alone, all_harness_faults, baseline_repeat"
        ),
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="report path")
    parser.add_argument(
        "--engine-profile-cache-ttl",
        type=float,
        default=None,
        help=(
            "the engine's profile cache TTL, as declared by the operator, "
            "recorded in the report; this script cannot read it from the engine"
        ),
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
