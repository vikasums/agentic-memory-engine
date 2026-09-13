import os
import pytest
from unittest.mock import patch
from agentic_memory.config import MemorySettings, default_settings

def test_config_defaults():
    settings = MemorySettings()
    assert settings.storage_backend == "sqlite"
    assert settings.embedder_provider == "fastembed"
    assert settings.extractor_provider == "ollama"
    assert settings.default_half_life_days == 30.0
    assert settings.candidate_overfetch_factor == 3

def test_config_env_overrides():
    env_vars = {
        "MEMORY_STORAGE_BACKEND": "mariadb",
        "MEMORY_EMBEDDER_PROVIDER": "openai",
        "MEMORY_EXTRACTOR_PROVIDER": "openai",
        "MEMORY_DEFAULT_HALF_LIFE_DAYS": "15.0",
        "MEMORY_CANDIDATE_OVERFETCH_FACTOR": "5"
    }

    with patch.dict(os.environ, env_vars):
        settings = MemorySettings()
        assert settings.storage_backend == "mariadb"
        assert settings.embedder_provider == "openai"
        assert settings.extractor_provider == "openai"
        assert settings.default_half_life_days == 15.0
        assert settings.candidate_overfetch_factor == 5
