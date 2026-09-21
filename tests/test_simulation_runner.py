"""Task 6 verification: SimulationRunner scenario playback.

Covers spec § 3.2 (event queue, wall-clock timing, progress), § 3.3 (ingest vs
get_profile), § 3.4 (monitoring events), § 3.5 (run metadata) and the § 5.1
special cases: ``profile_read`` facts are never ingested, ``metadata["user_id"]``
overrides ``scenario.user_id``, ``contradicts_fact_id`` is backfilled with
engine fact ids, and ``cache_002``'s TTL window is derived from the realised
timeline.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from simulation.audit_logger import AuditLogger
from simulation.engine_client import EngineClientError
from simulation.models import (
    ErrorClass,
    FactType,
    IngestionResult,
    MonitoringEventType,
    ProfileResult,
    RunStatus,
    Scenario,
    ScenarioCategory,
    ScenarioFact,
    ValidationResult,
)
from simulation.monitoring_service import MonitoringService
from simulation.scenario_generator import ScenarioGenerator
from simulation.simulation_runner import (
    RUN_DURATIONS_SECONDS,
    SimulationRunner,
    build_event_queue,
    classify_engine_error,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Stands in for EngineClient; records every call it receives."""

    def __init__(self, ingest_error=None, profile_error=None, profile_latency_us=1_000):
        self.ingests = []
        self.profiles = []
        self.on_request = None
        self._counter = 0
        self._ingest_error = ingest_error
        self._profile_error = profile_error
        self._profile_latency_us = profile_latency_us
        #: Optional callables consulted per call: fn(user_id, text) -> error|None
        self.ingest_error_for = None
        self.profile_error_for = None
        #: Optional callable: fn(user_id, call_index) -> latency_us
        self.profile_latency_for = None
        self.cache_status = None

    async def ingest(self, user_id, text, scope="user"):
        self.ingests.append((user_id, text, scope))
        error = self._ingest_error
        if self.ingest_error_for is not None:
            error = self.ingest_error_for(user_id, text) or error
        if error is not None:
            raise error
        self._counter += 1
        return IngestionResult(
            user_id=user_id,
            timestamp=0.0,
            fact_id=f"fact_{self._counter}",
            status="accepted",
            latency_us=1234,
        )

    async def get_profile(self, user_id, recent_days=7):
        index = len(self.profiles)
        self.profiles.append((user_id, recent_days))
        error = self._profile_error
        if self.profile_error_for is not None:
            error = self.profile_error_for(user_id, index) or error
        if error is not None:
            raise error
        latency = self._profile_latency_us
        if self.profile_latency_for is not None:
            latency = self.profile_latency_for(user_id, index)
        return ProfileResult(
            user_id=user_id,
            stable_facts=["a"],
            recent_activity=["b"],
            cache_status=self.cache_status,
            latency_us=latency,
        )


