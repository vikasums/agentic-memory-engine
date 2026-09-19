"""MonitoringService — real-time state tracking during a run (spec § 3.4).

Skeleton only. Task 5 implements event recording and the live metric rollups
consumed by the dashboard API (cache hit rate, latencies, active/inactive
memory counts, user-isolation checks).
"""

from typing import Any, Dict, List

from .models import MonitoringEvent


class MonitoringService:
    """Collects :class:`MonitoringEvent` records and derived metrics.

    Args:
        run_number: Run tag applied to every event recorded (spec § 7).
    """

    def __init__(self, run_number: int = 1) -> None:
        self.run_number = run_number
        self.events: List[MonitoringEvent] = []

    def log_event(self, event: MonitoringEvent) -> None:
        """Record one monitoring event. Implemented in Task 5."""
        raise NotImplementedError("MonitoringService.log_event is implemented in Task 5")

    def get_metrics(self) -> Dict[str, Any]:
        """Return current aggregate metrics. Implemented in Task 5."""
        raise NotImplementedError("MonitoringService.get_metrics is implemented in Task 5")
