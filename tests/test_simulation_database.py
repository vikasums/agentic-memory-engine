"""Task 1 verification: simulation package imports and audit schema creation."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import simulation
from simulation.database import init_audit_db, table_exists


EXPECTED_AUDIT_COLUMNS = {
    "event_id",
    "run_number",
    "timestamp",
    "user_id",
    "fact_id",
    "event_type",
    "before_state",
    "after_state",
    "source",
    "created_at",
}

EXPECTED_RUN_COLUMNS = {
    "run_number",
    "start_time",
    "end_time",
    "duration_seconds",
    "user_count",
    "scenario_count",
    "status",
}


@pytest.fixture()
def audit_db(tmp_path):
    return str(tmp_path / "audit_log.db")


def test_package_exports_core_symbols():
    for name in (
        "ScenarioGenerator",
        "AuditLogger",
        "EngineClient",
        "MonitoringService",
        "SimulationRunner",
        "ValidatorService",
        "MonitoringEvent",
        "AuditEvent",
        "RunMetadata",
        "init_audit_db",
    ):
        assert hasattr(simulation, name), f"missing export: {name}"


def test_init_creates_both_tables(audit_db):
    init_audit_db(audit_db)
    assert os.path.exists(audit_db)
    assert table_exists(audit_db, "audit_events")
    assert table_exists(audit_db, "run_metadata")


def test_schema_columns_match_spec(audit_db):
    init_audit_db(audit_db)
    conn = sqlite3.connect(audit_db)
    try:
        audit_cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_events)")}
        run_cols = {r[1] for r in conn.execute("PRAGMA table_info(run_metadata)")}
    finally:
        conn.close()
    assert audit_cols == EXPECTED_AUDIT_COLUMNS
    assert run_cols == EXPECTED_RUN_COLUMNS


def test_init_is_idempotent(audit_db):
    init_audit_db(audit_db)
    init_audit_db(audit_db)
    assert table_exists(audit_db, "audit_events")


def test_run_number_tagging_is_writable(audit_db):
    """Schema-level smoke check that every event can carry a run_number."""
    init_audit_db(audit_db)
    conn = sqlite3.connect(audit_db)
    try:
        conn.execute(
            "INSERT INTO audit_events "
            "(run_number, timestamp, user_id, fact_id, event_type, before_state, "
            "after_state, source, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (1, 15.0, "user_2", "mem_xyz", "created", None, "{}", "scenario_playback", 15.0),
        )
        conn.execute(
            "INSERT INTO run_metadata "
            "(run_number, start_time, end_time, duration_seconds, user_count, "
            "scenario_count, status) VALUES (?,?,?,?,?,?,?)",
            (1, 0.0, 60.0, 60.0, 5, 50, "completed"),
        )
        conn.commit()
        rows = conn.execute("SELECT run_number FROM audit_events WHERE run_number = 1").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1


def test_init_creates_parent_directory(tmp_path):
    nested = str(tmp_path / "data" / "runs" / "audit_log.db")
    init_audit_db(nested)
    assert os.path.exists(nested)


def test_skeletons_raise_not_implemented():
    # ScenarioGenerator.generate() is implemented as of Task 2 — see
    # tests/test_scenario_generator.py.
    with pytest.raises(NotImplementedError):
        simulation.MonitoringService(run_number=1).get_metrics()
