import pytest
import asyncio
from agentic_memory.models import Scope, FactRecord, MemoryRecord
from agentic_memory.engine import MemoryEngine
from .conftest import DummyEmbedder, DummyExtractor

@pytest.mark.asyncio
async def test_engine_ingest_and_retrieve(dummy_store):
    embedder = DummyEmbedder()
    extractor = DummyExtractor([
        FactRecord(subject="user", predicate="role", object_value="Architect")
    ])

    engine = MemoryEngine(store=dummy_store, embedder=embedder, extractor=extractor)

    user_id = "user_42"
    memory_ids = await engine.process_paragraph_async(
        text="User is an Architect",
        user_id=user_id,
        scope=Scope.USER
    )

    assert len(memory_ids) == 1

    # Retrieve memories
    records = engine.retrieve_memories(
        query="What is my role?",
        user_id=user_id,
        top_k=3,
        half_life_days=30.0
    )

    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, MemoryRecord)
    assert rec.text == "user role Architect"
    assert rec.scope == Scope.USER
    assert 0.0 <= rec.score <= 1.0
    assert rec.source == memory_ids[0]

@pytest.mark.asyncio
async def test_tenant_isolation(dummy_store):
    embedder = DummyEmbedder()
    extractor1 = DummyExtractor([FactRecord(subject="user", predicate="city", object_value="Paris")])
    extractor2 = DummyExtractor([FactRecord(subject="user", predicate="city", object_value="Tokyo")])

    engine1 = MemoryEngine(store=dummy_store, embedder=embedder, extractor=extractor1)
    engine2 = MemoryEngine(store=dummy_store, embedder=embedder, extractor=extractor2)

    await engine1.process_paragraph_async("User 1 in Paris", user_id="user_1", scope=Scope.USER)
    await engine2.process_paragraph_async("User 2 in Tokyo", user_id="user_2", scope=Scope.USER)

    # Query for user_1 should only yield Paris
    records1 = engine1.retrieve_memories("Where do I live?", user_id="user_1", top_k=5)
    assert len(records1) == 1
    assert "Paris" in records1[0].text

    # Query for user_2 should only yield Tokyo
    records2 = engine2.retrieve_memories("Where do I live?", user_id="user_2", top_k=5)
    assert len(records2) == 1
    assert "Tokyo" in records2[0].text
