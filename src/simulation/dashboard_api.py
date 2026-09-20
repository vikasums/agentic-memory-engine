"""Dashboard API — FastAPI endpoints for simulation monitoring (spec § 3.7 + design § 4).

Provides real-time monitoring endpoints for the React dashboard:
  - Simulation control (start, stop, status polling)
  - User state queries (facts, memory counts, profile cache status)
  - Audit log viewer with filtering
  - Validation results and summary
  - Aggregated metrics

The API manages a single active run at a time (``current_run``); calling /start
spawns the SimulationRunner in a background task and returns a run_id. Polling
/status tracks progress in 500ms intervals until completion.

CORS is enabled for localhost:3000 (React frontend).
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Sequence

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import settings
from .audit_logger import AuditLogger
from .engine_client import EngineClient
from .models import (
    AuditEventType,
    MonitoringEventType,
    RunResult,
    Scenario,
    ScenarioCategory,
)
from .monitoring_service import MonitoringService
from .scenario_generator import ScenarioGenerator
from .simulation_runner import RUN_DURATIONS_SECONDS, SimulationRunner
from .validator_service import ValidatorService

logger = logging.getLogger(__name__)

# Default user IDs for the dashboard (5 users per spec)
DEFAULT_DASHBOARD_USERS = [f"user_{i}" for i in range(1, 6)]


# =====================================================================
# Pydantic Response Models
# =====================================================================


class ProgressInfo(BaseModel):
    """Progress snapshot during a run."""

    events_played: int
    events_skipped: int
    errors: int
    percent_complete: float
    elapsed_seconds: float
    remaining_seconds: float
    last_scenario_id: Optional[str] = None


class StatusResponse(BaseModel):
    """Response for GET /simulate/{run_id}/status."""

    run_id: str
    run_number: int
    status: str  # "in_progress" | "completed" | "failed"
    progress: ProgressInfo
    duration_seconds: float
    events_total: int


class FactResponse(BaseModel):
    """A fact in a user's memory, as returned by GET /users/{user_id}/facts."""

    text: str
    timestamp: float
    is_active: bool
    contradictions: List[str] = Field(default_factory=list)


class MemoryCountResponse(BaseModel):
    """Response for GET /users/{user_id}/memory-count."""

    active: int
    inactive: int
    total: int


class ProfileCacheResponse(BaseModel):
    """Response for GET /users/{user_id}/profile-cache."""

    status: str  # "hit" | "miss" | "ttl"
    ttl_remaining_seconds: Optional[float] = None
    cache_timestamp: Optional[float] = None


class EventResponse(BaseModel):
    """An event in the audit log, returned by GET /audit/events."""

    event_id: int
    run_number: int
    timestamp: float
    user_id: str
    fact_id: str
    event_type: str
    before_state: Optional[Dict[str, Any]] = None
    after_state: Optional[Dict[str, Any]] = None
    source: str


class ValidationResultResponse(BaseModel):
    """One expected-outcome check result."""

    scenario_id: str
    outcome: str
    status: str  # "passed" | "failed" | "skipped"
    passed: bool
    details: Dict[str, Any] = Field(default_factory=dict)


class ValidationSummaryResponse(BaseModel):
    """Summary of validation results."""

    total_scenarios: int
    passed: int
    failed: int
    all_methods_agree: bool
    checks_total: int
    checks_passed: int
    checks_failed: int
    checks_skipped: int
    cross_validation_errors: int


class MetricsResponse(BaseModel):
    """Aggregated run metrics."""

    cache_hit_rate: float
    avg_latency_ms: float
    api_calls: int
    facts: Dict[str, int]
    storage_mb: Dict[str, float]


class StartRunRequest(BaseModel):
    """Request body for POST /simulate/start."""

    duration_seconds: int = Field(60, ge=0)
    scenarios: Optional[List[Scenario]] = None


class StartRunResponse(BaseModel):
    """Response for POST /simulate/start."""

    run_id: str
    run_number: int
    status: str


# =====================================================================
# Global state and helpers
# =====================================================================

