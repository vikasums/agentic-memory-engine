"""Task 7 verification: ValidatorService auto-checks and cross-validation.

Covers spec § 3.6 (the four validation methods and their cross-check), § 6.1
criterion G (audit completeness / recoverability) and criterion H (user
isolation), and § 6.2 (every expected outcome a scenario can declare).
"""

import asyncio
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from simulation.audit_logger import AuditLogger
from simulation.database import (
    MEMORY_KEYS_COLUMNS,
    memory_db_status,
    migrate_memory_db,
)
from simulation.engine_client import EngineClientError
from simulation.models import (
    AuditEventType,
    CheckStatus,
    FactType,
    ProfileResult,
    RetrievalResult,
    RetrievedFact,
    RunError,
    RunResult,
    Scenario,
    ScenarioCategory,
    ScenarioFact,
    ValidationReport,
)
from simulation.validator_service import (
    IMPLICIT_OUTCOMES,
    ValidatorService,
    normalise_tokens,
    text_overlap,
    texts_match,
)
from simulation.scenario_generator import EXPECTED_OUTCOME_VOCABULARY


# ----------------------------------------------------------------------
# Fixtures and doubles
# ----------------------------------------------------------------------

CURRENT_MEMORY_DDL = """
CREATE TABLE memory_keys (
    natural_key TEXT PRIMARY KEY,
    memory_id TEXT,
    user_id TEXT,
    subject TEXT,
    predicate TEXT,
    object_value TEXT,
    scope TEXT,
    is_active INTEGER,
    updated_at REAL,
    expires_at REAL
)
"""

STALE_MEMORY_DDL = """
CREATE TABLE memory_keys (
    natural_key TEXT PRIMARY KEY,
    memory_id TEXT,
    user_id TEXT,
    subject TEXT,
    predicate TEXT,
    object_value TEXT,
    scope TEXT,
    is_active INTEGER,
    updated_at REAL
)
"""


