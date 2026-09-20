"""Simulation & monitoring system for the agentic memory engine.

Implements the design in ``docs/SIMULATION_SYSTEM_DESIGN.md``: deterministic
scenario playback against the memory engine API, a persistent audit trail in
``audit_log.db``, real-time monitoring and cross-validated verification.

Phase 1 is deterministic — no LLM calls are made from this package.
"""

from .database import (
    DEFAULT_AUDIT_DB_PATH,
    connect,
    init_audit_db,
    migrate_audit_db,
    table_exists,
)
from .models import (
    AuditEvent,
    AuditEventType,
    AuditSource,
    ErrorClass,
    FactType,
    IngestionResult,
    MonitoringEvent,
    MonitoringEventType,
    ProfileResult,
    RetrievalResult,
    RetrievedFact,
    RunError,
    RunMetadata,
    RunResult,
    RunStatus,
    Scenario,
    ScenarioCategory,
    ScenarioFact,
    StorageMetrics,
    ValidationResult,
)
from .audit_logger import (
    VALID_EVENT_TYPES,
    VALID_RUN_STATUSES,
    VALID_SOURCES,
    AuditLogger,
)
from .engine_client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    STATUS_INVALID_RESPONSE,
    STATUS_TIMEOUT,
    STATUS_TRANSPORT_ERROR,
    EngineClient,
    EngineClientError,
)
from .monitoring_service import MonitoringService
from .scenario_generator import (
    CATEGORY_COUNTS,
    DEFAULT_MAX_DURATION_SECONDS,
    DEFAULT_USER_IDS,
    EXPECTED_OUTCOME_VOCABULARY,
    MIN_SCENARIO_COUNT,
    ScenarioGenerator,
    fact_to_dict,
    scenario_to_dict,
    scenarios_to_json,
)
from .simulation_runner import (
    REQUIRED_CACHE_TTL_KEY,
    RUN_DURATIONS_SECONDS,
    SimulationRunner,
    build_event_queue,
    classify_engine_error,
)
from .validator_service import ValidatorService

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_AUDIT_DB_PATH",
    "connect",
    "init_audit_db",
    "migrate_audit_db",
    "table_exists",
    "AuditEvent",
    "AuditEventType",
    "AuditSource",
    "ErrorClass",
    "FactType",
    "IngestionResult",
    "MonitoringEvent",
    "MonitoringEventType",
    "ProfileResult",
    "RetrievalResult",
    "RetrievedFact",
    "RunError",
    "RunMetadata",
    "RunResult",
    "RunStatus",
    "Scenario",
    "ScenarioCategory",
    "ScenarioFact",
    "StorageMetrics",
    "ValidationResult",
    "AuditLogger",
    "VALID_EVENT_TYPES",
    "VALID_RUN_STATUSES",
    "VALID_SOURCES",
    "EngineClient",
    "EngineClientError",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "STATUS_INVALID_RESPONSE",
    "STATUS_TIMEOUT",
    "STATUS_TRANSPORT_ERROR",
    "MonitoringService",
    "ScenarioGenerator",
    "CATEGORY_COUNTS",
    "DEFAULT_MAX_DURATION_SECONDS",
    "DEFAULT_USER_IDS",
    "EXPECTED_OUTCOME_VOCABULARY",
    "MIN_SCENARIO_COUNT",
    "fact_to_dict",
    "scenario_to_dict",
    "scenarios_to_json",
    "SimulationRunner",
    "RUN_DURATIONS_SECONDS",
    "REQUIRED_CACHE_TTL_KEY",
    "build_event_queue",
    "classify_engine_error",
    "ValidatorService",
    "__version__",
]
