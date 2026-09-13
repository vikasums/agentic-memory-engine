import time
import pytest
from agentic_memory.models import FactRecord, Scope, StoreFilter
from agentic_memory.store import SQLiteLanceDBStore

def test_store_upsert_and_deactivate(dummy_store):
    fact = FactRecord(subject="user", predicate="lives_in", object_value="Seattle")
    user_id = "user_123"

    mem_id_1 = dummy_store.upsert_fact(fact=fact, user_id=user_id, scope=Scope.USER)
    dummy_store.add_vector_record(
        memory_id=mem_id_1,
        user_id=user_id,
        scope=Scope.USER,
        text="user lives_in Seattle",
        vector=[0.1] * 384,
        timestamp=time.time()
    )

    # Verify search finds active record
    results = dummy_store.search_vectors([0.1] * 384, StoreFilter(user_id=user_id), limit=5)
    assert len(results) == 1
    assert results[0].id == mem_id_1
    assert results[0].text == "user lives_in Seattle"

    # Upsert updated fact with same natural key
    fact_updated = FactRecord(subject="user", predicate="lives_in", object_value="Portland")
    mem_id_2 = dummy_store.upsert_fact(fact=fact_updated, user_id=user_id, scope=Scope.USER)
    dummy_store.add_vector_record(
        memory_id=mem_id_2,
        user_id=user_id,
        scope=Scope.USER,
        text="user lives_in Portland",
        vector=[0.1] * 384,
        timestamp=time.time()
    )

    # Search should only return the active updated memory
    results_updated = dummy_store.search_vectors([0.1] * 384, StoreFilter(user_id=user_id), limit=5)
    assert len(results_updated) == 1
    assert results_updated[0].id == mem_id_2
    assert results_updated[0].text == "user lives_in Portland"

def test_store_injection_safety(dummy_store):
    # Test user_id containing SQL injection attempts
    malicious_user_id = "user_123'; DROP TABLE memory_keys; --"
    fact = FactRecord(subject="user", predicate="hobby", object_value="chess")

    mem_id = dummy_store.upsert_fact(fact=fact, user_id=malicious_user_id, scope=Scope.USER)
    dummy_store.add_vector_record(
        memory_id=mem_id,
        user_id=malicious_user_id,
        scope=Scope.USER,
        text="user hobby chess",
        vector=[0.2] * 384,
        timestamp=time.time()
    )

    # Query with malicious user ID to verify query doesn't crash or exploit
    results = dummy_store.search_vectors([0.2] * 384, StoreFilter(user_id=malicious_user_id), limit=5)
    assert len(results) == 1
    assert results[0].user_id == malicious_user_id
