"""SQLite schema management for the simulation audit trail (``audit_log.db``).

This module owns schema creation and migration only — no data operations live
here. The schema follows spec § 3.5, with run tagging per spec § 7 and three
deliberate strengthenings resolved during Task 3:

* ``audit_events.user_id`` is ``NOT NULL`` — criterion H (§ 6.1) requires every
  audited fact to carry the correct user, so an untagged row is rejected by the
  database rather than found later by the validator.
* ``audit_events.created_at`` has a sub-second epoch DEFAULT, so a row written
  by any client (not just :class:`~simulation.audit_logger.AuditLogger`) is
  still stamped.
* ``run_metadata.notes`` exists — spec § 7.1 lists ``notes`` among the fields
  every run is tagged with, although the § 3.5 DDL omits it.

:func:`init_audit_db` migrates a database written by the earlier schema in
place, so an existing ``audit_log.db`` keeps its rows and its ``event_id``
values (spec § 7.2: data is never deleted).

``audit_log.db`` is intentionally a separate database file from the memory
engine's ``memory.db`` so that the audit trail can never be affected by
memory pruning or engine migrations.
"""

import os
import sqlite3
from typing import Any, Dict, List, Optional

DEFAULT_AUDIT_DB_PATH = "audit_log.db"

#: The memory engine's own database. Owned by ``agentic_memory.store``; this
#: module only *reads* it and can add columns an older file predates, because
#: ValidatorService Method 1 (spec § 3.6) queries it directly.
DEFAULT_MEMORY_DB_PATH = "memory.db"

#: Columns ``agentic_memory.store.SQLiteLanceDBStore`` creates on
#: ``memory_keys`` today. A ``memory.db`` written before commit ``f43ecde``
#: (auto-expiry) has every column but ``expires_at``.
MEMORY_KEYS_COLUMNS = (
    "natural_key",
    "memory_id",
    "user_id",
    "subject",
    "predicate",
    "object_value",
    "scope",
    "is_active",
    "updated_at",
    "expires_at",
)

#: Columns :func:`migrate_memory_db` knows how to add in place. Only nullable
#: columns can be added by ``ALTER TABLE`` without rewriting existing rows.
_MEMORY_KEYS_ADDABLE: Dict[str, str] = {"expires_at": "REAL"}

#: Sub-second epoch default for ``audit_events.created_at`` (Task 1 finding 3).
#: ``unixepoch('subsec')`` would be tidier but needs SQLite >= 3.42; this
#: julian-day expression yields the same REAL epoch on every SQLite version,
#: which matters because ``requires-python`` is still ``>=3.9``.
CREATED_AT_DEFAULT_SQL = "(julianday('now') - 2440587.5) * 86400.0"

#: Substituted for a legacy NULL ``user_id`` when migrating to the NOT NULL
#: schema, so an old audit trail is never silently dropped (Task 1 finding 2).
UNKNOWN_USER_ID = "__unknown__"

AUDIT_EVENTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_number INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    user_id TEXT NOT NULL,
    fact_id TEXT,
    event_type TEXT NOT NULL,
    before_state TEXT,
    after_state TEXT,
    source TEXT,
    created_at REAL NOT NULL DEFAULT ({CREATED_AT_DEFAULT_SQL})
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
    status TEXT NOT NULL,
    notes TEXT
)
"""

AUDIT_EVENTS_COLUMNS = (
    "event_id",
    "run_number",
    "timestamp",
    "user_id",
    "fact_id",
    "event_type",
    "before_state",
    "after_state",
    "source",
    "created_at",
)

RUN_METADATA_COLUMNS = (
    "run_number",
    "start_time",
    "end_time",
    "duration_seconds",
    "user_count",
    "scenario_count",
    "status",
    "notes",
)

INDEX_NAMES = (
    "idx_audit_run",
    "idx_audit_user",
    "idx_audit_fact",
    "idx_audit_type",
    "idx_audit_run_ts",
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events (run_number)",
    "CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_events (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_fact ON audit_events (fact_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events (event_type)",
    "CREATE INDEX IF NOT EXISTS idx_audit_run_ts ON audit_events (run_number, timestamp)",
)


def connect(
    db_path: str = DEFAULT_AUDIT_DB_PATH,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open a connection to the audit database with sane defaults.

    Row factory is ``sqlite3.Row`` so callers can read columns by name.

    ``check_same_thread=False`` is used by :class:`~simulation.audit_logger.AuditLogger`,
    which holds one connection for the life of a run and serialises access with
    its own lock, so the connection may legitimately be used from the runner's
    playback thread as well as the dashboard's request thread.
    """
    _ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_audit_db(db_path: str = DEFAULT_AUDIT_DB_PATH) -> str:
    """Create (or migrate) the audit trail schema.

    Idempotent: safe to call on every process start. Returns the resolved
    database path so callers can log or assert on it.
    """
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(AUDIT_EVENTS_TABLE)
            conn.execute(RUN_METADATA_TABLE)
            _migrate(conn)
            for statement in INDEXES:
                conn.execute(statement)
    finally:
        conn.close()
    return db_path


