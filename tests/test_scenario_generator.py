"""Task 2 verification: ScenarioGenerator builds the spec § 5.1 catalogue."""

import json
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from simulation.models import FactType, Scenario, ScenarioCategory, ScenarioFact
from simulation.scenario_generator import (
    BASE_OUTCOMES,
    CATEGORY_COUNTS,
    EXPECTED_OUTCOME_VOCABULARY,
    MIN_SCENARIO_COUNT,
    SCENARIO_CATALOGUE,
    ScenarioGenerator,
    scenario_to_dict,
    scenarios_to_json,
)

# Spec § 5.1 category breakdown.
SPEC_COUNTS = {
    ScenarioCategory.CONTRADICTION: 10,
    ScenarioCategory.EXPIRY: 8,
    ScenarioCategory.CACHE: 6,
    ScenarioCategory.MULTI_USER: 10,
    ScenarioCategory.GENERIC: 10,
    ScenarioCategory.MODIFY: 6,
}

DURATION = 360.0


@pytest.fixture
def scenarios():
    return ScenarioGenerator(seed=42).generate(max_duration_seconds=DURATION)


# --- Catalogue shape ---------------------------------------------------------


def test_catalogue_has_50_scenarios():
    assert len(SCENARIO_CATALOGUE) == MIN_SCENARIO_COUNT == 50


def test_category_counts_match_spec():
    assert CATEGORY_COUNTS == SPEC_COUNTS


def test_generate_returns_50_scenarios(scenarios):
    assert len(scenarios) == 50
    assert all(isinstance(s, Scenario) for s in scenarios)


def test_all_six_categories_represented(scenarios):
    counts = Counter(s.category for s in scenarios)
    assert dict(counts) == SPEC_COUNTS


def test_scenario_ids_unique_and_prefixed(scenarios):
    ids = [s.scenario_id for s in scenarios]
    assert len(set(ids)) == len(ids)
    prefixes = {"contradiction", "expiry", "cache", "multi", "generic", "modify"}
    assert {i.rsplit("_", 1)[0] for i in ids} == prefixes


def test_every_scenario_has_facts_and_outcomes(scenarios):
    for scenario in scenarios:
        assert scenario.facts, scenario.scenario_id
        assert scenario.expected_outcomes, scenario.scenario_id
        assert all(isinstance(f, ScenarioFact) for f in scenario.facts)


def test_expected_outcomes_use_known_vocabulary(scenarios):
    for scenario in scenarios:
        assert set(BASE_OUTCOMES).issubset(scenario.expected_outcomes)
        for outcome in scenario.expected_outcomes:
            assert outcome in EXPECTED_OUTCOME_VOCABULARY, outcome
        assert len(set(scenario.expected_outcomes)) == len(scenario.expected_outcomes)


# --- Per-category semantics --------------------------------------------------


def _by_category(scenarios, category):
    return [s for s in scenarios if s.category is category]


def test_contradiction_scenarios_have_primary_then_contradiction(scenarios):
    for scenario in _by_category(scenarios, ScenarioCategory.CONTRADICTION):
        assert len(scenario.facts) == 2
        first, second = scenario.facts
        assert first.type is FactType.PRIMARY_FACT
        assert second.type is FactType.CONTRADICTION
        assert second.contradicts_scenario == scenario.scenario_id
        assert second.contradicts_fact_id is None  # filled in by ValidatorService
        assert second.timestamp > first.timestamp
        assert "old_fact_deactivated" in scenario.expected_outcomes
        assert "new_fact_active" in scenario.expected_outcomes


def test_expiry_scenarios_carry_ttl(scenarios):
    expiry = _by_category(scenarios, ScenarioCategory.EXPIRY)
    assert len(expiry) == 8
    ttls = set()
    for scenario in expiry:
        fact = scenario.facts[0]
        assert fact.type is FactType.EXPIRING_FACT
        assert fact.ttl_seconds and fact.ttl_seconds > 0
        assert "ttl_recorded" in scenario.expected_outcomes
        ttls.add(fact.ttl_seconds)
    assert 3600.0 in ttls  # 1 hour temporary task
    assert 86400.0 in ttls  # meeting tomorrow
    assert 7 * 86400.0 in ttls  # exam next Friday


