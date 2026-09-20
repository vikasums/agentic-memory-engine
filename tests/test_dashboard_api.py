"""Tests for the Dashboard API (Task 8).

Tests cover:
  - Simulation control (start, stop, status polling)
  - User state queries
  - Audit log filtering
  - Validation results
  - Metrics aggregation
  - Error handling and edge cases
"""

import asyncio
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

import sys
import os

# Add src to path so we can import simulation package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from simulation.dashboard_api import (
    app,
    RunState,
    current_run,
    engine_client,
    monitoring_service,
    audit_logger,
    validator_service,
    _status_from_runner,
)
from simulation.models import (
    AuditEvent,
    AuditEventType,
    AuditSource,
    RunResult,
    RunStatus,
    ValidationReport,
    ScenarioValidation,
    ValidationResult,
    CheckStatus,
)
from simulation.monitoring_service import MonitoringService
from simulation.audit_logger import AuditLogger
from simulation.engine_client import EngineClient
from simulation.validator_service import ValidatorService
from simulation.simulation_runner import SimulationRunner


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def client():
    """Test client for the FastAPI app."""
    return TestClient(app)


@pytest.fixture
def mock_engine_client():
    """Mock EngineClient."""
    client = MagicMock(spec=EngineClient)
    client.ingest = AsyncMock()
    client.get_profile = AsyncMock()
    return client


@pytest.fixture
def mock_monitoring_service():
    """Mock MonitoringService."""
    service = MagicMock(spec=MonitoringService)
    service.log_event = MagicMock()
    service.get_metrics = MagicMock(
        return_value={
            "cache_hit_rate": 0.75,
            "avg_latency_us": 45300,
            "latency_count": 100,
            "fact_ingested_count": 50,
            "contradiction_count": 10,
            "expiry_count": 2,
            "active_facts_by_user": {"user_1": 10, "user_2": 8},
            "inactive_facts_by_user": {"user_1": 5, "user_2": 3},
        }
    )
    return service


@pytest.fixture
def mock_audit_logger():
    """Mock AuditLogger."""
    logger = MagicMock(spec=AuditLogger)
    logger.start_run = MagicMock()
    logger.end_run = MagicMock()
    logger.write_event = MagicMock()
    logger.get_events = MagicMock(return_value=[])
    return logger


@pytest.fixture
def mock_validator_service():
    """Mock ValidatorService."""
    validator = MagicMock(spec=ValidatorService)
    return validator


@pytest.fixture
def mock_runner(mock_engine_client, mock_monitoring_service, mock_audit_logger):
    """Mock SimulationRunner."""
    runner = MagicMock(spec=SimulationRunner)
    runner.client = mock_engine_client
    runner.monitor = mock_monitoring_service
    runner.auditor = mock_audit_logger
    runner.duration_seconds = 60.0
    runner.progress = {
        "events_total": 100,
        "events_played": 0,
        "events_skipped": 0,
        "errors": 0,
        "percent_complete": 0.0,
        "elapsed_seconds": 0.0,
        "remaining_seconds": 60.0,
        "last_scenario_id": None,
    }
    runner.run = AsyncMock()
    return runner


# =====================================================================
# Simulation Control Tests
# =====================================================================


@pytest.mark.asyncio
async def test_start_simulation_success(client, mock_runner):
    """Test POST /simulate/start with valid request."""
    with patch("simulation.dashboard_api.engine_client", mock_runner.client):
        with patch("simulation.dashboard_api.monitoring_service", mock_runner.monitor):
            with patch("simulation.dashboard_api.audit_logger", mock_runner.auditor):
                with patch("simulation.dashboard_api.validator_service", None):
                    with patch(
                        "simulation.dashboard_api.SimulationRunner",
                        return_value=mock_runner,
                    ):
                        response = client.post(
                            "/simulate/start",
                            json={
                                "duration_seconds": 60,
                                "scenarios": None,
                            },
                        )

                        assert response.status_code == 200
                        data = response.json()
                        assert "run_id" in data
                        assert data["run_number"] == 1
                        assert data["status"] == "in_progress"