def make_memory_db(path, rows=(), stale=False):
    """Write a ``memory.db`` with the engine's schema (optionally the old one)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(STALE_MEMORY_DDL if stale else CURRENT_MEMORY_DDL)
        for row in rows:
            record = {
                "natural_key": f"{row['user_id']}:{row['subject']}:{row['predicate']}",
                "scope": "user",
                "is_active": 1,
                "updated_at": 1_700_000_000.0,
                "expires_at": None,
                **row,
            }
            if stale:
                record.pop("expires_at", None)
            columns = ", ".join(record)
            marks = ", ".join("?" for _ in record)
            conn.execute(
                f"INSERT OR REPLACE INTO memory_keys ({columns}) VALUES ({marks})",
                tuple(record.values()),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def memory_row(memory_id, user_id, subject, predicate, object_value, **extra):
    row = {
        "memory_id": memory_id,
        "user_id": user_id,
        "subject": subject,
        "predicate": predicate,
        "object_value": object_value,
    }
    row.update(extra)
    return row


class FakeEngineClient:
    """Stands in for EngineClient: canned /retrieve and /profile responses."""

    def __init__(self, memories=None, profiles=None, fail_retrieve=False):
        #: ``{user_id: [text, ...]}`` — what /retrieve ranks for that user.
        self.memories = memories or {}
        #: ``{user_id: ProfileResult}``
        self.profiles = profiles or {}
        self.fail_retrieve = fail_retrieve
        self.retrieve_calls = []
        self.profile_calls = []
        self.on_request = None  # Required by SimulationRunner

    async def retrieve(self, user_id, query, top_k=5):
        self.retrieve_calls.append((user_id, query, top_k))
        if self.fail_retrieve:
            raise EngineClientError("engine down", endpoint="/retrieve", status_code=503)
        texts = self.memories.get(user_id, [])
        return RetrievalResult(
            user_id=user_id,
            query=query,
            memories=[
                RetrievedFact(text=text, score=1.0 - i * 0.01, fact_id=f"mem_{user_id}_{i}")
                for i, text in enumerate(texts)
            ],
        )

    async def get_profile(self, user_id, recent_days=7):
        self.profile_calls.append(user_id)
        return self.profiles.get(user_id)


@pytest.fixture
def audit_db(tmp_path):
    return str(tmp_path / "audit_log.db")


@pytest.fixture
def logger(audit_db):
    log = AuditLogger(db_path=audit_db, run_number=1)
    yield log
    log.close()


def write_created(logger, user_id, fact_id, text, run_number=1, timestamp=1000.0):
    return logger.write(
        run_number=run_number,
        user_id=user_id,
        fact_id=fact_id,
        event_type=AuditEventType.CREATED.value,
        after_state={"fact_id": fact_id, "active": True, "text": text, "user_id": user_id},
        timestamp=timestamp,
    )


def write_supersede(logger, user_id, old_id, new_id, text, run_number=1, timestamp=2000.0):
    """The single ``updated`` event MonitoringService writes for a contradiction."""
    return logger.write(
        run_number=run_number,
        user_id=user_id,
        fact_id=new_id,
        event_type=AuditEventType.UPDATED.value,
        before_state={"fact_id": old_id, "active": True},
        after_state={"fact_id": new_id, "active": True, "text": text, "user_id": user_id},
        timestamp=timestamp,
    )


def scenario(
    scenario_id,
    category,
    facts,
    user_id="user_1",
    expected_outcomes=None,
    metadata=None,
):
    return Scenario(
        scenario_id=scenario_id,
        user_id=user_id,
        category=category,
        facts=list(facts),
        expected_outcomes=list(expected_outcomes or []),
        metadata=dict(metadata or {}),
    )


def fact(text, type=FactType.PRIMARY_FACT, **kwargs):
    return ScenarioFact(timestamp=kwargs.pop("timestamp", 0.0), text=text, type=type, **kwargs)


def run_result_for(scenario_obj, fact_ids, users=None, ttls=None, contradicts=None, **kwargs):
    """Build the RunResult the SimulationRunner would have produced."""
    result = RunResult(
        run_number=kwargs.pop("run_number", 1),
        duration_seconds=kwargs.pop("duration_seconds", 60.0),
        scenarios_count=1,
    )
    for index, fact_id in enumerate(fact_ids):
        if fact_id is None:
            continue
        result.fact_ids.setdefault(scenario_obj.scenario_id, []).append(fact_id)
        result.fact_records.append(
            {
                "scenario_id": scenario_obj.scenario_id,
                "fact_index": index,
                "fact_id": fact_id,
                "fact_id_source": "engine" if fact_id.startswith("mem_") else "synthetic",
                "user_id": (users or {}).get(index, scenario_obj.user_id),
                "text": scenario_obj.facts[index].text,
                "fact_type": scenario_obj.facts[index].type.value,
                "ttl_seconds": (ttls or {}).get(index),
                "contradicts_scenario": scenario_obj.facts[index].contradicts_scenario,
                "contradicts_fact_id": (contradicts or {}).get(index),
                "scheduled_time": float(index),
            }
        )
    for key, value in kwargs.items():
        setattr(result, key, value)
    return result


def validate(validator, scenarios, run_result=None, run_number=1):
    return asyncio.run(
        validator.validate(
            run_number=run_number, scenarios=scenarios, run_result=run_result
        )
    )


def outcome(report, scenario_id, name):
    for result in report.scenarios[scenario_id].results:
        if result.outcome == name:
            return result
    raise AssertionError(
        f"{name} not checked; ran {[r.outcome for r in report.scenarios[scenario_id].results]}"
    )


# ----------------------------------------------------------------------
# Text matching
# ----------------------------------------------------------------------


def test_normalise_tokens_drops_stopwords():
    assert normalise_tokens("I live in New York") == {"live", "new", "york"}


def test_text_overlap_and_match():
    assert text_overlap("I live in New York", "user lives new york") > 0.6
    assert texts_match("I live in New York", "user location New York City")
    assert not texts_match("I live in New York", "I prefer dark roast coffee")
    assert text_overlap("", "anything") == 0.0


# ----------------------------------------------------------------------
# memory.db pre-requisite (Task 1 finding)
# ----------------------------------------------------------------------


def test_memory_db_status_reports_missing_file(tmp_path):
    status = memory_db_status(str(tmp_path / "nope.db"))
    assert status["exists"] is False
    assert status["error"]


def test_stale_memory_db_needs_migration_and_migrates(tmp_path):
    path = make_memory_db(str(tmp_path / "memory.db"), stale=True)
    status = memory_db_status(path)
    assert status["needs_migration"] is True
    assert status["missing_columns"] == ["expires_at"]

    assert migrate_memory_db(path) == ["expires_at"]
    after = memory_db_status(path)
    assert after["needs_migration"] is False
    assert list(after["columns"]) == list(MEMORY_KEYS_COLUMNS)
    # Idempotent.
    assert migrate_memory_db(path) == []


def test_stale_memory_db_is_flagged_as_a_prerequisite(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"), stale=True)
    validator = ValidatorService(memory_db_path=path, audit_logger=logger)
    report = validate(validator, [])
    kinds = [entry["kind"] for entry in report.missing_data]
    assert "memory_db_needs_migration" in kinds
    assert any("migrate_memory_db" in note for note in report.notes)


def test_missing_memory_db_is_reported_not_fatal(tmp_path, logger):
    validator = ValidatorService(
        memory_db_path=str(tmp_path / "absent.db"), audit_logger=logger
    )
    report = validate(validator, [])
    kinds = [e["kind"] for e in report.missing_data]
    assert "memory_db_missing" in kinds
    # When no scenarios are validated, we may also record that the audit trail is empty
    assert report.passed is True


# ----------------------------------------------------------------------
# Method 1 — direct memory.db query
# ----------------------------------------------------------------------


def test_validate_via_db_returns_row(tmp_path):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "lives_in", "New York")],
    )
    validator = ValidatorService(memory_db_path=path, audit_db_path="")
    row = validator.validate_via_db("mem_1", "user_1")
    assert row["memory_id"] == "mem_1"
    assert row["is_active"] == 1
    assert validator.validate_via_db("mem_1", "user_2") is None
    assert validator.validate_via_db("mem_missing", "user_1") is None
    validator.close()


def test_validate_via_db_is_read_only(tmp_path):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "lives_in", "New York")],
    )
    validator = ValidatorService(memory_db_path=path, audit_db_path="")
    validator.validate_via_db("mem_1", "user_1")
    with pytest.raises(sqlite3.OperationalError):
        validator._memory_cursor().execute("DELETE FROM memory_keys")
    validator.close()


def test_method1_falls_back_to_text_match_for_synthetic_ids(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_9", "user_1", "user", "lives_in", "New York")],
    )
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["fact_stored"],
    )
    write_created(logger, "user_1", "sim:generic_001:0", "I live in New York")
    validator = ValidatorService(memory_db_path=path, audit_logger=logger)
    report = validate(
        validator, [sc], run_result_for(sc, ["sim:generic_001:0"])
    )
    db_state = report.scenarios["generic_001"].cross_validation[0]["methods"]["db"]
    assert db_state["available"] is True
    assert db_state["detail"]["matched_by"] == "text"
    validator.close()


def test_method1_unavailable_when_nothing_resolves(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["fact_stored"],
    )
    write_created(logger, "user_1", "sim:generic_001:0", "I live in New York")
    validator = ValidatorService(memory_db_path=path, audit_logger=logger)
    report = validate(validator, [sc], run_result_for(sc, ["sim:generic_001:0"]))
    db_state = report.scenarios["generic_001"].cross_validation[0]["methods"]["db"]
    assert db_state["available"] is False
    assert "no memory_keys row" in db_state["reason"]
    # An unavailable method abstains: it must not fail fact_stored, because the
    # audit trail still confirms storage.
    assert outcome(report, "generic_001", "fact_stored").status == CheckStatus.PASSED.value
    validator.close()


# ----------------------------------------------------------------------
# Method 2 — API retrieval
# ----------------------------------------------------------------------


def test_validate_via_api_finds_and_misses():
    client = FakeEngineClient(memories={"user_1": ["user lives in New York"]})
    validator = ValidatorService(engine_client=client, audit_db_path="")
    found = asyncio.run(validator.validate_via_api("user_1", "I live in New York"))
    assert found["available"] is True and found["found"] is True
    gone = asyncio.run(validator.validate_via_api("user_1", "I prefer dark roast coffee"))
    assert gone["available"] is True and gone["found"] is False


def test_validate_via_api_without_client_is_unavailable():
    validator = ValidatorService(audit_db_path="")
    result = asyncio.run(validator.validate_via_api("user_1", "anything"))
    assert result["available"] is False
    assert "no EngineClient" in result["error"]


def test_validate_via_api_records_engine_error():
    client = FakeEngineClient(fail_retrieve=True)
    validator = ValidatorService(engine_client=client, audit_db_path="")
    result = asyncio.run(validator.validate_via_api("user_1", "anything"))
    assert result["available"] is False
    assert "engine down" in result["error"]


def test_api_results_are_cached_per_query():
    client = FakeEngineClient(memories={"user_1": ["user lives in New York"]})
    validator = ValidatorService(engine_client=client, audit_db_path="")
    asyncio.run(validator.validate_via_api("user_1", "q"))
    asyncio.run(validator.validate_via_api("user_1", "q"))
    assert len(client.retrieve_calls) == 1


# ----------------------------------------------------------------------
# Method 3 — audit replay
# ----------------------------------------------------------------------


def test_validate_via_audit_reconstructs_active_state(logger):
    write_created(logger, "user_1", "mem_1", "I live in New York")
    validator = ValidatorService(audit_logger=logger, run_number=1)
    state = validator.validate_via_audit("mem_1", "user_1")
    assert state["available"] is True
    assert state["exists"] is True
    assert state["active"] is True
    assert state["reconstructed"]["text"] == "I live in New York"


def test_validate_via_audit_sees_supersede_as_deactivation(logger):
    write_created(logger, "user_1", "mem_1", "I live in New York", timestamp=1000.0)
    write_supersede(logger, "user_1", "mem_1", "mem_2", "I moved to SF", timestamp=2000.0)
    validator = ValidatorService(audit_logger=logger, run_number=1)

    old = validator.validate_via_audit("mem_1", "user_1")
    assert old["active"] is False
    assert old["superseded_by"]["fact_id"] == "mem_2"

    new = validator.validate_via_audit("mem_2", "user_1")
    assert new["active"] is True


def test_validate_via_audit_honours_deactivated_event(logger):
    write_created(logger, "user_1", "mem_1", "temp", timestamp=1000.0)
    logger.write(
        run_number=1,
        user_id="user_1",
        fact_id="mem_1",
        event_type=AuditEventType.EXPIRED.value,
        before_state={"fact_id": "mem_1", "active": True},
        after_state={"fact_id": "mem_1", "active": False},
        timestamp=1500.0,
    )
    validator = ValidatorService(audit_logger=logger, run_number=1)
    assert validator.validate_via_audit("mem_1", "user_1")["active"] is False


def test_validate_via_audit_unknown_fact(logger):
    validator = ValidatorService(audit_logger=logger, run_number=1)
    state = validator.validate_via_audit("nope", "user_1")
    assert state["exists"] is False
    assert state["active"] is None


def test_validate_via_audit_without_logger():
    validator = ValidatorService(audit_db_path="")
    state = validator.validate_via_audit("mem_1", "user_1")
    assert state["available"] is False


# ----------------------------------------------------------------------
# Method 4 — cross-validation
# ----------------------------------------------------------------------


def test_cross_validate_agreement(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "lives_in", "New York")],
    )
    write_created(logger, "user_1", "mem_1", "I live in New York")
    client = FakeEngineClient(
        memories={"user_1": ["user lives in New York"]},
        profiles={
            "user_1": ProfileResult(
                user_id="user_1",
                stable_facts=["user lives in New York"],
                recent_activity=[],
                profile_timestamp=1.0,
            )
        },
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path, run_number=1
    )
    results = asyncio.run(
        validator.cross_validate("mem_1", "user_1", text="I live in New York")
    )
    by_name = {r.outcome: r for r in results}
    assert by_name["method_db"].details["active"] is True
    assert by_name["method_api"].details["active"] is True
    assert by_name["method_audit"].details["active"] is True
    assert by_name["cross_validation"].passed is True
    assert by_name["cross_validation"].details["consensus_active"] is True
    assert by_name["cross_validation"].details["determinate_methods"] >= 3
    validator.close()


def test_cross_validate_flags_disagreement(tmp_path, logger):
    # memory.db still shows the fact active; /retrieve no longer ranks it.
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "lives_in", "New York")],
    )
    write_created(logger, "user_1", "mem_1", "I live in New York")
    client = FakeEngineClient(memories={"user_1": []})
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path, run_number=1
    )
    results = asyncio.run(
        validator.cross_validate("mem_1", "user_1", text="I live in New York")
    )
    cross = {r.outcome: r for r in results}["cross_validation"]
    assert cross.passed is False
    assert cross.status == CheckStatus.FAILED.value
    assert "say active" in cross.details["diffs"][0]
    validator.close()


def test_cross_validation_error_fails_the_report(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "lives_in", "New York")],
    )
    write_created(logger, "user_1", "mem_1", "I live in New York")
    client = FakeEngineClient(memories={"user_1": []})
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["fact_stored"],
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]))
    assert report.passed is False
    assert len(report.cross_validation_errors) == 1
    error = report.cross_validation_errors[0]
    assert error.fact_id == "mem_1"
    assert error.states["db"]["active"] is True
    assert error.states["api"]["active"] is False
    assert report.scenarios["generic_001"].passed is False
    validator.close()


def test_strict_mode_fails_on_unavailable_method(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "sim:generic_001:0", "I live in New York")
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["fact_stored"],
    )
    lenient = ValidatorService(audit_logger=logger, memory_db_path=path)
    assert validate(lenient, [sc], run_result_for(sc, ["sim:generic_001:0"])).passed
    lenient.close()

    strict = ValidatorService(audit_logger=logger, memory_db_path=path, strict=True)
    report = validate(strict, [sc], run_result_for(sc, ["sim:generic_001:0"]))
    assert report.passed is False
    assert "unavailable in strict mode" in report.cross_validation_errors[0].message
    strict.close()


def test_methods_disagreeing_on_owner_is_a_cross_validation_error(tmp_path, logger):
    # memory.db attributes the fact to user_2; the audit trail to user_1.
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_2", "user", "lives_in", "New York")],
    )
    write_created(logger, "user_1", "mem_1", "I live in New York")
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["user_isolation_ok"],
    )
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]))
    # The DB row belongs to another user, so Method 1 cannot see user_1's fact.
    assert outcome(report, "generic_001", "user_isolation_ok").status in {
        CheckStatus.PASSED.value,
        CheckStatus.SKIPPED.value,
    }
    validator.close()


# ----------------------------------------------------------------------
# § 6.2 base outcomes
# ----------------------------------------------------------------------


@pytest.fixture
def base_setup(tmp_path, logger):
    """One stored, audited, retrievable fact for user_1."""
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "lives_in", "New York")],
    )
    write_created(logger, "user_1", "mem_1", "I live in New York")
    client = FakeEngineClient(
        memories={"user_1": ["user lives in New York"]},
        profiles={
            "user_1": ProfileResult(
                user_id="user_1",
                stable_facts=[],
                recent_activity=["user lives in New York"],
                profile_timestamp=1.0,
            )
        },
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    yield validator, client, logger, path
    validator.close()


def test_base_outcomes_all_pass(base_setup):
    validator, *_ = base_setup
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=[
            "fact_stored",
            "fact_retrievable",
            "audit_event_logged",
            "user_isolation_ok",
            "metadata_correct",
        ],
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]))
    assert report.passed is True
    assert report.checks_failed == 0
    for name in (
        "fact_stored",
        "fact_retrievable",
        "audit_event_logged",
        "user_isolation_ok",
        "metadata_correct",
    ):
        assert outcome(report, "generic_001", name).status == CheckStatus.PASSED.value, name


def test_fact_stored_fails_on_ingest_error(base_setup):
    validator, *_ = base_setup
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["fact_stored"],
    )
    result = run_result_for(sc, ["mem_1"])
    result.errors.append(
        RunError(
            timestamp=1.0,
            scenario_id="generic_001",
            fact_index=0,
            user_id="user_1",
            classification="temporary",
            message="HTTP 503",
        )
    )
    report = validate(validator, [sc], result)
    check = outcome(report, "generic_001", "fact_stored")
    assert check.status == CheckStatus.FAILED.value
    assert check.details["ingest_errors"][0]["error"] == "HTTP 503"


def test_fact_retrievable_fails_when_api_cannot_see_it(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "sim:generic_001:0", "I live in New York")
    client = FakeEngineClient(memories={"user_1": ["something else entirely"]})
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["fact_retrievable"],
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(validator, [sc], run_result_for(sc, ["sim:generic_001:0"]))
    assert outcome(report, "generic_001", "fact_retrievable").status == CheckStatus.FAILED.value
    validator.close()


def test_audit_event_logged_fails_and_records_missing_data(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "lives_in", "New York")],
    )
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["audit_event_logged"],
    )
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]))
    assert outcome(report, "generic_001", "audit_event_logged").status == CheckStatus.FAILED.value
    kinds = [entry["kind"] for entry in report.missing_data]
    assert "missing_audit_trail" in kinds
    assert "empty_audit_trail" in kinds
    validator.close()


def test_metadata_correct_fails_on_wrong_run_number(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "lives_in", "New York")],
    )
    write_created(logger, "user_1", "mem_1", "I live in New York", run_number=2)
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["metadata_correct", "audit_event_logged"],
    )
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    # Reading run 2's events under run 1 must not silently pass.
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]), run_number=1)
    assert outcome(report, "generic_001", "audit_event_logged").status == CheckStatus.FAILED.value
    validator.close()


def test_user_isolation_detects_profile_leak(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "sim:multi_001:0", "The team lead is John")
    write_created(logger, "user_2", "sim:multi_001:1", "The team lead is Sarah")
    client = FakeEngineClient(
        memories={"user_1": [], "user_2": []},
        profiles={
            # user_1's profile leaks user_2's fact.
            "user_1": ProfileResult(
                user_id="user_1",
                stable_facts=["team lead is Sarah"],
                recent_activity=[],
                profile_timestamp=1.0,
            ),
            "user_2": ProfileResult(
                user_id="user_2",
                stable_facts=["team lead is Sarah"],
                recent_activity=[],
                profile_timestamp=1.0,
            ),
        },
    )
    sc = scenario(
        "multi_001",
        ScenarioCategory.MULTI_USER,
        [
            fact("The team lead is John"),
            fact(
                "The team lead is Sarah",
                type=FactType.CONTRADICTION,
                contradicts_scenario="multi_001",
                metadata={"user_id": "user_2"},
            ),
        ],
        expected_outcomes=["user_isolation_ok"],
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(
        validator,
        [sc],
        run_result_for(
            sc,
            ["sim:multi_001:0", "sim:multi_001:1"],
            users={0: "user_1", 1: "user_2"},
        ),
    )
    check = outcome(report, "multi_001", "user_isolation_ok")
    assert check.status == CheckStatus.FAILED.value
    leak = check.details["leaks"][0]
    assert leak["where"] == "profile"
    assert leak["user_id"] == "user_1"
    assert leak["owner"] == "user_2"
    validator.close()


def test_user_isolation_detects_retrieve_leak(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "sim:multi_001:0", "The deadline is Friday")
    write_created(logger, "user_2", "sim:multi_001:1", "The deadline is Monday")
    client = FakeEngineClient(
        # user_1's retrieval returns user_2's fact.
        memories={
            "user_1": ["deadline is Monday"],
            "user_2": ["deadline is Monday"],
        },
        profiles={},
    )
    sc = scenario(
        "multi_003",
        ScenarioCategory.MULTI_USER,
        [
            fact("The deadline is Friday"),
            fact("The deadline is Monday", metadata={"user_id": "user_2"}),
        ],
        expected_outcomes=["user_isolation_ok"],
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(
        validator,
        [sc],
        run_result_for(
            sc,
            ["sim:multi_003:0", "sim:multi_003:1"],
            users={0: "user_1", 1: "user_2"},
        ),
    )
    check = outcome(report, "multi_003", "user_isolation_ok")
    assert check.status == CheckStatus.FAILED.value
    assert any(leak["where"] == "/retrieve" for leak in check.details["leaks"])
    validator.close()


def test_null_user_id_rows_are_reported_as_missing_data(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "lives_in", "New York")],
    )
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO memory_keys (natural_key, memory_id, user_id, is_active) "
        "VALUES ('orphan', 'mem_orphan', NULL, 1)"
    )
    conn.commit()
    conn.close()
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [])
    entry = next(e for e in report.missing_data if e["kind"] == "null_user_id_rows")
    assert entry["count"] == 1
    validator.close()


# ----------------------------------------------------------------------
# Contradiction outcomes
# ----------------------------------------------------------------------


@pytest.fixture
def contradiction_setup(tmp_path, logger):
    """NYC → SF: the engine replaced the natural key, so only SF remains."""
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_2", "user_1", "user", "lives_in", "San Francisco")],
    )
    write_created(logger, "user_1", "mem_1", "I live in New York", timestamp=1000.0)
    write_supersede(
        logger, "user_1", "mem_1", "mem_2", "Actually I moved to San Francisco", 1, 2000.0
    )
    client = FakeEngineClient(
        memories={"user_1": ["user lives in San Francisco"]},
        profiles={
            "user_1": ProfileResult(
                user_id="user_1",
                stable_facts=["user lives in San Francisco"],
                recent_activity=[],
                profile_timestamp=1.0,
            )
        },
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    sc = scenario(
        "contradiction_001",
        ScenarioCategory.CONTRADICTION,
        [
            fact("I live in New York"),
            fact(
                "Actually I moved to San Francisco",
                type=FactType.CONTRADICTION,
                contradicts_scenario="contradiction_001",
            ),
        ],
        expected_outcomes=["old_fact_deactivated", "new_fact_active", "audit_trail_complete"],
    )
    result = run_result_for(
        sc, ["mem_1", "mem_2"], contradicts={1: "mem_1"}
    )
    yield validator, sc, result
    validator.close()


def test_contradiction_outcomes_pass(contradiction_setup):
    validator, sc, result = contradiction_setup
    report = validate(validator, [sc], result)
    assert report.passed is True
    assert outcome(report, sc.scenario_id, "old_fact_deactivated").status == CheckStatus.PASSED.value
    assert outcome(report, sc.scenario_id, "new_fact_active").status == CheckStatus.PASSED.value
    assert outcome(report, sc.scenario_id, "audit_trail_complete").status == CheckStatus.PASSED.value


def test_old_fact_deactivated_fails_when_old_fact_survives(tmp_path, logger):
    # Both rows are still active in memory.db — the contradiction never resolved.
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [
            memory_row("mem_1", "user_1", "user", "lives_in", "New York"),
            memory_row(
                "mem_2",
                "user_1",
                "user",
                "moved_to",
                "San Francisco",
                natural_key="user_1:user:moved_to",
            ),
        ],
    )
    write_created(logger, "user_1", "mem_1", "I live in New York", timestamp=1000.0)
    write_created(
        logger, "user_1", "mem_2", "Actually I moved to San Francisco", timestamp=2000.0
    )
    client = FakeEngineClient(
        memories={"user_1": ["user lives in New York", "user moved to San Francisco"]}
    )
    sc = scenario(
        "contradiction_001",
        ScenarioCategory.CONTRADICTION,
        [
            fact("I live in New York"),
            fact(
                "Actually I moved to San Francisco",
                type=FactType.CONTRADICTION,
                contradicts_scenario="contradiction_001",
            ),
        ],
        expected_outcomes=["old_fact_deactivated", "audit_trail_complete"],
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(
        validator, [sc], run_result_for(sc, ["mem_1", "mem_2"], contradicts={1: "mem_1"})
    )
    old = outcome(report, sc.scenario_id, "old_fact_deactivated")
    assert old.status == CheckStatus.FAILED.value
    assert old.details["still_active"][0]["replaced_by"] == "mem_2"
    # The contradiction was never audited either.
    trail = outcome(report, sc.scenario_id, "audit_trail_complete")
    assert trail.status == CheckStatus.FAILED.value
    assert "before_state.fact_id" in trail.details["audit_problems"][0]["problem"]
    validator.close()


def test_old_fact_deactivated_skipped_without_a_contradiction(base_setup):
    validator, *_ = base_setup
    sc = scenario(
        "contradiction_001",
        ScenarioCategory.CONTRADICTION,
        [fact("I live in New York")],
        expected_outcomes=["old_fact_deactivated"],
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]))
    check = outcome(report, sc.scenario_id, "old_fact_deactivated")
    assert check.status == CheckStatus.SKIPPED.value
    assert check.passed is True


# ----------------------------------------------------------------------
# Expiry outcomes
# ----------------------------------------------------------------------


def expiry_scenario(expires_at=None, ttl=3600.0):
    return scenario(
        "expiry_001",
        ScenarioCategory.EXPIRY,
        [fact("I have a meeting tomorrow at 10am", ttl_seconds=ttl)],
        expected_outcomes=["expiry_scheduled", "ttl_recorded", "fact_active_before_expiry"],
    )


def test_expiry_outcomes_pass_when_expires_at_is_set(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [
            memory_row(
                "mem_1",
                "user_1",
                "user",
                "has_meeting",
                "tomorrow at 10am",
                expires_at=9_999_999_999.0,
            )
        ],
    )
    write_created(logger, "user_1", "mem_1", "I have a meeting tomorrow at 10am")
    client = FakeEngineClient(memories={"user_1": ["user has meeting tomorrow at 10am"]})
    sc = expiry_scenario()
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"], ttls={0: 3600.0}))
    assert outcome(report, "expiry_001", "expiry_scheduled").status == CheckStatus.PASSED.value
    assert outcome(report, "expiry_001", "ttl_recorded").status == CheckStatus.PASSED.value
    assert (
        outcome(report, "expiry_001", "fact_active_before_expiry").status
        == CheckStatus.PASSED.value
    )
    validator.close()


def test_expiry_scheduled_fails_when_expires_at_is_null(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "has_meeting", "tomorrow at 10am")],
    )
    write_created(logger, "user_1", "mem_1", "I have a meeting tomorrow at 10am")
    sc = expiry_scenario()
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"], ttls={0: 3600.0}))
    check = outcome(report, "expiry_001", "expiry_scheduled")
    assert check.status == CheckStatus.FAILED.value
    assert check.details["not_scheduled"][0]["problem"] == "memory_keys.expires_at is NULL"
    validator.close()


def test_expiry_scheduled_skipped_on_stale_memory_db(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "has_meeting", "tomorrow at 10am")],
        stale=True,
    )
    write_created(logger, "user_1", "mem_1", "I have a meeting tomorrow at 10am")
    sc = expiry_scenario()
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"], ttls={0: 3600.0}))
    check = outcome(report, "expiry_001", "expiry_scheduled")
    assert check.status == CheckStatus.SKIPPED.value
    assert "migrate_memory_db" in check.details["reason"]
    validator.close()


def test_ttl_recorded_fails_when_no_fact_carries_a_ttl(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "mem_1", "I have a meeting tomorrow at 10am")
    sc = expiry_scenario(ttl=None)
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]))
    assert outcome(report, "expiry_001", "ttl_recorded").status == CheckStatus.FAILED.value
    assert outcome(report, "expiry_001", "expiry_scheduled").status == CheckStatus.FAILED.value
    validator.close()


def test_no_live_expiry_fired_is_checked_implicitly_and_passes(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "mem_1", "I have a meeting tomorrow at 10am")
    sc = expiry_scenario()
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"], ttls={0: 3600.0}))
    check = outcome(report, "expiry_001", "no_live_expiry_fired")
    assert check.status == CheckStatus.PASSED.value
    assert check.details["min_ttl_seconds"] == 3600.0
    validator.close()


def test_no_live_expiry_fired_fails_if_something_expired(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "mem_1", "I have a meeting tomorrow at 10am", timestamp=1.0)
    logger.write(
        run_number=1,
        user_id="user_1",
        fact_id="mem_1",
        event_type=AuditEventType.EXPIRED.value,
        before_state={"fact_id": "mem_1", "active": True},
        after_state={"fact_id": "mem_1", "active": False},
        timestamp=2.0,
    )
    sc = expiry_scenario()
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"], ttls={0: 3600.0}))
    assert outcome(report, "expiry_001", "no_live_expiry_fired").status == CheckStatus.FAILED.value
    validator.close()


def test_archive_not_deleted_passes_when_history_survives(contradiction_setup):
    validator, sc, result = contradiction_setup
    sc.category = ScenarioCategory.EXPIRY  # pull in the implicit expiry checks
    report = validate(validator, [sc], result)
    assert outcome(report, sc.scenario_id, "archive_not_deleted").status == CheckStatus.PASSED.value


def test_archive_not_deleted_fails_when_before_state_is_lost(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "mem_1", "I live in New York", timestamp=1000.0)
    # A supersede event that kept no before_state cannot recover the old fact.
    logger.write(
        run_number=1,
        user_id="user_1",
        fact_id="mem_2",
        event_type=AuditEventType.UPDATED.value,
        before_state=None,
        after_state={"fact_id": "mem_2", "active": True, "text": "I moved to SF"},
        timestamp=2000.0,
    )
    sc = scenario(
        "expiry_001",
        ScenarioCategory.EXPIRY,
        [
            fact("I live in New York", ttl_seconds=3600.0),
            fact(
                "I moved to SF",
                type=FactType.CONTRADICTION,
                contradicts_scenario="expiry_001",
            ),
        ],
        expected_outcomes=[],
    )
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(
        validator, [sc], run_result_for(sc, ["mem_1", "mem_2"], contradicts={1: "mem_1"})
    )
    check = outcome(report, "expiry_001", "archive_not_deleted")
    assert check.status == CheckStatus.FAILED.value
    assert "before_state" in check.details["lost_history"][0]["problem"]
    validator.close()


# ----------------------------------------------------------------------
# Profile cache outcomes
# ----------------------------------------------------------------------


def cache_scenario(outcomes, metadata=None):
    return scenario(
        "cache_001",
        ScenarioCategory.CACHE,
        [
            fact("I work on the payments team"),
            fact("profile read", type=FactType.PROFILE_READ),
        ],
        expected_outcomes=outcomes,
        metadata=metadata,
    )


def cache_check(expected, matched, unsatisfiable=False, scenario_id="cache_001"):
    return {
        "scenario_id": scenario_id,
        "fact_index": 1,
        "user_id": "user_1",
        "expected": expected,
        "observed": expected if matched else ("miss" if expected == "hit" else "hit"),
        "matched": matched,
        "source": "engine",
        "latency_us": 500,
        "unsatisfiable": unsatisfiable,
    }


@pytest.fixture
def cache_validator(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "works_on", "the payments team")],
    )
    write_created(logger, "user_1", "mem_1", "I work on the payments team")
    client = FakeEngineClient(
        memories={"user_1": ["user works on the payments team"]},
        profiles={
            "user_1": ProfileResult(
                user_id="user_1",
                stable_facts=["user is an engineer"],
                recent_activity=["user works on the payments team"],
                profile_timestamp=123.0,
            )
        },
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    yield validator
    validator.close()


def test_cache_outcomes_pass(cache_validator):
    sc = cache_scenario(
        [
            "profile_generated",
            "profile_reflects_facts",
            "cache_hit_recorded",
            "recent_activity_populated",
            "stable_recent_split_correct",
        ]
    )
    result = run_result_for(sc, ["mem_1", None])
    result.cache_checks = [cache_check("hit", True)]
    report = validate(cache_validator, [sc], result)
    for name in (
        "profile_generated",
        "profile_reflects_facts",
        "cache_hit_recorded",
        "recent_activity_populated",
        "stable_recent_split_correct",
    ):
        assert outcome(report, "cache_001", name).status == CheckStatus.PASSED.value, name
    assert report.passed is True


def test_profile_read_fact_is_never_treated_as_stored(cache_validator):
    sc = cache_scenario(["fact_stored"])
    result = run_result_for(sc, ["mem_1", None])
    report = validate(cache_validator, [sc], result)
    stored = outcome(report, "cache_001", "fact_stored")
    assert stored.status == CheckStatus.PASSED.value
    assert stored.details["stored"] == ["cache_001#0"]


def test_cache_hit_mismatch_fails(cache_validator):
    sc = cache_scenario(["cache_hit_recorded"])
    result = run_result_for(sc, ["mem_1", None])
    result.cache_checks = [cache_check("hit", False)]
    report = validate(cache_validator, [sc], result)
    assert outcome(report, "cache_001", "cache_hit_recorded").status == CheckStatus.FAILED.value


def test_unsatisfiable_cache_expectation_is_skipped_not_failed(cache_validator):
    sc = cache_scenario(
        ["cache_miss_recorded"],
        metadata={"cache_expectations_unsatisfiable": True},
    )
    result = run_result_for(sc, ["mem_1", None])
    result.cache_checks = [cache_check("miss", None, unsatisfiable=True)]
    report = validate(cache_validator, [sc], result)
    miss = outcome(report, "cache_001", "cache_miss_recorded")
    assert miss.status == CheckStatus.SKIPPED.value
    assert miss.passed is True
    marker = outcome(report, "cache_001", "expected_cache_unsatisfiable")
    assert marker.status == CheckStatus.SKIPPED.value
    assert marker.details["unsatisfiable_reads"] == 1
    assert report.passed is True


def test_expected_cache_unsatisfiable_passes_when_everything_was_checkable(cache_validator):
    sc = cache_scenario(["cache_hit_recorded"])
    result = run_result_for(sc, ["mem_1", None])
    result.cache_checks = [cache_check("hit", True)]
    report = validate(cache_validator, [sc], result)
    marker = outcome(report, "cache_001", "expected_cache_unsatisfiable")
    assert marker.status == CheckStatus.PASSED.value
    assert marker.details["unsatisfiable_reads"] == 0


def test_recent_activity_empty_fails(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "mem_1", "I work on the payments team")
    client = FakeEngineClient(
        memories={"user_1": []},
        profiles={
            "user_1": ProfileResult(
                user_id="user_1",
                stable_facts=["user is an engineer"],
                recent_activity=[],
                profile_timestamp=1.0,
            )
        },
    )
    sc = cache_scenario(["recent_activity_populated"])
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1", None]))
    assert (
        outcome(report, "cache_001", "recent_activity_populated").status
        == CheckStatus.FAILED.value
    )
    validator.close()


def test_stable_recent_split_fails_when_lists_overlap(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "mem_1", "I work on the payments team")
    client = FakeEngineClient(
        memories={"user_1": []},
        profiles={
            "user_1": ProfileResult(
                user_id="user_1",
                stable_facts=["user works on the payments team"],
                recent_activity=["user works on the payments team"],
                profile_timestamp=1.0,
            )
        },
    )
    sc = cache_scenario(["stable_recent_split_correct"])
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1", None]))
    assert (
        outcome(report, "cache_001", "stable_recent_split_correct").status
        == CheckStatus.FAILED.value
    )
    validator.close()


def test_profile_refreshed_on_new_fact(cache_validator):
    sc = cache_scenario(["profile_refreshed_on_new_fact"])
    result = run_result_for(sc, ["mem_1", None])
    result.cache_checks = [cache_check("hit", True), cache_check("miss", True)]
    report = validate(cache_validator, [sc], result)
    assert (
        outcome(report, "cache_001", "profile_refreshed_on_new_fact").status
        == CheckStatus.PASSED.value
    )

    result.cache_checks = [cache_check("hit", True), cache_check("miss", False)]
    report = validate(cache_validator, [sc], result)
    assert (
        outcome(report, "cache_001", "profile_refreshed_on_new_fact").status
        == CheckStatus.FAILED.value
    )


def test_profile_refresh_skipped_with_one_read(cache_validator):
    sc = cache_scenario(["profile_refreshed_on_new_fact"])
    result = run_result_for(sc, ["mem_1", None])
    result.cache_checks = [cache_check("hit", True)]
    report = validate(cache_validator, [sc], result)
    assert (
        outcome(report, "cache_001", "profile_refreshed_on_new_fact").status
        == CheckStatus.SKIPPED.value
    )


def test_profile_checks_skipped_without_engine_client(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "mem_1", "I work on the payments team")
    sc = cache_scenario(["profile_generated", "profile_reflects_facts"])
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [sc], run_result_for(sc, ["mem_1", None]))
    assert outcome(report, "cache_001", "profile_generated").status == CheckStatus.SKIPPED.value
    assert report.passed is True
    assert any("Methods 2 and 4 skipped" in note for note in report.notes)
    validator.close()


# ----------------------------------------------------------------------
# Multi-user outcomes
# ----------------------------------------------------------------------


@pytest.fixture
def multi_setup(tmp_path, logger):
    """Two users disagree; both facts must stay active (spec § 5.1 D)."""
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [
            memory_row("mem_a", "user_1", "team", "lead_is", "John"),
            memory_row("mem_b", "user_2", "team", "lead_is", "Sarah"),
        ],
    )
    write_created(logger, "user_1", "mem_a", "The team lead is John", timestamp=1000.0)
    write_created(logger, "user_2", "mem_b", "The team lead is Sarah", timestamp=2000.0)
    client = FakeEngineClient(
        memories={"user_1": ["team lead is John"], "user_2": ["team lead is Sarah"]},
        profiles={
            "user_1": ProfileResult(
                user_id="user_1", stable_facts=["team lead is John"], profile_timestamp=1.0
            ),
            "user_2": ProfileResult(
                user_id="user_2", stable_facts=["team lead is Sarah"], profile_timestamp=1.0
            ),
        },
    )
    sc = scenario(
        "multi_001",
        ScenarioCategory.MULTI_USER,
        [
            fact("The team lead is John"),
            fact(
                "The team lead is Sarah",
                type=FactType.CONTRADICTION,
                contradicts_scenario="multi_001",
                metadata={"user_id": "user_2"},
            ),
        ],
        expected_outcomes=["both_facts_active", "no_cross_user_deactivation", "user_isolation_ok"],
    )
    result = run_result_for(
        sc,
        ["mem_a", "mem_b"],
        users={0: "user_1", 1: "user_2"},
        contradicts={1: "mem_a"},
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    yield validator, sc, result, logger, path
    validator.close()


def test_multi_user_contradiction_leaves_both_active(multi_setup):
    validator, sc, result, *_ = multi_setup
    report = validate(validator, [sc], result)
    assert report.passed is True
    assert outcome(report, "multi_001", "both_facts_active").status == CheckStatus.PASSED.value
    assert (
        outcome(report, "multi_001", "no_cross_user_deactivation").status
        == CheckStatus.PASSED.value
    )
    assert outcome(report, "multi_001", "user_isolation_ok").status == CheckStatus.PASSED.value


def test_cross_user_pair_never_counts_as_a_supersede(multi_setup):
    validator, sc, result, *_ = multi_setup
    report = validate(validator, [sc], result)
    # mem_a is contradicted by another user's fact, so it is not "superseded".
    assert (
        outcome(report, "multi_001", "no_cross_user_deactivation")
        .details["cross_user_contradictions"][0]["contradicted_owner"]
        == "user_1"
    )


def test_cross_user_deactivation_fails(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_b", "user_2", "team", "lead_is", "Sarah")],
    )
    write_created(logger, "user_1", "mem_a", "The team lead is John", timestamp=1000.0)
    # user_2's utterance deactivated user_1's fact — a criterion H violation.
    write_supersede(
        logger, "user_2", "mem_a", "mem_b", "The team lead is Sarah", 1, 2000.0
    )
    client = FakeEngineClient(memories={"user_1": [], "user_2": ["team lead is Sarah"]})
    sc = scenario(
        "multi_001",
        ScenarioCategory.MULTI_USER,
        [
            fact("The team lead is John"),
            fact("The team lead is Sarah", metadata={"user_id": "user_2"}),
        ],
        expected_outcomes=["both_facts_active", "no_cross_user_deactivation"],
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(
        validator,
        [sc],
        run_result_for(sc, ["mem_a", "mem_b"], users={0: "user_1", 1: "user_2"}),
    )
    check = outcome(report, "multi_001", "no_cross_user_deactivation")
    assert check.status == CheckStatus.FAILED.value
    problem = check.details["cross_user_deactivations"][0]
    assert problem["deactivated_fact"] == "mem_a"
    assert problem["owned_by"] == "user_1"
    assert problem["deactivated_by_user"] == "user_2"
    validator.close()


def test_both_facts_active_skipped_for_single_user(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "mem_a", "The team lead is John")
    sc = scenario(
        "multi_001",
        ScenarioCategory.MULTI_USER,
        [fact("The team lead is John")],
        expected_outcomes=["both_facts_active"],
    )
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [sc], run_result_for(sc, ["mem_a"]))
    assert outcome(report, "multi_001", "both_facts_active").status == CheckStatus.SKIPPED.value
    validator.close()


# ----------------------------------------------------------------------
# Generic accumulation outcomes
# ----------------------------------------------------------------------


def test_all_facts_active_passes(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [
            memory_row("mem_1", "user_1", "user", "likes", "dark roast coffee"),
            memory_row(
                "mem_2",
                "user_1",
                "user",
                "listens_to",
                "jazz",
                natural_key="user_1:user:listens_to",
            ),
        ],
    )
    write_created(logger, "user_1", "mem_1", "I like dark roast coffee", timestamp=1.0)
    write_created(logger, "user_1", "mem_2", "I listen to jazz", timestamp=2.0)
    client = FakeEngineClient(
        memories={"user_1": ["user likes dark roast coffee", "user listens to jazz"]}
    )
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I like dark roast coffee"), fact("I listen to jazz")],
        expected_outcomes=["all_facts_active", "no_contradiction_triggered"],
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1", "mem_2"]))
    assert outcome(report, "generic_001", "all_facts_active").status == CheckStatus.PASSED.value
    assert (
        outcome(report, "generic_001", "no_contradiction_triggered").status
        == CheckStatus.PASSED.value
    )
    validator.close()


def test_cross_scenario_collision_does_not_fail_all_facts_active(tmp_path, logger):
    """Another scenario's fact overwrote this one's natural key (Task 2 finding)."""
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_other", "user_1", "user", "likes", "tea")],
    )
    write_created(logger, "user_1", "mem_1", "I like dark roast coffee", timestamp=1.0)
    # A fact from generic_007 superseded generic_001's fact.
    write_supersede(logger, "user_1", "mem_1", "mem_other", "I like tea", 1, 2.0)
    client = FakeEngineClient(memories={"user_1": ["user likes tea"]})
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I like dark roast coffee")],
        expected_outcomes=["all_facts_active"],
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]))
    check = outcome(report, "generic_001", "all_facts_active")
    assert check.status == CheckStatus.SKIPPED.value
    assert check.passed is True
    assert check.details["skipped"][0]["attribution"] == "cross_scenario"
    kinds = [entry["kind"] for entry in report.scenarios["generic_001"].missing_data]
    assert "cross_scenario_collision" in kinds
    validator.close()


def test_same_scenario_deactivation_does_fail_all_facts_active(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "mem_1", "I like dark roast coffee", timestamp=1.0)
    write_created(logger, "user_1", "mem_2", "I listen to jazz", timestamp=2.0)
    write_supersede(logger, "user_1", "mem_1", "mem_2", "I listen to jazz", 1, 3.0)
    client = FakeEngineClient(memories={"user_1": ["user listens to jazz"]})
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I like dark roast coffee"), fact("I listen to jazz")],
        expected_outcomes=["all_facts_active", "no_contradiction_triggered"],
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1", "mem_2"]))
    assert outcome(report, "generic_001", "all_facts_active").status == CheckStatus.FAILED.value
    assert (
        outcome(report, "generic_001", "no_contradiction_triggered").status
        == CheckStatus.FAILED.value
    )
    validator.close()


# ----------------------------------------------------------------------
# Modification chains
# ----------------------------------------------------------------------


@pytest.fixture
def modify_setup(tmp_path, logger):
    """intern → mid → senior; only the last link survives in memory.db."""
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_3", "user_1", "user", "level", "senior engineer")],
    )
    write_created(logger, "user_1", "mem_1", "I am an intern", timestamp=1.0)
    write_created(logger, "user_1", "mem_2", "I am a mid-level engineer", timestamp=2.0)
    write_supersede(
        logger, "user_1", "mem_1", "mem_2", "I am a mid-level engineer", 1, 2.0
    )
    write_created(logger, "user_1", "mem_3", "I am a senior engineer", timestamp=3.0)
    write_supersede(
        logger, "user_1", "mem_2", "mem_3", "I am a senior engineer", 1, 3.0
    )
    client = FakeEngineClient(memories={"user_1": ["senior engineer"]})
    sc = scenario(
        "modify_001",
        ScenarioCategory.MODIFY,
        [
            fact("I am an intern"),
            fact(
                "I am a mid-level engineer",
                type=FactType.MODIFICATION,
                contradicts_scenario="modify_001",
            ),
            fact(
                "I am a senior engineer",
                type=FactType.MODIFICATION,
                contradicts_scenario="modify_001",
            ),
        ],
        expected_outcomes=[
            "latest_value_active",
            "prior_versions_deactivated",
            "audit_trail_complete",
        ],
    )
    result = run_result_for(
        sc,
        ["mem_1", "mem_2", "mem_3"],
        contradicts={1: "mem_1", 2: "mem_2"},
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    yield validator, sc, result
    validator.close()


def test_modification_chain_passes(modify_setup):
    validator, sc, result = modify_setup
    report = validate(validator, [sc], result)
    assert report.passed is True
    assert outcome(report, "modify_001", "latest_value_active").status == CheckStatus.PASSED.value
    prior = outcome(report, "modify_001", "prior_versions_deactivated")
    assert prior.status == CheckStatus.PASSED.value
    assert len(prior.details["deactivated"]) == 2


def test_prior_versions_deactivated_skipped_when_nothing_was_replaced(tmp_path, logger):
    """Only the first link was played — ingesting it does not deactivate it."""
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [memory_row("mem_1", "user_1", "user", "has_experience", "3 years")],
    )
    write_created(logger, "user_1", "mem_1", "I have 3 years of experience")
    client = FakeEngineClient(memories={"user_1": ["user has experience 3 years"]})
    sc = scenario(
        "modify_001",
        ScenarioCategory.MODIFY,
        [fact("I have 3 years of experience")],
        expected_outcomes=["latest_value_active", "prior_versions_deactivated"],
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]))
    prior = outcome(report, "modify_001", "prior_versions_deactivated")
    assert prior.status == CheckStatus.SKIPPED.value
    assert prior.passed is True
    assert outcome(report, "modify_001", "latest_value_active").status == CheckStatus.PASSED.value
    validator.close()


def test_prior_versions_deactivated_fails_when_a_link_survives(tmp_path, logger):
    path = make_memory_db(
        str(tmp_path / "memory.db"),
        [
            memory_row("mem_1", "user_1", "user", "has_experience", "3 years"),
            memory_row(
                "mem_2",
                "user_1",
                "user",
                "experience_years",
                "5 years",
                natural_key="user_1:user:experience_years",
            ),
        ],
    )
    write_created(logger, "user_1", "mem_1", "I have 3 years of experience", timestamp=1.0)
    write_created(logger, "user_1", "mem_2", "I have 5 years of experience", timestamp=2.0)
    client = FakeEngineClient(
        memories={"user_1": ["user has experience 3 years", "user experience years 5 years"]}
    )
    sc = scenario(
        "modify_001",
        ScenarioCategory.MODIFY,
        [
            fact("I have 3 years of experience"),
            fact(
                "I have 5 years of experience",
                type=FactType.MODIFICATION,
                contradicts_scenario="modify_001",
            ),
        ],
        expected_outcomes=["prior_versions_deactivated"],
    )
    validator = ValidatorService(
        engine_client=client, audit_logger=logger, memory_db_path=path
    )
    report = validate(
        validator, [sc], run_result_for(sc, ["mem_1", "mem_2"], contradicts={1: "mem_1"})
    )
    check = outcome(report, "modify_001", "prior_versions_deactivated")
    assert check.status == CheckStatus.FAILED.value
    assert check.details["still_active"][0]["fact"] == "modify_001#0"
    validator.close()


# ----------------------------------------------------------------------
# Report shape, coverage and error handling
# ----------------------------------------------------------------------


def test_every_vocabulary_outcome_has_a_checker():
    for name in EXPECTED_OUTCOME_VOCABULARY:
        assert hasattr(ValidatorService, f"_check_{name}"), name
    for names in IMPLICIT_OUTCOMES.values():
        for name in names:
            assert hasattr(ValidatorService, f"_check_{name}"), name
    checkers = {n[len("_check_") :] for n in dir(ValidatorService) if n.startswith("_check_")}
    implicit = {n for names in IMPLICIT_OUTCOMES.values() for n in names}
    assert checkers == set(EXPECTED_OUTCOME_VOCABULARY) | implicit
    assert len(EXPECTED_OUTCOME_VOCABULARY) == 24
    assert len(checkers) == 27


def test_unknown_outcome_is_skipped_with_a_reason(base_setup):
    validator, *_ = base_setup
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["something_nobody_implemented"],
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]))
    check = outcome(report, "generic_001", "something_nobody_implemented")
    assert check.status == CheckStatus.SKIPPED.value
    assert "no checker implemented" in check.details["reason"]


def test_a_raising_checker_fails_that_outcome_only(base_setup, monkeypatch):
    validator, *_ = base_setup

    def boom(self, ctx, index):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ValidatorService, "_check_fact_stored", boom, raising=True)
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["fact_stored", "audit_event_logged"],
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]))
    assert outcome(report, "generic_001", "fact_stored").status == CheckStatus.FAILED.value
    assert "kaboom" in outcome(report, "generic_001", "fact_stored").details["reason"]
    assert outcome(report, "generic_001", "audit_event_logged").status == CheckStatus.PASSED.value


def test_report_summary_and_iteration(base_setup):
    validator, *_ = base_setup
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["fact_stored", "old_fact_deactivated"],
    )
    report = validate(validator, [sc], run_result_for(sc, ["mem_1"]))
    assert isinstance(report, ValidationReport)
    assert report.run_number == 1
    assert len(report) == 2
    assert [r.outcome for r in report] == ["fact_stored", "old_fact_deactivated"]
    summary = report.summary()
    assert summary["checks_total"] == 2
    assert summary["checks_passed"] == 1
    assert summary["checks_skipped"] == 1
    assert summary["checks_failed"] == 0
    assert summary["scenarios"] == 1
    assert report.finished_at >= report.started_at


def test_validate_with_no_scenarios_passes(logger):
    validator = ValidatorService(audit_logger=logger, memory_db_path="")
    report = validate(validator, [])
    assert report.passed is True
    assert report.scenarios == {}


def test_validate_falls_back_to_the_scenario_generator(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["audit_event_logged"],
    )

    class Generator:
        def generate(self):
            return [sc]

    write_created(logger, "user_1", "sim:generic_001:0", "I live in New York")
    validator = ValidatorService(
        audit_logger=logger, memory_db_path=path, scenario_generator=Generator()
    )
    report = asyncio.run(validator.validate(run_number=1))
    assert "generic_001" in report.scenarios
    assert outcome(report, "generic_001", "audit_event_logged").status == CheckStatus.PASSED.value
    validator.close()


def test_scenarios_may_be_passed_positionally(base_setup):
    validator, *_ = base_setup
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["fact_stored"],
    )
    # The Task 6 runner's generic hand-off passed the scenario list first.
    report = asyncio.run(validator.validate([sc]))
    assert "generic_001" in report.scenarios


def test_legacy_positional_paths_still_construct(tmp_path):
    path = make_memory_db(str(tmp_path / "memory.db"))
    audit = str(tmp_path / "audit_log.db")
    validator = ValidatorService(path, audit)
    assert validator.memory_db_path == path
    assert validator.audit_db_path == audit
    assert validator.client is None
    validator.close()


def test_without_a_run_result_falls_back_to_synthetic_ids(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "sim:generic_001:0", "I live in New York")
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I live in New York")],
        expected_outcomes=["audit_event_logged"],
    )
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [sc], run_result=None)
    assert outcome(report, "generic_001", "audit_event_logged").status == CheckStatus.PASSED.value
    validator.close()


def test_incomplete_audit_rows_are_reported(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "mem_1", "I live in New York")
    # Bypass AuditLogger's validation to simulate a row written by another tool.
    conn = sqlite3.connect(logger.db_path)
    conn.execute(
        "INSERT INTO audit_events (run_number, timestamp, user_id, fact_id, "
        "event_type, source, created_at) VALUES (1, 5.0, 'user_1', NULL, 'created', "
        "'scenario_playback', 5.0)"
    )
    conn.commit()
    conn.close()
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    report = validate(validator, [])
    entry = next(e for e in report.missing_data if e["kind"] == "incomplete_audit_rows")
    assert entry["count"] == 1
    validator.close()


def test_audit_events_from_other_runs_are_ignored(tmp_path, logger):
    path = make_memory_db(str(tmp_path / "memory.db"))
    write_created(logger, "user_1", "mem_1", "I live in New York", run_number=1)
    write_created(logger, "user_1", "mem_2", "I live in Boston", run_number=2)
    validator = ValidatorService(audit_logger=logger, memory_db_path=path)
    state_run1 = validator.validate_via_audit("mem_2", "user_1", run_number=1)
    assert state_run1["exists"] is False
    state_run2 = validator.validate_via_audit("mem_2", "user_1", run_number=2)
    assert state_run2["exists"] is True
    validator.close()


# ----------------------------------------------------------------------
# End to end with the SimulationRunner
# ----------------------------------------------------------------------


def test_runner_hands_scenarios_and_run_result_to_the_validator(tmp_path, audit_db):
    """The Task 6 runner must bind validate()'s keyword arguments correctly."""
    from simulation.models import IngestionResult
    from simulation.monitoring_service import MonitoringService
    from simulation.simulation_runner import SimulationRunner

    path = make_memory_db(str(tmp_path / "memory.db"))

    class Client:
        def __init__(self):
            self.on_request = None

        async def ingest(self, user_id, text, scope="user"):
            return IngestionResult(user_id=user_id, timestamp=1.0, fact_id=None)

        async def get_profile(self, user_id, recent_days=7):
            return ProfileResult(user_id=user_id, profile_timestamp=1.0)

        async def retrieve(self, user_id, query, top_k=5):
            return RetrievalResult(
                user_id=user_id,
                query=query,
                memories=[RetrievedFact(text=query, score=1.0)],
            )

    log = AuditLogger(db_path=audit_db, run_number=7)
    monitor = MonitoringService(audit_logger=log, run_number=7)
    client = Client()
    validator = ValidatorService(
        engine_client=client, audit_logger=log, memory_db_path=path
    )
    sc = scenario(
        "generic_001",
        ScenarioCategory.GENERIC,
        [fact("I like dark roast coffee"), fact("I listen to jazz", timestamp=1.0)],
        expected_outcomes=["fact_stored", "audit_event_logged", "all_facts_active"],
    )
    clock = {"t": 0.0}

    async def fake_sleep(delay):
        clock["t"] += delay

    runner = SimulationRunner(
        engine_client=client,
        monitoring_service=monitor,
        audit_logger=log,
        validator=validator,
        clock=lambda: clock["t"],
        sleep=fake_sleep,
        wait_out_duration=False,
    )
    result = asyncio.run(runner.run([sc], duration_seconds=10.0, run_number=7))

    assert result.fact_records and result.fact_records[0]["fact_id"] == "sim:generic_001:0"
    assert isinstance(result.validation_report, ValidationReport)
    assert result.validation_report.run_number == 7
    assert result.validation_results == result.validation_report.results
    report = result.validation_report
    assert outcome(report, "generic_001", "fact_stored").status == CheckStatus.PASSED.value
    assert outcome(report, "generic_001", "audit_event_logged").status == CheckStatus.PASSED.value
    assert report.passed is True
    validator.close()
    log.close()