def migrate_audit_db(db_path: str = DEFAULT_AUDIT_DB_PATH) -> str:
    """Bring an existing ``audit_log.db`` up to the current schema.

    Equivalent to :func:`init_audit_db` — kept as a separate name so callers
    that only mean to upgrade an existing file read clearly.
    """
    return init_audit_db(db_path)


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade a schema written by an earlier revision of this module.

    Two changes are applied in place (both resolve findings deferred from the
    Task 1 review):

    * ``run_metadata.notes`` — added by ``ALTER TABLE`` when absent (spec § 7.1).
    * ``audit_events`` — ``user_id NOT NULL`` (criterion H) and a ``created_at``
      DEFAULT. SQLite cannot alter a column's constraints, so the table is
      rebuilt and its rows copied across.
    """
    if "notes" not in _column_names(conn, "run_metadata"):
        conn.execute("ALTER TABLE run_metadata ADD COLUMN notes TEXT")
    if _audit_events_needs_rebuild(conn):
        _rebuild_audit_events(conn)


def _audit_events_needs_rebuild(conn: sqlite3.Connection) -> bool:
    rows = {row["name"]: row for row in conn.execute("PRAGMA table_info(audit_events)")}
    if not rows:
        return False
    user_id = rows.get("user_id")
    created_at = rows.get("created_at")
    if user_id is None or created_at is None:
        return True
    return not user_id["notnull"] or created_at["dflt_value"] is None


def _rebuild_audit_events(conn: sqlite3.Connection) -> None:
    carried = [c for c in _column_names(conn, "audit_events") if c in AUDIT_EVENTS_COLUMNS]
    select_list = ", ".join(
        f"COALESCE(user_id, '{UNKNOWN_USER_ID}')" if c == "user_id" else c for c in carried
    )
    # Indexes follow the table through a RENAME, so drop them before the
    # rebuild — otherwise recreating them by the same names collides.
    for name in INDEX_NAMES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("ALTER TABLE audit_events RENAME TO audit_events_legacy")
    conn.execute(AUDIT_EVENTS_TABLE)
    if carried:
        conn.execute(
            f"INSERT INTO audit_events ({', '.join(carried)}) "
            f"SELECT {select_list} FROM audit_events_legacy"
        )
    conn.execute("DROP TABLE audit_events_legacy")


def _column_names(conn: sqlite3.Connection, table: str) -> List[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


# ----------------------------------------------------------------------
# memory.db (read-only, plus the one additive migration Task 1 flagged)
# ----------------------------------------------------------------------


def connect_memory_db(
    db_path: str = DEFAULT_MEMORY_DB_PATH,
    read_only: bool = True,
) -> sqlite3.Connection:
    """Open the engine's ``memory.db``.

    Read-only by default: ValidatorService Method 1 (spec § 3.6) must never be
    able to mutate the state it is auditing. Raises
    :class:`FileNotFoundError` rather than letting SQLite create an empty file.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"memory.db not found at {db_path!r}")
    if read_only:
        uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def memory_db_status(db_path: str = DEFAULT_MEMORY_DB_PATH) -> Dict[str, Any]:
    """Describe whether ``memory.db`` can serve Method 1.

    Returns ``{path, exists, readable, has_memory_keys, columns,
    missing_columns, needs_migration, error}``. ``needs_migration`` is True for
    the stale file Task 1 found: ``memory_keys`` without ``expires_at``, which
    the engine's pruner (and every expiry check) needs.
    """
    status: Dict[str, Any] = {
        "path": db_path,
        "exists": os.path.exists(db_path),
        "readable": False,
        "has_memory_keys": False,
        "columns": [],
        "missing_columns": [],
        "needs_migration": False,
        "error": None,
    }
    if not status["exists"]:
        status["error"] = f"memory.db not found at {db_path!r}"
        return status
    try:
        conn = connect_memory_db(db_path)
    except Exception as exc:  # pragma: no cover - permissions / corruption
        status["error"] = str(exc)
        return status
    try:
        status["readable"] = True
        columns = _column_names(conn, "memory_keys")
        status["has_memory_keys"] = bool(columns)
        status["columns"] = columns
        if columns:
            missing = [c for c in MEMORY_KEYS_COLUMNS if c not in columns]
            status["missing_columns"] = missing
            status["needs_migration"] = bool(missing)
        else:
            status["error"] = "memory.db has no memory_keys table"
    except Exception as exc:  # pragma: no cover - corruption
        status["error"] = str(exc)
    finally:
        conn.close()
    return status


def migrate_memory_db(db_path: str = DEFAULT_MEMORY_DB_PATH) -> List[str]:
    """Add the columns a stale ``memory.db`` is missing; return their names.

    Only additive ``ALTER TABLE ... ADD COLUMN`` statements are issued, so no
    row is rewritten and nothing is deleted (spec § 7.2). Idempotent: a
    current file returns ``[]``.

    Task 1 flagged this as the pre-requisite for ValidatorService Method 1 —
    a ``memory.db`` predating commit ``f43ecde`` has no ``expires_at``, so the
    engine's pruner raises and no expiry scenario can be validated.
    """
    status = memory_db_status(db_path)
    if status["error"] and not status["has_memory_keys"]:
        raise RuntimeError(status["error"])
    addable = [c for c in status["missing_columns"] if c in _MEMORY_KEYS_ADDABLE]
    if not addable:
        return []
    conn = connect_memory_db(db_path, read_only=False)
    try:
        with conn:
            for column in addable:
                conn.execute(
                    f"ALTER TABLE memory_keys ADD COLUMN {column} "
                    f"{_MEMORY_KEYS_ADDABLE[column]}"
                )
    finally:
        conn.close()
    return addable


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