class RunState:
    """Tracks one active simulation run."""

    def __init__(
        self,
        run_id: str,
        run_number: int,
        task: asyncio.Task[RunResult],
        runner: SimulationRunner,
    ) -> None:
        self.run_id = run_id
        self.run_number = run_number
        self.task = task
        self.runner = runner
        self.result: Optional[RunResult] = None
        self.start_time = time.time()


# Global state
current_run: Optional[RunState] = None
engine_client: Optional[EngineClient] = None
monitoring_service: Optional[MonitoringService] = None
audit_logger: Optional[AuditLogger] = None
validator_service: Optional[ValidatorService] = None


def _status_from_runner(run_state: RunState) -> str:
    """Derive run status from task and runner state."""
    if run_state.task.done():
        if run_state.result is not None:
            return run_state.result.status
        try:
            run_state.result = run_state.task.result()
            return run_state.result.status
        except Exception:
            return "failed"
    return "in_progress"


# =====================================================================
# FastAPI lifespan
# =====================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore
    """Manage services during app startup and shutdown.

    Services are initialized with settings from config module (environment
    variables or .env file). See Task 11: Configuration & Environment Setup.
    """
    global engine_client, monitoring_service, audit_logger, validator_service

    # Startup
    logger.info(
        "Dashboard API starting with settings: "
        f"api_url={settings.engine_api_base_url}, "
        f"ttl={settings.profile_cache_ttl_seconds}s, "
        f"debug={settings.debug}"
    )

    engine_client = EngineClient(base_url=settings.engine_api_base_url)
    audit_logger = AuditLogger(db_path=settings.audit_db_path)
    monitoring_service = MonitoringService(audit_logger=audit_logger)
    validator_service = ValidatorService(
        engine_client=engine_client,
        audit_logger=audit_logger,
    )

    logger.info("Dashboard API services initialized")
    yield

    # Shutdown
    logger.info("Dashboard API services shutting down")
    if audit_logger:
        audit_logger.close()


# =====================================================================
# FastAPI app setup
# =====================================================================

app = FastAPI(title="Dashboard API", version="0.1.0", lifespan=lifespan)

# CORS for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================================================================
# Simulation Control Endpoints
# =====================================================================


@app.post("/simulate/start", response_model=StartRunResponse)
async def start_simulation(request: StartRunRequest) -> StartRunResponse:
    """Start a new simulation run.

    Returns a run_id for polling via GET /simulate/{run_id}/status.

    Query params:
      - duration_seconds: 60, 120, 240, or 360 (default 60)
      - scenarios: optional list of Scenario objects (JSON)
    """
    global current_run

    if current_run is not None and not current_run.task.done():
        raise HTTPException(status_code=400, detail="A run is already in progress")

    if engine_client is None:
        raise HTTPException(status_code=500, detail="EngineClient not initialized")

    # Create runner with configured profile cache TTL from settings.
    # This enables cache_002 scenario to observe TTL misses by setting a
    # lower TTL in the environment or .env file (spec § 5.1 C, Task 11).
    # Also provide a ScenarioGenerator so scenarios can be auto-generated when
    # not explicitly provided (task 10 integration tests).
    runner = SimulationRunner(
        engine_client=engine_client,
        monitoring_service=monitoring_service,
        audit_logger=audit_logger,
        validator=validator_service,
        generator=ScenarioGenerator(),
        duration_seconds=float(request.duration_seconds),
        run_number=1,
        configured_profile_cache_ttl_seconds=settings.profile_cache_ttl_seconds,
        wait_out_duration=True,
    )

    # Spawn background task
    run_id = str(uuid.uuid4())[:8]
    task = asyncio.create_task(
        runner.run(
            scenarios=request.scenarios,
            duration_seconds=float(request.duration_seconds),
            run_number=1,
        )
    )

    logger.info(f"Starting run {run_id}, duration {request.duration_seconds}s")
    current_run = RunState(
        run_id=run_id,
        run_number=1,
        task=task,
        runner=runner,
    )

    return StartRunResponse(
        run_id=run_id,
        run_number=1,
        status="in_progress",
    )


