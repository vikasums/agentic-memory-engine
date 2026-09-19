"""AuditLogger — persistent audit trail writer for ``audit_log.db`` (spec § 3.5).

Skeleton only. Task 3 implements event writes, run metadata lifecycle and
audit-trail queries. Schema creation lives in :mod:`simulation.database`.

Guarantees the implementation must uphold (spec § 3.5, § 6.1 criterion G):
  - every fact event (created/updated/deactivated/expired) is logged
  - before/after states are recorded as JSON
  - every row carries ``run_number`` for traceability (spec § 7)
  - rows are never deleted
"""

from typing import Optional

from .database import DEFAULT_AUDIT_DB_PATH, init_audit_db
from .models import AuditEvent, RunMetadata


class AuditLogger:
    """Writes simulation events to the audit database.

    Args:
        db_path: Path to ``audit_log.db``.
        run_number: Run tag applied to every event written by this instance.
    """

    def __init__(
        self,
        db_path: str = DEFAULT_AUDIT_DB_PATH,
        run_number: int = 1,
        auto_init: bool = True,
    ) -> None:
        self.db_path = db_path
        self.run_number = run_number
        if auto_init:
            init_audit_db(db_path)

    def write(self, event: AuditEvent) -> int:
        """Persist one audit event; returns the assigned ``event_id``.

        Implemented in Task 3.
        """
        raise NotImplementedError("AuditLogger.write is implemented in Task 3")

    def start_run(self, metadata: RunMetadata) -> None:
        """Record the start of a simulation run. Implemented in Task 3."""
        raise NotImplementedError("AuditLogger.start_run is implemented in Task 3")

    def complete_run(self, end_time: float, status: Optional[str] = None) -> None:
        """Close out a run's metadata row. Implemented in Task 3."""
        raise NotImplementedError("AuditLogger.complete_run is implemented in Task 3")
