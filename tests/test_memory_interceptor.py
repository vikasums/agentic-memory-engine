import pytest
from unittest.mock import patch, MagicMock
from agentic_memory.memory_interceptor import MemoryInterceptor

def test_interceptor_inject_context_success():
    interceptor = MemoryInterceptor(memory_api_base="http://localhost:8000")

    mock_memories = [
        {
            "text": "user lives_in Zurich",
            "score": 0.95,
            "similarity": 0.95,
            "decay_factor": 1.0,
            "source": "mem_001",
            "timestamp": 1000.0,
            "scope": "user",
            "age_days": 2.0
        }
    ]

    with patch.object(interceptor, "_fetch_memories", return_value=mock_memories):
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is my location?"}
        ]

        hydrated = interceptor.inject_context(messages, user_id="user_123")

        assert len(hydrated) == 2
        assert hydrated[0]["role"] == "system"
        assert "[RELEVANT USER CONTEXT (Hints - Not database ground truth)]" in hydrated[0]["content"]
        assert "- user lives_in Zurich (confidence: 95%, 2d ago)" in hydrated[0]["content"]

def test_interceptor_inject_new_system_message():
    interceptor = MemoryInterceptor(memory_api_base="http://localhost:8000")
    mock_memories = [{"text": "user works_as Engineer", "score": 0.88, "age_days": 5.0}]

    with patch.object(interceptor, "_fetch_memories", return_value=mock_memories):
        messages = [
            {"role": "user", "content": "What do I do?"}
        ]

        hydrated = interceptor.inject_context(messages, user_id="user_456")

        assert len(hydrated) == 2
        assert hydrated[0]["role"] == "system"
        assert "You are a helpful assistant." in hydrated[0]["content"]
        assert "- user works_as Engineer (confidence: 88%, 5d ago)" in hydrated[0]["content"]
        assert hydrated[1]["role"] == "user"

def test_interceptor_empty_user_message():
    interceptor = MemoryInterceptor()
    messages = [{"role": "system", "content": "System prompt only"}]

    hydrated = interceptor.inject_context(messages, user_id="user_789")
    assert hydrated == messages

def test_interceptor_unreachable_api():
    interceptor = MemoryInterceptor(memory_api_base="http://invalid-host-999:8000")
    messages = [{"role": "user", "content": "Hello"}]

    hydrated = interceptor.inject_context(messages, user_id="user_999")
    assert hydrated == messages
