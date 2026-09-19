"""SQLite schema management for the simulation audit trail (``audit_log.db``).

This module owns schema creation only — no data operations live here. The
schema follows spec § 3.5 verbatim, with run tagging per spec § 7.

``audit_log.db`` is intentionally a separate database file from the memory
engine's ``memory.db`` so that the audit trail can never be affected by
memory pruning or engine migrations.
"""

import os
import sqlite3
from typing import Optional

DEFAULT_AUDIT_DB_PATH = "audit_log.db"

AUDIT_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_number INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    user_id TEXT,
    fact_id TEXT,
    event_type TEXT NOT NULL,
    before_state TEXT,
    after_state TEXT,
    source TEXT,
    created_at REAL NOT NULL
)
"""

RUN_METADATA_TABLE = """
CREATE TABLE IF NOT EXISTS run_metadata (
    run_number INTEGER PRIMARY KEY,
    start_time REAL NOT NULL,
    end_time REAL,
    duration_seconds REAL,
    user_count INTEGER,
    scenario_count INTEGER,
    status TEXT NOT NULL
)
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events (run_number)",
    "CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_events (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_fact ON audit_events (fact_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events (event_type)",
    "CREATE INDEX IF NOT EXISTS idx_audit_run_ts ON audit_events (run_number, timestamp)",
)


def connect(db_path: str = DEFAULT_AUDIT_DB_PATH) -> sqlite3.Connection:
    """Open a connection to the audit database with sane defaults.

    Row factory is ``sqlite3.Row`` so callers can read columns by name.
    """
    _ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_audit_db(db_path: str = DEFAULT_AUDIT_DB_PATH) -> str:
    """Create the audit trail schema if it does not already exist.

    Idempotent: safe to call on every process start. Returns the resolved
    database path so callers can log or assert on it.
    """
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(AUDIT_EVENTS_TABLE)
            conn.execute(RUN_METADATA_TABLE)
            for statement in INDEXES:
                conn.execute(statement)
    finally:
        conn.close()
    return db_path


def table_exists(db_path: str, table_name: str) -> bool:
    """Return True if ``table_name`` exists in the database at ``db_path``."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _ensure_parent_dir(db_path: str) -> Optional[str]:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    return parent