def test_cache_scenarios_have_profile_reads(scenarios):
    cache = _by_category(scenarios, ScenarioCategory.CACHE)
    assert len(cache) == 6
    for scenario in cache:
        reads = [f for f in scenario.facts if f.type is FactType.PROFILE_READ]
        assert reads, scenario.scenario_id
        for read in reads:
            assert read.metadata["action"] == "get_profile"
            assert read.metadata["expected_cache"] in {"hit", "miss"}
    hit = next(s for s in cache if s.metadata["catalogue_id"] == "cache_001")
    assert "cache_hit_recorded" in hit.expected_outcomes
    miss = next(s for s in cache if s.metadata["catalogue_id"] == "cache_002")
    assert "cache_miss_recorded" in miss.expected_outcomes
    assert miss.metadata["requires_profile_cache_ttl_seconds"] > 0


def test_multi_user_scenarios_span_two_users(scenarios):
    multi = _by_category(scenarios, ScenarioCategory.MULTI_USER)
    assert len(multi) == 10
    for scenario in multi:
        peer = scenario.metadata["peer_user_id"]
        assert peer != scenario.user_id
        assert scenario.metadata["user_ids"] == [scenario.user_id, peer]
        second = scenario.facts[1]
        assert second.metadata["user_id"] == peer
        assert second.metadata["cross_user"] is True
        assert "no_cross_user_deactivation" in scenario.expected_outcomes


def test_generic_scenarios_accumulate_without_conflicts(scenarios):
    generic = _by_category(scenarios, ScenarioCategory.GENERIC)
    assert len(generic) == 10
    for scenario in generic:
        assert len(scenario.facts) >= 3
        assert all(f.type is FactType.PRIMARY_FACT for f in scenario.facts)
        assert all(f.contradicts_scenario is None for f in scenario.facts)
        assert "no_contradiction_triggered" in scenario.expected_outcomes


def test_modify_scenarios_chain_updates(scenarios):
    modify = _by_category(scenarios, ScenarioCategory.MODIFY)
    assert len(modify) == 6
    for scenario in modify:
        assert len(scenario.facts) >= 3
        assert scenario.facts[0].type is FactType.PRIMARY_FACT
        for fact in scenario.facts[1:]:
            assert fact.type is FactType.MODIFICATION
            assert fact.contradicts_scenario == scenario.scenario_id
        assert "latest_value_active" in scenario.expected_outcomes
    progression = next(s for s in modify if s.metadata["catalogue_id"] == "modify_001")
    assert [f.text for f in progression.facts] == [
        "I have 3 years of experience",
        "I now have 5 years of experience",
        "I now have 8 years of experience",
    ]


# --- Random distribution -----------------------------------------------------


def test_scenarios_spread_across_five_users(scenarios):
    users = {s.user_id for s in scenarios}
    assert users <= {f"user_{i}" for i in range(1, 6)}
    assert len(users) == 5


def test_num_users_controls_user_pool():
    scenarios = ScenarioGenerator(seed=7).generate(num_users=3, max_duration_seconds=DURATION)
    assert {s.user_id for s in scenarios} == {"user_1", "user_2", "user_3"}


def test_explicit_user_ids_override_num_users():
    generator = ScenarioGenerator(seed=1, user_ids=["alice", "bob"])
    scenarios = generator.generate(num_users=5, max_duration_seconds=DURATION)
    assert {s.user_id for s in scenarios} == {"alice", "bob"}


def test_timestamps_within_duration_and_ordered(scenarios):
    for scenario in scenarios:
        times = [f.timestamp for f in scenario.facts]
        assert times == sorted(times), scenario.scenario_id
        assert all(0.0 <= t <= DURATION for t in times), scenario.scenario_id
        assert len(set(times)) == len(times), scenario.scenario_id


def test_timestamps_spread_over_the_run(scenarios):
    times = [f.timestamp for s in scenarios for f in s.facts]
    assert min(times) < DURATION * 0.25
    assert max(times) > DURATION * 0.6


def test_scenarios_sorted_by_first_fact_time(scenarios):
    starts = [s.facts[0].timestamp for s in scenarios]
    assert starts == sorted(starts)


def test_short_duration_still_produces_ordered_timestamps():
    scenarios = ScenarioGenerator(seed=3).generate(max_duration_seconds=5.0)
    for scenario in scenarios:
        times = [f.timestamp for f in scenario.facts]
        assert times == sorted(times)
        assert all(0.0 <= t <= 5.0 for t in times)


def test_zero_duration_collapses_to_zero():
    scenarios = ScenarioGenerator(seed=3).generate(max_duration_seconds=0.0)
    assert all(f.timestamp == 0.0 for s in scenarios for f in s.facts)


