"""Data structures shared across the simulation services.

Spec references:
  - § 3.1 ScenarioGenerator (scenario / fact structures)
  - § 3.4 MonitoringService (MonitoringEvent)
  - § 3.5 AuditLogger (AuditEvent, RunMetadata)
  - § 7   Data Persistence & Run Tagging (run_number on every event)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ScenarioCategory(str, Enum):
    """Categories of deterministic test scenarios (spec § 5.1)."""

    CONTRADICTION = "contradiction"
    EXPIRY = "expiry"
    CACHE = "cache"
    MULTI_USER = "multi"
    GENERIC = "generic"
    MODIFY = "modify"


class FactType(str, Enum):
    """Role a scenario fact plays within its scenario (spec § 3.1)."""

    PRIMARY_FACT = "primary_fact"
    CONTRADICTION = "contradiction"
    MODIFICATION = "modification"
    EXPIRING_FACT = "expiring_fact"
    PROFILE_READ = "profile_read"
    RETRIEVAL = "retrieval"


class AuditEventType(str, Enum):
    """Lifecycle events persisted to ``audit_events.event_type`` (spec § 3.5)."""

    CREATED = "created"
    UPDATED = "updated"
    DEACTIVATED = "deactivated"
    EXPIRED = "expired"


class AuditSource(str, Enum):
    """Origin of an audit event, persisted to ``audit_events.source`` (spec § 3.5)."""

    SCENARIO_PLAYBACK = "scenario_playback"
    USER_INTERACTION = "user_interaction"
    TTL_PRUNING = "ttl_pruning"


class MonitoringEventType(str, Enum):
    """Real-time monitoring event kinds (spec § 3.4)."""

    FACT_INGESTED = "fact_ingested"
    CONTRADICTION_RESOLVED = "contradiction_resolved"
    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
    EXPIRY_FIRED = "expiry_fired"
    RETRIEVAL = "retrieval"
    VALIDATION = "validation"


class RunStatus(str, Enum):
    """Values for ``run_metadata.status`` (spec § 3.5 / § 7.1)."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ScenarioFact:
    """A single timed utterance inside a scenario (spec § 3.1)."""

    timestamp: float
    text: str
    type: FactType = FactType.PRIMARY_FACT
    contradicts_scenario: Optional[str] = None
    contradicts_fact_id: Optional[str] = None
    ttl_seconds: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    """A deterministic test scenario assigned to one user (spec § 3.1)."""

    scenario_id: str
    user_id: str
    category: ScenarioCategory
    facts: List[ScenarioFact] = field(default_factory=list)
    expected_outcomes: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MonitoringEvent:
    """In-memory state-change event tracked during a run (spec § 3.4)."""

    timestamp: float
    run_number: int
    event_type: str
    user_id: str
    fact_id: str
    old_state: Dict[str, Any] = field(default_factory=dict)
    new_state: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditEvent:
    """One row of ``audit_events`` (spec § 3.5).

    ``event_id`` is assigned by SQLite on insert, so it is ``None`` until then.
    ``before_state`` / ``after_state`` are held as dicts here and serialised to
    JSON by the AuditLogger (Task 3).
    """

    run_number: int
    timestamp: float
    user_id: str
    fact_id: str
    event_type: str
    before_state: Optional[Dict[str, Any]] = None
    after_state: Optional[Dict[str, Any]] = None
    source: str = AuditSource.SCENARIO_PLAYBACK.value
    created_at: Optional[float] = None
    event_id: Optional[int] = None


@dataclass
class RunMetadata:
    """One row of ``run_metadata`` (spec § 3.5 / § 7.1)."""

    run_number: int
    start_time: float
    end_time: Optional[float] = None
    duration_seconds: Optional[float] = None
    user_count: int = 0
    scenario_count: int = 0
    status: str = RunStatus.IN_PROGRESS.value


@dataclass
class ValidationResult:
    """Outcome of a single expected-outcome check (spec § 3.6)."""

    scenario_id: str
    outcome: str
    passed: bool
    details: Dict[str, Any] = field(default_factory=dict)
