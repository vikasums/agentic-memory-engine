"""AuditLogger — persistent audit trail writer for ``audit_log.db`` (spec § 3.5).

Schema creation and migration live in :mod:`simulation.database`; this module
owns every data operation against the audit trail.

Guarantees upheld here (spec § 3.5, § 6.1 criterion G, § 7):
  - every fact event (created/updated/deactivated/expired) is logged
  - before/after states are recorded as JSON, so a fact's state is replayable
  - every row carries ``run_number``, ``user_id`` and ``created_at``
  - rows are only ever inserted — nothing in this module deletes an event

Typical use::

    logger = AuditLogger("audit_log.db")
    logger.start_run(1, user_count=5, scenario_count=50, notes="Initial check")
    logger.write(1, "user_1", "mem_abc", "created", None, {"text": "..."},
                 "scenario_playback")
    logger.end_run(1, status="completed")
"""

import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from .database import DEFAULT_AUDIT_DB_PATH, connect, init_audit_db
from .models import AuditEvent, AuditEventType, AuditSource, RunMetadata, RunStatus

#: Allowed ``audit_events.event_type`` values (spec § 3.5).
VALID_EVENT_TYPES = frozenset(member.value for member in AuditEventType)

#: Allowed ``audit_events.source`` values (spec § 3.5).
VALID_SOURCES = frozenset(member.value for member in AuditSource)

#: Allowed ``run_metadata.status`` values (spec § 3.5 / § 7.1).
VALID_RUN_STATUSES = frozenset(member.value for member in RunStatus)

_INSERT_EVENT = """
INSERT INTO audit_events
    (run_number, timestamp, user_id, fact_id, event_type,
     before_state, after_state, source, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPSERT_RUN = """
INSERT INTO run_metadata
    (run_number, start_time, end_time, duration_seconds,
     user_count, scenario_count, status, notes)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(run_number) DO UPDATE SET
    start_time = excluded.start_time,
    end_time = excluded.end_time,
    duration_seconds = excluded.duration_seconds,
    user_count = excluded.user_count,
    scenario_count = excluded.scenario_count,
    status = excluded.status,
    notes = excluded.notes
