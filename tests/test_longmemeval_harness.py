"""The LongMemEval harness's scoring, without a live engine.

The metrics are proxies, so they have to be exactly the proxies the report says
they are: token coverage at a 0.6 bar, the superseded value taken from the
earlier answer-bearing session, and a selection that keeps both buckets when a
limit is applied.
"""

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "lme_harness", ROOT / "benchmarks" / "longmemeval" / "run_benchmark.py"
)
lme = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lme)


def _instance(**overrides):
    base = {
        "question_id": "ku_1",
        "question_type": "knowledge-update",
        "question": "What was my personal best time in the charity 5K run?",
        "answer": "25 minutes and 50 seconds",
        "haystack_dates": ["2023/05/25", "2023/05/27"],
        "haystack_session_ids": ["s1", "s2"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "Remind me when my soccer game is"},
                {
                    "role": "user",
                    "content": "I set a personal best of 27 minutes 10 seconds",
                    "has_answer": True,
                },
                {"role": "assistant", "content": "Nice work"},
            ],
            [
                {
                    "role": "user",
                    "content": "My new personal best is 25 minutes and 50 seconds",
                    "has_answer": True,
                }
            ],
        ],
    }
    base.update(overrides)
    return base


class TestCoverage:
    def test_full_coverage_when_every_content_token_is_present(self):
        assert lme.coverage("Dubai office", "user works_in Dubai office") == 1.0

    def test_stopwords_do_not_count_towards_coverage(self):
        # "in the" carries no discriminating content, so its absence is not a miss.
        assert lme.coverage("in the Dubai office", "Dubai office") == 1.0

    def test_partial_coverage_is_a_fraction(self):
        assert lme.coverage("Dubai office tower", "Dubai office") == pytest.approx(2 / 3)

    def test_empty_target_scores_zero_rather_than_dividing_by_zero(self):
        assert lme.coverage("", "anything") == 0.0
        assert lme.coverage("the of and", "anything") == 0.0

    def test_best_coverage_reports_the_winning_text(self):
        score, text = lme.best_coverage(
            "25 minutes 50 seconds",
            ["user ran 27 minutes 10 seconds", "user best_time 25 minutes 50 seconds"],
        )
        assert score == 1.0
        assert text == "user best_time 25 minutes 50 seconds"

    def test_best_coverage_of_nothing_is_zero(self):
        assert lme.best_coverage("Dubai", []) == (0.0, None)


class TestAnswerVariants:
    def test_a_parenthetical_alternative_becomes_its_own_variant(self):
        variants = lme.answer_variants("25 minutes and 50 seconds (or 25:50)")
        assert "25:50" in variants
        assert "25 minutes and 50 seconds" in variants

    def test_a_stored_short_form_scores_full_coverage_through_a_variant(self):
        score, text, variant = lme.best_variant_coverage(
            "25 minutes and 50 seconds (or 25:50)",
            ["user has_personal_best_time 25:50"],
        )
        assert score == 1.0
        assert variant == "25:50"
        assert text == "user has_personal_best_time 25:50"

    def test_an_answer_without_parentheses_has_one_variant(self):
        assert lme.answer_variants("Berlin") == ["Berlin"]

    def test_no_answer_has_no_variants(self):
        assert lme.answer_variants(None) == []
        assert lme.best_variant_coverage(None, ["anything"]) == (0.0, None, None)


class TestBeliefState:
    def test_deactivated_old_value_is_supersession(self):
        assert (
            lme.belief_state(
                answer_in_store=True,
                stale_present=True,
                stale_active=False,
                stale_deactivated=True,
            )
            == "superseded"
        )

    def test_both_values_active_is_accumulation(self):
        assert (
            lme.belief_state(
                answer_in_store=True,
                stale_present=True,
                stale_active=True,
                stale_deactivated=False,
            )
            == "both_active"
        )

    def test_old_value_absent_entirely_is_an_extraction_gap(self):
        # Nothing was superseded because the old fact never reached the store,
        # so this instance does not evidence belief revision either way.
        assert (
            lme.belief_state(
                answer_in_store=True,
                stale_present=True,
                stale_active=False,
                stale_deactivated=False,
            )
            == "old_value_never_stored"
        )

    def test_missing_current_value_is_reported_as_such(self):
        assert (
            lme.belief_state(
                answer_in_store=False,
                stale_present=True,
                stale_active=False,
                stale_deactivated=True,
            )
            == "current_value_missing"
        )

    def test_an_instance_without_an_annotated_old_value_is_not_classified(self):
        assert (
            lme.belief_state(
                answer_in_store=True,
                stale_present=False,
                stale_active=False,
                stale_deactivated=False,
            )
            == "no_superseded_statement_annotated"
        )


class TestSupersededStatement:
    def test_the_earlier_answer_bearing_turn_is_the_superseded_one(self):
        stale = lme.superseded_statement(_instance())
        assert stale == "I set a personal best of 27 minutes 10 seconds"

    def test_a_single_evidence_turn_has_nothing_superseded(self):
        instance = _instance(
            haystack_sessions=[
                [{"role": "user", "content": "One statement", "has_answer": True}]
            ]
        )
        assert lme.superseded_statement(instance) is None

    def test_two_bearing_turns_in_the_same_session_supersede_nothing(self):
        # Both are current as far as session order can tell.
        instance = _instance(
            haystack_sessions=[
                [
                    {"role": "user", "content": "First", "has_answer": True},
                    {"role": "user", "content": "Second", "has_answer": True},
                ]
            ]
        )
        assert lme.superseded_statement(instance) is None


