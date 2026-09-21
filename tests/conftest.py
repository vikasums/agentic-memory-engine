import tempfile
import pytest
from typing import List
from agentic_memory.models import FactRecord, Scope, StoreFilter, ScoredMemory
from agentic_memory.store import SQLiteLanceDBStore
from agentic_memory.providers import Embedder, Extractor

class DummyEmbedder:
    """Deterministic dummy embedder for unit testing."""
    def __init__(self, dimension: int = 384):
        self._dim = dimension

    def embed(self, texts: List[str]) -> List[List[float]]:
        # Return simple pseudo-embeddings based on text length
        embeddings = []
        for t in texts:
            val = float(len(t) % 10) / 10.0
            embeddings.append([val] * self._dim)
        return embeddings

    @property
    def dimension(self) -> int:
        return self._dim

class DummyExtractor:
    """Mock extractor returning pre-configured facts."""
    def __init__(self, facts: List[FactRecord] = None):
        self.facts = facts or [
            FactRecord(subject="user", predicate="lives_in", object_value="Seattle", confidence=0.9)
        ]

    async def extract_facts(self, text: str) -> List[FactRecord]:
        return self.facts

@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir

@pytest.fixture
def dummy_store(temp_dir):
    db_path = f"{temp_dir}/test_memory.db"
    lance_path = f"{temp_dir}/test_lancedb"
    store = SQLiteLanceDBStore(db_path=db_path, lancedb_path=lance_path)
    yield store
    store.close()


# ============================================================================
# Simulation system test fixtures (Task 11: Configuration & Environment Setup)
# ============================================================================

@pytest.fixture
def test_settings(temp_dir):
    """Test settings with short TTLs and durations for fast unit tests.

    Override profile_cache_ttl_seconds to ~30s to enable cache_002 scenario
    validation within a 60-second test run. Reduce durations to 10/20s
    for unit test speed, and limit scenario set to 10 for quick turnover.
    """
    from config import Settings

    return Settings(
        profile_cache_ttl_seconds=30,  # ~30s for cache miss to be observable in tests
        engine_api_base_url="http://localhost:8000",
        audit_db_path=f"{temp_dir}/test_audit_log.db",
        run_durations=[10, 20],  # Short durations for unit test speed
        scenario_set_size=10,  # Subset for unit tests
        debug=False,
    )


@pytest.fixture
def test_settings_short_ttl(temp_dir):
    """Ultra-short TTL settings for testing cache behavior specifically.

    Use this when testing cache_001/cache_002 scenarios in isolation.
    TTL is set to 1 second to force misses quickly.
    """
    from config import Settings

    return Settings(
        profile_cache_ttl_seconds=1,
        engine_api_base_url="http://localhost:8000",
        audit_db_path=f"{temp_dir}/test_audit_log_short_ttl.db",
        run_durations=[5],  # Very short for cache tests
        scenario_set_size=10,
        debug=True,
    )
