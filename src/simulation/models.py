"""Data structures shared across the simulation services.

Spec references:
  - § 3.1 ScenarioGenerator (scenario / fact structures)
  - § 3.3 EngineClient (IngestionResult, RetrievalResult, ProfileResult,
          StorageMetrics)
  - § 3.4 MonitoringService (MonitoringEvent)
  - § 3.5 AuditLogger (AuditEvent, RunMetadata)
  - § 7   Data Persistence & Run Tagging (run_number on every event)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional


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
    #: Free-text run label, e.g. "Progressive validation" (spec § 7.1). The
    #: § 3.5 DDL omits it; the column was added in Task 3 so the dataclass and
    #: the table stay one-to-one.
    notes: Optional[str] = None


@dataclass
class IngestionResult:
    """Outcome of ``POST /ingest`` (spec § 3.3).

    One utterance can yield several facts, so ``memory_ids`` and ``fact_texts``
    carry all of them while ``fact_id`` stays the first for single-id callers.
    Deployments whose ``/ingest`` is fire-and-forget return none of these, and
    ``timestamp`` then falls back to the moment the response was parsed.
    """

    user_id: str
    timestamp: float
    fact_id: Optional[str] = None
    status: Optional[str] = None
    scope: Optional[str] = None
    #: Round-trip latency in microseconds (spec § 3.4 consumes this).
    latency_us: int = 0
    #: Every memory id the engine created for this utterance.
    memory_ids: List[str] = field(default_factory=list)
    #: The normalised ``subject predicate object`` text stored for each id.
    fact_texts: List[str] = field(default_factory=list)
    #: True when the engine reported what it extracted. Distinguishes "the
    #: extractor found nothing in this utterance" from "this deployment's
    #: /ingest does not report ids at all", which look identical otherwise.
    extraction_reported: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def extracted_nothing(self) -> bool:
        """The engine ran extraction on this utterance and stored no fact."""
        return self.extraction_reported and not self.memory_ids


@dataclass
class RetrievedFact:
    """One scored memory inside a :class:`RetrievalResult` (spec § 3.3).

    The engine has no ``fact_id`` key on retrieved memories — it returns the
    store's memory id in ``source`` — so the client mirrors that id into
    :attr:`fact_id` when no explicit one is present.
    """

    text: str
    score: float
    fact_id: Optional[str] = None
    similarity: Optional[float] = None
    decay_factor: Optional[float] = None
    source: Optional[str] = None
    timestamp: Optional[float] = None
    scope: Optional[str] = None
    age_days: Optional[float] = None
    #: Any additional keys the engine returned, kept verbatim.
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    """Outcome of ``POST /retrieve`` (spec § 3.3).

    Behaves like the ``List[{fact_id, text, score, ...}]`` the spec describes:
    it is iterable, sized and indexable over :attr:`memories`.
    """

    user_id: str
    query: str
    memories: List[RetrievedFact] = field(default_factory=list)
    latency_us: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.memories)

    def __iter__(self) -> Iterator[RetrievedFact]:
        return iter(self.memories)

    def __getitem__(self, index: int) -> RetrievedFact:
        return self.memories[index]

    @property
    def texts(self) -> List[str]:
        """Just the memory texts, in rank order."""
        return [memory.text for memory in self.memories]


@dataclass
class ProfileResult:
    """Outcome of the profile endpoint (spec § 3.3).

    ``cache_status`` is ``"hit"`` / ``"miss"`` when the engine reports it and
    ``None`` when it does not — the current API returns the same body for a
    cached and a freshly generated profile, so callers that need cache signal
    fall back to :attr:`latency_us` (spec § 3.4).
    """

    user_id: str
    stable_facts: List[str] = field(default_factory=list)
    recent_activity: List[str] = field(default_factory=list)
    profile_timestamp: Optional[float] = None
    cache_status: Optional[str] = None
    latency_us: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StorageMetrics:
    """Outcome of ``GET /metrics`` (spec § 3.3).

    The engine reports sizes in kilobytes split across SQLite and LanceDB; the
    client normalises them into a single ``memory_size_bytes`` total.
    """

    total_facts: int = 0
    active_facts: int = 0
    inactive_facts: int = 0
    memory_size_bytes: int = 0
    latency_us: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


class CheckStatus(str, Enum):
    """Tri-state outcome of one expected-outcome check (spec § 3.6).

    ``SKIPPED`` is not a pass and not a failure: the check could not be
    decided, because a validation method was unavailable (no engine fact id to
    look up in ``memory.db``), because the expectation was marked
    unsatisfiable by the Task 6 TTL solver, or because the outcome does not
    apply to this scenario. A run passes when nothing FAILED.
    """

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ValidationResult:
    """Outcome of a single expected-outcome check (spec § 3.6).

    ``passed`` is False only for :attr:`CheckStatus.FAILED`; a skipped check
    reports ``passed=True`` with ``status="skipped"`` so that callers which
    only look at the boolean never turn "undecidable" into "broken".
    """

    scenario_id: str
    outcome: str
    passed: bool
    details: Dict[str, Any] = field(default_factory=dict)
    status: str = CheckStatus.PASSED.value

    @property
    def skipped(self) -> bool:
        return self.status == CheckStatus.SKIPPED.value

    @property
    def failed(self) -> bool:
        return self.status == CheckStatus.FAILED.value


@dataclass
class CrossValidationError:
    """Two validation methods disagreed about one fact (spec § 3.6 Method 4)."""

    scenario_id: str
    fact_id: str
    user_id: str
    message: str
    #: ``{method: {available, exists, active, ...}}`` for all four methods.
    states: Dict[str, Any] = field(default_factory=dict)
    #: Human-readable ``"db says active, audit says inactive"`` lines.
    diffs: List[str] = field(default_factory=list)


@dataclass
class ScenarioValidation:
    """Every check run against one scenario (spec § 6.2)."""

    scenario_id: str
    category: str
    user_ids: List[str] = field(default_factory=list)
    passed: bool = True
    results: List[ValidationResult] = field(default_factory=list)
    #: One entry per fact: the four method states plus whether they agreed.
    cross_validation: List[Dict[str, Any]] = field(default_factory=list)
    #: NULL user ids, absent audit trails, unresolvable fact ids, …
    missing_data: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def failed_outcomes(self) -> List[str]:
        return [r.outcome for r in self.results if r.failed]

    @property
    def skipped_outcomes(self) -> List[str]:
        return [r.outcome for r in self.results if r.skipped]


@dataclass
class ValidationReport:
    """Result of one ValidatorService pass over a run (spec § 3.6, § 6.2).

    Iterating a report yields its flat :class:`ValidationResult` list, so it
    can be dropped straight into ``RunResult.validation_results``.
    """

    run_number: int
    passed: bool = True
    scenarios: Dict[str, ScenarioValidation] = field(default_factory=dict)
    cross_validation_errors: List[CrossValidationError] = field(default_factory=list)
    missing_data: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: Optional[float] = None

    @property
    def results(self) -> List[ValidationResult]:
        """Every check from every scenario, in scenario order."""
        return [r for sv in self.scenarios.values() for r in sv.results]

    def __iter__(self) -> Iterator[ValidationResult]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    @property
    def checks_total(self) -> int:
        return len(self.results)

    @property
    def checks_passed(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.PASSED.value)

    @property
    def checks_failed(self) -> int:
        return sum(1 for r in self.results if r.failed)

    @property
    def checks_skipped(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    @property
    def failed_scenarios(self) -> List[str]:
        return [sid for sid, sv in self.scenarios.items() if not sv.passed]

    def summary(self) -> Dict[str, Any]:
        """Flat counts for the dashboard and the run report."""
        return {
            "run_number": self.run_number,
            "passed": self.passed,
            "scenarios": len(self.scenarios),
            "scenarios_failed": len(self.failed_scenarios),
            "checks_total": self.checks_total,
            "checks_passed": self.checks_passed,
            "checks_failed": self.checks_failed,
            "checks_skipped": self.checks_skipped,
            "cross_validation_errors": len(self.cross_validation_errors),
            "missing_data": len(self.missing_data),
        }


class ErrorClass(str, Enum):
    """How the SimulationRunner classified a failed engine call (spec § 3.2).

    The runner never retries and never aborts a run on one of these: it records
    the classification and plays the next event.
    """

    #: HTTP 4xx — the request itself is wrong; replaying it will fail again.
    PERMANENT = "permanent"
    #: HTTP 5xx — the engine is unhealthy; a later call may still succeed.
    TEMPORARY = "temporary"
    #: No response inside the client timeout.
    TIMEOUT = "timeout"
    #: Connection/transport failure or an unparseable response body.
    TRANSPORT = "transport"
    #: Anything the runner did not expect, kept so a run can never crash.
    UNEXPECTED = "unexpected"


@dataclass
class RunError:
    """One failed event during playback (spec § 3.2 resilience).

    ``error`` holds the originating exception — an
    :class:`~simulation.engine_client.EngineClientError` for every
    classification but :attr:`ErrorClass.UNEXPECTED`. It is typed as
    ``Exception`` here so :mod:`simulation.models` stays free of an import
    cycle with :mod:`simulation.engine_client`.
    """

    timestamp: float
    scenario_id: str
    fact_index: int
    user_id: str
    classification: str
    message: str
    endpoint: Optional[str] = None
    status_code: Optional[int] = None
    error: Optional[Exception] = None


@dataclass
class RunResult:
    """Report for one simulation run (spec § 3.2 step 4).

    ``facts_count`` counts the events the runner actually played — ingests plus
    profile reads — which is ``events_played``; ``events_skipped`` counts facts
    scheduled past the end of the run.
    """

    run_number: int
    duration_seconds: float
    scenarios_count: int
    facts_count: int = 0
    errors: List[RunError] = field(default_factory=list)
    status: str = RunStatus.IN_PROGRESS.value

    # -- timing / progress -------------------------------------------------
    start_time: float = 0.0
    end_time: Optional[float] = None
    elapsed_seconds: float = 0.0
    events_total: int = 0
    events_played: int = 0
    events_skipped: int = 0

    # -- playback breakdown ------------------------------------------------
    user_count: int = 0
    ingested_count: int = 0
    profile_read_count: int = 0
    contradiction_count: int = 0
    #: ``{scenario_id: [engine fact_id, ...]}`` in playback order.
    fact_ids: Dict[str, List[str]] = field(default_factory=dict)
    #: One record per ingested fact, in playback order: ``scenario_id``,
    #: ``fact_index``, ``fact_id``, ``fact_id_source``, ``user_id``, ``text``,
    #: ``fact_type``, ``ttl_seconds``, ``contradicts_scenario``,
    #: ``contradicts_fact_id``, ``scheduled_time``. The ValidatorService
    #: (Task 7) needs the fact-index → engine-fact-id mapping, which
    #: :attr:`fact_ids` alone cannot give it once a fact fails to ingest.
    fact_records: List[Dict[str, Any]] = field(default_factory=list)
    #: One record per ``profile_read`` fact: expected vs observed cache status.
    cache_checks: List[Dict[str, Any]] = field(default_factory=list)

    # -- post-run ----------------------------------------------------------
    metrics: Dict[str, Any] = field(default_factory=dict)
    validation_results: List[ValidationResult] = field(default_factory=list)
    #: The full :class:`ValidationReport` when the ValidatorService returned
    #: one (spec § 3.6); ``None`` when validation was skipped.
    validation_report: Optional["ValidationReport"] = None
    #: Profile cache TTL the engine needs for the run's cache expectations to
    #: hold (spec § 5.1 C / ``cache_002``); ``None`` when the default suffices.
    profile_cache_ttl_seconds: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    @property
    def engine_errors(self) -> List[Exception]:
        """The underlying exceptions behind :attr:`errors`, in order."""
        return [item.error for item in self.errors if item.error is not None]

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def succeeded(self) -> bool:
        """True when the run completed and every played event succeeded."""
        return self.status == RunStatus.COMPLETED.value and not self.errors
