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
    FactType,
    MonitoringEvent,
    MonitoringEventType,
    RunMetadata,
    RunStatus,
    Scenario,
    ScenarioCategory,
    ScenarioFact,
    ValidationResult,
)
from .audit_logger import (
    VALID_EVENT_TYPES,
    VALID_RUN_STATUSES,
    VALID_SOURCES,
    AuditLogger,
)
from .engine_client import EngineClient
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
from .simulation_runner import RUN_DURATIONS_SECONDS, SimulationRunner
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
    "FactType",
    "MonitoringEvent",
    "MonitoringEventType",
    "RunMetadata",
    "RunStatus",
    "Scenario",
    "ScenarioCategory",
    "ScenarioFact",
    "ValidationResult",
    "AuditLogger",
    "VALID_EVENT_TYPES",
    "VALID_RUN_STATUSES",
    "VALID_SOURCES",
    "EngineClient",
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
    "ValidatorService",
    "__version__",
]