@app.post("/simulate/{run_id}/stop")
async def stop_simulation(run_id: str) -> Dict[str, Any]:
    """Stop an in-progress simulation run."""
    global current_run

    if current_run is None or current_run.run_id != run_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    if current_run.task.done():
        raise HTTPException(status_code=400, detail="Run is not in progress")

    current_run.task.cancel()
    try:
        await current_run.task
    except asyncio.CancelledError:
        pass

    return {"status": "stopped", "run_id": run_id}


@app.get("/simulate/{run_id}/status", response_model=StatusResponse)
async def get_status(run_id: str) -> StatusResponse:
    """Poll simulation run status and progress.

    Called every 500ms from the React frontend during playback.
    """
    global current_run

    if current_run is None or current_run.run_id != run_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    progress = current_run.runner.progress
    status = _status_from_runner(current_run)

    return StatusResponse(
        run_id=run_id,
        run_number=current_run.run_number,
        status=status,
        progress=ProgressInfo(
            events_played=progress.get("events_played", 0),
            events_skipped=progress.get("events_skipped", 0),
            errors=progress.get("errors", 0),
            percent_complete=progress.get("percent_complete", 0.0),
            elapsed_seconds=progress.get("elapsed_seconds", 0.0),
            remaining_seconds=progress.get("remaining_seconds", 0.0),
            last_scenario_id=progress.get("last_scenario_id"),
        ),
        duration_seconds=float(current_run.runner.duration_seconds),
        events_total=progress.get("events_total", 0),
    )


# =====================================================================
# User State Endpoints
# =====================================================================


@app.get("/users/{user_id}/facts", response_model=List[FactResponse])
async def get_user_facts(user_id: str) -> List[FactResponse]:
    """Return all facts for a user."""
    if audit_logger is None:
        return []

    events = audit_logger.get_events(user_id=user_id)
    facts: Dict[str, Dict[str, Any]] = {}

    for event in events:
        fact_id = event.fact_id
        if fact_id not in facts:
            facts[fact_id] = {
                "text": "",
                "timestamp": event.timestamp,
                "is_active": False,
                "contradictions": [],
            }

        if event.event_type in (AuditEventType.CREATED.value, AuditEventType.UPDATED.value):
            facts[fact_id]["is_active"] = True
            if event.after_state and "text" in event.after_state:
                facts[fact_id]["text"] = event.after_state.get("text", "")
        elif event.event_type in (AuditEventType.DEACTIVATED.value, AuditEventType.EXPIRED.value):
            facts[fact_id]["is_active"] = False

        # Extract contradictions from after_state if present
        if event.event_type == AuditEventType.UPDATED.value and event.after_state:
            if "contradicts_fact_id" in event.after_state:
                facts[fact_id]["contradictions"].append(event.after_state["contradicts_fact_id"])

    return [
        FactResponse(
            text=f["text"],
            timestamp=f["timestamp"],
            is_active=f["is_active"],
            contradictions=f["contradictions"],
        )
        for f in facts.values()
    ]


@app.get("/users/{user_id}/memory-count", response_model=MemoryCountResponse)
async def get_memory_count(user_id: str) -> MemoryCountResponse:
    """Return active/inactive memory counts for a user."""
    if monitoring_service is None:
        return MemoryCountResponse(active=0, inactive=0, total=0)

    metrics = monitoring_service.get_metrics()
    active = metrics.get("active_facts_by_user", {}).get(user_id, 0)
    inactive = metrics.get("inactive_facts_by_user", {}).get(user_id, 0)

    return MemoryCountResponse(active=active, inactive=inactive, total=active + inactive)


@app.get("/users/{user_id}/profile-cache", response_model=ProfileCacheResponse)
async def get_profile_cache(user_id: str) -> ProfileCacheResponse:
    """Return profile cache status for a user."""
    if monitoring_service is None:
        return ProfileCacheResponse(status="unknown")

    metrics = monitoring_service.get_metrics()
    cache_hit_rate = metrics.get("cache_hit_rate", 0.0)

    # Simple heuristic: if hit rate > 0.7, assume cache is hitting
    status = "hit" if cache_hit_rate > 0.7 else "miss"

    return ProfileCacheResponse(
        status=status,
        ttl_remaining_seconds=None,
        cache_timestamp=time.time(),
    )


# =====================================================================
# Audit Log Endpoints
# =====================================================================


