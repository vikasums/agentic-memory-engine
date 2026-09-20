#!/usr/bin/env python3
"""Orchestrator: Run all 4 progressive validation runs sequentially.

Executes:
  1. run_validation_1min.py (Task 12)
  2. run_validation_2min.py (Task 13a)
  3. run_validation_4min.py (Task 13b)
  4. run_validation_6min.py (Task 14)

Generates:
  - reports/run_1_1min.json
  - reports/run_2_2min.json
  - reports/run_3_4min.json
  - reports/run_4_6min.json
  - reports/summary.json (aggregated results)
  - VALIDATION_RESULTS.md (human-readable summary)
"""

import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def run_validation_script(script_name: str) -> bool:
    """Run a validation script and return success status."""
    script_path = Path(__file__).parent / script_name
    logger.info(f"Running {script_name}...")

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=False,
            timeout=600,  # 10-minute timeout per run
        )
        success = result.returncode == 0
        if success:
            logger.info(f"{script_name} completed successfully")
        else:
            logger.error(f"{script_name} failed with return code {result.returncode}")
        return success
    except subprocess.TimeoutExpired:
        logger.error(f"{script_name} timed out after 10 minutes")
        return False
    except Exception as e:
        logger.error(f"{script_name} failed: {e}", exc_info=True)
        return False


def load_report(report_path: Path) -> Dict[str, Any]:
    """Load a JSON report file."""
    if not report_path.exists():
        logger.warning(f"Report not found: {report_path}")
        return {}

    try:
        with open(report_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load report {report_path}: {e}")
        return {}


def aggregate_reports(reports_dir: Path) -> Dict[str, Any]:
    """Aggregate all run reports into summary statistics."""
    reports = []
    report_files = [
        "run_1_1min.json",
        "run_2_2min.json",
        "run_3_4min.json",
        "run_4_6min.json",
    ]

    for report_file in report_files:
        report_path = reports_dir / report_file
        report = load_report(report_path)
        if report:
            reports.append(report)

    if not reports:
        logger.warning("No reports found to aggregate")
        return {}

    # Calculate aggregated metrics
    total_duration = sum(r.get("duration_seconds", 0) for r in reports)
    total_facts_played = sum(r.get("scenario_summary", {}).get("facts_played", 0) for r in reports)
    total_facts_skipped = sum(r.get("scenario_summary", {}).get("facts_skipped", 0) for r in reports)
    total_errors = sum(r.get("scenario_summary", {}).get("errors", 0) for r in reports)
    total_scenarios = sum(r.get("scenario_summary", {}).get("total_scenarios", 0) for r in reports)

    avg_cache_hit_rate = (
        sum(r.get("metrics", {}).get("cache_hit_rate", 0) for r in reports) / len(reports)
        if reports
        else 0
    )
    avg_latency_ms = (
        sum(r.get("metrics", {}).get("avg_latency_ms", 0) for r in reports) / len(reports)
        if reports
        else 0
    )

    # Check if all runs passed
    all_passed = all(r.get("status") == "completed" for r in reports)

    # Check criteria
    criterion_g_pass = all(r.get("criterion_g_audit", {}).get("audit_completeness") == "PASS" for r in reports)
    criterion_h_pass = all(r.get("criterion_h_isolation", {}).get("user_isolation") == "PASS" for r in reports)

    summary = {
        "timestamp": time.time(),
        "total_runs": len(reports),
        "all_runs_completed": all_passed,
        "total_duration_seconds": total_duration,
        "total_facts_played": total_facts_played,
        "total_facts_skipped": total_facts_skipped,
        "total_errors": total_errors,
        "total_scenarios": total_scenarios,
        "average_cache_hit_rate": avg_cache_hit_rate,
        "average_latency_ms": avg_latency_ms,
        "criterion_g_audit_pass": criterion_g_pass,
        "criterion_h_isolation_pass": criterion_h_pass,
        "runs": [
            {
                "run_number": r.get("run_number"),
                "duration": r.get("duration_seconds"),
                "status": r.get("status"),
                "facts_played": r.get("scenario_summary", {}).get("facts_played", 0),
                "errors": r.get("scenario_summary", {}).get("errors", 0),
            }
            for r in reports
        ],
    }

    return summary


def generate_markdown_report(summary: Dict[str, Any], reports_dir: Path) -> str:
    """Generate human-readable markdown report."""
    run_lines = []
    for run in summary.get("runs", []):
        run_lines.append(
            f"  - Run {run['run_number']}: {run['duration']}s, "
            f"{run['facts_played']} facts, "
            f"{run['errors']} errors, "
            f"Status: {run['status']}"
        )

    runs_text = "\n".join(run_lines)

    markdown = f"""# Validation Results Summary

**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}

## Overview

- **Total Runs:** {summary.get('total_runs', 0)}
- **All Runs Completed:** {'Yes' if summary.get('all_runs_completed') else 'No'}
- **Total Duration:** {summary.get('total_duration_seconds', 0)}s
- **Total Facts Played:** {summary.get('total_facts_played', 0)}
- **Total Errors:** {summary.get('total_errors', 0)}

## Run Summary

{runs_text}

## Performance Metrics

- **Average Cache Hit Rate:** {summary.get('average_cache_hit_rate', 0):.2%}
- **Average Latency:** {summary.get('average_latency_ms', 0):.2f}ms

## Validation Criteria

### Criterion G: Audit Completeness
- **Status:** {'PASS' if summary.get('criterion_g_audit_pass') else 'FAIL'}
- **Result:** Every fact has corresponding audit event

### Criterion H: User Isolation
- **Status:** {'PASS' if summary.get('criterion_h_isolation_pass') else 'FAIL'}
- **Result:** No user fact leaks detected

## Detailed Reports

Individual run reports are available in JSON format:

- `reports/run_1_1min.json` - 1-minute smoke test
- `reports/run_2_2min.json` - 2-minute extended validation
- `reports/run_3_4min.json` - 4-minute extended validation
- `reports/run_4_6min.json` - 6-minute comprehensive validation

## Success Criteria Checklist

- [{'x' if summary.get('all_runs_completed') else ' '}] All 4 runs COMPLETED (no failures)
- [{'x' if summary.get('total_errors', 1) == 0 else ' '}] Validation pass rate >= 95% per run
- [{'x' if summary.get('criterion_g_audit_pass') else ' '}] Criterion G PASS: audit_completeness >= 99%
- [{'x' if summary.get('criterion_h_isolation_pass') else ' '}] Criterion H PASS: user_isolation = 100%

"""
    return markdown


def main():
    """Run all validation runs and generate reports."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger.info("Starting progressive validation runs orchestration...")

    # Run all validation scripts
    scripts = [
        "run_validation_1min.py",
        "run_validation_2min.py",
        "run_validation_4min.py",
        "run_validation_6min.py",
    ]

    results = {}
    for script in scripts:
        success = run_validation_script(script)
        results[script] = success
        if not success:
            logger.error(f"Stopping orchestration due to failure in {script}")
            return 1

    logger.info("All validation runs completed successfully")

    # Aggregate reports
    reports_dir = Path(__file__).parent.parent / "reports"
    summary = aggregate_reports(reports_dir)

    if summary:
        summary_path = reports_dir / "summary.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        logger.info(f"Summary saved to {summary_path}")

        # Generate markdown report
        markdown_report = generate_markdown_report(summary, reports_dir)
        markdown_path = Path(__file__).parent.parent / "VALIDATION_RESULTS.md"
        with open(markdown_path, "w") as f:
            f.write(markdown_report)
        logger.info(f"Markdown report saved to {markdown_path}")

    logger.info("Orchestration complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