"""

_SELECT_EVENT_COLUMNS = (
    "event_id, run_number, timestamp, user_id, fact_id, event_type, "
    "before_state, after_state, source, created_at"
)


class AuditLogger:
    """Writes simulation events to the audit database.

    One connection is held for the life of the instance and every statement is
    serialised through an :class:`threading.RLock`, so the same logger can be
    shared by the playback loop and the dashboard without interleaving writes.
    Each ``write`` commits on its own; use :meth:`write_many` to commit a batch.

    Args:
        db_path: Path to ``audit_log.db``.
        run_number: Default run tag applied to events written without one.
        auto_init: Create/migrate the schema on construction (spec § 3.5).
    """

    def __init__(
        self,
        db_path: str = DEFAULT_AUDIT_DB_PATH,
        run_number: int = 1,
        auto_init: bool = True,
    ) -> None:
        self.db_path = db_path
        self.run_number = run_number
        self._lock = threading.RLock()
        if auto_init:
            init_audit_db(db_path)
        self._conn = connect(db_path, check_same_thread=False)

    # ------------------------------------------------------------------
    # Event writes
    # ------------------------------------------------------------------

    def write(
        self,
        run_number: Union[int, AuditEvent, None] = None,
        user_id: Optional[str] = None,
        fact_id: Optional[str] = None,
        event_type: Union[str, AuditEventType, None] = None,
        before_state: Optional[Union[Dict[str, Any], str]] = None,
        after_state: Optional[Union[Dict[str, Any], str]] = None,
        source: Union[str, AuditSource] = AuditSource.SCENARIO_PLAYBACK,
        timestamp: Optional[float] = None,
    ) -> int:
        """Persist one audit event and return its ``event_id``.

        ``before_state`` / ``after_state`` are serialised to JSON (a ``str`` is
        stored verbatim, on the assumption it is already JSON). ``timestamp``
        defaults to now, as does ``created_at``, which is always stamped here.

        An :class:`~simulation.models.AuditEvent` may be passed as the single
        positional argument instead — see :meth:`write_event`.

        Raises:
            ValueError: on a missing ``run_number``/``user_id``, or an
                ``event_type``/``source`` outside the spec § 3.5 vocabulary.
        """
        if isinstance(run_number, AuditEvent):
            return self.write_event(run_number)
        event = AuditEvent(
            run_number=self.run_number if run_number is None else run_number,
            timestamp=time.time() if timestamp is None else timestamp,
            user_id=user_id,
            fact_id=fact_id,
            event_type=event_type,
            before_state=before_state,
            after_state=after_state,
            source=source,
        )
        return self.write_event(event)

    def write_event(self, event: AuditEvent) -> int:
        """Persist one :class:`~simulation.models.AuditEvent`.

        The event's ``event_id`` and ``created_at`` are filled in from the
        insert, so the caller's object matches the stored row.
        """
        row = self._event_row(event)
        with self._lock, self._conn:
            cursor = self._conn.execute(_INSERT_EVENT, row)
            event_id = int(cursor.lastrowid)
        event.event_id = event_id
        event.created_at = row[-1]
        return event_id

    def write_many(self, events: Iterable[AuditEvent]) -> List[int]:
        """Persist several events in a single transaction; returns their ids.

        Either every event is stored or none is: a validation failure or a
        database error rolls the whole batch back.
        """
        batch = list(events)
        rows = [self._event_row(event) for event in batch]
        event_ids: List[int] = []
        with self._lock, self._conn:
            for row in rows:
                cursor = self._conn.execute(_INSERT_EVENT, row)
                event_ids.append(int(cursor.lastrowid))
        for event, event_id, row in zip(batch, event_ids, rows):
            event.event_id = event_id
            event.created_at = row[-1]
        return event_ids

    # ------------------------------------------------------------------
    # Run metadata lifecycle (spec § 7.1)
    # ------------------------------------------------------------------

    def start_run(
        self,
        run_number: Union[int, RunMetadata, None] = None,
        user_count: int = 0,
        scenario_count: int = 0,
        notes: Optional[str] = None,
        start_time: Optional[float] = None,
    ) -> None:
        """Open a run's metadata row with ``status='in_progress'``.

        Re-starting the same ``run_number`` overwrites that row rather than
        failing, so an aborted run can be replayed under its own number. Its
        audit events are never touched (spec § 7.2 — data is never deleted).

        A :class:`~simulation.models.RunMetadata` may be passed as the single
        positional argument instead.
        """
        if isinstance(run_number, RunMetadata):
            metadata = run_number
        else:
            metadata = RunMetadata(
                run_number=self.run_number if run_number is None else run_number,
                start_time=time.time() if start_time is None else start_time,
                user_count=user_count,
                scenario_count=scenario_count,
                status=RunStatus.IN_PROGRESS.value,
                notes=notes,
            )
        self._write_run(metadata)

    def end_run(
        self,
        run_number: Optional[int] = None,
        status: Union[str, RunStatus] = RunStatus.COMPLETED,
        end_time: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Close a run: stamps ``end_time``, ``duration_seconds`` and ``status``.

        ``duration_seconds`` is derived from the stored ``start_time``.
        ``notes`` replaces the run's notes when given, and is left alone
        otherwise.

        Raises:
            ValueError: if the run was never started, or ``status`` is outside
                the spec § 3.5 vocabulary.
        """
        run_number = self.run_number if run_number is None else run_number
        status_value = _enum_value(status)
        if status_value not in VALID_RUN_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(VALID_RUN_STATUSES)}, got {status_value!r}"
            )
        end_time = time.time() if end_time is None else end_time
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT start_time FROM run_metadata WHERE run_number = ?",
                (run_number,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"run {run_number} was never started — call start_run() first"
                )
            duration = end_time - float(row["start_time"])
            if notes is None:
                self._conn.execute(
                    "UPDATE run_metadata SET end_time = ?, duration_seconds = ?, "
                    "status = ? WHERE run_number = ?",
                    (end_time, duration, status_value, run_number),
                )
            else:
                self._conn.execute(
                    "UPDATE run_metadata SET end_time = ?, duration_seconds = ?, "
                    "status = ?, notes = ? WHERE run_number = ?",
                    (end_time, duration, status_value, notes, run_number),
                )

    def complete_run(
        self,
        end_time: Optional[float] = None,
        status: Union[str, RunStatus, None] = None,
        run_number: Optional[int] = None,
    ) -> None:
        """Close out this logger's run. Alias of :meth:`end_run`."""
        self.end_run(
            run_number=run_number,
            status=RunStatus.COMPLETED if status is None else status,
            end_time=end_time,
        )

    def get_run(self, run_number: Optional[int] = None) -> Optional[RunMetadata]:
        """Return a run's metadata row, or ``None`` if it was never started."""
        run_number = self.run_number if run_number is None else run_number
        with self._lock:
            row = self._conn.execute(
                "SELECT run_number, start_time, end_time, duration_seconds, "
                "user_count, scenario_count, status, notes "
                "FROM run_metadata WHERE run_number = ?",
                (run_number,),
            ).fetchone()
        return _row_to_run_metadata(row) if row is not None else None

    # ------------------------------------------------------------------
    # Audit trail queries (spec § 6.1 criterion G, § 7.3)
    # ------------------------------------------------------------------

    def get_events(
        self,
        run_number: Optional[int] = None,
        user_id: Optional[str] = None,
        fact_id: Optional[str] = None,
        event_type: Union[str, AuditEventType, None] = None,
        source: Union[str, AuditSource, None] = None,
        limit: Optional[int] = None,
    ) -> List[AuditEvent]:
        """Return matching events in playback order (``timestamp``, ``event_id``).

        Every argument is an optional filter; with none of them, the whole audit
        trail is returned. ``before_state`` / ``after_state`` come back as dicts.
        """
        clauses: List[str] = []
        params: List[Any] = []
        for column, value in (
            ("run_number", run_number),
            ("user_id", user_id),
            ("fact_id", fact_id),
            ("event_type", _enum_value(event_type) if event_type is not None else None),
            ("source", _enum_value(source) if source is not None else None),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = f"SELECT {_SELECT_EVENT_COLUMNS} FROM audit_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp ASC, event_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_audit_event(row) for row in rows]

    def replay_fact(
        self,
        user_id: str,
        fact_id: str,
        run_number: Optional[int] = None,
    ) -> List[AuditEvent]:
        """Return one fact's event history, oldest first (spec § 3.6 Method 3)."""
        return self.get_events(run_number=run_number, user_id=user_id, fact_id=fact_id)

    def reconstruct_state(
        self,
        user_id: str,
        fact_id: str,
        run_number: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Replay a fact's events and return its final state.

        ``None`` means the fact has no audited history, or its last event left
        it with no state (deactivated or expired) — criterion G's "replay events
        to recover state".
        """
        events = self.replay_fact(user_id, fact_id, run_number=run_number)
        if not events:
            return None
        return events[-1].after_state

    def count_events(self, run_number: Optional[int] = None) -> int:
        """Number of audited events, optionally scoped to one run."""
        sql = "SELECT COUNT(*) AS n FROM audit_events"
        params: Sequence[Any] = ()
        if run_number is not None:
            sql += " WHERE run_number = ?"
            params = (run_number,)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection. Safe to call more than once."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "AuditLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _event_row(self, event: AuditEvent):
        """Validate an event and render it as an ``audit_events`` row tuple."""
        run_number = event.run_number
        if run_number is None:
            raise ValueError("run_number is required — every event is run-tagged (spec § 7)")
        if not isinstance(run_number, int) or isinstance(run_number, bool):
            raise ValueError(f"run_number must be an int, got {run_number!r}")

        user_id = event.user_id
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError(
                "user_id is required and must be a non-empty string "
                "(criterion H: facts tagged with correct user_id)"
            )

        event_type = _enum_value(event.event_type)
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(
                f"event_type must be one of {sorted(VALID_EVENT_TYPES)}, got {event_type!r}"
            )

        source = _enum_value(event.source)
        if source not in VALID_SOURCES:
            raise ValueError(
                f"source must be one of {sorted(VALID_SOURCES)}, got {source!r}"
            )

        fact_id = event.fact_id
        if fact_id is not None and not isinstance(fact_id, str):
            fact_id = str(fact_id)

        timestamp = time.time() if event.timestamp is None else float(event.timestamp)
        created_at = time.time() if event.created_at is None else float(event.created_at)

        return (
            run_number,
            timestamp,
            user_id,
            fact_id,
            event_type,
            _to_json(event.before_state),
            _to_json(event.after_state),
            source,
            created_at,
        )

    def _write_run(self, metadata: RunMetadata) -> None:
        status = _enum_value(metadata.status)
        if status not in VALID_RUN_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(VALID_RUN_STATUSES)}, got {status!r}"
            )
        if metadata.run_number is None:
            raise ValueError("run_number is required")
        with self._lock, self._conn:
            self._conn.execute(
                _UPSERT_RUN,
                (
                    metadata.run_number,
                    metadata.start_time,
                    metadata.end_time,
                    metadata.duration_seconds,
                    metadata.user_count,
                    metadata.scenario_count,
                    status,
                    metadata.notes,
                ),
            )


def _enum_value(value: Any) -> Any:
    """Unwrap an ``Enum`` to its value, leaving anything else untouched."""
    return value.value if hasattr(value, "value") else value


def _to_json(state: Optional[Union[Dict[str, Any], str]]) -> Optional[str]:
    """Serialise a before/after state for storage.

    ``None`` stays NULL; a ``str`` is stored verbatim (already JSON); anything
    else is JSON-encoded with sorted keys so two equal states compare equal as
    text. Values JSON cannot represent fall back to their ``str()``.
    """
    if state is None:
        return None
    if isinstance(state, str):
        return state
    return json.dumps(state, sort_keys=True, default=str)


def _from_json(text: Optional[str]) -> Optional[Any]:
    """Inverse of :func:`_to_json`; unparseable text is returned as-is."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _row_to_audit_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        run_number=row["run_number"],
        timestamp=row["timestamp"],
        user_id=row["user_id"],
        fact_id=row["fact_id"],
        event_type=row["event_type"],
        before_state=_from_json(row["before_state"]),
        after_state=_from_json(row["after_state"]),
        source=row["source"],
        created_at=row["created_at"],
        event_id=row["event_id"],
    )


def _row_to_run_metadata(row: sqlite3.Row) -> RunMetadata:
    return RunMetadata(
        run_number=row["run_number"],
        start_time=row["start_time"],
        end_time=row["end_time"],
        duration_seconds=row["duration_seconds"],
        user_count=row["user_count"],
        scenario_count=row["scenario_count"],
        status=row["status"],
        notes=row["notes"],
    )
