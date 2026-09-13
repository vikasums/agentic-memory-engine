import asyncio
import time
from . import config
from .metrics import logger, time_operation

class MemoryPruner:
    """Background service that periodically prunes inactive or expired memories."""

    def __init__(self, engine, prune_interval_hours: float = config.PRUNE_INTERVAL_HOURS):
        self.engine = engine
        self.prune_interval_sec = prune_interval_hours * 3600.0
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._run_loop())

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _run_loop(self):
        while True:
            try:
                await asyncio.sleep(self.prune_interval_sec)
                self.prune_now()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pruner background error: {e}")

    @time_operation("pruning_execution")
    def prune_now(
        self,
        inactive_retention_days: float = config.INACTIVE_RETENTION_DAYS,
        max_age_days: float = config.MAX_MEMORY_AGE_DAYS
    ) -> dict:
        now = time.time()
        inactive_cutoff = now - (inactive_retention_days * 86400.0)
        max_age_cutoff = now - (max_age_days * 86400.0)

        rows_deleted = self.engine.store.delete_expired(inactive_cutoff, max_age_cutoff)

        return {
            "sqlite_rows_deleted": rows_deleted,
            "inactive_cutoff": inactive_cutoff,
            "max_age_cutoff": max_age_cutoff
        }