# --- Determinism -------------------------------------------------------------


def test_same_seed_is_reproducible():
    first = ScenarioGenerator(seed=99).generate(max_duration_seconds=DURATION)
    second = ScenarioGenerator(seed=99).generate(max_duration_seconds=DURATION)
    assert scenarios_to_json(first) == scenarios_to_json(second)


def test_repeated_generate_calls_are_stable():
    generator = ScenarioGenerator(seed=5)
    assert scenarios_to_json(generator.generate()) == scenarios_to_json(generator.generate())


def test_different_seeds_differ():
    a = ScenarioGenerator(seed=1).generate(max_duration_seconds=DURATION)
    b = ScenarioGenerator(seed=2).generate(max_duration_seconds=DURATION)
    assert scenarios_to_json(a) != scenarios_to_json(b)


def test_fact_texts_are_seed_independent():
    a = ScenarioGenerator(seed=1).generate(max_duration_seconds=DURATION)
    b = ScenarioGenerator(seed=2).generate(max_duration_seconds=DURATION)
    def texts_by_catalogue_id(scenarios):
        return {s.metadata["catalogue_id"]: [f.text for f in s.facts] for s in scenarios}

    assert texts_by_catalogue_id(a) == texts_by_catalogue_id(b)


# --- Count handling ----------------------------------------------------------


def test_more_than_50_scenarios_repeats_catalogue():
    scenarios = ScenarioGenerator(seed=11).generate(num_scenarios=75, max_duration_seconds=DURATION)
    assert len(scenarios) == 75
    ids = [s.scenario_id for s in scenarios]
    assert len(set(ids)) == 75
    assert any(i.endswith("_r2") for i in ids)
    repeat = next(s for s in scenarios if s.scenario_id.endswith("_r2"))
    for fact in repeat.facts:
        if fact.contradicts_scenario is not None:
            assert fact.contradicts_scenario == repeat.scenario_id


def test_fewer_scenarios_keeps_all_categories():
    scenarios = ScenarioGenerator(seed=13).generate(num_scenarios=12, max_duration_seconds=DURATION)
    assert len(scenarios) == 12
    assert {s.category for s in scenarios} == set(SPEC_COUNTS)


def test_invalid_counts_rejected():
    with pytest.raises(ValueError):
        ScenarioGenerator().generate(num_scenarios=0)
    with pytest.raises(TypeError):
        # Old skeleton call style generate(<duration>) must not pass silently.
        ScenarioGenerator().generate(60.0)
    with pytest.raises(ValueError):
        ScenarioGenerator().generate(num_users=0)
    with pytest.raises(ValueError):
        ScenarioGenerator().generate(max_duration_seconds=-1)


def test_duration_seconds_alias():
    scenarios = ScenarioGenerator(seed=4).generate(duration_seconds=60.0)
    assert all(f.timestamp <= 60.0 for s in scenarios for f in s.facts)


# --- Serialisation -----------------------------------------------------------


def test_scenario_serialises_to_spec_json(scenarios):
    contradiction = next(s for s in scenarios if s.category is ScenarioCategory.CONTRADICTION)
    payload = scenario_to_dict(contradiction)
    assert set(payload) == {
        "scenario_id",
        "user_id",
        "category",
        "facts",
        "expected_outcomes",
        "metadata",
    }
    first, second = payload["facts"]
    assert set(first) >= {"timestamp", "text", "type"}
    assert first["type"] == "primary_fact"
    assert second["type"] == "contradiction"
    assert second["contradicts_scenario"] == payload["scenario_id"]
    assert second["contradicts_fact_id"] is None


def test_full_set_round_trips_through_json(scenarios):
    decoded = json.loads(scenarios_to_json(scenarios))
    assert len(decoded) == 50
    assert all(isinstance(entry["scenario_id"], str) for entry in decoded)
    # Enums must serialise to their string values, not repr.
    assert {entry["category"] for entry in decoded} == {c.value for c in ScenarioCategory}


def test_ttl_and_metadata_survive_serialisation(scenarios):
    expiry = next(s for s in scenarios if s.category is ScenarioCategory.EXPIRY)
    payload = json.loads(scenarios_to_json([expiry]))[0]
    assert payload["facts"][0]["ttl_seconds"] > 0
    assert payload["metadata"]["catalogue_id"].startswith("expiry_")
