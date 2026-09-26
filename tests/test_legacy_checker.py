"""The legacy checker reproduces the pre-fix harness faults, one at a time.

Track T1 needs a number for each fault, not one number for all of them, so
every fault is an independent flag on :class:`LegacyCheckerConfig` and nothing
here touches the engine: the same engine build and the same store answer both
the baseline run and the legacy run.

The faults, and the commit that fixed each:

1. ``synthetic_fact_ids`` — ``2132dc8``. ``/ingest`` was fire-and-forget, so the
   harness invented ``sim:<scenario>:<index>`` ids while the engine stored
   ``mem_*``.
2. ``compare_utterance_text`` — ``2132dc8``. The harness compared the scenario
   sentence against a stored triple, scoring 0.500 against a 0.6 bar.
3. ``assume_long_profile_cache`` — ``2132dc8``. A 3600s profile cache TTL over a
   60-360s run, so checks read a profile cached before the run began. This one
   is a deployment setting rather than harness logic, so it carries an
   ``engine_ttl_seconds`` value the A/B script applies to the engine.
4. ``silence_is_failure`` — ``b779619``. An utterance the extractor found no
   fact in was reported as a storage failure.
5. ``script_decides_ownership`` — ``b779619``. Criterion H was decided by the
   scenario script's phrasing rather than by who owns the text in memory.db.
6. ``reuse_run_number`` — ``8a80d91``. Every run reused ``run_number=1``, so
   validation replayed earlier runs and manufactured cross-user findings.
7. ``literal_verdicts`` — ``8a80d91``. The report writer asserted
   ``"user_isolation": "PASS"`` and decided audit completeness by
   ``events_played > 0``.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from simulation.legacy_checker import (  # noqa: E402
    ALL_FAULTS,
    LegacyCheckerConfig,
    legacy_criterion_payload,
)
from simulation.models import (  # noqa: E402
    FactType,
    Scenario,
    ScenarioCategory,
    ScenarioFact,
)
from simulation.validator_service import ValidatorService, _FactRef  # noqa: E402


def _ref(**overrides):
    base = dict(
        scenario_id="generic_001",
        fact_index=0,
        text="I work in Dubai",
        user_id="user_1",
        fact_type="primary_fact",
        fact_id="mem_1",
        memory_ids=["mem_1"],
        stored_texts=["user works_in Dubai"],
        extraction_reported=True,
    )
    base.update(overrides)
    return _FactRef(**base)


class TestConfigSurface:
    def test_default_config_enables_no_fault(self):
        config = LegacyCheckerConfig()
        assert config.any_enabled is False
        assert config.enabled_faults == ()

    def test_all_faults_builds_every_flag(self):
        config = LegacyCheckerConfig.from_names(["all"])
        assert set(config.enabled_faults) == set(ALL_FAULTS)

    def test_a_single_fault_enables_only_itself(self):
        config = LegacyCheckerConfig.from_names(["silence_is_failure"])
        assert config.enabled_faults == ("silence_is_failure",)
        assert config.silence_is_failure is True
        assert config.synthetic_fact_ids is False

    def test_an_unknown_fault_name_is_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            LegacyCheckerConfig.from_names(["no_such_fault"])
        assert "no_such_fault" in str(excinfo.value)

    def test_engine_ttl_is_only_set_by_the_cache_fault(self):
        assert LegacyCheckerConfig().engine_ttl_seconds is None
        cache = LegacyCheckerConfig.from_names(["assume_long_profile_cache"])
        assert cache.engine_ttl_seconds == 3600.0


class TestFault2CompareUtteranceText:
    def test_fixed_validator_prefers_the_stored_triple(self):
        validator = ValidatorService()
        assert validator._match_texts(_ref())[0] == "user works_in Dubai"

    def test_legacy_validator_compares_the_utterance_only(self):
        validator = ValidatorService(
            legacy=LegacyCheckerConfig.from_names(["compare_utterance_text"])
        )
        assert validator._match_texts(_ref()) == ["I work in Dubai"]

    def test_the_legacy_comparison_scores_below_the_match_bar(self):
        # This is the arithmetic that produced the false misses: the harness
        # scored the sentence against the triple the engine actually stored.
        from simulation.validator_service import text_overlap

        score = text_overlap("I work in Dubai", "user works_in Dubai")
        assert score < ValidatorService().match_threshold


class TestFault4SilenceIsFailure:
    def test_fixed_validator_treats_engine_silence_as_nothing_to_check(self):
        validator = ValidatorService()
        ref = _ref(memory_ids=[], extraction_reported=True)
        assert validator._extracted_nothing(ref) is True

    def test_legacy_validator_treats_engine_silence_as_a_present_fact(self):
        validator = ValidatorService(
            legacy=LegacyCheckerConfig.from_names(["silence_is_failure"])
        )
        ref = _ref(memory_ids=[], extraction_reported=True)
        assert validator._extracted_nothing(ref) is False


class TestFault5ScriptDecidesOwnership:
    @pytest.fixture
    def memory_db(self, tmp_path):
        path = tmp_path / "memory.db"
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE memory_keys (
                natural_key TEXT PRIMARY KEY,
                memory_id TEXT,
                user_id TEXT,
                subject TEXT,
                predicate TEXT,
                object_value TEXT,
                is_active INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO memory_keys VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "user_3:user:works_on",
                "mem_3",
                "user_3",
                "user",
                "works_on",
                "recipe planning side project",
                1,
            ),
        )
        conn.commit()
        conn.close()
        return str(path)

    def test_fixed_validator_asks_the_store_who_owns_the_text(self, memory_db):
        validator = ValidatorService(memory_db_path=memory_db)
        assert (
            validator._user_owns_text(
                "user_3", "user works_on recipe planning side project"
            )
            is True
        )

    def test_legacy_validator_never_credits_the_owner(self, memory_db):
        validator = ValidatorService(
            memory_db_path=memory_db,
            legacy=LegacyCheckerConfig.from_names(["script_decides_ownership"]),
        )
        assert (
            validator._user_owns_text(
                "user_3", "user works_on recipe planning side project"
            )
            is False
        )


class TestFault1SyntheticFactIds:
    @staticmethod
    def _scenario():
        return Scenario(
            scenario_id="generic_001",
            category=ScenarioCategory.GENERIC,
            user_id="user_1",
            facts=[
                ScenarioFact(
                    timestamp=0.0,
                    text="I work in Dubai",
                    type=FactType.PRIMARY_FACT,
                )
            ],
            expected_outcomes=["fact_stored"],
        )

    class _RunResult:
        errors = ()
        fact_ids = {}
        fact_records = [
            {
                "scenario_id": "generic_001",
                "fact_index": 0,
                "user_id": "user_1",
                "fact_id": "mem_1",
                "fact_id_source": "engine",
                "memory_ids": ["mem_1"],
                "stored_texts": ["user works_in Dubai"],
                "extraction_reported": True,
            }
        ]

    def test_fixed_validator_keeps_the_engine_ids(self):
        validator = ValidatorService()
        ref = validator._build_refs(self._scenario(), self._RunResult())[0]
        assert ref.fact_id == "mem_1"
        assert ref.memory_ids == ["mem_1"]
        assert ref.stored_texts == ["user works_in Dubai"]

    def test_legacy_validator_invents_an_id_the_engine_never_issued(self):
        validator = ValidatorService(
            legacy=LegacyCheckerConfig.from_names(["synthetic_fact_ids"])
        )
        ref = validator._build_refs(self._scenario(), self._RunResult())[0]
        assert ref.fact_id == "sim:generic_001:0"
        assert ref.memory_ids == []
        assert ref.stored_texts == []
        # Pre-fix ``/ingest`` reported nothing about extraction, so silence and
        # an empty extraction were indistinguishable.
        assert ref.extraction_reported is False
        assert validator._extracted_nothing(ref) is False


class TestFaults6And7ReportWriter:
    def test_literal_verdicts_assert_pass_without_evidence(self):
        payload = legacy_criterion_payload(
            LegacyCheckerConfig.from_names(["literal_verdicts"]),
            audit_tally={"passed": 0, "failed": 12, "skipped": 0},
            isolation_tally={"passed": 0, "failed": 30, "skipped": 0},
            events_played=120,
        )
        assert payload["criterion_h_isolation"]["verdict"] == "PASS"
        assert payload["criterion_h_isolation"]["isolation_pass_rate"] == 1.0
        assert payload["criterion_g_audit"]["verdict"] == "PASS"
        assert payload["criterion_g_audit"]["basis"] == "events_played > 0"

    def test_literal_verdicts_still_claim_pass_when_nothing_ran(self):
        payload = legacy_criterion_payload(
            LegacyCheckerConfig.from_names(["literal_verdicts"]),
            audit_tally={"passed": 0, "failed": 0, "skipped": 0},
            isolation_tally={"passed": 0, "failed": 0, "skipped": 0},
            events_played=0,
        )
        assert payload["criterion_h_isolation"]["verdict"] == "PASS"
        # events_played was 0, which is the one case the old criterion caught.
        assert payload["criterion_g_audit"]["verdict"] == "FAIL"

    def test_without_the_fault_the_payload_is_empty(self):
        assert (
            legacy_criterion_payload(
                LegacyCheckerConfig(),
                audit_tally={"passed": 76, "failed": 0, "skipped": 0},
                isolation_tally={"passed": 50, "failed": 0, "skipped": 0},
                events_played=120,
            )
            == {}
        )

    def test_run_number_reuse_pins_the_number_to_one(self):
        config = LegacyCheckerConfig.from_names(["reuse_run_number"])
        assert config.run_number_for(next_free=137) == 1

    def test_without_the_fault_the_next_free_number_is_used(self):
        assert LegacyCheckerConfig().run_number_for(next_free=137) == 137