class FakeClock:
    """Monotonic clock whose only advance comes from the injected sleep."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def make_runner(client=None, monitor=None, **kwargs):
    clock = FakeClock()
    runner = SimulationRunner(
        engine_client=client or FakeClient(),
        monitoring_service=monitor,
        clock=clock,
        sleep=clock.sleep,
        wait_out_duration=kwargs.pop("wait_out_duration", False),
        **kwargs,
    )
    return runner, clock


def fact(timestamp, text, type=FactType.PRIMARY_FACT, **kwargs):
    return ScenarioFact(timestamp=timestamp, text=text, type=type, **kwargs)


def scenario(scenario_id, user_id, facts, category=ScenarioCategory.GENERIC, metadata=None):
    return Scenario(
        scenario_id=scenario_id,
        user_id=user_id,
        category=category,
        facts=list(facts),
        expected_outcomes=[],
        metadata=dict(metadata or {}),
    )


# ---------------------------------------------------------------------------
# Event queue flattening (spec § 3.2)
# ---------------------------------------------------------------------------


def test_event_queue_is_globally_timestamp_ordered_not_scenario_ordered():
    a = scenario("a", "user_1", [fact(0.0, "a0"), fact(20.0, "a1")])
    b = scenario("b", "user_2", [fact(5.0, "b0"), fact(10.0, "b1")])

    queue = build_event_queue([a, b])

    assert [(e.scenario.scenario_id, e.fact_index) for e in queue] == [
        ("a", 0),
        ("b", 0),
        ("b", 1),
        ("a", 1),
    ]
    assert [e.sequence for e in queue] == [0, 1, 2, 3]


def test_event_queue_keeps_scenario_fact_order_on_timestamp_ties():
    a = scenario("a", "user_1", [fact(5.0, "a0"), fact(5.0, "a1"), fact(5.0, "a2")])
    b = scenario("b", "user_2", [fact(5.0, "b0")])

    queue = build_event_queue([b, a])

    assert [(e.scenario.scenario_id, e.fact_index) for e in queue] == [
        ("a", 0),
        ("a", 1),
        ("a", 2),
        ("b", 0),
    ]


def test_event_queue_handles_empty_input_and_factless_scenarios():
    assert build_event_queue([]) == []
    assert build_event_queue([scenario("empty", "user_1", [])]) == []


def test_generated_catalogue_flattens_into_one_ordered_queue():
    scenarios = ScenarioGenerator(seed=7).generate(max_duration_seconds=60.0)
    queue = build_event_queue(scenarios)

    assert len(queue) == sum(len(s.facts) for s in scenarios)
    timestamps = [event.timestamp for event in queue]
    assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# Playback (spec § 3.2 step 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plays_every_fact_and_reports_counts():
    client = FakeClient()
    monitor = MonitoringService(run_number=3)
    runner, _ = make_runner(client, monitor)
    scenarios = [
        scenario("s1", "user_1", [fact(1.0, "one"), fact(2.0, "two")]),
        scenario("s2", "user_2", [fact(3.0, "three")]),
    ]

    result = await runner.run(scenarios, duration_seconds=10.0, run_number=3)

    assert result.status == RunStatus.COMPLETED.value
    assert result.run_number == 3
    assert result.duration_seconds == 10.0
    assert result.scenarios_count == 2
    assert result.facts_count == 3
    assert result.events_total == 3
    assert result.events_played == 3
    assert result.events_skipped == 0
    assert result.ingested_count == 3
    assert result.user_count == 2
    assert result.errors == []
    assert result.succeeded is True
    assert [text for _, text, _ in client.ingests] == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_run_records_engine_fact_ids_per_scenario():
    client = FakeClient()
    runner, _ = make_runner(client)
    scenarios = [scenario("s1", "user_1", [fact(0.0, "a"), fact(1.0, "b")])]

    result = await runner.run(scenarios, duration_seconds=5.0)

    assert result.fact_ids == {"s1": ["fact_1", "fact_2"]}


@pytest.mark.asyncio
async def test_run_falls_back_to_synthetic_fact_id_when_engine_returns_none():
    class NoIdClient(FakeClient):
        async def ingest(self, user_id, text, scope="user"):
            await super().ingest(user_id, text, scope)
            return IngestionResult(user_id=user_id, timestamp=0.0, fact_id=None, status="accepted")

    monitor = MonitoringService()
    runner, _ = make_runner(NoIdClient(), monitor)
    result = await runner.run(
        [scenario("s1", "user_1", [fact(0.0, "a")])], duration_seconds=5.0
    )

    assert result.fact_ids == {"s1": ["sim:s1:0"]}
    event = monitor.get_events()[0]
    assert event.metadata["fact_id_source"] == "synthetic"


@pytest.mark.asyncio
async def test_run_requires_an_engine_client():
    runner = SimulationRunner()
    with pytest.raises(ValueError, match="EngineClient"):
        await runner.run([], duration_seconds=1.0)


@pytest.mark.asyncio
async def test_run_rejects_negative_duration():
    runner, _ = make_runner()
    with pytest.raises(ValueError, match="duration_seconds"):
        await runner.run([], duration_seconds=-1.0)


@pytest.mark.asyncio
async def test_run_without_scenarios_uses_the_generator():
    client = FakeClient()
    runner, _ = make_runner(client, generator=ScenarioGenerator(seed=1))

    result = await runner.run(duration_seconds=60.0, run_number=1)

    assert result.scenarios_count >= 50
    assert result.events_played == result.events_total


@pytest.mark.asyncio
async def test_run_without_scenarios_or_generator_raises():
    runner, _ = make_runner()
    with pytest.raises(ValueError, match="ScenarioGenerator"):
        await runner.run(duration_seconds=10.0)


# ---------------------------------------------------------------------------
# Timing accuracy (spec § 3.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_fire_at_their_scheduled_wall_clock_offsets():
    client = FakeClient()
    runner, clock = make_runner(client)
    fired = []

    original = client.ingest

    async def recording_ingest(user_id, text, scope="user"):
        fired.append((clock.now, text))
        return await original(user_id, text, scope)

    client.ingest = recording_ingest
    scenarios = [scenario("s1", "user_1", [fact(0.0, "t0"), fact(2.5, "t2"), fact(9.0, "t9")])]

    await runner.run(scenarios, duration_seconds=10.0)

    assert fired == [(0.0, "t0"), (2.5, "t2"), (9.0, "t9")]


@pytest.mark.asyncio
async def test_runner_never_sleeps_backwards_for_an_already_due_event():
    runner, clock = make_runner()
    scenarios = [scenario("s1", "user_1", [fact(0.0, "a"), fact(0.0, "b")])]

    await runner.run(scenarios, duration_seconds=5.0)

    assert all(delay > 0 for delay in clock.sleeps)


@pytest.mark.asyncio
async def test_wait_out_duration_holds_the_run_open_to_the_end():
    client = FakeClient()
    clock = FakeClock()
    runner = SimulationRunner(
        engine_client=client, clock=clock, sleep=clock.sleep, wait_out_duration=True
    )

    result = await runner.run(
        [scenario("s1", "user_1", [fact(1.0, "a")])], duration_seconds=60.0
    )

    assert clock.now == pytest.approx(60.0)
    assert result.elapsed_seconds == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_facts_scheduled_past_the_run_are_skipped_not_played():
    client = FakeClient()
    runner, _ = make_runner(client)
    scenarios = [scenario("s1", "user_1", [fact(5.0, "inside"), fact(90.0, "outside")])]

    result = await runner.run(scenarios, duration_seconds=60.0)

    assert result.events_played == 1
    assert result.events_skipped == 1
    assert [text for _, text, _ in client.ingests] == ["inside"]
    assert any("skipped" in note for note in result.notes)


@pytest.mark.parametrize("duration", RUN_DURATIONS_SECONDS)
@pytest.mark.asyncio
async def test_progressive_run_durations_play_the_whole_catalogue(duration):
    client = FakeClient()
    runner, clock = make_runner(client, generator=ScenarioGenerator(seed=4))

    result = await runner.run(duration_seconds=duration, run_number=1)

    assert result.events_skipped == 0
    assert result.events_played == result.events_total
    assert clock.now <= duration


# ---------------------------------------------------------------------------
# Progress tracking (spec § 3.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_tracks_events_time_and_remaining_time():
    runner, _ = make_runner()
    seen = []
    runner.on_progress = lambda snapshot: seen.append(snapshot)
    scenarios = [scenario("s1", "user_1", [fact(0.0, "a"), fact(5.0, "b"), fact(10.0, "c")])]

    await runner.run(scenarios, duration_seconds=20.0)

    assert [s["events_played"] for s in seen] == [1, 2, 3]
    assert [s["current_time"] for s in seen] == [0.0, 5.0, 10.0]
    assert [s["remaining_seconds"] for s in seen] == [20.0, 15.0, 10.0]
    assert seen[-1]["percent_complete"] == pytest.approx(100.0)
    assert seen[-1]["last_scenario_id"] == "s1"
    assert runner.progress["events_total"] == 3


@pytest.mark.asyncio
async def test_async_progress_callback_is_awaited_and_failures_are_swallowed():
    runner, _ = make_runner()
    seen = []

    async def bad_callback(snapshot):
        seen.append(snapshot["events_played"])
        raise RuntimeError("dashboard is down")

    runner.on_progress = bad_callback
    result = await runner.run(
        [scenario("s1", "user_1", [fact(0.0, "a"), fact(1.0, "b")])], duration_seconds=5.0
    )

    assert seen == [1, 2]
    assert result.status == RunStatus.COMPLETED.value


# ---------------------------------------------------------------------------
# profile_read handling (spec § 5.1 C)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_read_calls_get_profile_and_is_never_ingested():
    client = FakeClient()
    runner, _ = make_runner(client)
    scenarios = [
        scenario(
            "cache_001",
            "user_1",
            [
                fact(0.0, "I lead the guild"),
                fact(1.0, "Generate profile", FactType.PROFILE_READ,
                     metadata={"expected_cache": "miss", "action": "get_profile"}),
            ],
            category=ScenarioCategory.CACHE,
        )
    ]

    result = await runner.run(scenarios, duration_seconds=10.0)

    assert [text for _, text, _ in client.ingests] == ["I lead the guild"]
    assert client.profiles == [("user_1", 7)]
    assert result.ingested_count == 1
    assert result.profile_read_count == 1
    assert result.fact_ids == {"cache_001": ["fact_1"]}


@pytest.mark.asyncio
async def test_profile_read_compares_cache_status_reported_by_the_engine():
    client = FakeClient()
    client.cache_status = "hit"
    monitor = MonitoringService()
    runner, _ = make_runner(client, monitor)
    scenarios = [
        scenario(
            "cache_001",
            "user_1",
            [fact(0.0, "read", FactType.PROFILE_READ, metadata={"expected_cache": "hit"})],
            category=ScenarioCategory.CACHE,
        )
    ]

    result = await runner.run(scenarios, duration_seconds=5.0)

    check = result.cache_checks[0]
    assert check["expected"] == "hit"
    assert check["observed"] == "hit"
    assert check["matched"] is True
    assert check["source"] == "engine"
    assert monitor.get_events()[0].event_type == MonitoringEventType.CACHE_HIT.value


@pytest.mark.asyncio
async def test_profile_cache_status_falls_back_to_latency_thresholds():
    client = FakeClient()
    client.profile_latency_for = lambda user_id, index: [5_000, 500_000, 50_000][index]
    monitor = MonitoringService()
    runner, _ = make_runner(client, monitor)
    reads = [
        fact(float(i), "read", FactType.PROFILE_READ, metadata={"expected_cache": "hit"})
        for i in range(3)
    ]
    result = await runner.run(
        [scenario("cache_x", "user_1", reads, category=ScenarioCategory.CACHE)],
        duration_seconds=10.0,
    )

    observed = [c["observed"] for c in result.cache_checks]
    sources = [c["source"] for c in result.cache_checks]
    assert observed == ["hit", "miss", None]
    assert sources == ["latency", "latency", "indeterminate"]
    assert [c["matched"] for c in result.cache_checks] == [True, False, None]
    assert [e.event_type for e in monitor.get_events()] == [
        MonitoringEventType.CACHE_HIT.value,
        MonitoringEventType.CACHE_MISS.value,
        MonitoringEventType.RETRIEVAL.value,
    ]


@pytest.mark.asyncio
async def test_profile_read_uses_the_user_id_override():
    client = FakeClient()
    runner, _ = make_runner(client)
    scenarios = [
        scenario(
            "cache_y",
            "user_1",
            [fact(0.0, "read", FactType.PROFILE_READ, metadata={"user_id": "user_9"})],
            category=ScenarioCategory.CACHE,
        )
    ]

    await runner.run(scenarios, duration_seconds=5.0)

    assert client.profiles == [("user_9", 7)]


# ---------------------------------------------------------------------------
# user_id override (spec § 5.1 D)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenarios_round_tripped_through_json_still_play_correctly():
    """A JSON round trip turns the enums into plain strings (spec § 3.1)."""
    client = FakeClient()
    monitor = MonitoringService()
    runner, _ = make_runner(client, monitor)
    s = scenario(
        "cache_001",
        "user_1",
        [
            fact(0.0, "a stable fact"),
            fact(1.0, "read", FactType.PROFILE_READ, metadata={"expected_cache": "miss"}),
        ],
        category=ScenarioCategory.CACHE,
    )
    # Exactly what json.loads gives back for scenario_to_dict output.
    s.category = "cache"
    for f in s.facts:
        f.type = f.type.value

    result = await runner.run([s], duration_seconds=10.0)

    assert [text for _, text, _ in client.ingests] == ["a stable fact"]
    assert client.profiles == [("user_1", 7)]
    assert result.profile_read_count == 1
    assert monitor.get_events()[0].metadata["fact_type"] == "primary_fact"
    assert monitor.get_events()[0].metadata["scenario_category"] == "cache"


def test_resolve_user_id_prefers_fact_metadata():
    s = scenario("multi_001", "user_1", [])
    assert SimulationRunner.resolve_user_id(s, fact(0.0, "x")) == "user_1"
    assert (
        SimulationRunner.resolve_user_id(s, fact(0.0, "x", metadata={"user_id": "user_4"}))
        == "user_4"
    )
    assert (
        SimulationRunner.resolve_user_id(s, fact(0.0, "x", metadata={"user_id": "  "}))
        == "user_1"
    )


@pytest.mark.asyncio
async def test_multi_user_scenario_ingests_into_both_users():
    client = FakeClient()
    monitor = MonitoringService()
    runner, _ = make_runner(client, monitor)
    scenarios = [
        scenario(
            "multi_001",
            "user_1",
            [
                fact(0.0, "I live in Boston"),
                fact(
                    5.0,
                    "I live in Denver",
                    FactType.CONTRADICTION,
                    contradicts_scenario="multi_001",
                    metadata={"user_id": "user_2"},
                ),
            ],
            category=ScenarioCategory.MULTI_USER,
            metadata={"peer_user_id": "user_2", "user_ids": ["user_1", "user_2"]},
        )
    ]

    result = await runner.run(scenarios, duration_seconds=10.0)

    assert [user for user, _, _ in client.ingests] == ["user_1", "user_2"]
    assert result.user_count == 2
    # No cross-user deactivation: both stay plain ingests.
    assert result.contradiction_count == 0
    types = [e.event_type for e in monitor.get_events()]
    assert types == [MonitoringEventType.FACT_INGESTED.value] * 2
    peer_event = monitor.get_events()[1]
    assert peer_event.metadata["cross_user_contradiction"] is True
    assert peer_event.metadata["no_deactivation_expected"] is True


@pytest.mark.asyncio
async def test_multi_user_facts_stay_active_for_both_users():
    monitor = MonitoringService()
    runner, _ = make_runner(FakeClient(), monitor)
    scenarios = [
        scenario(
            "multi_002",
            "user_1",
            [
                fact(0.0, "primary"),
                fact(
                    2.0,
                    "peer",
                    FactType.CONTRADICTION,
                    contradicts_scenario="multi_002",
                    metadata={"user_id": "user_2"},
                ),
            ],
            category=ScenarioCategory.MULTI_USER,
        )
    ]

    await runner.run(scenarios, duration_seconds=10.0)

    metrics = monitor.get_metrics()
    assert metrics["active_facts_by_user"] == {"user_1": 1, "user_2": 1}
    assert metrics["total_inactive_facts"] == 0


# ---------------------------------------------------------------------------
# contradicts_fact_id backfill (spec § 5.1 A / F)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contradiction_backfills_the_engine_fact_id_of_the_earlier_fact():
    monitor = MonitoringService()
    runner, _ = make_runner(FakeClient(), monitor)
    primary = fact(0.0, "I live in New York")
    conflict = fact(
        5.0,
        "Actually I moved to San Francisco",
        FactType.CONTRADICTION,
        contradicts_scenario="contradiction_001",
    )
    scenarios = [
        scenario(
            "contradiction_001",
            "user_1",
            [primary, conflict],
            category=ScenarioCategory.CONTRADICTION,
        )
    ]

    result = await runner.run(scenarios, duration_seconds=10.0)

    assert conflict.contradicts_fact_id == "fact_1"
    assert result.contradiction_count == 1
    events = monitor.get_events()
    assert events[1].event_type == MonitoringEventType.CONTRADICTION_RESOLVED.value
    assert events[1].metadata["old_fact_id"] == "fact_1"
    assert events[1].metadata["new_fact_id"] == "fact_2"
    assert events[1].old_state == {"fact_id": "fact_1", "active": True}


@pytest.mark.asyncio
async def test_modification_chain_backfills_against_the_most_recent_prior_fact():
    monitor = MonitoringService()
    runner, _ = make_runner(FakeClient(), monitor)
    facts = [
        fact(0.0, "v1"),
        fact(1.0, "v2", FactType.MODIFICATION, contradicts_scenario="modify_001"),
        fact(2.0, "v3", FactType.MODIFICATION, contradicts_scenario="modify_001"),
    ]
    scenarios = [
        scenario("modify_001", "user_1", facts, category=ScenarioCategory.MODIFY)
    ]

    result = await runner.run(scenarios, duration_seconds=10.0)

    assert facts[1].contradicts_fact_id == "fact_1"
    assert facts[2].contradicts_fact_id == "fact_2"
    assert result.contradiction_count == 2
    metrics = monitor.get_metrics()
    assert metrics["inactive_facts_by_user"] == {"user_1": 2}
    assert metrics["active_facts_by_user"] == {"user_1": 1}


@pytest.mark.asyncio
async def test_backfill_skips_when_the_earlier_fact_failed_to_ingest():
    client = FakeClient()
    client.ingest_error_for = lambda user_id, text: (
        EngineClientError("boom", endpoint="/ingest", status_code=500)
        if text == "v1"
        else None
    )
    monitor = MonitoringService()
    runner, _ = make_runner(client, monitor)
    conflict = fact(1.0, "v2", FactType.CONTRADICTION, contradicts_scenario="c1")
    scenarios = [scenario("c1", "user_1", [fact(0.0, "v1"), conflict])]

    result = await runner.run(scenarios, duration_seconds=10.0)

    assert conflict.contradicts_fact_id is None
    assert result.contradiction_count == 0
    assert monitor.get_events()[0].event_type == MonitoringEventType.FACT_INGESTED.value


@pytest.mark.asyncio
async def test_backfill_ignores_facts_scheduled_after_the_contradiction():
    conflict = fact(1.0, "early conflict", FactType.CONTRADICTION, contradicts_scenario="c2")
    later = fact(2.0, "later primary")
    # The conflict is fact_index 0, so nothing earlier exists in its scenario.
    scenarios = [scenario("c2", "user_1", [conflict, later])]
    runner, _ = make_runner()

    result = await runner.run(scenarios, duration_seconds=10.0)

    assert conflict.contradicts_fact_id is None
    assert result.contradiction_count == 0


# ---------------------------------------------------------------------------
# cache_002 TTL planning (spec § 5.1 C)
# ---------------------------------------------------------------------------


def _cache_pair(hit_gap, miss_gap):
    hit = scenario(
        "cache_001",
        "user_1",
        [
            fact(0.0, "generate", FactType.PROFILE_READ, metadata={"expected_cache": "miss"}),
            fact(hit_gap, "hit", FactType.PROFILE_READ, metadata={"expected_cache": "hit"}),
        ],
        category=ScenarioCategory.CACHE,
    )
    miss = scenario(
        "cache_002",
        "user_2",
        [
            fact(0.0, "generate", FactType.PROFILE_READ, metadata={"expected_cache": "miss"}),
            fact(miss_gap, "miss", FactType.PROFILE_READ, metadata={"expected_cache": "miss"}),
        ],
        category=ScenarioCategory.CACHE,
        metadata={"requires_profile_cache_ttl_seconds": 30.0},
    )
    return hit, miss


def test_profile_cache_ttl_is_derived_from_the_cache_002_gap():
    runner, _ = make_runner()
    hit, miss = _cache_pair(hit_gap=1.0, miss_gap=20.0)

    ttl, notes = runner.plan_profile_cache_ttl([hit, miss])

    assert ttl == pytest.approx(10.0)
    assert miss.metadata["effective_profile_cache_ttl_seconds"] == ttl
    assert any("3600s" in note for note in notes)
    # A hit gap already inside the TTL is left exactly where it was.
    assert hit.facts[1].timestamp == 1.0
    assert "timestamp_adjusted_from" not in hit.facts[1].metadata


def test_profile_cache_ttl_is_none_when_no_scenario_requires_one():
    runner, _ = make_runner()
    plain = scenario(
        "cache_003",
        "user_1",
        [fact(0.0, "read", FactType.PROFILE_READ, metadata={"expected_cache": "miss"})],
        category=ScenarioCategory.CACHE,
    )

    ttl, notes = runner.plan_profile_cache_ttl([plain])

    assert ttl is None
    assert notes == []


def test_late_cache_hit_read_is_pulled_inside_the_ttl():
    runner, _ = make_runner()
    # The generator only guarantees a *minimum* gap, so cache_001's "immediate"
    # read routinely lands later than cache_002's post-TTL read.
    hit, miss = _cache_pair(hit_gap=25.0, miss_gap=20.0)

    ttl, notes = runner.plan_profile_cache_ttl([hit, miss])

    assert ttl == pytest.approx(10.0)
    assert hit.facts[1].timestamp == pytest.approx(5.0)
    assert hit.facts[1].metadata["timestamp_adjusted_from"] == 25.0
    assert hit.facts[1].metadata["timestamp_adjusted_reason"] == "profile_cache_ttl"
    assert "expected_cache_unsatisfiable" not in hit.facts[1].metadata
    assert any("moved from" in note for note in notes)


def test_retiming_never_reorders_a_scenario():
    runner, _ = make_runner()
    hit, miss = _cache_pair(hit_gap=40.0, miss_gap=20.0)
    hit.facts.insert(0, fact(0.0, "a stable fact"))
    hit.facts[1].timestamp = 1.0
    hit.facts[2].timestamp = 41.0

    runner.plan_profile_cache_ttl([hit, miss])

    timestamps = [f.timestamp for f in hit.facts]
    assert timestamps == sorted(timestamps)
    assert timestamps[2] == pytest.approx(6.0)


def test_hit_read_separated_by_a_non_read_fact_is_marked_unsatisfiable():
    runner, _ = make_runner()
    _, miss = _cache_pair(hit_gap=1.0, miss_gap=20.0)
    hit = scenario(
        "cache_006",
        "user_1",
        [
            fact(0.0, "generate", FactType.PROFILE_READ, metadata={"expected_cache": "miss"}),
            fact(10.0, "a new fact in between"),
            fact(30.0, "hit", FactType.PROFILE_READ, metadata={"expected_cache": "hit"}),
        ],
        category=ScenarioCategory.CACHE,
    )

    ttl, notes = runner.plan_profile_cache_ttl([hit, miss])

    assert ttl == pytest.approx(10.0)
    assert hit.facts[2].timestamp == 30.0  # untouched
    assert hit.facts[2].metadata["expected_cache_unsatisfiable"] is True
    assert any("cannot be moved inside" in note for note in notes)


def test_configured_ttl_is_checked_instead_of_derived():
    runner, _ = make_runner(configured_profile_cache_ttl_seconds=3600.0)
    hit, miss = _cache_pair(hit_gap=1.0, miss_gap=20.0)

    ttl, notes = runner.plan_profile_cache_ttl([hit, miss])

    assert ttl == 3600.0
    assert any("is not shorter than the realised" in note for note in notes)
    # Only the miss expectation is impossible under the engine's 3600s default.
    assert miss.facts[1].metadata["expected_cache_unsatisfiable"] is True
    assert "expected_cache_unsatisfiable" not in hit.facts[1].metadata


def test_configured_ttl_inside_the_window_marks_nothing_unsatisfiable():
    runner, _ = make_runner(configured_profile_cache_ttl_seconds=10.0)
    hit, miss = _cache_pair(hit_gap=1.0, miss_gap=20.0)

    ttl, notes = runner.plan_profile_cache_ttl([hit, miss])

    assert ttl == 10.0
    assert notes == []
    assert "expected_cache_unsatisfiable" not in miss.facts[1].metadata


def test_a_cache_002_gap_too_short_to_host_a_ttl_is_unsatisfiable():
    runner, _ = make_runner()
    hit, miss = _cache_pair(hit_gap=0.2, miss_gap=1.0)

    ttl, notes = runner.plan_profile_cache_ttl([hit, miss])

    assert ttl is None
    assert any("too short to host" in note for note in notes)
    assert miss.facts[1].metadata["expected_cache_unsatisfiable"] is True
    assert hit.facts[1].metadata["expected_cache_unsatisfiable"] is True


@pytest.mark.asyncio
async def test_unsatisfiable_cache_expectation_is_not_scored_during_the_run():
    client = FakeClient()
    client.cache_status = "hit"
    runner, _ = make_runner(client)
    hit, miss = _cache_pair(hit_gap=0.2, miss_gap=1.0)

    result = await runner.run([hit, miss], duration_seconds=60.0)

    assert result.profile_cache_ttl_seconds is None
    unsatisfiable = [c for c in result.cache_checks if c["unsatisfiable"]]
    assert len(unsatisfiable) == 2
    assert all(check["matched"] is None for check in unsatisfiable)


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("duration", RUN_DURATIONS_SECONDS)
def test_generated_runs_always_reach_a_usable_cache_ttl(seed, duration):
    scenarios = ScenarioGenerator(seed=seed).generate(max_duration_seconds=duration)
    runner, _ = make_runner()

    ttl, _ = runner.plan_profile_cache_ttl(scenarios)

    assert ttl is not None
    assert 0 < ttl < duration
    cache_001 = next(s for s in scenarios if s.scenario_id.startswith("cache_001"))
    hit_read = cache_001.facts[2]
    assert hit_read.timestamp - cache_001.facts[1].timestamp < ttl
    assert "expected_cache_unsatisfiable" not in hit_read.metadata
    # Retiming must never break the ascending order the queue relies on.
    timestamps = [f.timestamp for f in cache_001.facts]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_retimed_reads_are_played_at_their_adjusted_offsets():
    client = FakeClient()
    runner, clock = make_runner(client)
    fired = []

    async def recording_profile(user_id, recent_days=7):
        fired.append(clock.now)
        return await FakeClient.get_profile(client, user_id, recent_days)

    client.get_profile = recording_profile
    hit, miss = _cache_pair(hit_gap=25.0, miss_gap=20.0)

    await runner.run([hit], duration_seconds=60.0)
    played_gap = fired[1] - fired[0]

    # Planning needs cache_002 present to derive a TTL, so re-plan with both.
    assert played_gap == pytest.approx(25.0)  # no cache_002 → no retiming

    fired.clear()
    hit2, miss2 = _cache_pair(hit_gap=25.0, miss_gap=20.0)
    client2 = FakeClient()
    runner2, clock2 = make_runner(client2)

    async def recording_profile2(user_id, recent_days=7):
        fired.append(clock2.now)
        return await FakeClient.get_profile(client2, user_id, recent_days)

    client2.get_profile = recording_profile2
    await runner2.run([hit2, miss2], duration_seconds=60.0)

    # hit2: reads at 0.0 and the retimed 5.0; miss2: reads at 0.0 and 20.0.
    assert sorted(fired) == [0.0, 0.0, 5.0, 20.0]


# ---------------------------------------------------------------------------
# Error resilience (spec § 3.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected",
    [
        (EngineClientError("bad", endpoint="/ingest", status_code=422), ErrorClass.PERMANENT.value),
        (EngineClientError("bad", endpoint="/ingest", status_code=404), ErrorClass.PERMANENT.value),
        (EngineClientError("boom", endpoint="/ingest", status_code=500), ErrorClass.TEMPORARY.value),
        (EngineClientError("boom", endpoint="/ingest", status_code=503), ErrorClass.TEMPORARY.value),
        (EngineClientError("POST /ingest timed out after 30.0s", endpoint="/ingest"), ErrorClass.TIMEOUT.value),
        (EngineClientError("failed to connect", endpoint="/ingest"), ErrorClass.TRANSPORT.value),
    ],
)
def test_error_classification(error, expected):
    assert classify_engine_error(error) == expected


@pytest.mark.asyncio
async def test_a_failing_event_does_not_stop_the_run():
    client = FakeClient()
    client.ingest_error_for = lambda user_id, text: (
        EngineClientError("nope", endpoint="/ingest", status_code=500) if text == "b" else None
    )
    runner, _ = make_runner(client)
    scenarios = [
        scenario("s1", "user_1", [fact(0.0, "a"), fact(1.0, "b"), fact(2.0, "c")])
    ]

    result = await runner.run(scenarios, duration_seconds=10.0)

    assert result.status == RunStatus.COMPLETED.value
    assert result.events_played == 3
    assert result.ingested_count == 2
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.scenario_id == "s1"
    assert error.fact_index == 1
    assert error.user_id == "user_1"
    assert error.classification == ErrorClass.TEMPORARY.value
    assert error.endpoint == "/ingest"
    assert error.status_code == 500
    assert isinstance(result.engine_errors[0], EngineClientError)
    assert result.succeeded is False


@pytest.mark.asyncio
async def test_profile_read_failure_is_recorded_and_playback_continues():
    client = FakeClient(profile_error=EngineClientError("down", endpoint="/profile", status_code=503))
    runner, _ = make_runner(client)
    scenarios = [
        scenario(
            "cache_001",
            "user_1",
            [fact(0.0, "read", FactType.PROFILE_READ), fact(1.0, "a fact")],
            category=ScenarioCategory.CACHE,
        )
    ]

    result = await runner.run(scenarios, duration_seconds=10.0)

    assert result.profile_read_count == 0
    assert result.ingested_count == 1
    assert result.cache_checks == []
    assert result.errors[0].endpoint == "/profile"


@pytest.mark.asyncio
async def test_unexpected_exceptions_are_classified_and_survived():
    client = FakeClient(ingest_error=RuntimeError("kaboom"))
    runner, _ = make_runner(client)

    result = await runner.run(
        [scenario("s1", "user_1", [fact(0.0, "a")])], duration_seconds=5.0
    )

    assert result.errors[0].classification == ErrorClass.UNEXPECTED.value
    assert result.status == RunStatus.FAILED.value


@pytest.mark.asyncio
async def test_run_is_failed_when_every_event_errors():
    client = FakeClient(ingest_error=EngineClientError("down", endpoint="/ingest", status_code=500))
    runner, _ = make_runner(client)

    result = await runner.run(
        [scenario("s1", "user_1", [fact(0.0, "a"), fact(1.0, "b")])], duration_seconds=5.0
    )

    assert result.status == RunStatus.FAILED.value
    assert len(result.errors) == 2
    assert result.ingested_count == 0


@pytest.mark.asyncio
async def test_monitoring_failures_never_break_playback():
    class BrokenMonitor(MonitoringService):
        def log_event(self, **kwargs):
            raise RuntimeError("monitor exploded")

    runner, _ = make_runner(FakeClient(), BrokenMonitor())

    result = await runner.run(
        [scenario("s1", "user_1", [fact(0.0, "a")])], duration_seconds=5.0
    )

    assert result.status == RunStatus.COMPLETED.value
    assert result.ingested_count == 1


# ---------------------------------------------------------------------------
# Monitoring / audit integration (spec § 3.4, § 3.5, § 7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_played_fact_produces_a_run_tagged_monitoring_event():
    monitor = MonitoringService(run_number=99)
    runner, _ = make_runner(FakeClient(), monitor)
    scenarios = [
        scenario("s1", "user_1", [fact(0.0, "a")]),
        scenario("s2", "user_2", [fact(1.0, "b")]),
    ]

    await runner.run(scenarios, duration_seconds=5.0, run_number=4)

    events = monitor.get_events()
    assert len(events) == 2
    assert {event.run_number for event in events} == {4}
    assert events[0].metadata["scenario_id"] == "s1"
    assert events[0].metadata["scheduled_time"] == 0.0
    assert events[0].metadata["fact_type"] == FactType.PRIMARY_FACT.value


@pytest.mark.asyncio
async def test_run_metadata_and_audit_rows_are_written(tmp_path):
    audit = AuditLogger(str(tmp_path / "audit_log.db"), run_number=2)
    monitor = MonitoringService(audit_logger=audit, run_number=2)
    runner, _ = make_runner(FakeClient(), monitor, audit_logger=audit)
    scenarios = [
        scenario(
            "c1",
            "user_1",
            [
                fact(0.0, "I live in New York"),
                fact(1.0, "I moved to SF", FactType.CONTRADICTION, contradicts_scenario="c1"),
            ],
            category=ScenarioCategory.CONTRADICTION,
        )
    ]

    result = await runner.run(scenarios, duration_seconds=5.0, run_number=2)

    metadata = audit.get_run(2)
    assert metadata is not None
    assert metadata.status == RunStatus.COMPLETED.value
    assert metadata.scenario_count == 1
    assert metadata.user_count == 1
    assert metadata.duration_seconds is not None

    events = audit.get_events(run_number=2)
    assert [event.event_type for event in events] == ["created", "updated"]
    assert all(event.run_number == 2 for event in events)
    assert result.metrics["fact_ingested_count"] == 1
    audit.close()


@pytest.mark.asyncio
async def test_failed_run_closes_its_metadata_row_as_failed(tmp_path):
    audit = AuditLogger(str(tmp_path / "audit_log.db"), run_number=5)
    client = FakeClient(ingest_error=EngineClientError("no", endpoint="/ingest", status_code=500))
    runner, _ = make_runner(client, MonitoringService(audit_logger=audit), audit_logger=audit)

    await runner.run([scenario("s1", "user_1", [fact(0.0, "a")])], duration_seconds=5.0, run_number=5)

    assert audit.get_run(5).status == RunStatus.FAILED.value
    audit.close()


@pytest.mark.asyncio
async def test_runner_wires_the_monitoring_callback_onto_the_client():
    client = FakeClient()
    monitor = MonitoringService()
    runner, _ = make_runner(client, monitor)

    await runner.run([scenario("s1", "user_1", [fact(0.0, "a")])], duration_seconds=5.0)

    assert client.on_request == monitor.on_request


@pytest.mark.asyncio
async def test_runner_leaves_an_existing_client_callback_alone():
    client = FakeClient()
    sentinel = lambda *args: None
    client.on_request = sentinel
    runner, _ = make_runner(client, MonitoringService())

    await runner.run([scenario("s1", "user_1", [fact(0.0, "a")])], duration_seconds=5.0)

    assert client.on_request is sentinel


# ---------------------------------------------------------------------------
# Validation hand-off (spec § 3.2 step 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validator_is_called_after_playback():
    calls = []

    class Validator:
        def validate(self, scenarios, run_number=None):
            calls.append((len(scenarios), run_number))
            return [ValidationResult("s1", "fact_stored", True)]

    runner, _ = make_runner(validator=Validator())
    result = await runner.run(
        [scenario("s1", "user_1", [fact(0.0, "a")])], duration_seconds=5.0, run_number=6
    )

    assert calls == [(1, 6)]
    assert result.validation_results[0].outcome == "fact_stored"


@pytest.mark.asyncio
async def test_async_validator_is_awaited():
    class Validator:
        async def validate(self, scenarios):
            return [ValidationResult("s1", "fact_retrievable", True)]

    runner, _ = make_runner(validator=Validator())
    result = await runner.run(
        [scenario("s1", "user_1", [fact(0.0, "a")])], duration_seconds=5.0
    )

    assert [r.outcome for r in result.validation_results] == ["fact_retrievable"]


@pytest.mark.asyncio
async def test_unimplemented_validator_is_noted_not_fatal():
    class Validator:
        def validate(self, scenarios, run_number=None):
            raise NotImplementedError("Task 7")

    runner, _ = make_runner(validator=Validator())
    result = await runner.run(
        [scenario("s1", "user_1", [fact(0.0, "a")])], duration_seconds=5.0
    )

    assert result.status == RunStatus.COMPLETED.value
    assert any("not implemented" in note for note in result.notes)


@pytest.mark.asyncio
async def test_missing_validator_is_noted():
    runner, _ = make_runner()
    result = await runner.run(
        [scenario("s1", "user_1", [fact(0.0, "a")])], duration_seconds=5.0
    )

    assert any("no ValidatorService" in note for note in result.notes)


# ---------------------------------------------------------------------------
# End-to-end against the real EngineClient over a mock transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_run_against_a_real_engine_client(tmp_path):
    import httpx

    from simulation.engine_client import EngineClient

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/ingest":
            body = request.read().decode()
            return httpx.Response(
                202, json={"status": "accepted", "fact_id": f"mem_{len(seen)}", "text": body}
            )
        if request.url.path.startswith("/profile"):
            if request.method == "GET":
                return httpx.Response(405, json={"detail": "use POST"})
            return httpx.Response(
                200,
                json={"user_id": "user_1", "stable_facts": ["x"], "recent_activity": []},
            )
        return httpx.Response(404, json={"detail": "nope"})

    audit = AuditLogger(str(tmp_path / "audit_log.db"), run_number=1)
    monitor = MonitoringService(audit_logger=audit, run_number=1)
    clock = FakeClock()
    async with EngineClient(transport=httpx.MockTransport(handler)) as client:
        runner = SimulationRunner(
            engine_client=client,
            monitoring_service=monitor,
            audit_logger=audit,
            clock=clock,
            sleep=clock.sleep,
            wait_out_duration=False,
        )
        conflict = fact(
            4.0, "I moved to SF", FactType.CONTRADICTION, contradicts_scenario="c1"
        )
        scenarios = [
            scenario(
                "c1",
                "user_1",
                [fact(0.0, "I live in NYC"), conflict],
                category=ScenarioCategory.CONTRADICTION,
            ),
            scenario(
                "cache_001",
                "user_1",
                [fact(2.0, "read", FactType.PROFILE_READ, metadata={"expected_cache": "hit"})],
                category=ScenarioCategory.CACHE,
            ),
        ]
        result = await runner.run(scenarios, duration_seconds=60.0, run_number=1)

    assert result.status == RunStatus.COMPLETED.value
    assert result.errors == []
    assert result.ingested_count == 2
    assert result.profile_read_count == 1
    # Four requests reach the engine: ingest, the GET /profile probe (405), the
    # POST /profile fallback, then the second ingest — hence mem_1 / mem_4.
    assert conflict.contradicts_fact_id == "mem_1"
    assert result.fact_ids["c1"] == ["mem_1", "mem_4"]
    # The profile read went through the profile endpoint, never through /ingest.
    assert ("POST", "/ingest") in seen
    assert any(path.startswith("/profile") for _, path in seen)
    # Latency landed on the MonitoringService through the on_request callback.
    assert result.metrics["latency_count"] == len(seen)
    assert [e.event_type for e in audit.get_events(run_number=1)] == ["created", "updated"]
    audit.close()
