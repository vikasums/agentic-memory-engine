"""EngineClient — async HTTP client for the memory engine API (spec § 3.3).

Wraps the FastAPI memory engine (``agentic_memory.main_api``) endpoints used by
the simulation: ``/ingest``, ``/retrieve``, the profile endpoint and
``/metrics``. Every call is awaited through a shared ``httpx.AsyncClient`` with
a timeout, is logged (timestamp, user, endpoint, params, latency, result) and
reports its round-trip latency in microseconds so the MonitoringService
(spec § 3.4) can track API latencies and infer profile cache hits.

Deliberately **no retries and no backoff** — resilience belongs to the runner
(Task 6). A failed call raises :class:`EngineClientError` immediately.

Endpoint note: the spec writes the profile call as ``get_profile(user_id,
recent_days)`` without pinning a verb, and the shipped engine exposes it as
``POST /profile``. ``profile_style="auto"`` (the default) probes
``GET /profile/<user_id>`` once and falls back to ``POST /profile``, then
remembers the verb that worked for the rest of the client's life. Pass
``profile_style="post"`` to skip the probe against a known engine.
"""

import inspect
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .models import (
    IngestionResult,
    ProfileResult,
    RetrievalResult,
    RetrievedFact,
    StorageMetrics,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Sentinel values handed to ``on_request`` when no HTTP status was received.
STATUS_TIMEOUT = -1
STATUS_TRANSPORT_ERROR = -2
STATUS_INVALID_RESPONSE = -3

#: ``on_request(endpoint, user_id, latency_us, status)`` — ``status`` is the
#: HTTP status code, or one of the ``STATUS_*`` sentinels above.
OnRequest = Callable[[str, Optional[str], int, int], Any]

#: Statuses that mean "this engine does not serve the profile over GET".
_PROFILE_GET_UNSUPPORTED = (404, 405, 422)


class EngineClientError(Exception):
    """Any failed engine call: HTTP 4xx/5xx, timeout, transport or bad JSON.

    Attributes:
        endpoint: Logical endpoint the call targeted, e.g. ``"/retrieve"``.
        status_code: HTTP status when one was received, else ``None``.
    """

    def __init__(
        self,
        message: str,
        endpoint: Optional[str] = None,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code


def _elapsed_us(start: float) -> int:
    """Microseconds since ``start`` (a ``time.perf_counter()`` reading)."""
    return int((time.perf_counter() - start) * 1_000_000)


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> List[str]:
    """Coerce a JSON value into a list of strings, tolerating a bare string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item if isinstance(item, str) else str(item) for item in value]
    return [str(value)]


def _first_present(payload: Dict[str, Any], *keys: str) -> Any:
    """First non-``None`` value among ``keys``; ``None`` when none are set."""
    for key in keys:
        if payload.get(key) is not None:
            return payload[key]
    return None


def _error_detail(response: httpx.Response) -> str:
    """Short, human-readable body excerpt for an error message."""
    try:
        body = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:200] if text else "<empty body>"
    if isinstance(body, dict):
        detail = body.get("detail", body)
        return str(detail)[:200]
    return str(body)[:200]


class EngineClient:
    """Async client wrapping the memory engine REST API (spec § 3.3).

    Usage::

        async with EngineClient("http://localhost:8000") as client:
            await client.ingest("user_a", "I live in Seattle")
            hits = await client.retrieve("user_a", "where do I live?")

    Calling a method without the context manager also works; the connection
    pool is created lazily and :meth:`close` releases it.

    Args:
        base_url: Root URL of the running memory engine API.
        timeout: Per-request timeout in seconds, applied to every call.
        on_request: Optional callback invoked exactly once per HTTP request as
            ``on_request(endpoint, user_id, latency_us, status)``. May be a
            coroutine function. Exceptions it raises are logged and swallowed
            so monitoring can never break a simulation run.
        profile_style: ``"auto"`` (probe GET then fall back to POST), ``"get"``
            or ``"post"``.
        transport: Optional ``httpx`` transport override. Production code
            leaves this ``None``; tests pass an ``httpx.MockTransport``.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        on_request: Optional[OnRequest] = None,
        profile_style: str = "auto",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if profile_style not in ("auto", "get", "post"):
            raise ValueError(
                "profile_style must be one of 'auto', 'get', 'post'; "
                f"got {profile_style!r}"
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.on_request = on_request
        self._profile_style = profile_style
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "EngineClient":
        self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _ensure_client(self) -> httpx.AsyncClient:
        """Return the shared connection pool, creating it on first use."""
        if self._closed:
            raise EngineClientError("EngineClient is closed")
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        """Release the underlying HTTP connection pool. Idempotent."""
        self._closed = True
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    # -- request plumbing --------------------------------------------------

    async def _emit(
        self,
        endpoint: str,
        user_id: Optional[str],
        latency_us: int,
        status: int,
    ) -> None:
        """Invoke ``on_request``, never letting it break the caller."""
        if self.on_request is None:
            return
        try:
            result = self.on_request(endpoint, user_id, latency_us, status)
            if inspect.isawaitable(result):
                await result
        except Exception:  # pragma: no cover - defensive
            logger.exception("on_request callback failed for %s", endpoint)

    async def _request(
        self,
        method: str,
        path: str,
        endpoint: str,
        user_id: Optional[str] = None,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], int]:
        """Perform one HTTP call and return ``(payload, latency_us)``.

        Emits exactly one ``on_request`` callback on every path — success,
        HTTP error, timeout, transport failure and unparseable body — and
        raises :class:`EngineClientError` for anything but a 2xx JSON object.
        """
        client = self._ensure_client()
        request_params = dict(params) if params else None
        call_time = time.time()
        start = time.perf_counter()

        try:
            response = await client.request(
                method, path, json=json_body, params=request_params
            )
        except httpx.TimeoutException as exc:
            latency_us = _elapsed_us(start)
            await self._emit(endpoint, user_id, latency_us, STATUS_TIMEOUT)
            self._log(endpoint, user_id, json_body or request_params, latency_us, "timeout")
            raise EngineClientError(
                f"{method} {endpoint} timed out after {self.timeout}s",
                endpoint=endpoint,
            ) from exc
        except httpx.HTTPError as exc:
            latency_us = _elapsed_us(start)
            await self._emit(endpoint, user_id, latency_us, STATUS_TRANSPORT_ERROR)
            self._log(endpoint, user_id, json_body or request_params, latency_us, "transport error")
            raise EngineClientError(
                f"{method} {endpoint} failed to connect: {exc}",
                endpoint=endpoint,
            ) from exc

        latency_us = _elapsed_us(start)
        status = response.status_code

        if status >= 400:
            await self._emit(endpoint, user_id, latency_us, status)
            detail = _error_detail(response)
            self._log(endpoint, user_id, json_body or request_params, latency_us, f"HTTP {status}")
            raise EngineClientError(
                f"{method} {endpoint} failed with HTTP {status}: {detail}",
                endpoint=endpoint,
                status_code=status,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            await self._emit(endpoint, user_id, latency_us, STATUS_INVALID_RESPONSE)
            self._log(endpoint, user_id, json_body or request_params, latency_us, "invalid JSON")
            raise EngineClientError(
                f"{method} {endpoint} returned a non-JSON body "
                f"(HTTP {status}): {(response.text or '')[:200]!r}",
                endpoint=endpoint,
                status_code=status,
            ) from exc

        if not isinstance(payload, dict):
            await self._emit(endpoint, user_id, latency_us, STATUS_INVALID_RESPONSE)
            self._log(endpoint, user_id, json_body or request_params, latency_us, "unexpected JSON shape")
            raise EngineClientError(
                f"{method} {endpoint} returned {type(payload).__name__}, expected a JSON object",
                endpoint=endpoint,
                status_code=status,
            )

        await self._emit(endpoint, user_id, latency_us, status)
        logger.debug(
            "engine call time=%.6f user=%s endpoint=%s params=%s latency_us=%d status=%d result_keys=%s",
            call_time,
            user_id,
            endpoint,
            json_body or request_params,
            latency_us,
            status,
            sorted(payload),
        )
        return payload, latency_us

    def _log(
        self,
        endpoint: str,
        user_id: Optional[str],
        params: Any,
        latency_us: int,
        outcome: str,
    ) -> None:
        """Record a failed call (spec § 3.3 result tracking)."""
        logger.warning(
            "engine call time=%.6f user=%s endpoint=%s params=%s latency_us=%d result=%s",
            time.time(),
            user_id,
            endpoint,
            params,
            latency_us,
            outcome,
        )

    # -- API methods -------------------------------------------------------

    async def ingest(
        self, user_id: str, text: str, scope: str = "user"
    ) -> IngestionResult:
        """``POST /ingest`` — submit one utterance for fact extraction.

        The engine accepts with HTTP 202 and extracts in the background, so a
        successful result means *queued*, not *stored*.
        """
        payload, latency_us = await self._request(
            "POST",
            "/ingest",
            endpoint="/ingest",
            user_id=user_id,
            json_body={"user_id": user_id, "text": text, "scope": scope},
        )
        return IngestionResult(
            user_id=str(payload.get("user_id") or user_id),
            timestamp=_as_float(payload.get("timestamp"), None) or time.time(),
            fact_id=self._coerce_id(_first_present(payload, "fact_id", "memory_id", "id")),
            status=payload.get("status"),
            scope=payload.get("scope") or scope,
            latency_us=latency_us,
            raw=payload,
        )

    async def retrieve(
        self, user_id: str, query: str, top_k: int = 5
    ) -> RetrievalResult:
        """``POST /retrieve`` — rank the user's memories against ``query``."""
        payload, latency_us = await self._request(
            "POST",
            "/retrieve",
            endpoint="/retrieve",
            user_id=user_id,
            json_body={"user_id": user_id, "query": query, "top_k": top_k},
        )
        raw_memories = _first_present(payload, "memories", "results", "facts") or []
        if not isinstance(raw_memories, list):
            raise EngineClientError(
                f"/retrieve returned {type(raw_memories).__name__} for memories, expected a list",
                endpoint="/retrieve",
            )
        return RetrievalResult(
            user_id=str(payload.get("user_id") or user_id),
            query=str(payload.get("query") or query),
            memories=[self._to_fact(item) for item in raw_memories],
            latency_us=latency_us,
            raw=payload,
        )

    async def get_profile(self, user_id: str, recent_days: int = 7) -> ProfileResult:
        """Fetch the user's profile (stable facts + recent activity).

        Uses the verb the engine supports; see the module docstring on
        ``profile_style``.
        """
        payload: Optional[Dict[str, Any]] = None
        latency_us = 0

        if self._profile_style in ("auto", "get"):
            try:
                payload, latency_us = await self._get_profile_via_get(user_id, recent_days)
                self._profile_style = "get"
            except EngineClientError as exc:
                if self._profile_style != "auto" or exc.status_code not in _PROFILE_GET_UNSUPPORTED:
                    raise
                logger.debug(
                    "GET /profile/<user_id> unsupported (HTTP %s); using POST /profile",
                    exc.status_code,
                )
                payload = None

        if payload is None:
            payload, latency_us = await self._request(
                "POST",
                "/profile",
                endpoint="/profile",
                user_id=user_id,
                json_body={"user_id": user_id, "recent_days": recent_days},
            )
            self._profile_style = "post"

        return ProfileResult(
            user_id=str(payload.get("user_id") or user_id),
            stable_facts=_as_str_list(payload.get("stable_facts")),
            recent_activity=_as_str_list(payload.get("recent_activity")),
            profile_timestamp=_as_float(
                _first_present(payload, "profile_timestamp", "timestamp"), None
            ),
            cache_status=self._coerce_cache_status(payload),
            latency_us=latency_us,
            raw=payload,
        )

    async def _get_profile_via_get(
        self, user_id: str, recent_days: int
    ) -> Tuple[Dict[str, Any], int]:
        return await self._request(
            "GET",
            f"/profile/{quote(str(user_id), safe='')}",
            endpoint="/profile",
            user_id=user_id,
            params={"recent_days": recent_days},
        )

    async def get_metrics(self) -> StorageMetrics:
        """``GET /metrics`` — storage footprint and active/inactive counts."""
        payload, latency_us = await self._request(
            "GET", "/metrics", endpoint="/metrics"
        )

        active = _as_int(_first_present(payload, "active_facts", "active_memories"))
        inactive = _as_int(_first_present(payload, "inactive_facts", "inactive_memories"))
        total_raw = _first_present(payload, "total_facts", "total_memories")
        total = _as_int(total_raw) if total_raw is not None else active + inactive

        size_bytes = _first_present(payload, "memory_size_bytes", "total_size_bytes")
        if size_bytes is None:
            size_kb = (_as_float(payload.get("sqlite_file_size_kb"), 0.0) or 0.0) + (
                _as_float(payload.get("lancedb_folder_size_kb"), 0.0) or 0.0
            )
            size_bytes = size_kb * 1024

        return StorageMetrics(
            total_facts=total,
            active_facts=active,
            inactive_facts=inactive,
            memory_size_bytes=int(_as_float(size_bytes, 0.0) or 0.0),
            latency_us=latency_us,
            raw=payload,
        )

    # -- parsing helpers ---------------------------------------------------

    @staticmethod
    def _coerce_id(value: Any) -> Optional[str]:
        return None if value is None else str(value)

    @staticmethod
    def _coerce_cache_status(payload: Dict[str, Any]) -> Optional[str]:
        """Normalise whatever cache signal the engine exposes, if any."""
        status = payload.get("cache_status")
        if isinstance(status, str):
            return status
        cached = _first_present(payload, "cached", "cache_hit", "from_cache")
        if isinstance(cached, bool):
            return "hit" if cached else "miss"
        return None

    @classmethod
    def _to_fact(cls, item: Any) -> RetrievedFact:
        """Map one ``/retrieve`` memory object onto :class:`RetrievedFact`."""
        if not isinstance(item, dict):
            raise EngineClientError(
                f"/retrieve returned a {type(item).__name__} memory, expected a JSON object",
                endpoint="/retrieve",
            )
        known = {
            "text",
            "score",
            "fact_id",
            "memory_id",
            "id",
            "similarity",
            "decay_factor",
            "source",
            "timestamp",
            "scope",
            "age_days",
        }
        # The shipped engine has no ``fact_id`` key on retrieved memories; it
        # puts the store's memory id in ``source`` (engine.py builds
        # ``MemoryRecord(source=cand.id)``), so that is the last fallback and
        # gives Tasks 5-6 a stable id to correlate audit events against.
        return RetrievedFact(
            text=str(item.get("text", "")),
            score=_as_float(item.get("score"), 0.0) or 0.0,
            fact_id=cls._coerce_id(
                _first_present(item, "fact_id", "memory_id", "id", "source")
            ),
            similarity=_as_float(item.get("similarity"), None),
            decay_factor=_as_float(item.get("decay_factor"), None),
            source=item.get("source"),
            timestamp=_as_float(item.get("timestamp"), None),
            scope=item.get("scope"),
            age_days=_as_float(item.get("age_days"), None),
            extra={k: v for k, v in item.items() if k not in known},
        )
