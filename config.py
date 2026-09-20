"""Configuration & environment setup for the simulation system.

Central settings class that reads from environment variables with sensible
defaults. All configuration flows through this module to enable easy testing,
environment-specific overrides, and audit trail tracking.

Settings cover:
  - Profile cache TTL (for cache_002 scenario validation)
  - Engine API base URL (for EngineClient)
  - Audit database path (for AuditLogger)
  - Run durations (for progressive validation: 1/2/4/6 minutes)
  - Scenario set size (number of scenarios to generate)
  - Debug mode (verbose logging)
"""

import os
from typing import List

from pydantic import BaseModel, Field


class Settings(BaseModel):
    """Application settings loaded from environment variables.

    All fields have sensible defaults that match current hardcoded values,
    ensuring backward compatibility. Override via environment variables
    (see .env.example for the list).

    Uses the same Field(default_factory=lambda: os.getenv(...)) pattern as
    agentic_memory.config.MemorySettings for consistency with the engine.
    """

    # Profile cache TTL (seconds) — must match the engine's profile cache window.
    # The engine's store.py:225 caches user profiles; cache_002 scenario requires
    # this value to be lowered for its expected miss to be observable within a
    # 60-360 second run. Set to ~30s for testing, 3600s for production.
    profile_cache_ttl_seconds: int = Field(
        default_factory=lambda: int(os.getenv("PROFILE_CACHE_TTL_SECONDS", "3600")),
        description="Profile cache TTL in seconds (default 3600, ~30 for cache_002 testing)",
    )

    # Engine API endpoint — the memory engine's FastAPI server.
    engine_api_base_url: str = Field(
        default_factory=lambda: os.getenv("ENGINE_API_BASE_URL", "http://localhost:8000"),
        description="Memory engine API base URL",
    )

    # Audit database path — where AuditLogger persists all events.
    audit_db_path: str = Field(
        default_factory=lambda: os.getenv("AUDIT_DB_PATH", "./audit_log.db"),
        description="SQLite audit log database path",
    )

    # Run durations for progressive validation runs (in seconds).
    # Spec § 1.2: Run 1 (1m), Run 2 (2m), Run 3 (4m), Run 4 (6m).
    # Overridden at the dashboard/test level, not usually via env.
    run_durations: List[int] = Field(
        default=[60, 120, 240, 360],
        description="Run durations in seconds for progressive validation",
    )

    # Scenario set size — how many scenarios to generate per run.
    # Spec § 5: default is 50+, distributed across 6 categories (10/8/6/10/10/6).
    scenario_set_size: int = Field(
        default_factory=lambda: int(os.getenv("SCENARIO_SET_SIZE", "50")),
        description="Number of scenarios to generate per run",
    )

    # Debug mode — enable verbose logging and extra diagnostics.
    debug: bool = Field(
        default_factory=lambda: os.getenv("DEBUG", "false").lower() in ("true", "1", "yes"),
        description="Enable debug logging and verbose output",
    )


# Global settings instance — loaded once at module import.
settings = Settings()
