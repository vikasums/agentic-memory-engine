"""Task 3 verification: AuditLogger writes, run metadata and audit replay.

Covers spec § 3.5 (schema + write contract), § 6.1 criterion G (audit
completeness / replayability), criterion H (user tagging) and § 7 (run tagging).
"""

import datetime
import json
import os
import sqlite3
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from simulation.audit_logger import AuditLogger
from simulation.database import init_audit_db
from simulation.models import AuditEvent, AuditEventType, AuditSource, RunMetadata, RunStatus


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "audit_log.db")


@pytest.fixture()
def logger(db_path):
    instance = AuditLogger(db_path)
    yield instance
    instance.close()


def raw_rows(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# write()
# ---------------------------------------------------------------------------


def test_write_inserts_all_mandatory_fields(logger, db_path):
    before = time.time()
    event_id = logger.write(
        1,
        "user_1",
        "mem_abc",
        "created",
        None,
        {"text": "I love pizza", "confidence": 0.9},
        "scenario_playback",
        timestamp=123.5,
    )
    assert isinstance(event_id, int) and event_id > 0

    rows = raw_rows(db_path, "SELECT * FROM audit_events")
    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == event_id
    assert row["run_number"] == 1
    assert row["timestamp"] == 123.5
    assert row["user_id"] == "user_1"
    assert row["fact_id"] == "mem_abc"
    assert row["event_type"] == "created"
    assert row["before_state"] is None
    assert json.loads(row["after_state"]) == {"text": "I love pizza", "confidence": 0.9}
    assert row["source"] == "scenario_playback"
    assert row["created_at"] >= before


def test_event_ids_are_stable_and_monotonic(logger):
    ids = [
        logger.write(1, "user_1", f"mem_{i}", "created", None, {"i": i}, "scenario_playback")
        for i in range(5)
    ]
    assert ids == sorted(ids)
    assert len(set(ids)) == 5


def test_write_stamps_created_at_and_timestamp(logger, db_path):
    before = time.time()
    logger.write(1, "user_1", "mem_abc", "created", None, {}, "scenario_playback")
    after = time.time()
    row = raw_rows(db_path, "SELECT timestamp, created_at FROM audit_events")[0]
    assert before <= row["created_at"] <= after
    assert before <= row["timestamp"] <= after


def test_created_at_has_schema_default_for_external_writers(db_path):
    """Finding 3: a row written without created_at is still stamped."""
    init_audit_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        before = time.time()
        conn.execute(
            "INSERT INTO audit_events (run_number, timestamp, user_id, fact_id, "
            "event_type, source) VALUES (?,?,?,?,?,?)",
            (1, 5.0, "user_1", "mem_abc", "created", "ttl_pruning"),
        )
        conn.commit()
    finally:
        conn.close()
    created_at = raw_rows(db_path, "SELECT created_at FROM audit_events")[0]["created_at"]
    assert created_at == pytest.approx(before, abs=5.0)


def test_write_accepts_enum_members(logger, db_path):
    logger.write(
        2,
        "user_2",
        "mem_x",
        AuditEventType.DEACTIVATED,
        {"active": True},
        {"active": False},
        AuditSource.USER_INTERACTION,
    )
    row = raw_rows(db_path, "SELECT event_type, source FROM audit_events")[0]
    assert row["event_type"] == "deactivated"
    assert row["source"] == "user_interaction"


@pytest.mark.parametrize("event_type", [e.value for e in AuditEventType])
def test_every_spec_event_type_is_accepted(logger, event_type):
    assert logger.write(1, "user_1", "mem_a", event_type, None, {}, "scenario_playback") > 0


@pytest.mark.parametrize("source", [s.value for s in AuditSource])
def test_every_spec_source_is_accepted(logger, source):
    assert logger.write(1, "user_1", "mem_a", "created", None, {}, source) > 0


def test_write_accepts_an_audit_event_positionally(logger, db_path):
    event = AuditEvent(
        run_number=3,
        timestamp=7.0,
        user_id="user_3",
        fact_id="mem_q",
        event_type=AuditEventType.EXPIRED.value,
        before_state={"active": True},
        after_state=None,
        source=AuditSource.TTL_PRUNING.value,
    )
    event_id = logger.write(event)
    assert event.event_id == event_id
    assert event.created_at is not None
    row = raw_rows(db_path, "SELECT * FROM audit_events")[0]
    assert (row["event_type"], row["source"]) == ("expired", "ttl_pruning")


def test_state_serialisation_round_trips(logger):
    logger.write(
        1, "user_1", "mem_a", "updated",
        {"text": "old", "nested": {"b": 2, "a": 1}},
        {"text": "new"},
        "scenario_playback",
    )
    event = logger.get_events()[0]
    assert event.before_state == {"text": "old", "nested": {"b": 2, "a": 1}}
    assert event.after_state == {"text": "new"}


def test_preserialised_json_state_is_stored_verbatim(logger, db_path):
    logger.write(1, "user_1", "mem_a", "created", None, '{"already":"json"}', "scenario_playback")
    stored = raw_rows(db_path, "SELECT after_state FROM audit_events")[0]["after_state"]
    assert stored == '{"already":"json"}'
    assert logger.get_events()[0].after_state == {"already": "json"}


def test_state_keys_are_sorted_for_stable_comparison(logger, db_path):
    logger.write(1, "user_1", "mem_a", "created", None, {"z": 1, "a": 2}, "scenario_playback")
    stored = raw_rows(db_path, "SELECT after_state FROM audit_events")[0]["after_state"]
    assert stored == '{"a": 2, "z": 1}'


def test_non_json_values_fall_back_to_str(logger):
    """A state the engine hands over must never crash the audit write."""
    logger.write(
        1, "user_1", "mem_a", "created", None,
        {"when": datetime.datetime(2026, 9, 19, 12, 0)}, "scenario_playback",
    )
    assert logger.get_events()[0].after_state["when"] == "2026-09-19 12:00:00"


# ---------------------------------------------------------------------------
# Validation — findings 2 and run tagging (§ 7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user_id", [None, "", "   "])
def test_write_rejects_missing_user_id(logger, user_id):
    """Finding 2 / criterion H: every audited fact carries a user."""
    with pytest.raises(ValueError, match="user_id"):
        logger.write(1, user_id, "mem_a", "created", None, {}, "scenario_playback")


def test_schema_rejects_null_user_id(db_path):
    init_audit_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO audit_events (run_number, timestamp, user_id, fact_id, "
                "event_type, source, created_at) VALUES (?,?,?,?,?,?,?)",
                (1, 5.0, None, "mem_a", "created", "scenario_playback", 5.0),
            )
    finally:
        conn.close()


def test_write_rejects_non_integer_run_number(logger):
    with pytest.raises(ValueError, match="run_number"):
        logger.write("one", "user_1", "mem_a", "created", None, {}, "scenario_playback")


def test_write_defaults_to_the_loggers_run_number(db_path):
    with AuditLogger(db_path, run_number=7) as logger:
        logger.write(None, "user_1", "mem_a", "created", None, {}, "scenario_playback")
    assert raw_rows(db_path, "SELECT run_number FROM audit_events")[0]["run_number"] == 7


def test_write_rejects_unknown_event_type(logger):
    with pytest.raises(ValueError, match="event_type"):
        logger.write(1, "user_1", "mem_a", "archived", None, {}, "scenario_playback")


def test_write_rejects_unknown_source(logger):
    with pytest.raises(ValueError, match="source"):
        logger.write(1, "user_1", "mem_a", "created", None, {}, "manual_edit")


def test_rejected_write_leaves_no_row(logger, db_path):
    with pytest.raises(ValueError):
        logger.write(1, None, "mem_a", "created", None, {}, "scenario_playback")
    assert raw_rows(db_path, "SELECT COUNT(*) AS n FROM audit_events")[0]["n"] == 0


# ---------------------------------------------------------------------------
# write_many()
# ---------------------------------------------------------------------------


def test_write_many_commits_a_batch(logger):
    events = [
        AuditEvent(1, float(i), "user_1", f"mem_{i}", "created", None, {"i": i}, "scenario_playback")
        for i in range(3)
    ]
    ids = logger.write_many(events)
    assert len(set(ids)) == 3
    assert [e.event_id for e in events] == ids
    assert logger.count_events(run_number=1) == 3


def test_write_many_is_all_or_nothing(logger, db_path):
    events = [
        AuditEvent(1, 1.0, "user_1", "mem_ok", "created", None, {}, "scenario_playback"),
        AuditEvent(1, 2.0, None, "mem_bad", "created", None, {}, "scenario_playback"),
    ]
    with pytest.raises(ValueError):
        logger.write_many(events)
    assert raw_rows(db_path, "SELECT COUNT(*) AS n FROM audit_events")[0]["n"] == 0


# ---------------------------------------------------------------------------
# Run metadata (§ 7.1)
# ---------------------------------------------------------------------------


def test_start_run_records_in_progress_with_notes(logger, db_path):
    logger.start_run(1, user_count=5, scenario_count=50, notes="Initial end-to-end check")
    row = raw_rows(db_path, "SELECT * FROM run_metadata WHERE run_number = 1")[0]
    assert row["status"] == RunStatus.IN_PROGRESS.value
    assert (row["user_count"], row["scenario_count"]) == (5, 50)
    assert row["notes"] == "Initial end-to-end check"
    assert row["start_time"] > 0
    assert row["end_time"] is None
    assert row["duration_seconds"] is None


def test_end_run_stamps_end_time_duration_and_status(logger):
    logger.start_run(1, user_count=5, scenario_count=50, start_time=1000.0)
    logger.end_run(1, end_time=1060.0)
    run = logger.get_run(1)
    assert run.status == RunStatus.COMPLETED.value
    assert run.end_time == 1060.0
    assert run.duration_seconds == pytest.approx(60.0)


def test_end_run_can_mark_a_failure_and_replace_notes(logger):
    logger.start_run(2, notes="Progressive validation")
    logger.end_run(2, status=RunStatus.FAILED, notes="engine unreachable")
    run = logger.get_run(2)
    assert run.status == "failed"
    assert run.notes == "engine unreachable"


def test_end_run_keeps_notes_when_not_given(logger):
    logger.start_run(2, notes="Progressive validation")
    logger.end_run(2)
    assert logger.get_run(2).notes == "Progressive validation"


def test_end_run_requires_a_started_run(logger):
    with pytest.raises(ValueError, match="never started"):
        logger.end_run(99)


def test_end_run_rejects_unknown_status(logger):
    logger.start_run(1)
    with pytest.raises(ValueError, match="status"):
        logger.end_run(1, status="half_done")


def test_start_run_accepts_run_metadata(logger):
    logger.start_run(RunMetadata(run_number=4, start_time=10.0, user_count=2, scenario_count=8))
    run = logger.get_run(4)
    assert (run.run_number, run.user_count, run.scenario_count) == (4, 2, 8)
    assert run.status == RunStatus.IN_PROGRESS.value


def test_restarting_a_run_replaces_metadata_but_keeps_its_events(logger):
    logger.start_run(1, user_count=5, scenario_count=50, notes="first attempt")
    logger.write(1, "user_1", "mem_a", "created", None, {}, "scenario_playback")
    logger.end_run(1, status=RunStatus.FAILED)
    logger.start_run(1, user_count=5, scenario_count=50, notes="retry")
    run = logger.get_run(1)
    assert run.status == RunStatus.IN_PROGRESS.value
    assert run.notes == "retry"
    assert run.end_time is None
    assert logger.count_events(run_number=1) == 1  # § 7.2: events are never deleted


def test_get_run_returns_none_for_unknown_run(logger):
    assert logger.get_run(1234) is None


def test_complete_run_alias_uses_the_loggers_run_number(db_path):
    with AuditLogger(db_path, run_number=3) as logger:
        logger.start_run(start_time=500.0)
        logger.complete_run(end_time=560.0)
        run = logger.get_run()
    assert run.run_number == 3
    assert run.duration_seconds == pytest.approx(60.0)
    assert run.status == RunStatus.COMPLETED.value


# ---------------------------------------------------------------------------
# Audit trail queries (§ 6.1 criterion G, § 7.3)
# ---------------------------------------------------------------------------


def seed_trail(logger):
    logger.write(1, "user_1", "mem_a", "created", None, {"text": "v1"}, "scenario_playback", timestamp=10.0)
    logger.write(1, "user_1", "mem_a", "updated", {"text": "v1"}, {"text": "v2"}, "scenario_playback", timestamp=20.0)
    logger.write(1, "user_1", "mem_a", "deactivated", {"text": "v2"}, None, "scenario_playback", timestamp=30.0)
    logger.write(1, "user_2", "mem_b", "created", None, {"text": "other"}, "user_interaction", timestamp=15.0)
    logger.write(2, "user_1", "mem_c", "expired", {"text": "old"}, None, "ttl_pruning", timestamp=5.0)


def test_get_events_orders_by_timestamp(logger):
    seed_trail(logger)
    timestamps = [e.timestamp for e in logger.get_events(run_number=1)]
    assert timestamps == sorted(timestamps)


@pytest.mark.parametrize(
    "filters,expected",
    [
        ({"run_number": 1}, 4),
        ({"run_number": 2}, 1),
        ({"user_id": "user_1"}, 4),
        ({"user_id": "user_2"}, 1),
        ({"fact_id": "mem_a"}, 3),
        ({"event_type": "created"}, 2),
        ({"source": AuditSource.TTL_PRUNING}, 1),
        ({"run_number": 1, "user_id": "user_1", "event_type": "updated"}, 1),
    ],
)
def test_get_events_filters(logger, filters, expected):
    seed_trail(logger)
    assert len(logger.get_events(**filters)) == expected


def test_get_events_limit(logger):
    seed_trail(logger)
    assert len(logger.get_events(limit=2)) == 2


def test_replay_fact_reconstructs_the_full_lifecycle(logger):
    seed_trail(logger)
    events = logger.replay_fact("user_1", "mem_a")
    assert [e.event_type for e in events] == ["created", "updated", "deactivated"]
    assert events[0].before_state is None
    assert events[1].before_state == {"text": "v1"}
    assert events[1].after_state == {"text": "v2"}


def test_reconstruct_state_returns_the_latest_state(logger):
    logger.write(1, "user_1", "mem_a", "created", None, {"text": "v1"}, "scenario_playback", timestamp=10.0)
    logger.write(1, "user_1", "mem_a", "updated", {"text": "v1"}, {"text": "v2"}, "scenario_playback", timestamp=20.0)
    assert logger.reconstruct_state("user_1", "mem_a") == {"text": "v2"}


def test_reconstruct_state_is_none_after_deactivation(logger):
    seed_trail(logger)
    assert logger.reconstruct_state("user_1", "mem_a") is None
    assert logger.reconstruct_state("user_1", "never_seen") is None


def test_user_isolation_in_the_audit_trail(logger):
    """Criterion H: a per-user query returns only that user's events."""
    seed_trail(logger)
    assert {e.user_id for e in logger.get_events(user_id="user_1")} == {"user_1"}
    assert {e.fact_id for e in logger.get_events(user_id="user_2")} == {"mem_b"}


def test_count_events_scopes_to_a_run(logger):
    seed_trail(logger)
    assert logger.count_events() == 5
    assert logger.count_events(run_number=2) == 1


def test_manual_verification_queries_from_spec_7_3(logger, db_path):
    """The two queries § 7.3 says an operator runs after each run."""
    logger.start_run(1, user_count=5, scenario_count=50, notes="Initial end-to-end check")
    seed_trail(logger)
    logger.end_run(1)
    events = raw_rows(
        db_path, "SELECT * FROM audit_events WHERE run_number = ? ORDER BY timestamp", (1,)
    )
    assert [r["timestamp"] for r in events] == [10.0, 15.0, 20.0, 30.0]
    run = raw_rows(db_path, "SELECT * FROM run_metadata WHERE run_number = ?", (1,))[0]
    assert run["status"] == "completed"


# ---------------------------------------------------------------------------
# Concurrency and lifecycle
# ---------------------------------------------------------------------------


def test_concurrent_writes_get_unique_event_ids(logger):
    ids = []
    lock = threading.Lock()

    def worker(worker_id):
        local = [
            logger.write(1, f"user_{worker_id}", f"mem_{i}", "created", None, {"i": i}, "scenario_playback")
            for i in range(25)
        ]
        with lock:
            ids.extend(local)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ids) == 100
    assert len(set(ids)) == 100
    assert logger.count_events(run_number=1) == 100