@pytest.mark.asyncio
async def test_start_simulation_already_running(client, mock_runner):
    """Test POST /simulate/start when a run is already in progress."""
    import simulation.dashboard_api

    # Set up a running task
    task = asyncio.create_task(asyncio.sleep(100))
    run_state = RunState(
        run_id="test_run",
        run_number=1,
        task=task,
        runner=mock_runner,
    )
    simulation.dashboard_api.current_run = run_state

    try:
        response = client.post(
            "/simulate/start",
            json={
                "duration_seconds": 60,
                "scenarios": None,
            },
        )

        assert response.status_code == 400
        assert "already in progress" in response.json()["detail"]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        simulation.dashboard_api.current_run = None


@pytest.mark.asyncio
async def test_start_simulation_with_duration_options(client, mock_runner):
    """Test /simulate/start with different duration options."""
    with patch("simulation.dashboard_api.engine_client", mock_runner.client):
        with patch("simulation.dashboard_api.monitoring_service", mock_runner.monitor):
            with patch("simulation.dashboard_api.audit_logger", mock_runner.auditor):
                with patch("simulation.dashboard_api.validator_service", None):
                    with patch(
                        "simulation.dashboard_api.SimulationRunner",
                        return_value=mock_runner,
                    ):
                        for duration in [60, 120, 240, 360]:
                            response = client.post(
                                "/simulate/start",
                                json={
                                    "duration_seconds": duration,
                                    "scenarios": None,
                                },
                            )

                            assert response.status_code == 200
                            data = response.json()
                            assert data["status"] == "in_progress"

                            # Clean up the current run
                            import simulation.dashboard_api
                            if simulation.dashboard_api.current_run:
                                simulation.dashboard_api.current_run.task.cancel()
                                try:
                                    await simulation.dashboard_api.current_run.task
                                except asyncio.CancelledError:
                                    pass
                                simulation.dashboard_api.current_run = None


@pytest.mark.asyncio
async def test_get_status_success(client, mock_runner):
    """Test GET /simulate/{run_id}/status."""
    import simulation.dashboard_api

    # Create a completed task
    async def dummy_run():
        result = RunResult(
            run_number=1,
            duration_seconds=60.0,
            scenarios_count=10,
            events_total=50,
            status=RunStatus.COMPLETED.value,
        )
        return result

    task = asyncio.create_task(dummy_run())
    run_state = RunState(
        run_id="test_run_123",
        run_number=1,
        task=task,
        runner=mock_runner,
    )
    simulation.dashboard_api.current_run = run_state

    try:
        response = client.get("/simulate/test_run_123/status")

        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == "test_run_123"
        assert data["run_number"] == 1
        assert "progress" in data
        assert "duration_seconds" in data
    finally:
        await task
        simulation.dashboard_api.current_run = None


@pytest.mark.asyncio
async def test_get_status_not_found(client):
    """Test GET /simulate/{run_id}/status with non-existent run."""
    import simulation.dashboard_api

    simulation.dashboard_api.current_run = None

    response = client.get("/simulate/nonexistent_run/status")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_stop_simulation_not_found(client):
    """Test POST /simulate/{run_id}/stop with non-existent run."""
    import simulation.dashboard_api

    simulation.dashboard_api.current_run = None

    response = client.post("/simulate/nonexistent/stop")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


# =====================================================================
# User State Tests
# =====================================================================


@pytest.mark.asyncio
async def test_get_user_facts(client, mock_audit_logger):
    """Test GET /users/{user_id}/facts."""
    import simulation.dashboard_api

    # Mock audit events
    events = [
        AuditEvent(
            run_number=1,
            timestamp=time.time(),
            user_id="user_1",
            fact_id="fact_1",
            event_type=AuditEventType.CREATED.value,
            after_state={"text": "Test fact", "active": True},
            source=AuditSource.SCENARIO_PLAYBACK.value,
        ),
    ]
    mock_audit_logger.get_events.return_value = events
    simulation.dashboard_api.audit_logger = mock_audit_logger

    response = client.get("/users/user_1/facts")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["text"] == "Test fact"
    assert data[0]["is_active"] is True


