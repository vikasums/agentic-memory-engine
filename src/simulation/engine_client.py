"""EngineClient — HTTP client for the memory engine API (spec § 3.3).

Skeleton only. Tasks 4-7 implement the async calls against the FastAPI memory
engine (``/ingest``, ``/retrieve``, ``/profile``, ``/metrics``) plus per-call
tracking of timestamp, user, request params, response and latency.
"""

from typing import Any, Dict

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0


class EngineClient:
    """Async client wrapping the memory engine REST API.

    Args:
        base_url: Root URL of the running memory engine API.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def ingest(self, user_id: str, text: str, scope: str = "user") -> Dict[str, Any]:
        """POST /ingest. Implemented in Task 4."""
        raise NotImplementedError("EngineClient.ingest is implemented in Task 4")

    async def retrieve(self, user_id: str, query: str, top_k: int = 5) -> Dict[str, Any]:
        """POST /retrieve. Implemented in Task 4."""
        raise NotImplementedError("EngineClient.retrieve is implemented in Task 4")

    async def get_profile(self, user_id: str, recent_days: int = 7) -> Dict[str, Any]:
        """GET /profile. Implemented in Task 4."""
        raise NotImplementedError("EngineClient.get_profile is implemented in Task 4")

    async def get_metrics(self) -> Dict[str, Any]:
        """GET /metrics. Implemented in Task 4."""
        raise NotImplementedError("EngineClient.get_metrics is implemented in Task 4")

    async def close(self) -> None:
        """Release the underlying HTTP connection pool. Implemented in Task 4."""
        return None
