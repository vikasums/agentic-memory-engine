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
