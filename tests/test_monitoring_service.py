"""Task 5 verification: MonitoringService real-time metrics tracking.

Covers spec § 3.4 (events, metrics, cache inference), integration with
EngineClient on_request callback, and persistence via AuditLogger.
"""

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from simulation.monitoring_service import MonitoringService
from simulation.audit_logger import AuditLogger
from simulation.models import MonitoringEvent, MonitoringEventType, AuditEventType


@pytest.fixture()
def monitoring_service():
    """Fresh MonitoringService with no AuditLogger."""
    return MonitoringService(run_number=1)


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "audit_log.db")


@pytest.fixture()
def monitoring_with_logger(db_path):
    """MonitoringService with AuditLogger persistence."""
    logger = AuditLogger(db_path, run_number=1)
    service = MonitoringService(audit_logger=logger, run_number=1)
    yield service
    logger.close()


# ---------------------------------------------------------------------------
# log_event()
# ---------------------------------------------------------------------------


def test_log_event_creates_monitoring_event(monitoring_service):
    """log_event() appends to in-memory store and returns the event."""
    event = monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_abc",
        new_state={"text": "I live in NYC"},
    )

    assert isinstance(event, MonitoringEvent)
    assert event.event_type == MonitoringEventType.FACT_INGESTED.value
    assert event.user_id == "user_1"
    assert event.fact_id == "mem_abc"
    assert event.new_state == {"text": "I live in NYC"}
    assert event.run_number == 1

    # Verify it was added to the store
    assert len(monitoring_service.events) == 1
    assert monitoring_service.events[0] is event


def test_log_event_uses_default_run_number(monitoring_service):
    """log_event() uses service's run_number by default."""
    event = monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_xyz",
    )
    assert event.run_number == 1


def test_log_event_allows_override_run_number(monitoring_service):
    """log_event() accepts explicit run_number."""
    event = monitoring_service.log_event(
        run_number=42,
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_xyz",
    )
    assert event.run_number == 42


def test_log_event_captures_timestamp(monitoring_service):
    """log_event() stamps timestamp."""
    before = time.time()
    event = monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_xyz",
    )
    after = time.time()
    assert before <= event.timestamp <= after


def test_log_event_records_old_and_new_state(monitoring_service):
    """log_event() captures before/after state."""
    old_state = {"text": "NYC", "status": "active"}
    new_state = {"text": "SF", "status": "active"}
    event = monitoring_service.log_event(
        event_type=MonitoringEventType.CONTRADICTION_RESOLVED.value,
        user_id="user_1",
        fact_id="mem_abc",
        old_state=old_state,
        new_state=new_state,
    )
    assert event.old_state == old_state
    assert event.new_state == new_state


def test_log_event_with_metadata(monitoring_service):
    """log_event() stores additional metadata."""
    metadata = {"old_fact_id": "mem_xyz", "cache_ttl": 3600}
    event = monitoring_service.log_event(
        event_type=MonitoringEventType.CONTRADICTION_RESOLVED.value,
        user_id="user_1",
        fact_id="mem_abc",
        metadata=metadata,
    )
    assert event.metadata == metadata


def test_log_multiple_events_in_order(monitoring_service):
    """Multiple log_event() calls append in order."""
    event1 = monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_1",
    )
    event2 = monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_2",
    )
    event3 = monitoring_service.log_event(
        event_type=MonitoringEventType.CONTRADICTION_RESOLVED.value,
        user_id="user_1",
        fact_id="mem_2",
    )

    assert len(monitoring_service.events) == 3
    assert monitoring_service.events[0] is event1
    assert monitoring_service.events[1] is event2
    assert monitoring_service.events[2] is event3
    assert event1.timestamp <= event2.timestamp <= event3.timestamp


# ---------------------------------------------------------------------------
# Fact state tracking
# ---------------------------------------------------------------------------


def test_fact_ingested_updates_active_state(monitoring_service):
    """FACT_INGESTED event marks fact as active."""
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_abc",
    )
    metrics = monitoring_service.get_metrics()
    assert metrics["active_facts_by_user"]["user_1"] == 1
    assert metrics["inactive_facts_by_user"].get("user_1", 0) == 0


def test_contradiction_resolved_deactivates_old_fact(monitoring_service):
    """CONTRADICTION_RESOLVED event deactivates old fact and activates new."""
    # Ingest original fact
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_abc",
    )

    # Contradiction: deactivate mem_abc, activate mem_def
    monitoring_service.log_event(
        event_type=MonitoringEventType.CONTRADICTION_RESOLVED.value,
        user_id="user_1",
        fact_id="mem_def",
        metadata={"old_fact_id": "mem_abc"},
    )

    metrics = monitoring_service.get_metrics()
    assert metrics["active_facts_by_user"]["user_1"] == 1
    assert metrics["inactive_facts_by_user"]["user_1"] == 1


