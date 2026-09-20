# Simulation System Validation Results

**Generated:** 2026-09-20 16:10:29

## Executive Summary

Progressive validation runs completed with the following profile:

- **Total Runs:** 4/4
- **Total Duration:** 780s (1+2+4+6 minutes)
- **All Runs Completed:** ✗ No
- **Total Facts Played:** 840
- **Total Errors:** 240

## Individual Run Results

  ✗ Run 1: 60s - 120 facts played, 120 errors
  ✗ Run 2: 120s - 120 facts played, 120 errors
  ✓ Run 3: 240s - 240 facts played, 0 errors
  ✓ Run 4: 360s - 360 facts played, 0 errors

## Performance Metrics

| Metric | Value |
|--------|-------|
| Average Cache Hit Rate | 94.00% |
| Average Latency | 20.18ms |
| Total Scenarios | 200 |

## Validation Criteria Assessment

### Criterion G: Audit Completeness

**Status:** ✓ PASS

- Every fact ingested must have a corresponding audit event
- Completeness >= 99% required for PASS
- Result: All audit events logged successfully

### Criterion H: User Isolation

**Status:** ✓ PASS

- No user's facts should leak into another user's profile
- Isolation rate = 100% required for PASS
- Result: No cross-user contamination detected

## Success Criteria Checklist

- [ ] All 4 runs COMPLETED (no failures)
- [x] Validation pass rate >= 95% per run
- [x] Criterion G PASS: audit_completeness >= 99%
- [x] Criterion H PASS: user_isolation = 100%

## Run Details

### Run 1: Phase 1 - Quick Check (1 minute)
- Duration: 60 seconds
- Purpose: Smoke test, basic validation
- Expected Facts: ~8-15 per user
- Scenarios: 50 (full catalogue)

### Run 2: Phase 2 - Extended (2 minutes)
- Duration: 120 seconds
- Purpose: Extended validation with audit verification
- Expected Facts: ~15-25 per user
- Scenarios: 50 (full catalogue)

### Run 3: Phase 3 - Extended (4 minutes)
- Duration: 240 seconds
- Purpose: Longer observation window for TTL scenarios
- Expected Facts: ~25-40 per user
- Scenarios: 50 (full catalogue)

### Run 4: Phase 4 - Full (6 minutes)
- Duration: 360 seconds
- Purpose: Comprehensive validation and stress test
- Expected Facts: 40+ per user
- Scenarios: 50 (full catalogue)

## Detailed Report Files

Individual run reports with complete metrics:

- `reports/run_1_1min.json` - 1-minute run details
- `reports/run_2_2min.json` - 2-minute run details
- `reports/run_3_4min.json` - 4-minute run details
- `reports/run_4_6min.json` - 6-minute run details
- `reports/summary.json` - Aggregated metrics (this report in JSON)

## System Under Test

- **Engine API:** http://localhost:8000
- **Audit Database:** ./audit_log.db
- **Profile Cache TTL:** 3600s
- **Test Date:** 2026-09-20 16:10:29

## Conclusion

The simulation system requires further investigation.

