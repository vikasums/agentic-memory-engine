"""SimulationRunner — orchestrates scenario playback (spec § 3.2, § 4).

Skeleton only. Task 6 implements the timed event queue that drives 1/2/4/6
minute runs: wait until each fact's scheduled time, call the engine, record
monitoring metrics, write audit events, then trigger validation and tag all
data with ``run_number``.
"""

from typing import Any, Dict, Optional

from .audit_logger import AuditLogger
from .engine_client import EngineClient
from .monitoring_service import MonitoringService
from .scenario_generator import ScenarioGenerator
from .validator_service import ValidatorService

# Progressive validation durations from spec § 1.2 (run 1..4).
RUN_DURATIONS_SECONDS = (60.0, 120.0, 240.0, 360.0)


class SimulationRunner:
    """Drives one simulation run end to end."""

    def __init__(
        self,
        run_number: int,
        duration_seconds: float,
        generator: Optional[ScenarioGenerator] = None,
        client: Optional[EngineClient] = None,
        monitor: Optional[MonitoringService] = None,
        auditor: Optional[AuditLogger] = None,
        validator: Optional[ValidatorService] = None,
    ) -> None:
        self.run_number = run_number
        self.duration_seconds = duration_seconds
        self.generator = generator
        self.client = client
        self.monitor = monitor
        self.auditor = auditor
        self.validator = validator

    async def run(self) -> Dict[str, Any]:
        """Execute the run and return its report. Implemented in Task 6."""
        raise NotImplementedError("SimulationRunner.run is implemented in Task 6")