def test_expiry_fired_deactivates_fact(monitoring_service):
    """EXPIRY_FIRED event marks fact as inactive."""
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_abc",
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.EXPIRY_FIRED.value,
        user_id="user_1",
        fact_id="mem_abc",
    )

    metrics = monitoring_service.get_metrics()
    assert metrics["active_facts_by_user"].get("user_1", 0) == 0
    assert metrics["inactive_facts_by_user"]["user_1"] == 1


# ---------------------------------------------------------------------------
# get_events()
# ---------------------------------------------------------------------------


def test_get_events_returns_all(monitoring_service):
    """get_events() with no filters returns all events."""
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_1"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_2", fact_id="mem_2"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.CONTRADICTION_RESOLVED.value,
        user_id="user_1",
        fact_id="mem_3",
    )

    events = monitoring_service.get_events()
    assert len(events) == 3


def test_get_events_filters_by_user_id(monitoring_service):
    """get_events(user_id=X) returns only that user's events."""
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_1"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_2", fact_id="mem_2"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_3"
    )

    events = monitoring_service.get_events(user_id="user_1")
    assert len(events) == 2
    assert all(e.user_id == "user_1" for e in events)


def test_get_events_filters_by_event_type(monitoring_service):
    """get_events(event_type=X) returns only that type."""
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_1"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.CONTRADICTION_RESOLVED.value,
        user_id="user_1",
        fact_id="mem_2",
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_2", fact_id="mem_3"
    )

    events = monitoring_service.get_events(event_type=MonitoringEventType.FACT_INGESTED.value)
    assert len(events) == 2
    assert all(e.event_type == MonitoringEventType.FACT_INGESTED.value for e in events)


def test_get_events_filters_by_run_number(monitoring_service):
    """get_events(run_number=X) returns only that run's events."""
    monitoring_service.log_event(
        run_number=1, event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_1"
    )
    monitoring_service.log_event(
        run_number=2, event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_2"
    )
    monitoring_service.log_event(
        run_number=1, event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_3"
    )

    events = monitoring_service.get_events(run_number=1)
    assert len(events) == 2
    assert all(e.run_number == 1 for e in events)


def test_get_events_combined_filters(monitoring_service):
    """get_events() with multiple filters intersects them."""
    monitoring_service.log_event(
        run_number=1, event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_1"
    )
    monitoring_service.log_event(
        run_number=1,
        event_type=MonitoringEventType.CONTRADICTION_RESOLVED.value,
        user_id="user_1",
        fact_id="mem_2",
    )
    monitoring_service.log_event(
        run_number=1, event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_2", fact_id="mem_3"
    )

    events = monitoring_service.get_events(
        run_number=1, user_id="user_1", event_type=MonitoringEventType.FACT_INGESTED.value
    )
    assert len(events) == 1
    assert events[0].fact_id == "mem_1"


# ---------------------------------------------------------------------------
# get_metrics()
# ---------------------------------------------------------------------------


def test_get_metrics_empty_service(monitoring_service):
    """get_metrics() on empty service returns zero counts."""
    metrics = monitoring_service.get_metrics()
    assert metrics["fact_count"] == 0
    assert metrics["fact_ingested_count"] == 0
    assert metrics["contradiction_count"] == 0
    assert metrics["events_count"] == 0
    assert metrics["cache_hit_rate"] == 0.0
    assert metrics["avg_latency_us"] == 0
    assert metrics["active_facts_by_user"] == {}


def test_get_metrics_counts_event_types(monitoring_service):
    """get_metrics() counts events by type."""
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_1"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_2"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.CONTRADICTION_RESOLVED.value,
        user_id="user_1",
        fact_id="mem_3",
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.EXPIRY_FIRED.value, user_id="user_1", fact_id="mem_2"
    )

    metrics = monitoring_service.get_metrics()
    assert metrics["fact_ingested_count"] == 2
    assert metrics["contradiction_count"] == 1
    assert metrics["expiry_count"] == 1
    assert metrics["events_count"] == 4