class TestUserTurns:
    def test_only_user_turns_are_ingested_and_order_is_kept(self):
        turns = lme.user_turns(_instance())
        assert [text for _, text in turns] == [
            "Remind me when my soccer game is",
            "I set a personal best of 27 minutes 10 seconds",
            "My new personal best is 25 minutes and 50 seconds",
        ]
        assert [session for session, _ in turns] == [0, 0, 1]


class TestSelection:
    @staticmethod
    def _pool():
        answerable = [
            _instance(question_id=f"ku_{i}", question_type="knowledge-update")
            for i in range(4)
        ]
        other = [
            _instance(question_id=f"tr_{i}", question_type="temporal-reasoning")
            for i in range(3)
        ]
        abstention = [
            _instance(question_id=f"tr_{i}_abs", question_type="temporal-reasoning")
            for i in range(3)
        ]
        return answerable + other + abstention

    def test_question_type_filter_does_not_drop_abstention_instances(self):
        chosen = lme.select(
            self._pool(),
            question_types=["knowledge-update"],
            include_abstention=True,
            limit=None,
        )
        ids = [i["question_id"] for i in chosen]
        assert [i for i in ids if i.startswith("ku_")] == ["ku_0", "ku_1", "ku_2", "ku_3"]
        assert [i for i in ids if i.endswith("_abs")] == [
            "tr_0_abs",
            "tr_1_abs",
            "tr_2_abs",
        ]
        assert "tr_0" not in ids

    def test_no_abstention_flag_removes_that_bucket(self):
        chosen = lme.select(
            self._pool(),
            question_types=["knowledge-update"],
            include_abstention=False,
            limit=None,
        )
        assert all(not i["question_id"].endswith("_abs") for i in chosen)

    def test_limit_applies_per_bucket(self):
        chosen = lme.select(
            self._pool(),
            question_types=["knowledge-update"],
            include_abstention=True,
            limit=2,
        )
        ids = [i["question_id"] for i in chosen]
        assert ids == ["ku_0", "ku_1", "tr_0_abs", "tr_1_abs"]


class TestStoreState:
    @pytest.fixture
    def memory_db(self, tmp_path):
        path = tmp_path / "bench_memory.db"
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
                scope TEXT,
                is_active INTEGER,
                updated_at REAL,
                expires_at REAL
            )
            """
        )
        conn.executemany(
            "INSERT INTO memory_keys VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "lme_ku_1:user:best_time",
                    "mem_2",
                    "lme_ku_1",
                    "user",
                    "best_time",
                    "25 minutes 50 seconds",
                    "user",
                    1,
                    0.0,
                    None,
                ),
                (
                    "lme_ku_1:user:plays",
                    "mem_3",
                    "lme_ku_1",
                    "user",
                    "plays",
                    "soccer",
                    "user",
                    0,
                    0.0,
                    None,
                ),
                (
                    "other:user:lives_in",
                    "mem_4",
                    "other_user",
                    "user",
                    "lives_in",
                    "Berlin",
                    "user",
                    1,
                    0.0,
                    None,
                ),
            ],
        )
        conn.commit()
        conn.close()
        return str(path)

    def test_counts_are_per_user_and_split_by_active_flag(self, memory_db):
        state = lme.store_state(memory_db, "lme_ku_1")
        assert state["available"] is True
        assert state["active"] == 1
        assert state["inactive"] == 1
        assert state["active_texts"] == ["user best_time 25 minutes 50 seconds"]
        assert state["inactive_texts"] == ["user plays soccer"]

    def test_another_user_is_not_counted(self, memory_db):
        assert lme.store_state(memory_db, "other_user")["active"] == 1

    def test_a_missing_database_is_reported_unavailable(self, tmp_path):
        state = lme.store_state(str(tmp_path / "nope.db"), "lme_ku_1")
        assert state["available"] is False
        assert "reason" in state


class TestSummary:
    def test_rates_are_computed_over_the_right_buckets(self):
        rows = [
            {
                "question_type": "knowledge-update",
                "abstention": False,
                "answer_supported": True,
                "answer_in_store": True,
                "stale_returned": False,
                "belief_state": "superseded",
                "returned_count": 5,
                "store": {"available": True, "active": 4, "inactive": 1},
            },
            {
                "question_type": "knowledge-update",
                "abstention": False,
                "answer_supported": False,
                "answer_in_store": True,
                "stale_returned": True,
                "belief_state": "both_active",
                "returned_count": 5,
                "store": {"available": True, "active": 6, "inactive": 0},
            },
            {
                "question_type": "temporal-reasoning",
                "abstention": True,
                "answer_supported": False,
                "stale_returned": False,
                "returned_count": 5,
                "store": {"available": True, "active": 3, "inactive": 0},
            },
        ]
        summary = lme.summarise(rows)
        ku = summary["knowledge_update"]
        assert ku["instances"] == 2
        assert ku["answer_supported_rate"] == 0.5
        assert ku["stale_returned_rate"] == 0.5
        assert ku["any_deactivated_row_rate"] == 0.5
        assert ku["belief_state_counts"] == {"superseded": 1, "both_active": 1}
        assert ku["mean_active_facts"] == 5.0
        assert summary["abstention"]["instances"] == 1
        assert summary["abstention"]["returned_something_rate"] == 1.0

    def test_empty_buckets_report_none_rather_than_zero(self):
        summary = lme.summarise([])
        assert summary["knowledge_update"]["answer_supported_rate"] is None
        assert summary["abstention"]["returned_something_rate"] is None
