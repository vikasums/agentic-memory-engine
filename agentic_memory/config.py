import os
from typing import Literal, Optional
from pydantic import BaseModel, Field

def _env(key: str, default: str) -> str:
    return os.getenv(key, default)

class MemorySettings(BaseModel):
    """Central configuration settings for Agentic Memory Engine."""
    
    # Storage settings
    storage_backend: Literal["sqlite", "mariadb"] = Field(default_factory=lambda: os.getenv("MEMORY_STORAGE_BACKEND", "sqlite"))
    db_path: str = Field(default_factory=lambda: os.getenv("MEMORY_DB_PATH", "memory.db"))
    lancedb_path: str = Field(default_factory=lambda: os.getenv("LANCEDB_PATH", "./lancedb_data"))
    mariadb_url: Optional[str] = Field(default_factory=lambda: os.getenv("MEMORY_MARIADB_URL", None))

    # Embedding provider settings
    embedder_provider: Literal["fastembed", "openai"] = Field(default_factory=lambda: os.getenv("MEMORY_EMBEDDER_PROVIDER", "fastembed"))
    embedder_model: str = Field(default_factory=lambda: os.getenv("MEMORY_EMBEDDER_MODEL", "BAAI/bge-small-en-v1.5"))
    embedder_base_url: Optional[str] = Field(default_factory=lambda: os.getenv("MEMORY_EMBEDDER_BASE_URL", "http://localhost:11434/v1"))
    embedder_api_key: str = Field(default_factory=lambda: os.getenv("MEMORY_EMBEDDER_API_KEY", "token"))

    # Extractor provider settings
    extractor_provider: Literal["ollama", "openai"] = Field(default_factory=lambda: os.getenv("MEMORY_EXTRACTOR_PROVIDER", "ollama"))
    extractor_model: str = Field(default_factory=lambda: os.getenv("MEMORY_EXTRACTOR_MODEL", "qwen2.5:14b-instruct"))
    extractor_base_url: Optional[str] = Field(default_factory=lambda: os.getenv("MEMORY_EXTRACTOR_BASE_URL", "http://localhost:11434/v1"))
    extractor_api_key: str = Field(default_factory=lambda: os.getenv("MEMORY_EXTRACTOR_API_KEY", "token"))

    # Time decay & ranking settings
    default_half_life_days: float = Field(default_factory=lambda: float(os.getenv("MEMORY_DEFAULT_HALF_LIFE_DAYS", "30.0")))
    candidate_overfetch_factor: int = Field(default_factory=lambda: int(os.getenv("MEMORY_CANDIDATE_OVERFETCH_FACTOR", "3")))

    # Maintenance & Pruning settings
    inactive_retention_days: float = Field(default_factory=lambda: float(os.getenv("MEMORY_INACTIVE_RETENTION_DAYS", "7.0")))
    max_memory_age_days: float = Field(default_factory=lambda: float(os.getenv("MEMORY_MAX_MEMORY_AGE_DAYS", "180.0")))
    prune_interval_hours: float = Field(default_factory=lambda: float(os.getenv("MEMORY_PRUNE_INTERVAL_HOURS", "24.0")))


# Backward-compatible global constants
default_settings = MemorySettings()
DB_PATH = default_settings.db_path
LANCEDB_PATH = default_settings.lancedb_path
OLLAMA_MODEL = default_settings.extractor_model
EMBEDDING_MODEL = default_settings.embedder_model
DEFAULT_HALF_LIFE_DAYS = default_settings.default_half_life_days
CANDIDATE_OVERFETCH_FACTOR = default_settings.candidate_overfetch_factor
INACTIVE_RETENTION_DAYS = default_settings.inactive_retention_days
MAX_MEMORY_AGE_DAYS = default_settings.max_memory_age_days
PRUNE_INTERVAL_HOURS = default_settings.prune_interval_hours