def test_events_survive_reopening_the_database(db_path):
    with AuditLogger(db_path) as first:
        event_id = first.write(1, "user_1", "mem_a", "created", None, {"text": "v1"}, "scenario_playback")
    with AuditLogger(db_path) as second:
        events = second.get_events()
    assert [e.event_id for e in events] == [event_id]


def test_close_is_idempotent(db_path):
    logger = AuditLogger(db_path)
    logger.close()
    logger.close()


# ---------------------------------------------------------------------------
# Schema migration (findings 1-3 applied to an existing audit_log.db)
# ---------------------------------------------------------------------------


LEGACY_AUDIT_EVENTS = """
CREATE TABLE audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_number INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    user_id TEXT,
    fact_id TEXT,
    event_type TEXT NOT NULL,
    before_state TEXT,
    after_state TEXT,
    source TEXT,
    created_at REAL NOT NULL
)
"""

LEGACY_RUN_METADATA = """
CREATE TABLE run_metadata (
    run_number INTEGER PRIMARY KEY,
    start_time REAL NOT NULL,
    end_time REAL,
    duration_seconds REAL,
    user_count INTEGER,
    scenario_count INTEGER,
    status TEXT NOT NULL
)
"""


@pytest.fixture()
def legacy_db(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute(LEGACY_AUDIT_EVENTS)
        conn.execute(LEGACY_RUN_METADATA)
        conn.execute("CREATE INDEX idx_audit_run ON audit_events (run_number)")
        conn.execute("CREATE INDEX idx_audit_user ON audit_events (user_id)")
        conn.execute(
            "INSERT INTO audit_events (event_id, run_number, timestamp, user_id, fact_id, "
            "event_type, before_state, after_state, source, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (41, 1, 1.0, "user_1", "mem_old", "created", None, "{}", "scenario_playback", 1.0),
        )
        conn.execute(
            "INSERT INTO audit_events (event_id, run_number, timestamp, user_id, fact_id, "
            "event_type, before_state, after_state, source, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (42, 1, 2.0, None, "mem_untagged", "created", None, "{}", "scenario_playback", 2.0),
        )
        conn.execute(
            "INSERT INTO run_metadata (run_number, start_time, status) VALUES (1, 0.0, 'completed')"
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_migration_preserves_rows_and_event_ids(legacy_db):
    init_audit_db(legacy_db)
    rows = raw_rows(legacy_db, "SELECT event_id, user_id FROM audit_events ORDER BY event_id")
    assert [r["event_id"] for r in rows] == [41, 42]
    assert rows[0]["user_id"] == "user_1"
    assert rows[1]["user_id"] == "__unknown__"  # legacy NULL, never dropped
    assert raw_rows(legacy_db, "SELECT COUNT(*) AS n FROM run_metadata")[0]["n"] == 1


def test_migration_applies_the_three_findings(legacy_db):
    init_audit_db(legacy_db)
    audit_cols = {r["name"]: r for r in raw_rows(legacy_db, "PRAGMA table_info(audit_events)")}
    run_cols = {r["name"] for r in raw_rows(legacy_db, "PRAGMA table_info(run_metadata)")}
    assert audit_cols["user_id"]["notnull"] == 1
    assert audit_cols["created_at"]["dflt_value"] is not None
    assert "notes" in run_cols


def test_migration_restores_indexes_and_leaves_no_legacy_table(legacy_db):
    init_audit_db(legacy_db)
    names = {
        r["name"]
        for r in raw_rows(legacy_db, "SELECT name FROM sqlite_master WHERE type IN ('index','table')")
    }
    assert "audit_events_legacy" not in names
    for index in ("idx_audit_run", "idx_audit_user", "idx_audit_fact", "idx_audit_run_ts"):
        assert index in names


def test_migrated_db_keeps_autoincrement_past_existing_ids(legacy_db):
    init_audit_db(legacy_db)
    with AuditLogger(legacy_db) as logger:
        event_id = logger.write(1, "user_1", "mem_new", "created", None, {}, "scenario_playback")
    assert event_id > 42


def test_migration_is_idempotent(legacy_db):
    init_audit_db(legacy_db)
    init_audit_db(legacy_db)
    assert raw_rows(legacy_db, "SELECT COUNT(*) AS n FROM audit_events")[0]["n"] == 2