@app.get("/audit/events", response_model=List[EventResponse])
async def get_audit_events(
    user_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    fact_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> List[EventResponse]:
    """Query audit events with optional filtering.

    Query params:
      - user_id: filter by user
      - event_type: filter by event type (created, updated, deactivated, expired)
      - fact_id: filter by fact ID
      - limit: max results (default 100, max 1000)
    """
    if audit_logger is None:
        return []

    events = audit_logger.get_events(
        user_id=user_id,
        event_type=event_type,
        fact_id=fact_id,
        limit=limit,
    )

    return [
        EventResponse(
            event_id=event.event_id or 0,
            run_number=event.run_number,
            timestamp=event.timestamp,
            user_id=event.user_id,
            fact_id=event.fact_id,
            event_type=event.event_type,
            before_state=event.before_state,
            after_state=event.after_state,
            source=event.source,
        )
        for event in events
    ]


# =====================================================================
# Validation Endpoints
# =====================================================================


@app.get("/validation/results", response_model=List[ValidationResultResponse])
async def get_validation_results() -> List[ValidationResultResponse]:
    """Return validation results from the latest completed run."""
    global current_run

    if current_run is None or not current_run.task.done():
        return []

    if current_run.result is None:
        try:
            current_run.result = current_run.task.result()
        except Exception:
            return []

    results = []
    if current_run.result.validation_report:
        for result in current_run.result.validation_report.results:
            results.append(
                ValidationResultResponse(
                    scenario_id=result.scenario_id,
                    outcome=result.outcome,
                    status=result.status,
                    passed=result.passed,
                    details=result.details,
                )
            )

    return results


@app.get("/validation/summary", response_model=ValidationSummaryResponse)
async def get_validation_summary() -> ValidationSummaryResponse:
    """Return validation summary from the latest completed run."""
    global current_run

    if current_run is None or not current_run.task.done():
        raise HTTPException(status_code=400, detail="No completed run")

    if current_run.result is None:
        try:
            current_run.result = current_run.task.result()
        except Exception:
            raise HTTPException(status_code=500, detail="Could not retrieve run result")

    if not current_run.result.validation_report:
        raise HTTPException(status_code=400, detail="No validation report")

    report = current_run.result.validation_report
    return ValidationSummaryResponse(
        total_scenarios=len(report.scenarios),
        passed=len([s for s in report.scenarios.values() if s.passed]),
        failed=len([s for s in report.scenarios.values() if not s.passed]),
        all_methods_agree=len(report.cross_validation_errors) == 0,
        checks_total=report.checks_total,
        checks_passed=report.checks_passed,
        checks_failed=report.checks_failed,
        checks_skipped=report.checks_skipped,
        cross_validation_errors=len(report.cross_validation_errors),
    )


# =====================================================================
# Metrics Endpoints
# =====================================================================


@app.get("/metrics", response_model=MetricsResponse)
async def get_metrics() -> MetricsResponse:
    """Return aggregated run metrics."""
    if monitoring_service is None:
        return MetricsResponse(
            cache_hit_rate=0.0,
            avg_latency_ms=0.0,
            api_calls=0,
            facts={},
            storage_mb={},
        )

    metrics = monitoring_service.get_metrics()

    return MetricsResponse(
        cache_hit_rate=metrics.get("cache_hit_rate", 0.0),
        avg_latency_ms=metrics.get("avg_latency_us", 0) / 1000.0,
        api_calls=metrics.get("latency_count", 0),
        facts={
            "created": metrics.get("fact_ingested_count", 0),
            "updated": metrics.get("contradiction_count", 0),
            "deactivated": 0,
            "expired": metrics.get("expiry_count", 0),
        },
        storage_mb={
            "memory_db": 0.0,
            "audit_log_db": 0.0,
        },
    )


# =====================================================================
# Health check
# =====================================================================


@app.get("/health")
async def health_check() -> Dict[str, str]:
    """Health check endpoint."""
    return {
        "status": "ok",
        "engine_client": "ready" if engine_client else "not initialized",
        "audit_logger": "ready" if audit_logger else "not initialized",
        "monitoring_service": "ready" if monitoring_service else "not initialized",
        "validator_service": "ready" if validator_service else "not initialized",
    }
