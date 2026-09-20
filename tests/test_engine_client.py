"""Tests for the simulation EngineClient (spec § 3.3).

Every test drives the real ``httpx.AsyncClient`` through an ``httpx.MockTransport``
so request formation, error handling, timeouts and latency reporting are all
exercised end-to-end without a running memory engine.
"""

import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from simulation.engine_client import (
    STATUS_INVALID_RESPONSE,
    STATUS_TIMEOUT,
    STATUS_TRANSPORT_ERROR,
    EngineClient,
    EngineClientError,
)
from simulation.models import (
    IngestionResult,
    ProfileResult,
    RetrievalResult,
    StorageMetrics,
)

BASE_URL = "http://engine.test"


def make_client(handler, **kwargs):
    """EngineClient wired to a MockTransport running ``handler``."""
    kwargs.setdefault("profile_style", "post")
    return EngineClient(
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def recording_handler(response_factory):
    """Handler that records every request it sees and returns a response."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response_factory(request)

    return handler, seen


class CallbackSpy:
    """Captures ``on_request(endpoint, user_id, latency_us, status)`` calls."""

    def __init__(self):
        self.calls = []

    def __call__(self, endpoint, user_id, latency_us, status):
        self.calls.append((endpoint, user_id, latency_us, status))


# --------------------------------------------------------------------------
# Request formation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_posts_expected_body_and_parses_result():
    handler, seen = recording_handler(
        lambda r: httpx.Response(202, json={"status": "queued", "user_id": "u1", "scope": "user"})
    )
    async with make_client(handler) as client:
        result = await client.ingest("u1", "I live in Seattle")

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/ingest"
    assert json.loads(request.content) == {
        "user_id": "u1",
        "text": "I live in Seattle",
        "scope": "user",
    }

    assert isinstance(result, IngestionResult)
    assert result.user_id == "u1"
    assert result.status == "queued"
    assert result.scope == "user"
    # /ingest is fire-and-forget, so no fact_id, and the timestamp is local.
    assert result.fact_id is None
    assert result.timestamp > 0
    assert result.latency_us > 0


@pytest.mark.asyncio
async def test_ingest_honours_custom_scope_and_returns_fact_id_when_present():
    handler, seen = recording_handler(
        lambda r: httpx.Response(202, json={"fact_id": 42, "timestamp": 1234.5})
    )
    async with make_client(handler) as client:
        result = await client.ingest("u1", "Company HQ is in Oslo", scope="global")

    assert json.loads(seen[0].content)["scope"] == "global"
    assert result.fact_id == "42"  # normalised to str
    assert result.timestamp == 1234.5


@pytest.mark.asyncio
async def test_retrieve_posts_top_k_and_maps_memories():
    payload = {
        "user_id": "u1",
        "query": "where do I live?",
        "memories": [
            {
                "text": "Lives in Seattle",
                "score": 0.91,
                "similarity": 0.95,
                "decay_factor": 0.96,
                "source": "chat",
                "timestamp": 1000.0,
                "scope": "user",
                "age_days": 1.5,
                "surprise_field": "kept",
            },
            {"text": "Works remotely", "score": 0.42},
        ],
    }
    handler, seen = recording_handler(lambda r: httpx.Response(200, json=payload))
    async with make_client(handler) as client:
        result = await client.retrieve("u1", "where do I live?", top_k=3)

    assert json.loads(seen[0].content) == {
        "user_id": "u1",
        "query": "where do I live?",
        "top_k": 3,
    }

    assert isinstance(result, RetrievalResult)
    assert len(result) == 2
    assert result.texts == ["Lives in Seattle", "Works remotely"]
    top = result[0]
    assert top.score == 0.91
    assert top.similarity == 0.95
    assert top.age_days == 1.5
    assert top.extra == {"surprise_field": "kept"}
    # Unknown fields on the second memory degrade to None, not KeyError.
    assert result[1].similarity is None
    assert result[1].fact_id is None
    assert [m.text for m in result] == result.texts  # iterable like a list


@pytest.mark.asyncio
async def test_retrieve_defaults_to_top_k_5_and_empty_memories():
    handler, seen = recording_handler(
        lambda r: httpx.Response(200, json={"user_id": "u1", "query": "q"})
    )
    async with make_client(handler) as client:
        result = await client.retrieve("u1", "q")

    assert json.loads(seen[0].content)["top_k"] == 5
    assert len(result) == 0


@pytest.mark.asyncio
async def test_get_profile_posts_recent_days_and_parses_lists():
    payload = {
        "user_id": "u1",
        "stable_facts": ["Lives in Seattle"],
        "recent_activity": ["Booked a flight", "Bought a bike"],
        "profile_timestamp": 999.0,
    }
    handler, seen = recording_handler(lambda r: httpx.Response(200, json=payload))
    async with make_client(handler) as client:
        result = await client.get_profile("u1", recent_days=14)

    assert seen[0].method == "POST"
    assert str(seen[0].url) == f"{BASE_URL}/profile"
    assert json.loads(seen[0].content) == {"user_id": "u1", "recent_days": 14}

    assert isinstance(result, ProfileResult)
    assert result.stable_facts == ["Lives in Seattle"]
    assert result.recent_activity == ["Booked a flight", "Bought a bike"]
    assert result.profile_timestamp == 999.0
    # The shipped engine gives no cache signal; the field stays None.
    assert result.cache_status is None
    assert result.latency_us > 0


@pytest.mark.asyncio
async def test_get_profile_surfaces_cache_status_when_engine_reports_it():
    handler, _ = recording_handler(
        lambda r: httpx.Response(200, json={"user_id": "u1", "cached": True})
    )
    async with make_client(handler) as client:
        assert (await client.get_profile("u1")).cache_status == "hit"

    handler, _ = recording_handler(
        lambda r: httpx.Response(200, json={"user_id": "u1", "cache_status": "miss"})
    )
    async with make_client(handler) as client:
        assert (await client.get_profile("u1")).cache_status == "miss"


@pytest.mark.asyncio
async def test_get_profile_auto_probes_get_then_falls_back_to_post():
    def handler(request: httpx.Response) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, json={"user_id": "u 1", "stable_facts": []})

    handler, seen = recording_handler(handler)
    async with make_client(handler, profile_style="auto") as client:
        await client.get_profile("u 1")
        await client.get_profile("u 1")

    # Probe + POST on the first call, then POST only — the verb is remembered.
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/profile/u 1"),
        ("POST", "/profile"),
        ("POST", "/profile"),
    ]
    # The user id is percent-encoded into the path, not injected raw.
    assert seen[0].url.raw_path.startswith(b"/profile/u%201")
    assert seen[0].url.params["recent_days"] == "7"


@pytest.mark.asyncio
async def test_get_profile_auto_uses_get_when_supported():
    handler, seen = recording_handler(
        lambda r: httpx.Response(200, json={"user_id": "u1", "stable_facts": ["a"]})
    )
    async with make_client(handler, profile_style="auto") as client:
        result = await client.get_profile("u1")
        await client.get_profile("u1")

    assert result.stable_facts == ["a"]
    assert [r.method for r in seen] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_get_profile_auto_does_not_mask_server_errors():
    handler, seen = recording_handler(lambda r: httpx.Response(500, text="boom"))
    async with make_client(handler, profile_style="auto") as client:
        with pytest.raises(EngineClientError) as excinfo:
            await client.get_profile("u1")

    assert excinfo.value.status_code == 500
    assert len(seen) == 1  # no POST fallback on a real failure


@pytest.mark.asyncio
async def test_get_metrics_normalises_engine_footprint():
    payload = {
        "sqlite_file_size_kb": 10.0,
        "lancedb_folder_size_kb": 2.5,
        "active_memories": 7,
        "inactive_memories": 3,
        "lancedb_total_rows": 10,
    }
    handler, seen = recording_handler(lambda r: httpx.Response(200, json=payload))
    async with make_client(handler) as client:
        metrics = await client.get_metrics()

    assert seen[0].method == "GET"
    assert str(seen[0].url) == f"{BASE_URL}/metrics"
    assert isinstance(metrics, StorageMetrics)
    assert metrics.active_facts == 7
    assert metrics.inactive_facts == 3
    assert metrics.total_facts == 10  # derived: active + inactive
    assert metrics.memory_size_bytes == int(12.5 * 1024)
    assert metrics.raw == payload


@pytest.mark.asyncio
async def test_get_metrics_prefers_explicit_spec_field_names():
    payload = {"total_facts": 12, "active_facts": 9, "memory_size_bytes": 4096}
    handler, _ = recording_handler(lambda r: httpx.Response(200, json=payload))
    async with make_client(handler) as client:
        metrics = await client.get_metrics()

    assert (metrics.total_facts, metrics.active_facts, metrics.memory_size_bytes) == (
        12,
        9,
        4096,
    )


@pytest.mark.asyncio
async def test_base_url_trailing_slash_is_normalised():
    handler, seen = recording_handler(lambda r: httpx.Response(200, json={}))
    client = EngineClient(
        base_url=f"{BASE_URL}/", transport=httpx.MockTransport(handler)
    )
    async with client:
        await client.get_metrics()

    assert str(seen[0].url) == f"{BASE_URL}/metrics"


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 404, 422, 500, 503])
@pytest.mark.asyncio
async def test_http_errors_raise_with_status_and_message(status):
    handler, _ = recording_handler(
        lambda r: httpx.Response(status, json={"detail": "Engine uninitialized"})
    )
    async with make_client(handler) as client:
        with pytest.raises(EngineClientError) as excinfo:
            await client.ingest("u1", "hello")

    error = excinfo.value
    assert error.status_code == status
    assert error.endpoint == "/ingest"
    assert str(status) in str(error)
    assert "Engine uninitialized" in str(error)


@pytest.mark.asyncio
async def test_http_error_with_non_json_body_still_reports_excerpt():
    handler, _ = recording_handler(lambda r: httpx.Response(502, text="<html>bad gateway</html>"))
    async with make_client(handler) as client:
        with pytest.raises(EngineClientError, match="bad gateway"):
            await client.get_metrics()


@pytest.mark.asyncio
async def test_timeout_raises_engine_client_error():
    def handler(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    async with make_client(handler, timeout=0.5) as client:
        with pytest.raises(EngineClientError) as excinfo:
            await client.retrieve("u1", "q")

    assert "timed out after 0.5s" in str(excinfo.value)
    assert excinfo.value.endpoint == "/retrieve"
    assert excinfo.value.status_code is None


@pytest.mark.asyncio
async def test_connection_error_raises_engine_client_error():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    async with make_client(handler) as client:
        with pytest.raises(EngineClientError, match="failed to connect"):
            await client.get_metrics()


@pytest.mark.asyncio
async def test_invalid_json_body_raises_engine_client_error():
    handler, _ = recording_handler(lambda r: httpx.Response(200, text="not json at all"))
    async with make_client(handler) as client:
        with pytest.raises(EngineClientError, match="non-JSON body"):
            await client.get_metrics()


@pytest.mark.asyncio
async def test_json_array_body_raises_engine_client_error():
    handler, _ = recording_handler(lambda r: httpx.Response(200, json=[1, 2, 3]))
    async with make_client(handler) as client:
        with pytest.raises(EngineClientError, match="expected a JSON object"):
            await client.get_metrics()


@pytest.mark.asyncio
async def test_retrieve_rejects_malformed_memories_payload():
    handler, _ = recording_handler(
        lambda r: httpx.Response(200, json={"memories": "nonsense"})
    )
    async with make_client(handler) as client:
        with pytest.raises(EngineClientError, match="expected a list"):
            await client.retrieve("u1", "q")

    handler, _ = recording_handler(
        lambda r: httpx.Response(200, json={"memories": ["just a string"]})
    )
    async with make_client(handler) as client:
        with pytest.raises(EngineClientError, match="expected a JSON object"):
            await client.retrieve("u1", "q")


@pytest.mark.asyncio
async def test_no_retries_on_failure():
    handler, seen = recording_handler(lambda r: httpx.Response(500, text="boom"))
    async with make_client(handler) as client:
        with pytest.raises(EngineClientError):
            await client.ingest("u1", "hello")

    assert len(seen) == 1  # Task 6 owns retries, not the client


@pytest.mark.asyncio
async def test_invalid_profile_style_rejected():
    with pytest.raises(ValueError, match="profile_style"):
        EngineClient(profile_style="patch")


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_manager_closes_pool_and_close_is_idempotent():
    handler, _ = recording_handler(lambda r: httpx.Response(200, json={}))
    client = make_client(handler)
    async with client:
        await client.get_metrics()

    assert client._client is None
    await client.close()  # idempotent

    with pytest.raises(EngineClientError, match="closed"):
        await client.get_metrics()


@pytest.mark.asyncio
async def test_works_without_context_manager():
    handler, seen = recording_handler(lambda r: httpx.Response(200, json={}))
    client = make_client(handler)
    try:
        await client.get_metrics()
        await client.get_metrics()
    finally:
        await client.close()

    assert len(seen) == 2  # connection pool reused


# --------------------------------------------------------------------------
# Latency + on_request hook (Task 5 integration)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_request_fires_once_per_call_with_latency_and_status():
    spy = CallbackSpy()
    handler, _ = recording_handler(lambda r: httpx.Response(200, json={"memories": []}))
    async with make_client(handler, on_request=spy) as client:
        result = await client.retrieve("u1", "q")

    assert len(spy.calls) == 1
    endpoint, user_id, latency_us, status = spy.calls[0]
    assert (endpoint, user_id, status) == ("/retrieve", "u1", 200)
    assert latency_us > 0
    assert latency_us == result.latency_us


@pytest.mark.asyncio
async def test_on_request_reports_http_error_status():
    spy = CallbackSpy()
    handler, _ = recording_handler(lambda r: httpx.Response(503, text="down"))
    async with make_client(handler, on_request=spy) as client:
        with pytest.raises(EngineClientError):
            await client.ingest("u1", "hello")

    assert [(c[0], c[1], c[3]) for c in spy.calls] == [("/ingest", "u1", 503)]


@pytest.mark.asyncio
async def test_on_request_reports_sentinel_statuses():
    def timeout_handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    def connect_handler(request):
        raise httpx.ConnectError("refused", request=request)

    spy = CallbackSpy()
    async with make_client(timeout_handler, on_request=spy) as client:
        with pytest.raises(EngineClientError):
            await client.get_metrics()
    assert spy.calls[-1][3] == STATUS_TIMEOUT

    async with make_client(connect_handler, on_request=spy) as client:
        with pytest.raises(EngineClientError):
            await client.get_metrics()
    assert spy.calls[-1][3] == STATUS_TRANSPORT_ERROR

    bad_json, _ = recording_handler(lambda r: httpx.Response(200, text="nope"))
    async with make_client(bad_json, on_request=spy) as client:
        with pytest.raises(EngineClientError):
            await client.get_metrics()
    assert spy.calls[-1][3] == STATUS_INVALID_RESPONSE

    assert len(spy.calls) == 3  # exactly one per request on every failure path


@pytest.mark.asyncio
async def test_on_request_accepts_an_async_callback():
    calls = []

    async def on_request(endpoint, user_id, latency_us, status):
        calls.append((endpoint, user_id, status))

    handler, _ = recording_handler(lambda r: httpx.Response(200, json={}))
    async with make_client(handler, on_request=on_request) as client:
        await client.get_metrics()

    assert calls == [("/metrics", None, 200)]


@pytest.mark.asyncio
async def test_failing_callback_never_breaks_the_call():
    def exploding(endpoint, user_id, latency_us, status):
        raise RuntimeError("monitoring is down")

    handler, _ = recording_handler(lambda r: httpx.Response(200, json={"memories": []}))
    async with make_client(handler, on_request=exploding) as client:
        result = await client.retrieve("u1", "q")

    assert len(result) == 0


@pytest.mark.asyncio
async def test_every_result_type_carries_latency_us():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"user_id": "u1"})

    async with make_client(handler) as client:
        results = [
            await client.ingest("u1", "hello"),
            await client.retrieve("u1", "q"),
            await client.get_profile("u1"),
            await client.get_metrics(),
        ]

    assert all(r.latency_us > 0 for r in results)


@pytest.mark.asyncio
async def test_profile_auto_probe_emits_a_callback_per_http_request():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(405, text="method not allowed")
        return httpx.Response(200, json={"user_id": "u1"})

    spy = CallbackSpy()
    async with make_client(handler, profile_style="auto", on_request=spy) as client:
        await client.get_profile("u1")

    assert [(c[0], c[3]) for c in spy.calls] == [("/profile", 405), ("/profile", 200)]
