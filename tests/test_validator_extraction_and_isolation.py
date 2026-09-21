"""Two validator behaviours that decide what the run numbers mean.

1. When the engine reports that its extractor produced no fact for an
   utterance, there is nothing in the store to retrieve or profile. Presence
   checks skip it instead of reporting a storage failure.
2. Criterion H is a claim about the store. A scenario script assigning a
   phrasing to one user does not stop another user legitimately holding their
   own fact that reads the same way, so ownership is settled by memory.db.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from simulation.models import IngestionResult  # noqa: E402
from simulation.validator_service import ValidatorService, _FactRef  # noqa: E402


def _ref(**overrides):
    base = dict(
        scenario_id="generic_001",
        fact_index=0,
        text="I run the on-call rotation for the ingestion service",
        user_id="user_1",
        fact_type="primary_fact",
        fact_id="sim:generic_001:0",
    )
    base.update(overrides)
    return _FactRef(**base)


class TestExtractionReported:
    def test_reported_with_no_ids_means_extracted_nothing(self):
        ref = _ref(extraction_reported=True, memory_ids=[])
        assert ref.extracted_nothing is True

    def test_reported_with_ids_is_a_normal_fact(self):
        ref = _ref(extraction_reported=True, memory_ids=["mem_1"])
        assert ref.extracted_nothing is False

    def test_silent_engine_is_not_treated_as_empty_extraction(self):
        # A deployment whose /ingest never reports ids looks identical to an
        # empty extraction unless the reported flag is tracked separately.
        ref = _ref(extraction_reported=False, memory_ids=[])
        assert ref.extracted_nothing is False

    def test_ingestion_result_flags_empty_extraction(self):
        reported_empty = IngestionResult(
            user_id="user_1", timestamp=0.0, extraction_reported=True
        )
        assert reported_empty.extracted_nothing is True

        silent = IngestionResult(user_id="user_1", timestamp=0.0)
        assert silent.extracted_nothing is False

    def test_match_texts_prefers_the_stored_triple(self):
        ref = _ref(stored_texts=["user works_on ingestion service"])
        assert ref.match_texts()[0] == "user works_on ingestion service"

    def test_match_texts_falls_back_to_the_utterance(self):
        ref = _ref(stored_texts=[])
        assert ref.match_texts() == [ref.text]

    def test_known_ids_includes_every_memory_id(self):
        ref = _ref(fact_id="mem_1", memory_ids=["mem_1", "mem_2"])
        assert set(ref.known_ids()) == {"mem_1", "mem_2"}


class TestStoreBackedOwnership:
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

    def test_user_holding_the_fact_owns_the_text(self, memory_db):
        validator = ValidatorService(memory_db_path=memory_db)
        assert (
            validator._user_owns_text("user_3", "user works_on recipe planning side project")
            is True
        )

    def test_user_without_the_fact_does_not_own_it(self, memory_db):
        validator = ValidatorService(memory_db_path=memory_db)
        assert (
            validator._user_owns_text("user_5", "user works_on recipe planning side project")
            is False
        )

    def test_unrelated_text_is_not_owned(self, memory_db):
        validator = ValidatorService(memory_db_path=memory_db)
        assert validator._user_owns_text("user_3", "user lives_in Dubai") is False

    def test_empty_text_is_never_owned(self, memory_db):
        validator = ValidatorService(memory_db_path=memory_db)
        assert validator._user_owns_text("user_3", "") is False