@pytest.mark.asyncio
async def test_get_memory_count(client, mock_monitoring_service):
    """Test GET /users/{user_id}/memory-count."""
    import simulation.dashboard_api

    simulation.dashboard_api.monitoring_service = mock_monitoring_service

    response = client.get("/users/user_1/memory-count")

    assert response.status_code == 200
    data = response.json()
    assert data["active"] == 10
    assert data["inactive"] == 5
    assert data["total"] == 15


@pytest.mark.asyncio
async def test_get_profile_cache_hit(client, mock_monitoring_service):
    """Test GET /users/{user_id}/profile-cache with hit status."""
    import simulation.dashboard_api

    mock_monitoring_service.get_metrics.return_value = {
        "cache_hit_rate": 0.85,
        "active_facts_by_user": {"user_1": 10},
        "inactive_facts_by_user": {"user_1": 5},
    }
    simulation.dashboard_api.monitoring_service = mock_monitoring_service

    response = client.get("/users/user_1/profile-cache")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "hit"


@pytest.mark.asyncio
async def test_get_profile_cache_miss(client, mock_monitoring_service):
    """Test GET /users/{user_id}/profile-cache with miss status."""
    import simulation.dashboard_api

    mock_monitoring_service.get_metrics.return_value = {
        "cache_hit_rate": 0.2,
        "active_facts_by_user": {"user_1": 10},
        "inactive_facts_by_user": {"user_1": 5},
    }
    simulation.dashboard_api.monitoring_service = mock_monitoring_service

    response = client.get("/users/user_1/profile-cache")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "miss"


# =====================================================================
# Audit Log Tests
# =====================================================================


@pytest.mark.asyncio
async def test_get_audit_events_no_filter(client, mock_audit_logger):
    """Test GET /audit/events with no filters."""
    import simulation.dashboard_api

    events = [
        AuditEvent(
            event_id=1,
            run_number=1,
            timestamp=time.time(),
            user_id="user_1",
            fact_id="fact_1",
            event_type=AuditEventType.CREATED.value,
            source=AuditSource.SCENARIO_PLAYBACK.value,
        ),
    ]
    mock_audit_logger.get_events.return_value = events
    simulation.dashboard_api.audit_logger = mock_audit_logger

    response = client.get("/audit/events")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["fact_id"] == "fact_1"


@pytest.mark.asyncio
async def test_get_audit_events_filter_by_user(client, mock_audit_logger):
    """Test GET /audit/events with user_id filter."""
    import simulation.dashboard_api

    events = [
        AuditEvent(
            event_id=1,
            run_number=1,
            timestamp=time.time(),
            user_id="user_1",
            fact_id="fact_1",
            event_type=AuditEventType.CREATED.value,
            source=AuditSource.SCENARIO_PLAYBACK.value,
        ),
    ]
    mock_audit_logger.get_events.return_value = events
    simulation.dashboard_api.audit_logger = mock_audit_logger

    response = client.get("/audit/events?user_id=user_1")

    assert response.status_code == 200
    mock_audit_logger.get_events.assert_called_with(
        user_id="user_1",
        event_type=None,
        fact_id=None,
        limit=100,
    )


@pytest.mark.asyncio
async def test_get_audit_events_filter_by_event_type(client, mock_audit_logger):
    """Test GET /audit/events with event_type filter."""
    import simulation.dashboard_api

    events = [
        AuditEvent(
            event_id=1,
            run_number=1,
            timestamp=time.time(),
            user_id="user_1",
            fact_id="fact_1",
            event_type=AuditEventType.CREATED.value,
            source=AuditSource.SCENARIO_PLAYBACK.value,
        ),
    ]
    mock_audit_logger.get_events.return_value = events
    simulation.dashboard_api.audit_logger = mock_audit_logger

    response = client.get("/audit/events?event_type=created")

    assert response.status_code == 200
    mock_audit_logger.get_events.assert_called_with(
        user_id=None,
        event_type="created",
        fact_id=None,
        limit=100,
    )