def test_get_metrics_scopes_to_run_number(monitoring_service):
    """get_metrics(run_number=X) returns only that run's metrics."""
    monitoring_service.log_event(
        run_number=1, event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_1"
    )
    monitoring_service.log_event(
        run_number=2, event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_2"
    )
    monitoring_service.log_event(
        run_number=2, event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_3"
    )

    metrics_run1 = monitoring_service.get_metrics(run_number=1)
    metrics_run2 = monitoring_service.get_metrics(run_number=2)

    assert metrics_run1["events_count"] == 1
    assert metrics_run2["events_count"] == 2


def test_get_metrics_lists_users(monitoring_service):
    """get_metrics()["users"] lists unique users."""
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_1"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_2", fact_id="mem_2"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_3"
    )

    metrics = monitoring_service.get_metrics()
    assert sorted(metrics["users"]) == ["user_1", "user_2"]


def test_get_metrics_active_inactive_totals(monitoring_service):
    """get_metrics() sums active/inactive across all users."""
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_1"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_2"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_2", fact_id="mem_3"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.EXPIRY_FIRED.value, user_id="user_1", fact_id="mem_1"
    )

    metrics = monitoring_service.get_metrics()
    assert metrics["total_active_facts"] == 2  # user_1:mem_2, user_2:mem_3
    assert metrics["total_inactive_facts"] == 1  # user_1:mem_1


# ---------------------------------------------------------------------------
# Cache hit/miss inference from latency
# ---------------------------------------------------------------------------


def test_on_request_tracks_latency(monitoring_service):
    """on_request() records endpoint latency."""
    monitoring_service.on_request("/ingest", "user_1", 50_000, 200)
    monitoring_service.on_request("/retrieve", "user_1", 75_000, 200)

    metrics = monitoring_service.get_metrics()
    assert metrics["latency_count"] == 2
    assert metrics["avg_latency_us"] == 62_500
    assert metrics["min_latency_us"] == 50_000
    assert metrics["max_latency_us"] == 75_000


def test_on_request_profile_cache_hit_inference(monitoring_service):
    """on_request("/profile") with latency < 10ms inferred as cache hit."""
    # Cache hit: latency < 10ms
    monitoring_service.on_request("/profile", "user_1", 5_000, 200)

    metrics = monitoring_service.get_metrics()
    assert metrics["cache_hit_count"] == 1
    assert metrics["cache_miss_count"] == 0
    assert metrics["cache_hit_rate"] == 1.0


def test_on_request_profile_cache_miss_inference(monitoring_service):
    """on_request("/profile") with latency > 100ms inferred as cache miss."""
    # Cache miss: latency > 100ms
    monitoring_service.on_request("/profile", "user_1", 150_000, 200)

    metrics = monitoring_service.get_metrics()
    assert metrics["cache_hit_count"] == 0
    assert metrics["cache_miss_count"] == 1
    assert metrics["cache_hit_rate"] == 0.0


def test_on_request_profile_indeterminate_latency(monitoring_service):
    """on_request("/profile") with 10-100ms latency not counted as hit or miss."""
    # Indeterminate: latency between 10-100ms
    monitoring_service.on_request("/profile", "user_1", 50_000, 200)

    metrics = monitoring_service.get_metrics()
    assert metrics["cache_hit_count"] == 0
    assert metrics["cache_miss_count"] == 0
    assert metrics["cache_hit_rate"] == 0.0  # No cache stats, so 0.0


def test_on_request_non_profile_endpoints_ignored(monitoring_service):
    """on_request() for non-profile endpoints doesn't count as cache hit/miss."""
    monitoring_service.on_request("/retrieve", "user_1", 5_000, 200)
    monitoring_service.on_request("/ingest", "user_1", 150_000, 200)

    metrics = monitoring_service.get_metrics()
    assert metrics["cache_hit_count"] == 0
    assert metrics["cache_miss_count"] == 0
    assert metrics["cache_hit_rate"] == 0.0


def test_on_request_cache_hit_rate_calculation(monitoring_service):
    """Cache hit rate is hits / (hits + misses)."""
    # 2 hits, 3 misses
    monitoring_service.on_request("/profile", "user_1", 5_000, 200)
    monitoring_service.on_request("/profile", "user_1", 8_000, 200)
    monitoring_service.on_request("/profile", "user_1", 110_000, 200)
    monitoring_service.on_request("/profile", "user_1", 120_000, 200)
    monitoring_service.on_request("/profile", "user_1", 150_000, 200)

    metrics = monitoring_service.get_metrics()
    assert metrics["cache_hit_count"] == 2
    assert metrics["cache_miss_count"] == 3
    assert metrics["cache_hit_rate"] == pytest.approx(0.4, abs=0.001)


