"""MonitoringService — real-time state tracking during a run (spec § 3.4).

Tracks monitoring events (fact ingestion, contradictions, cache hits/misses, TTL/expiry,
latencies) and computes derived metrics (cache hit rate, avg latency, fact counts, user
isolation). Optionally persists to AuditLogger for durability.

Spec reference:
  - § 3.4 MonitoringService (events, metrics, cache inference from latency)
  - § 3.3 EngineClient (latency_us field, on_request callback)
  - § 3.5 AuditLogger (state snapshots for disk persistence)
"""

import time
import threading
from typing import Any, Callable, Dict, List, Optional
from collections import defaultdict

from .audit_logger import AuditLogger
from .models import MonitoringEvent, MonitoringEventType, AuditEvent, AuditEventType


class MonitoringService:
    """Collects :class:`MonitoringEvent` records and derived metrics (spec § 3.4).

    Tracks real-time simulation state changes: fact ingestion, contradictions,
    cache hits/misses, TTL/expiry, API latencies, and user isolation. Events
    are stored in-memory and optionally persisted to an AuditLogger.

    Cache hit/miss inference from latency (spec § 3.4):
      - latency_us < 10ms → cache hit
      - latency_us > 100ms → cache miss
      - 10-100ms → indeterminate (not counted as either)

    Args:
        audit_logger: Optional AuditLogger for persistent state snapshots.
        run_number: Run tag applied to every event recorded (spec § 7).
    """

    #: Latency thresholds for cache hit/miss inference (microseconds)
    CACHE_HIT_THRESHOLD_US = 10_000  # 10ms
    CACHE_MISS_THRESHOLD_US = 100_000  # 100ms

    def __init__(
        self,
        audit_logger: Optional[AuditLogger] = None,
        run_number: int = 1,
    ) -> None:
        self.audit_logger = audit_logger
        self.run_number = run_number
        self.events: List[MonitoringEvent] = []

        #: In-memory metrics: {user_id: {event_type: count}}
        self._event_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

        #: API latencies: list of (endpoint, latency_us) pairs
        self._latencies: List[tuple] = []

        #: Cache hit/miss inferences: {user_id: {endpoint: {"hits": count, "misses": count}}}
        self._cache_stats: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"hits": 0, "misses": 0})
        )

        #: Active/inactive fact tracking: {user_id: {"active": set, "inactive": set}}
        self._fact_states: Dict[str, Dict[str, set]] = defaultdict(
            lambda: {"active": set(), "inactive": set()}
        )

        #: Lock for thread-safe updates
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Event recording (spec § 3.4)
    # ------------------------------------------------------------------

    def log_event(
        self,
        run_number: Optional[int] = None,
        event_type: str = "",
        user_id: str = "",
        fact_id: str = "",
        old_state: Optional[Dict[str, Any]] = None,
        new_state: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MonitoringEvent:
        """Record one monitoring event and return it.

        The event is appended to the in-memory store and optionally persisted
        to the AuditLogger. ``old_state`` and ``new_state`` are dicts
        representing before/after state of a fact.

        Args:
            run_number: Run tag (defaults to ``self.run_number``).
            event_type: One of ``MonitoringEventType`` values.
            user_id: User who owns the fact.
            fact_id: Unique fact identifier.
            old_state: State before this event (for updates).
            new_state: State after this event.
            metadata: Additional event context (cache info, TTL remaining, etc.).

        Returns:
            The created :class:`MonitoringEvent`.
        """
        run_number = self.run_number if run_number is None else run_number
        timestamp = time.time()

        event = MonitoringEvent(
            timestamp=timestamp,
            run_number=run_number,
            event_type=event_type,
            user_id=user_id,
            fact_id=fact_id,
            old_state=old_state or {},
            new_state=new_state or {},
            metadata=metadata or {},
        )

        with self._lock:
            self.events.append(event)
            self._update_metrics(event)

            # Optionally persist to audit logger
            if self.audit_logger is not None:
                self._persist_to_audit_logger(event)

        return event

    def _update_metrics(self, event: MonitoringEvent) -> None:
        """Update in-memory metrics based on the event."""
        user_id = event.user_id

        # Track event counts
        self._event_counts[user_id][event.event_type] += 1

        # Update fact state tracking
        if event.event_type == MonitoringEventType.FACT_INGESTED.value:
            self._fact_states[user_id]["active"].add(event.fact_id)
            self._fact_states[user_id]["inactive"].discard(event.fact_id)

        elif event.event_type == MonitoringEventType.CONTRADICTION_RESOLVED.value:
            # Old fact deactivated
            old_fact_id = event.metadata.get("old_fact_id")
            if old_fact_id:
                self._fact_states[user_id]["inactive"].add(old_fact_id)
                self._fact_states[user_id]["active"].discard(old_fact_id)
            # New fact active
            new_fact_id = event.metadata.get("new_fact_id") or event.fact_id
            self._fact_states[user_id]["active"].add(new_fact_id)
            self._fact_states[user_id]["inactive"].discard(new_fact_id)

        elif event.event_type == MonitoringEventType.EXPIRY_FIRED.value:
            self._fact_states[user_id]["inactive"].add(event.fact_id)
            self._fact_states[user_id]["active"].discard(event.fact_id)

    def _persist_to_audit_logger(self, event: MonitoringEvent) -> None:
        """Persist a monitoring event as an audit event."""
        if self.audit_logger is None:
            return

        # Map monitoring event type to audit event type
        audit_event_type = self._map_to_audit_type(event.event_type)
        if audit_event_type is None:
            return

        audit_event = AuditEvent(
            run_number=event.run_number,
            timestamp=event.timestamp,
            user_id=event.user_id,
            fact_id=event.fact_id,
            event_type=audit_event_type,
            before_state=event.old_state if event.old_state else None,
            after_state=event.new_state if event.new_state else None,
            source="scenario_playback",
        )
        self.audit_logger.write_event(audit_event)

    @staticmethod
    def _map_to_audit_type(monitoring_event_type: str) -> Optional[str]:
        """Map monitoring event type to AuditEventType vocabulary."""
        mapping = {
            MonitoringEventType.FACT_INGESTED.value: AuditEventType.CREATED.value,
            MonitoringEventType.CONTRADICTION_RESOLVED.value: AuditEventType.UPDATED.value,
            MonitoringEventType.EXPIRY_FIRED.value: AuditEventType.EXPIRED.value,
        }
        return mapping.get(monitoring_event_type)

    # ------------------------------------------------------------------
    # Event retrieval
    # ------------------------------------------------------------------

    def get_events(
        self,
        run_number: Optional[int] = None,
        user_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> List[MonitoringEvent]:
        """Return matching in-memory events.

        All arguments are optional filters; with none of them, all events
        are returned.

        Args:
            run_number: Filter by run number.
            user_id: Filter by user ID.
            event_type: Filter by event type.

        Returns:
            List of matching events, in order recorded.
        """
        with self._lock:
            results = self.events
            if run_number is not None:
                results = [e for e in results if e.run_number == run_number]
            if user_id is not None:
                results = [e for e in results if e.user_id == user_id]
            if event_type is not None:
                results = [e for e in results if e.event_type == event_type]
            return list(results)

    # ------------------------------------------------------------------
    # Metrics aggregation (spec § 3.4)
    # ------------------------------------------------------------------

    def get_metrics(self, run_number: Optional[int] = None) -> Dict[str, Any]:
        """Return current aggregate metrics.

        Computes: fact counts, cache hit rate, avg latency, active/inactive
        counts, memory tracking, and user isolation metrics.

        Args:
            run_number: Scope metrics to this run (defaults to all).

        Returns:
            Dict with keys:
              - ``fact_count``: total facts recorded
              - ``fact_ingested_count``: fact_ingested events
              - ``contradiction_count``: contradiction_resolved events
              - ``expiry_count``: expiry_fired events
              - ``cache_hit_rate``: fraction of profile calls with latency < 10ms
              - ``avg_latency_us``: mean API latency across all calls
              - ``min_latency_us``: minimum latency observed
              - ``max_latency_us``: maximum latency observed
              - ``latency_count``: total API calls tracked
              - ``active_facts_by_user``: {user_id: count}
              - ``inactive_facts_by_user``: {user_id: count}
              - ``users``: list of user_ids with events
              - ``events_count``: total events recorded (in run if scoped)
        """
        with self._lock:
            # Filter events by run_number if specified
            events = self.events
            if run_number is not None:
                events = [e for e in events if e.run_number == run_number]

            # Aggregate event counts
            event_type_counts: Dict[str, int] = defaultdict(int)
            for event in events:
                event_type_counts[event.event_type] += 1

            # Latency stats
            latencies = self._latencies
            if latencies:
                latency_values = [lat for _, lat in latencies]
                avg_latency = sum(latency_values) / len(latency_values)
                min_latency = min(latency_values)
                max_latency = max(latency_values)
            else:
                avg_latency = 0.0
                min_latency = 0
                max_latency = 0

            # Cache hit rate (from profile endpoint calls)
            cache_hits = 0
            cache_misses = 0
            for endpoint, latency_us in latencies:
                if endpoint in ("/profile", "get_profile"):
                    if latency_us < self.CACHE_HIT_THRESHOLD_US:
                        cache_hits += 1
                    elif latency_us > self.CACHE_MISS_THRESHOLD_US:
                        cache_misses += 1

            total_profile_calls = cache_hits + cache_misses
            cache_hit_rate = (
                cache_hits / total_profile_calls if total_profile_calls > 0 else 0.0
            )

            # Active/inactive fact tracking
            active_by_user = {
                user_id: len(states["active"])
                for user_id, states in self._fact_states.items()
            }
            inactive_by_user = {
                user_id: len(states["inactive"])
                for user_id, states in self._fact_states.items()
            }

            # User list (from events)
            users = sorted(set(e.user_id for e in events if e.user_id))

            return {
                "fact_count": event_type_counts.get(MonitoringEventType.FACT_INGESTED.value, 0),
                "fact_ingested_count": event_type_counts.get(
                    MonitoringEventType.FACT_INGESTED.value, 0
                ),
                "contradiction_count": event_type_counts.get(
                    MonitoringEventType.CONTRADICTION_RESOLVED.value, 0
                ),
                "cache_hit_count": cache_hits,
                "cache_miss_count": cache_misses,
                "expiry_count": event_type_counts.get(MonitoringEventType.EXPIRY_FIRED.value, 0),
                "cache_hit_rate": cache_hit_rate,
                "avg_latency_us": int(avg_latency),
                "min_latency_us": min_latency,
                "max_latency_us": max_latency,
                "latency_count": len(latencies),
                "active_facts_by_user": dict(active_by_user),
                "inactive_facts_by_user": dict(inactive_by_user),
                "total_active_facts": sum(active_by_user.values()),
                "total_inactive_facts": sum(inactive_by_user.values()),
                "users": users,
                "events_count": len(events),
            }

    # ------------------------------------------------------------------
    # EngineClient callback (spec § 3.3)
    # ------------------------------------------------------------------

    def on_request(
        self, endpoint: str, user_id: Optional[str], latency_us: int, status: int
    ) -> None:
        """Callback invoked by EngineClient for each API call.

        Tracks latency and infers cache hits for profile endpoint based on
        latency thresholds (spec § 3.4).

        Args:
            endpoint: Logical endpoint, e.g. ``"/profile"`` or ``"/retrieve"``.
            user_id: User ID associated with the call.
            latency_us: Round-trip latency in microseconds.
            status: HTTP status code (or ``STATUS_*`` sentinel).
        """
        with self._lock:
            self._latencies.append((endpoint, latency_us))

            # Infer cache hit/miss from profile call latency
            if endpoint in ("/profile", "get_profile") and user_id:
                if latency_us < self.CACHE_HIT_THRESHOLD_US:
                    self._cache_stats[user_id][endpoint]["hits"] += 1
                elif latency_us > self.CACHE_MISS_THRESHOLD_US:
                    self._cache_stats[user_id][endpoint]["misses"] += 1