@pytest.mark.asyncio
async def test_get_audit_events_limit(client, mock_audit_logger):
    """Test GET /audit/events with custom limit."""
    import simulation.dashboard_api

    events = []
    mock_audit_logger.get_events.return_value = events
    simulation.dashboard_api.audit_logger = mock_audit_logger

    response = client.get("/audit/events?limit=50")

    assert response.status_code == 200
    mock_audit_logger.get_events.assert_called_with(
        user_id=None,
        event_type=None,
        fact_id=None,
        limit=50,
    )


# =====================================================================
# Validation Tests
# =====================================================================


@pytest.mark.asyncio
async def test_get_validation_results(client, mock_runner):
    """Test GET /validation/results."""
    import simulation.dashboard_api

    # Create a completed run with validation report
    report = ValidationReport(
        run_number=1,
        passed=True,
        scenarios={
            "scenario_1": ScenarioValidation(
                scenario_id="scenario_1",
                category="contradiction",
            ),
        },
    )

    result = RunResult(
        run_number=1,
        duration_seconds=60.0,
        scenarios_count=1,
        events_total=10,
        status=RunStatus.COMPLETED.value,
        validation_report=report,
    )

    async def dummy_run():
        return result

    task = asyncio.create_task(dummy_run())
    run_state = RunState(
        run_id="test_run_val",
        run_number=1,
        task=task,
        runner=mock_runner,
    )
    simulation.dashboard_api.current_run = run_state

    try:
        response = client.get("/validation/results")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    finally:
        await task
        simulation.dashboard_api.current_run = None


def test_get_validation_summary_no_run(client):
    """Test GET /validation/summary with no completed run."""
    import simulation.dashboard_api

    simulation.dashboard_api.current_run = None

    response = client.get("/validation/summary")

    assert response.status_code == 400
    assert "No completed run" in response.json()["detail"]


# =====================================================================
# Metrics Tests
# =====================================================================


@pytest.mark.asyncio
async def test_get_metrics(client, mock_monitoring_service):
    """Test GET /metrics."""
    import simulation.dashboard_api

    simulation.dashboard_api.monitoring_service = mock_monitoring_service

    response = client.get("/metrics")

    assert response.status_code == 200
    data = response.json()
    assert "cache_hit_rate" in data
    assert "avg_latency_ms" in data
    assert "api_calls" in data
    assert "facts" in data
    assert "storage_mb" in data
    assert data["cache_hit_rate"] == 0.75
    assert data["avg_latency_ms"] > 0  # 45300 us -> 45.3 ms
    assert data["api_calls"] == 100


# =====================================================================
# Health Check Tests
# =====================================================================


def test_health_check(client):
    """Test GET /health."""
    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "engine_client" in data
    assert "audit_logger" in data
    assert "monitoring_service" in data
    assert "validator_service" in data


# =====================================================================
# Helper Function Tests
# =====================================================================


@pytest.mark.asyncio
async def test_status_from_runner_in_progress(mock_runner):
    """Test _status_from_runner with in-progress task."""
    task = asyncio.create_task(asyncio.sleep(100))
    run_state = RunState(
        run_id="test",
        run_number=1,
        task=task,
        runner=mock_runner,
    )

    try:
        status = _status_from_runner(run_state)
        assert status == "in_progress"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_status_from_runner_completed(mock_runner):
    """Test _status_from_runner with completed task."""
    result = RunResult(
        run_number=1,
        duration_seconds=60.0,
        scenarios_count=10,
        events_total=50,
        status=RunStatus.COMPLETED.value,
    )

    async def dummy_run():
        return result

    task = asyncio.create_task(dummy_run())
    run_state = RunState(
        run_id="test",
        run_number=1,
        task=task,
        runner=mock_runner,
    )

    await task
    status = _status_from_runner(run_state)
    assert status == RunStatus.COMPLETED.value