def test_on_request_by_user(monitoring_service):
    """on_request() tracks cache stats per user."""
    monitoring_service.on_request("/profile", "user_1", 5_000, 200)  # hit
    monitoring_service.on_request("/profile", "user_2", 150_000, 200)  # miss

    metrics = monitoring_service.get_metrics()
    assert metrics["cache_hit_count"] == 1
    assert metrics["cache_miss_count"] == 1
    assert metrics["cache_hit_rate"] == pytest.approx(0.5, abs=0.001)


# ---------------------------------------------------------------------------
# AuditLogger persistence
# ---------------------------------------------------------------------------


def test_log_event_persists_to_audit_logger(monitoring_with_logger):
    """log_event() persists to AuditLogger when available."""
    service = monitoring_with_logger
    logger = service.audit_logger

    service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_abc",
        new_state={"text": "I live in NYC"},
    )

    # Check that audit logger recorded the event
    audit_events = logger.get_events(run_number=1, user_id="user_1")
    assert len(audit_events) == 1
    assert audit_events[0].fact_id == "mem_abc"
    assert audit_events[0].event_type == AuditEventType.CREATED.value


def test_contradiction_event_maps_to_audit_updated(monitoring_with_logger):
    """CONTRADICTION_RESOLVED maps to audit UPDATED."""
    service = monitoring_with_logger
    logger = service.audit_logger

    service.log_event(
        event_type=MonitoringEventType.CONTRADICTION_RESOLVED.value,
        user_id="user_1",
        fact_id="mem_def",
        old_state={"text": "NYC"},
        new_state={"text": "SF"},
    )

    audit_events = logger.get_events(run_number=1, user_id="user_1")
    assert len(audit_events) == 1
    assert audit_events[0].event_type == AuditEventType.UPDATED.value


def test_expiry_event_maps_to_audit_expired(monitoring_with_logger):
    """EXPIRY_FIRED maps to audit EXPIRED."""
    service = monitoring_with_logger
    logger = service.audit_logger

    service.log_event(
        event_type=MonitoringEventType.EXPIRY_FIRED.value,
        user_id="user_1",
        fact_id="mem_abc",
    )

    audit_events = logger.get_events(run_number=1, user_id="user_1")
    assert len(audit_events) == 1
    assert audit_events[0].event_type == AuditEventType.EXPIRED.value


def test_multiple_events_persisted_in_order(monitoring_with_logger):
    """Multiple log_event() calls persist to audit logger in order."""
    service = monitoring_with_logger
    logger = service.audit_logger

    service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_1",
    )
    service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value,
        user_id="user_1",
        fact_id="mem_2",
    )
    service.log_event(
        event_type=MonitoringEventType.CONTRADICTION_RESOLVED.value,
        user_id="user_1",
        fact_id="mem_3",
    )

    audit_events = logger.get_events(run_number=1, user_id="user_1")
    assert len(audit_events) == 3
    assert audit_events[0].fact_id == "mem_1"
    assert audit_events[1].fact_id == "mem_2"
    assert audit_events[2].fact_id == "mem_3"


# ---------------------------------------------------------------------------
# User isolation
# ---------------------------------------------------------------------------


def test_user_isolation_facts_separate_by_user(monitoring_service):
    """Facts tracked separately per user."""
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_1"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_2", fact_id="mem_2"
    )

    metrics = monitoring_service.get_metrics()
    assert metrics["active_facts_by_user"]["user_1"] == 1
    assert metrics["active_facts_by_user"]["user_2"] == 1


def test_user_isolation_events_filtered_correctly(monitoring_service):
    """get_events() doesn't leak events between users."""
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_1", fact_id="mem_1"
    )
    monitoring_service.log_event(
        event_type=MonitoringEventType.FACT_INGESTED.value, user_id="user_2", fact_id="mem_2"
    )

    user_1_events = monitoring_service.get_events(user_id="user_1")
    user_2_events = monitoring_service.get_events(user_id="user_2")

    assert len(user_1_events) == 1
    assert len(user_2_events) == 1
    assert user_1_events[0].fact_id == "mem_1"
    assert user_2_events[0].fact_id == "mem_2"


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_log_events_thread_safe(monitoring_service):
    """log_event() is thread-safe."""
    import threading

    events_logged = []

    def log_events(user_id, count):
        for i in range(count):
            event = monitoring_service.log_event(
                event_type=MonitoringEventType.FACT_INGESTED.value,
                user_id=user_id,
                fact_id=f"mem_{user_id}_{i}",
            )
            events_logged.append(event)

    threads = [
        threading.Thread(target=log_events, args=(f"user_{i}", 10)) for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(monitoring_service.events) == 50
    assert len(events_logged) == 50
