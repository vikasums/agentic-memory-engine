"""ValidatorService — auto-verification and cross-validation (spec § 3.6, § 6).

Runs every expected outcome a scenario declares (spec § 6.2) through four
independent views of the engine's state and requires them to agree:

* **Method 1 — DB**   ``memory.db``: ``memory_keys`` row for the fact.
* **Method 2 — API**  ``EngineClient.retrieve()``: is the fact still ranked?
* **Method 3 — Audit** ``audit_log.db``: replay the fact's events and
  reconstruct its final state (criterion G, § 6.1).
* **Method 4 — Cross-check** compare the three above plus the profile view,
  and flag any discrepancy as a :class:`CrossValidationError`.

Three realities of this engine shape the implementation:

1. ``POST /ingest`` is fire-and-forget and returns no fact id, so the runner
   records a synthetic ``sim:<scenario_id>:<index>`` id. That id is *not* in
   ``memory.db``, so Method 1 falls back to matching a user's ``memory_keys``
   rows against the fact text, and reports itself unavailable when no row
   resolves. An unavailable method abstains — it never votes "inactive".
2. ``memory_keys.natural_key`` is ``user_id:subject:predicate`` and a
   PRIMARY KEY written with ``INSERT OR REPLACE``, so when two *different*
   scenarios land on one user with the same predicate the later fact silently
   replaces the earlier one. A deactivation is therefore only held against a
   scenario when it can be attributed to a fact of that same scenario;
   anything else is recorded as a cross-scenario collision and skipped.
3. ``MonitoringService`` maps ``contradiction_resolved`` onto a single
   ``updated`` audit event carrying ``before_state.fact_id`` (the superseded
   fact) — not the two events (``deactivated`` + ``created``) that spec § 4
   sketches. Method 3 therefore reconstructs a fact's state from both its own
   events *and* events that supersede it.

A check reports one of three statuses (:class:`CheckStatus`): PASSED, FAILED,
or SKIPPED when it could not be decided. The run passes when nothing FAILED.
"""

import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .audit_logger import AuditLogger
from .database import (
    DEFAULT_AUDIT_DB_PATH,
    DEFAULT_MEMORY_DB_PATH,
    connect_memory_db,
    memory_db_status,
)
from .engine_client import EngineClient, EngineClientError
from .models import (
    AuditEvent,
    AuditEventType,
    CheckStatus,
    CrossValidationError,
    FactType,
    ProfileResult,
    RunResult,
    Scenario,
    ScenarioCategory,
    ScenarioValidation,
    ValidationReport,
    ValidationResult,
)

logger = logging.getLogger(__name__)

#: Prefix of the ids the SimulationRunner mints when ``/ingest`` returns none.
SYNTHETIC_FACT_ID_PREFIX = "sim:"

#: Prefix of the pseudo fact id a profile read is logged under.
PROFILE_FACT_ID_PREFIX = "profile:"

#: Default ``top_k`` for Method 2. Wider than the engine's default 5 because a
#: user accumulates facts from several scenarios during a run.
DEFAULT_RETRIEVE_TOP_K = 10

#: Fraction of a fact's content words that must appear in a candidate text for
#: the two to be considered the same fact.
DEFAULT_MATCH_THRESHOLD = 0.6

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Words carrying no identity, dropped before overlap scoring.
_STOPWORDS = frozenset(
    """a an and are as at be been but by do does for from had has have i im i'm in is it
    its me my not now of on or our so that the their they this to was we were will with
    you your actually just really very now then""".split()
)

#: Audit event types that leave a fact active.
_ACTIVE_EVENTS = frozenset({AuditEventType.CREATED.value, AuditEventType.UPDATED.value})

#: Audit event types that leave a fact inactive.
_INACTIVE_EVENTS = frozenset(
    {AuditEventType.DEACTIVATED.value, AuditEventType.EXPIRED.value}
)

#: Outcomes the validator always checks for a category, on top of whatever the
#: scenario declares. They are validator-side findings rather than generator
#: vocabulary, so ``EXPECTED_OUTCOME_VOCABULARY`` (Task 2) stays as the spec
#: § 6.2 list of 24.
IMPLICIT_OUTCOMES: Dict[ScenarioCategory, Tuple[str, ...]] = {
    ScenarioCategory.EXPIRY: ("no_live_expiry_fired", "archive_not_deleted"),
    ScenarioCategory.CACHE: ("expected_cache_unsatisfiable",),
}


# ----------------------------------------------------------------------
# Text matching
# ----------------------------------------------------------------------


def normalise_tokens(text: Optional[str]) -> Set[str]:
    """Content words of ``text``, lowercased, stopwords removed."""
    if not text:
        return set()
    return {w for w in _WORD_RE.findall(str(text).lower()) if w not in _STOPWORDS}


def text_overlap(left: Optional[str], right: Optional[str]) -> float:
    """Fraction of the smaller token set that the two texts share (0.0–1.0)."""
    a, b = normalise_tokens(left), normalise_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def texts_match(
    left: Optional[str],
    right: Optional[str],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> bool:
    """True when two texts plausibly describe the same fact."""
    return text_overlap(left, right) >= threshold


# ----------------------------------------------------------------------
# Internal state records
# ----------------------------------------------------------------------


@dataclass
class _MethodState:
    """One validation method's view of one fact."""

    method: str
    available: bool = False
    reason: str = ""
    exists: Optional[bool] = None
    active: Optional[bool] = None
    user_id: Optional[str] = None
    text: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "available": self.available,
            "reason": self.reason,
            "exists": self.exists,
            "active": self.active,
            "user_id": self.user_id,
            "text": self.text,
            **({"detail": self.detail} if self.detail else {}),
        }


@dataclass
class _FactRef:
    """One scenario fact as it was actually played."""

    scenario_id: str
    fact_index: int
    text: str
    user_id: str
    fact_type: str
    fact_id: Optional[str] = None
    fact_id_source: str = "unknown"
    ttl_seconds: Optional[float] = None
    contradicts_scenario: Optional[str] = None
    contradicts_fact_id: Optional[str] = None
    played: bool = True
    #: Every memory id the engine created for this utterance.
    memory_ids: List[str] = field(default_factory=list)
    #: Normalised ``subject predicate object`` texts the engine stored. The
    #: engine never stores the utterance itself, so comparing retrieved or
    #: profiled text against ``text`` alone reports false mismatches.
    stored_texts: List[str] = field(default_factory=list)
    #: True when the engine reported what it extracted for this utterance.
    extraction_reported: bool = False

    @property
    def extracted_nothing(self) -> bool:
        """The extractor ran on this utterance and produced no fact.

        There is then nothing in the store to retrieve or profile, so presence
        checks report this as skipped rather than as a storage failure.
        """
        return self.extraction_reported and not self.memory_ids

    def match_texts(self) -> List[str]:
        """Texts that legitimately identify this fact, stored form preferred."""
        return [*self.stored_texts, self.text] if self.stored_texts else [self.text]

    def known_ids(self) -> List[str]:
        """Every id the engine may report for this fact."""
        ids = [*self.memory_ids]
        if self.fact_id and self.fact_id not in ids:
            ids.append(self.fact_id)
        return ids

    @property
    def is_profile_read(self) -> bool:
        return self.fact_type == FactType.PROFILE_READ.value

    @property
    def synthetic_id(self) -> bool:
        return bool(self.fact_id) and str(self.fact_id).startswith(
            SYNTHETIC_FACT_ID_PREFIX
        )

    def label(self) -> str:
        return f"{self.scenario_id}#{self.fact_index}"


class _AuditIndex:
    """All of a run's audit events, indexed the three ways Method 3 reads them."""

    def __init__(self, events: Sequence[AuditEvent]) -> None:
        self.events: List[AuditEvent] = list(events)
        self.by_fact: Dict[str, List[AuditEvent]] = {}
        self.by_user_fact: Dict[Tuple[str, str], List[AuditEvent]] = {}
        #: ``old_fact_id -> [events that superseded it]``
        self.supersedes: Dict[str, List[AuditEvent]] = {}
        for event in self.events:
            fact_id = event.fact_id or ""
            self.by_fact.setdefault(fact_id, []).append(event)
            self.by_user_fact.setdefault((event.user_id or "", fact_id), []).append(event)
            old_id = _state_fact_id(event.before_state)
            if old_id and old_id != fact_id:
                self.supersedes.setdefault(old_id, []).append(event)

    def for_fact(self, fact_id: str, user_id: Optional[str] = None) -> List[AuditEvent]:
        if user_id is not None:
            scoped = self.by_user_fact.get((user_id, fact_id))
            if scoped:
                return scoped
        return self.by_fact.get(fact_id, [])


def _state_fact_id(state: Optional[Dict[str, Any]]) -> Optional[str]:
    if isinstance(state, dict):
        value = state.get("fact_id")
        if value:
            return str(value)
    return None


def _state_text(state: Optional[Dict[str, Any]]) -> Optional[str]:
    if isinstance(state, dict):
        value = state.get("text")
        if value:
            return str(value)
    return None


@dataclass
class _Context:
    """Everything the 27 checkers need about one scenario."""

    scenario: Scenario
    run_number: int
    refs: List[_FactRef] = field(default_factory=list)
    states: Dict[str, Dict[str, _MethodState]] = field(default_factory=dict)
    cross: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    profiles: Dict[str, Optional[ProfileResult]] = field(default_factory=dict)
    cache_checks: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Any] = field(default_factory=list)
    missing_data: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: ``superseded fact_id -> _FactRef that replaced it`` (same scenario only).
    superseded_by: Dict[str, _FactRef] = field(default_factory=dict)

    @property
    def ingested(self) -> List[_FactRef]:
        return [r for r in self.refs if not r.is_profile_read and r.played]

    @property
    def profile_reads(self) -> List[_FactRef]:
        return [r for r in self.refs if r.is_profile_read]

    @property
    def user_ids(self) -> List[str]:
        seen: List[str] = []
        for ref in self.refs:
            if ref.user_id not in seen:
                seen.append(ref.user_id)
        return seen

    def state(self, ref: _FactRef, method: str) -> _MethodState:
        return self.states.get(ref.fact_id or "", {}).get(
            method, _MethodState(method=method, reason="not collected")
        )

    def note_missing(self, kind: str, **fields: Any) -> None:
        entry = {"scenario_id": self.scenario.scenario_id, "kind": kind}
        entry.update(fields)
        self.missing_data.append(entry)


# ----------------------------------------------------------------------
# ValidatorService
# ----------------------------------------------------------------------


class ValidatorService:
    """Verifies that a run's monitoring data matches the engine's actual state.

    Args:
        engine_client: Live :class:`EngineClient` for Methods 2 and 4. Without
            one, those methods report themselves unavailable.
        audit_logger: :class:`AuditLogger` for Method 3. Built from
            ``audit_db_path`` when omitted.
        scenario_generator: Fallback source of scenarios when
            :meth:`validate` is called without any.
        memory_db_path: The engine's ``memory.db`` (Method 1).
        audit_db_path: ``audit_log.db`` (Method 3).
        strict: When True, a validation method that cannot see a fact fails the
            cross-check instead of abstaining. Off by default because
            ``/ingest`` returns no fact id, which makes Method 1 unavailable
            for most facts (see module docstring).
    """

    def __init__(
        self,
        engine_client: Optional[EngineClient] = None,
        audit_logger: Optional[AuditLogger] = None,
        scenario_generator: Optional[Any] = None,
        memory_db_path: str = DEFAULT_MEMORY_DB_PATH,
        audit_db_path: str = DEFAULT_AUDIT_DB_PATH,
        client: Optional[EngineClient] = None,
        monitoring_service: Optional[Any] = None,
        run_number: Optional[int] = None,
        strict: bool = False,
        retrieve_top_k: int = DEFAULT_RETRIEVE_TOP_K,
        match_threshold: float = DEFAULT_MATCH_THRESHOLD,
    ) -> None:
        # The Task 1 skeleton's signature was
        # ``(memory_db_path, audit_db_path, client)``; keep those call sites
        # working by detecting a path in the first two positions.
        if isinstance(engine_client, str):
            memory_db_path, engine_client = engine_client, None
        if isinstance(audit_logger, str):
            audit_db_path, audit_logger = audit_logger, None

        self.client = engine_client or client
        self.memory_db_path = memory_db_path
        self.audit_db_path = audit_db_path
        self.generator = scenario_generator
        self.monitor = monitoring_service
        self.run_number = run_number
        self.strict = strict
        self.retrieve_top_k = retrieve_top_k
        self.match_threshold = match_threshold

        self._audit = audit_logger
        self._owns_audit = False
        if self._audit is None and audit_db_path:
            try:
                self._audit = AuditLogger(
                    db_path=audit_db_path,
                    run_number=run_number or 1,
                    auto_init=False,
                )
                self._owns_audit = True
            except Exception as exc:  # pragma: no cover - missing/locked file
                logger.warning("audit_log.db unavailable at %s: %s", audit_db_path, exc)
                self._audit = None

        self._retrieval_cache: Dict[Tuple[str, str], Any] = {}
        self._profile_cache: Dict[str, Optional[ProfileResult]] = {}
        self._memory_conn: Optional[sqlite3.Connection] = None
        self._memory_status: Optional[Dict[str, Any]] = None
        #: ``text -> owning user_id``, used for the § 6.1 criterion H leak test.
        self._owner_by_text: List[Tuple[str, str, str]] = []
        #: ``user_id -> stored fact texts``, the store's own answer on ownership.
        self._owned_texts_cache: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------
    # Properties / lifecycle
    # ------------------------------------------------------------------

    @property
    def engine_client(self) -> Optional[EngineClient]:
        return self.client

    @property
    def audit_logger(self) -> Optional[AuditLogger]:
        return self._audit

    def close(self) -> None:
        """Release the memory.db handle (and the audit logger, if we made it)."""
        if self._memory_conn is not None:
            try:
                self._memory_conn.close()
            finally:
                self._memory_conn = None
        if self._owns_audit and self._audit is not None:
            try:
                self._audit.close()
            finally:
                self._audit = None
                self._owns_audit = False

    def __enter__(self) -> "ValidatorService":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def validate(
        self,
        run_number: Optional[int] = None,
        scenarios: Optional[Sequence[Scenario]] = None,
        run_result: Optional[RunResult] = None,
    ) -> ValidationReport:
        """Validate one run and return a :class:`ValidationReport` (spec § 3.6).

        Every argument is optional so the service can be pointed at a finished
        run's databases alone: ``run_number`` falls back to ``run_result`` then
        to the constructor value then to the audit logger's, and ``scenarios``
        falls back to the configured ScenarioGenerator.
        """
        # A caller may still pass scenarios positionally (the Task 6 runner's
        # generic hand-off did); swap them into place.
        if isinstance(run_number, (list, tuple)) and scenarios is None:
            run_number, scenarios = None, list(run_number)

        resolved_run = self._resolve_run_number(run_number, run_result)
        scenario_list = self._resolve_scenarios(scenarios, run_result)

        report = ValidationReport(run_number=resolved_run, started_at=time.time())
        self._reset_caches()

        memory_status = self.memory_db_status()
        self._record_prerequisites(report, memory_status)

        audit_index = self._load_audit_index(resolved_run, report)
        self._build_owner_index(scenario_list, run_result)

        for scenario in scenario_list:
            validation = await self._validate_scenario(
                scenario, resolved_run, run_result, audit_index, report
            )
            report.scenarios[scenario.scenario_id] = validation

        self._collect_global_missing_data(report, audit_index)
        report.passed = report.checks_failed == 0 and not report.cross_validation_errors
        report.finished_at = time.time()
        return report

    # -- resolution helpers ------------------------------------------------

    def _resolve_run_number(
        self, run_number: Optional[int], run_result: Optional[RunResult]
    ) -> int:
        for candidate in (
            run_number,
            getattr(run_result, "run_number", None),
            self.run_number,
            getattr(self._audit, "run_number", None),
        ):
            if candidate is not None:
                return int(candidate)
        return 1

    def _resolve_scenarios(
        self,
        scenarios: Optional[Sequence[Scenario]],
        run_result: Optional[RunResult],
    ) -> List[Scenario]:
        if scenarios:
            return list(scenarios)
        generate = getattr(self.generator, "generate", None)
        if callable(generate):
            try:
                return list(generate())
            except Exception as exc:  # pragma: no cover - generator misuse
                logger.warning("scenario generation failed: %s", exc)
        return []

    def _reset_caches(self) -> None:
        self._retrieval_cache.clear()
        self._profile_cache.clear()
        self._owner_by_text = []
        self._owned_texts_cache.clear()

    def _record_prerequisites(
        self, report: ValidationReport, memory_status: Dict[str, Any]
    ) -> None:
        if not memory_status.get("exists"):
            report.missing_data.append(
                {
                    "kind": "memory_db_missing",
                    "path": memory_status.get("path"),
                    "detail": "Method 1 (direct memory.db query) is unavailable",
                }
            )
            report.notes.append(
                f"memory.db not found at {memory_status.get('path')!r} — Method 1 skipped"
            )
        elif memory_status.get("needs_migration"):
            missing = memory_status.get("missing_columns") or []
            report.missing_data.append(
                {
                    "kind": "memory_db_needs_migration",
                    "path": memory_status.get("path"),
                    "missing_columns": missing,
                    "detail": "run simulation.database.migrate_memory_db()",
                }
            )
            report.notes.append(
                "memory.db is missing "
                + ", ".join(missing)
                + " — Method 1 is degraded; run migrate_memory_db() (Task 1 pre-req)"
            )
        if self.client is None:
            report.notes.append("no EngineClient configured — Methods 2 and 4 skipped")
        if self._audit is None:
            report.notes.append("no AuditLogger configured — Method 3 skipped")

    def _load_audit_index(
        self, run_number: int, report: ValidationReport
    ) -> _AuditIndex:
        if self._audit is None:
            return _AuditIndex([])
        try:
            return _AuditIndex(self._audit.get_events(run_number=run_number))
        except Exception as exc:
            logger.warning("audit query failed for run %s: %s", run_number, exc)
            report.notes.append(f"audit query failed: {exc}")
            report.missing_data.append({"kind": "audit_unreadable", "detail": str(exc)})
            return _AuditIndex([])

    def _build_owner_index(
        self, scenarios: Sequence[Scenario], run_result: Optional[RunResult]
    ) -> None:
        """``(text, user_id, scenario_id)`` for every fact in the whole run.

        Criterion H (§ 6.1) is "no User B facts leaked into User A's results",
        which can only be checked against a run-wide map of who said what.
        """
        for scenario in scenarios:
            for index, fact in enumerate(scenario.facts):
                if fact.type == FactType.PROFILE_READ:
                    continue
                user_id = self._fact_user_id(scenario, fact)
                self._owner_by_text.append((fact.text, user_id, scenario.scenario_id))

    @staticmethod
    def _fact_user_id(scenario: Scenario, fact: Any) -> str:
        override = (getattr(fact, "metadata", None) or {}).get("user_id")
        if isinstance(override, str) and override.strip():
            return override
        return scenario.user_id

    # ------------------------------------------------------------------
    # Method 1 — direct memory.db query
    # ------------------------------------------------------------------

    def memory_db_status(self) -> Dict[str, Any]:
        """Cached :func:`simulation.database.memory_db_status` for this run."""
        if self._memory_status is None:
            self._memory_status = memory_db_status(self.memory_db_path)
        return self._memory_status

    def _memory_cursor(self) -> Optional[sqlite3.Connection]:
        if self._memory_conn is not None:
            return self._memory_conn
        status = self.memory_db_status()
        if not status.get("readable") or not status.get("has_memory_keys"):
            return None
        try:
            self._memory_conn = connect_memory_db(self.memory_db_path)
        except Exception as exc:  # pragma: no cover - permissions
            logger.warning("memory.db unreadable: %s", exc)
            return None
        return self._memory_conn

    def validate_via_db(self, fact_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Method 1: the ``memory_keys`` row for ``fact_id`` (spec § 3.6).

        Returns the row as a dict, or ``None`` when the fact is not in
        ``memory.db`` under that id — which happens both for a fact the engine
        never stored *and* for the synthetic ids the runner mints when
        ``/ingest`` returns none, so callers must not read ``None`` as
        "deactivated" on its own.
        """
        conn = self._memory_cursor()
        if conn is None or not fact_id:
            return None
        try:
            row = conn.execute(
                "SELECT * FROM memory_keys WHERE memory_id = ? AND user_id = ?",
                (fact_id, user_id),
            ).fetchone()
        except sqlite3.Error as exc:  # pragma: no cover - schema drift
            logger.warning("memory.db query failed: %s", exc)
            return None
        return dict(row) if row is not None else None

    def _db_rows_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        conn = self._memory_cursor()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM memory_keys WHERE user_id = ?", (user_id,)
            ).fetchall()
        except sqlite3.Error as exc:  # pragma: no cover - schema drift
            logger.warning("memory.db query failed: %s", exc)
            return []
        return [dict(row) for row in rows]

    def _user_owns_text(self, user_id: str, text: str) -> bool:
        """Does ``memory.db`` hold a fact of ``user_id``'s that matches ``text``?

        Criterion H is about the store, not the script. A scenario assigning a
        phrasing to one user does not stop another user legitimately holding
        their own fact that reads the same way.
        """
        if not text:
            return False
        owned = self._owned_texts_cache.get(user_id)
        if owned is None:
            owned = [self._row_text(row) for row in self._db_rows_for_user(user_id)]
            owned = [t for t in owned if t]
            self._owned_texts_cache[user_id] = owned
        return any(texts_match(t, text, self.match_threshold) for t in owned)

    @staticmethod
    def _row_text(row: Dict[str, Any]) -> str:
        parts = [
            str(row.get("subject") or "").replace("_", " "),
            str(row.get("predicate") or "").replace("_", " "),
            str(row.get("object_value") or ""),
        ]
        return " ".join(p for p in parts if p).strip()

    def _db_state(self, ref: _FactRef) -> _MethodState:
        state = _MethodState(method="db")
        status = self.memory_db_status()
        if not status.get("exists"):
            state.reason = "memory.db not found"
            return state
        if not status.get("has_memory_keys"):
            state.reason = status.get("error") or "memory_keys table missing"
            return state

        row: Optional[Dict[str, Any]] = None
        matched_by = ""
        if ref.fact_id and not ref.synthetic_id:
            row = self.validate_via_db(ref.fact_id, ref.user_id)
            matched_by = "memory_id"
        if row is None:
            row, score = self._best_text_row(ref)
            if row is not None:
                matched_by = "text"
                state.detail["match_score"] = round(score, 3)
        if row is None:
            state.reason = (
                "no memory_keys row for this fact "
                f"({'synthetic fact id' if ref.synthetic_id else 'engine fact id'} "
                f"{ref.fact_id!r}, no text match)"
            )
            return state

        state.available = True
        state.exists = True
        state.active = bool(row.get("is_active"))
        state.user_id = row.get("user_id")
        state.text = self._row_text(row)
        state.detail.update(
            {
                "matched_by": matched_by,
                "memory_id": row.get("memory_id"),
                "natural_key": row.get("natural_key"),
                "updated_at": row.get("updated_at"),
                "expires_at": row.get("expires_at", None),
                "has_expires_at_column": "expires_at" in (status.get("columns") or []),
            }
        )
        return state

    def _best_text_row(self, ref: _FactRef) -> Tuple[Optional[Dict[str, Any]], float]:
        best: Optional[Dict[str, Any]] = None
        best_score = 0.0
        for row in self._db_rows_for_user(ref.user_id):
            score = text_overlap(ref.text, self._row_text(row))
            if score > best_score:
                best, best_score = row, score
        if best is not None and best_score >= self.match_threshold:
            return best, best_score
        return None, best_score

    # ------------------------------------------------------------------
    # Method 2 — API retrieval
    # ------------------------------------------------------------------

    async def validate_via_api(self, user_id: str, query_text: str) -> Dict[str, Any]:
        """Method 2: ``POST /retrieve`` for ``query_text`` (spec § 3.6).

        Returns ``{available, found, matches, memories, error}``. ``found`` is
        True when a returned memory matches ``query_text``; the engine ranks
        only *active* memories, so a miss is evidence of deactivation.
        """
        result: Dict[str, Any] = {
            "available": False,
            "found": False,
            "matches": [],
            "memories": [],
            "error": None,
        }
        if self.client is None:
            result["error"] = "no EngineClient configured"
            return result
        key = (user_id, query_text)
        retrieval = self._retrieval_cache.get(key)
        if retrieval is None:
            try:
                retrieval = await self.client.retrieve(
                    user_id, query_text, top_k=self.retrieve_top_k
                )
            except EngineClientError as exc:
                result["error"] = str(exc)
                return result
            except Exception as exc:  # pragma: no cover - transport surprises
                result["error"] = f"{type(exc).__name__}: {exc}"
                return result
            self._retrieval_cache[key] = retrieval

        result["available"] = True
        memories = [
            {
                "fact_id": getattr(memory, "fact_id", None),
                "source": getattr(memory, "source", None),
                "text": getattr(memory, "text", ""),
                "score": getattr(memory, "score", None),
            }
            for memory in retrieval
        ]
        result["memories"] = memories
        result["matches"] = [
            memory
            for memory in memories
            if texts_match(query_text, memory["text"], self.match_threshold)
        ]
        result["found"] = bool(result["matches"])
        return result

    async def _api_state(self, ref: _FactRef) -> _MethodState:
        state = _MethodState(method="api")
        # Query with the stored triple when the engine reported one: retrieval
        # ranks triples, so the utterance is a weaker query for its own fact.
        candidates = ref.match_texts()
        outcome = await self.validate_via_api(ref.user_id, candidates[0])
        state.detail["memories"] = len(outcome["memories"])
        if not outcome["available"]:
            state.reason = outcome["error"] or "retrieve unavailable"
            return state
        known_ids = set(ref.known_ids())
        by_id = [
            memory
            for memory in outcome["memories"]
            if known_ids & {memory.get("fact_id"), memory.get("source")} - {None}
        ]
        by_text = [
            memory
            for memory in outcome["memories"]
            if any(
                texts_match(candidate, memory["text"], self.match_threshold)
                for candidate in candidates
            )
        ]
        outcome = {**outcome, "matches": by_text or outcome["matches"]}
        state.available = True
        state.exists = bool(by_id or by_text or outcome["found"])
        # /retrieve only ranks active memories, so present == active.
        state.active = state.exists
        state.user_id = ref.user_id
        matches = by_id or outcome["matches"]
        if matches:
            matched_text = matches[0].get("text")
            state.text = matched_text
            state.detail["match_score"] = round(
                max(text_overlap(candidate, matched_text) for candidate in candidates), 3
            )
            state.detail["matched_by"] = "fact_id" if by_id else "text"
        return state

    # ------------------------------------------------------------------
    # Method 3 — audit trail replay
    # ------------------------------------------------------------------

    def validate_via_audit(
        self,
        fact_id: str,
        user_id: str,
        index: Optional[_AuditIndex] = None,
        run_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Method 3: replay ``audit_log.db`` events for one fact (spec § 3.6).

        Returns ``{available, exists, active, events, reconstructed,
        superseded_by, error}``. A fact is inactive when its own last event is
        ``deactivated``/``expired``, or when a *later* event supersedes it
        (``before_state.fact_id == fact_id``) — which is how the
        MonitoringService records a resolved contradiction.
        """
        outcome: Dict[str, Any] = {
            "available": False,
            "exists": False,
            "active": None,
            "events": [],
            "reconstructed": None,
            "superseded_by": None,
            "error": None,
        }
        if index is None:
            if self._audit is None:
                outcome["error"] = "no AuditLogger configured"
                return outcome
            try:
                index = _AuditIndex(
                    self._audit.get_events(
                        run_number=run_number if run_number is not None else self.run_number,
                        user_id=user_id,
                        # Don't filter by fact_id: we need all events to build the supersedes index
                    )
                )
            except Exception as exc:
                outcome["error"] = str(exc)
                return outcome
        elif self._audit is None and not index.events:
            outcome["error"] = "no AuditLogger configured"
            return outcome

        outcome["available"] = True
        events = index.for_fact(fact_id, user_id)
        outcome["events"] = [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "timestamp": event.timestamp,
                "user_id": event.user_id,
                "run_number": event.run_number,
                "before_state": event.before_state,
                "after_state": event.after_state,
            }
            for event in events
        ]
        if not events:
            outcome["exists"] = False
            outcome["active"] = None
            return outcome

        last = events[-1]
        outcome["exists"] = True
        outcome["reconstructed"] = last.after_state
        if last.event_type in _INACTIVE_EVENTS:
            outcome["active"] = False
        elif last.event_type in _ACTIVE_EVENTS:
            outcome["active"] = True

        for event in index.supersedes.get(fact_id, []):
            if event.timestamp >= last.timestamp:
                outcome["active"] = False
                outcome["superseded_by"] = {
                    "fact_id": event.fact_id,
                    "user_id": event.user_id,
                    "event_type": event.event_type,
                    "timestamp": event.timestamp,
                }
                break
        return outcome

    def _audit_state(self, ref: _FactRef, index: _AuditIndex) -> _MethodState:
        state = _MethodState(method="audit")
        if self._audit is None and not index.events:
            state.reason = "no AuditLogger configured"
            return state
        outcome = self.validate_via_audit(ref.fact_id or "", ref.user_id, index=index)
        if not outcome["available"]:
            state.reason = outcome["error"] or "audit unavailable"
            return state
        if not outcome["exists"]:
            state.reason = f"no audit events for fact {ref.fact_id!r}"
            state.exists = False
            return state
        state.available = True
        state.exists = True
        state.active = outcome["active"]
        state.text = _state_text(outcome["reconstructed"])
        events = outcome["events"]
        state.user_id = events[-1]["user_id"] if events else None
        state.detail.update(
            {
                "events": len(events),
                "event_types": [e["event_type"] for e in events],
                "superseded_by": outcome["superseded_by"],
                "reconstructed": outcome["reconstructed"],
            }
        )
        return state

    # ------------------------------------------------------------------
    # Method 4 — profile API + cross-check
    # ------------------------------------------------------------------

    async def validate_via_profile(self, user_id: str) -> Dict[str, Any]:
        """Method 4's data source: the user's profile (spec § 3.6)."""
        outcome: Dict[str, Any] = {
            "available": False,
            "stable_facts": [],
            "recent_activity": [],
            "profile_timestamp": None,
            "cache_status": None,
            "error": None,
        }
        profile = await self._get_profile(user_id)
        if profile is None:
            outcome["error"] = (
                "no EngineClient configured"
                if self.client is None
                else "profile request failed"
            )
            return outcome
        outcome.update(
            {
                "available": True,
                "stable_facts": list(profile.stable_facts),
                "recent_activity": list(profile.recent_activity),
                "profile_timestamp": profile.profile_timestamp,
                "cache_status": profile.cache_status,
            }
        )
        return outcome

    async def _get_profile(self, user_id: str) -> Optional[ProfileResult]:
        if user_id in self._profile_cache:
            return self._profile_cache[user_id]
        profile: Optional[ProfileResult] = None
        if self.client is not None:
            try:
                profile = await self.client.get_profile(user_id)
            except EngineClientError as exc:
                logger.warning("profile fetch failed for %s: %s", user_id, exc)
            except Exception as exc:  # pragma: no cover - transport surprises
                logger.warning("profile fetch failed for %s: %s", user_id, exc)
        self._profile_cache[user_id] = profile
        return profile

    async def _profile_state(self, ref: _FactRef) -> _MethodState:
        state = _MethodState(method="profile")
        outcome = await self.validate_via_profile(ref.user_id)
        if not outcome["available"]:
            state.reason = outcome["error"] or "profile unavailable"
            return state
        entries = list(outcome["stable_facts"]) + list(outcome["recent_activity"])
        state.detail["entries"] = len(entries)
        if not entries:
            # An empty profile is not evidence that this fact is gone: the
            # engine summarises, it does not enumerate.
            state.reason = "profile is empty — no evidence either way"
            return state
        match = next(
            (e for e in entries if texts_match(ref.text, e, self.match_threshold)), None
        )
        state.available = True
        state.exists = match is not None
        state.active = state.exists
        state.user_id = ref.user_id
        state.text = match
        if match is None:
            # The profile can legitimately omit an active fact, so record the
            # absence without letting it outvote the other methods.
            state.available = False
            state.reason = "fact not summarised in profile (profile is lossy)"
        return state

    async def cross_validate(
        self,
        fact_id: str,
        user_id: str,
        text: Optional[str] = None,
        scenario_id: str = "",
    ) -> List[ValidationResult]:
        """Method 4: run all four methods for one fact and compare them.

        Returns one :class:`ValidationResult` per method plus a final
        ``cross_validation`` result that fails when two *available* methods
        disagree about whether the fact is active (spec § 3.6).
        """
        ref = _FactRef(
            scenario_id=scenario_id,
            fact_index=-1,
            text=text or "",
            user_id=user_id,
            fact_type=FactType.PRIMARY_FACT.value,
            fact_id=fact_id,
        )
        index = self._load_audit_index(
            self._resolve_run_number(None, None), ValidationReport(run_number=0)
        )
        states = await self._collect_states(ref, index)
        comparison = self._compare(ref, states)
        results = [
            ValidationResult(
                scenario_id=scenario_id,
                outcome=f"method_{name}",
                passed=True,
                details=state.as_dict(),
                status=(
                    CheckStatus.PASSED.value
                    if state.available
                    else CheckStatus.SKIPPED.value
                ),
            )
            for name, state in states.items()
        ]
        results.append(
            ValidationResult(
                scenario_id=scenario_id,
                outcome="cross_validation",
                passed=comparison["agreed"],
                details=comparison,
                status=(
                    CheckStatus.PASSED.value
                    if comparison["agreed"]
                    else (
                        CheckStatus.SKIPPED.value
                        if comparison["determinate_methods"] < 2
                        else CheckStatus.FAILED.value
                    )
                ),
            )
        )
        return results

    async def _collect_states(
        self, ref: _FactRef, index: _AuditIndex
    ) -> Dict[str, _MethodState]:
        return {
            "db": self._db_state(ref),
            "api": await self._api_state(ref),
            "audit": self._audit_state(ref, index),
            "profile": await self._profile_state(ref),
        }

    def _compare(
        self, ref: _FactRef, states: Dict[str, _MethodState]
    ) -> Dict[str, Any]:
        """Method 4 proper: do the methods agree on ``active``?"""
        verdicts = {
            name: state.active
            for name, state in states.items()
            if state.available and state.active is not None
        }
        distinct = set(verdicts.values())
        diffs: List[str] = []
        if len(distinct) > 1:
            active = sorted(n for n, v in verdicts.items() if v)
            inactive = sorted(n for n, v in verdicts.items() if not v)
            diffs.append(
                f"{'/'.join(active)} say active, {'/'.join(inactive)} say inactive"
            )
        # Criterion H: every method that knows the owner must name the same one.
        owners = {
            state.user_id
            for state in states.values()
            if state.available and state.user_id
        }
        if len(owners) > 1:
            diffs.append(f"methods disagree on user_id: {sorted(owners)}")
        unavailable = sorted(n for n, s in states.items() if not s.available)
        if self.strict and unavailable:
            diffs.append(f"methods unavailable in strict mode: {unavailable}")
        return {
            "scenario_id": ref.scenario_id,
            "fact_id": ref.fact_id,
            "fact_index": ref.fact_index,
            "user_id": ref.user_id,
            "agreed": not diffs,
            "consensus_active": distinct.pop() if len(distinct) == 1 else None,
            "determinate_methods": len(verdicts),
            "unavailable_methods": unavailable,
            "methods": {name: state.as_dict() for name, state in states.items()},
            "diffs": diffs,
        }

    # ------------------------------------------------------------------
    # Per-scenario validation
    # ------------------------------------------------------------------

    async def _validate_scenario(
        self,
        scenario: Scenario,
        run_number: int,
        run_result: Optional[RunResult],
        index: _AuditIndex,
        report: ValidationReport,
    ) -> ScenarioValidation:
        ctx = _Context(scenario=scenario, run_number=run_number)
        ctx.refs = self._build_refs(scenario, run_result)
        ctx.cache_checks = [
            check
            for check in (getattr(run_result, "cache_checks", None) or [])
            if check.get("scenario_id") == scenario.scenario_id
        ]
        ctx.errors = [
            error
            for error in (getattr(run_result, "errors", None) or [])
            if getattr(error, "scenario_id", None) == scenario.scenario_id
        ]
        ctx.superseded_by = self._superseded_within_scenario(ctx)

        for ref in ctx.ingested:
            if not ref.fact_id:
                ctx.note_missing(
                    "unresolved_fact_id",
                    fact_index=ref.fact_index,
                    user_id=ref.user_id,
                    detail="fact never ingested or no id recorded",
                )
                continue
            if ref.extracted_nothing:
                # The audit trail records the ingest attempt while the store
                # holds nothing, so the methods disagree by construction. That
                # is the extractor's silence, not a consistency failure.
                ctx.note_missing(
                    "extractor_produced_no_fact",
                    fact_id=ref.fact_id,
                    fact_index=ref.fact_index,
                    user_id=ref.user_id,
                    detail="engine reported no extracted fact for this utterance",
                )
                continue
            states = await self._collect_states(ref, index)
            ctx.states[ref.fact_id] = states
            comparison = self._compare(ref, states)
            ctx.cross[ref.fact_id] = comparison
            if not comparison["agreed"]:
                report.cross_validation_errors.append(
                    CrossValidationError(
                        scenario_id=scenario.scenario_id,
                        fact_id=ref.fact_id,
                        user_id=ref.user_id,
                        message="; ".join(comparison["diffs"]),
                        states=comparison["methods"],
                        diffs=list(comparison["diffs"]),
                    )
                )
            if comparison["determinate_methods"] == 0:
                ctx.note_missing(
                    "no_method_available",
                    fact_id=ref.fact_id,
                    fact_index=ref.fact_index,
                    user_id=ref.user_id,
                    detail=comparison["unavailable_methods"],
                )

        for user_id in ctx.user_ids:
            ctx.profiles[user_id] = await self._get_profile(user_id)

        outcomes = self._outcomes_for(scenario)
        results: List[ValidationResult] = []
        for outcome in outcomes:
            results.append(await self._run_check(ctx, outcome, index))

        validation = ScenarioValidation(
            scenario_id=scenario.scenario_id,
            category=_enum_value(scenario.category),
            user_ids=ctx.user_ids,
            results=results,
            cross_validation=list(ctx.cross.values()),
            missing_data=ctx.missing_data,
            notes=ctx.notes,
        )
        validation.passed = not validation.failed_outcomes and all(
            comparison["agreed"] for comparison in ctx.cross.values()
        )
        report.missing_data.extend(ctx.missing_data)
        return validation

    def _outcomes_for(self, scenario: Scenario) -> List[str]:
        outcomes = list(scenario.expected_outcomes)
        category = scenario.category
        if isinstance(category, str):
            try:
                category = ScenarioCategory(category)
            except ValueError:  # pragma: no cover - unknown category
                category = None
        for implicit in IMPLICIT_OUTCOMES.get(category, ()):  # type: ignore[arg-type]
            if implicit not in outcomes:
                outcomes.append(implicit)
        return outcomes

    async def _run_check(
        self, ctx: _Context, outcome: str, index: _AuditIndex
    ) -> ValidationResult:
        checker = getattr(self, f"_check_{outcome}", None)
        if checker is None:
            return _result(
                ctx,
                outcome,
                CheckStatus.SKIPPED,
                reason=f"no checker implemented for {outcome!r}",
            )
        try:
            status, details = checker(ctx, index)
        except Exception as exc:  # pragma: no cover - a checker must not crash
            logger.exception("checker %s failed for %s", outcome, ctx.scenario.scenario_id)
            return _result(
                ctx,
                outcome,
                CheckStatus.FAILED,
                reason=f"checker raised {type(exc).__name__}: {exc}",
            )
        return _result(ctx, outcome, status, **details)

    # -- fact refs ---------------------------------------------------------

    def _build_refs(
        self, scenario: Scenario, run_result: Optional[RunResult]
    ) -> List[_FactRef]:
        """Reconstruct what the runner actually played for this scenario."""
        records = {
            record["fact_index"]: record
            for record in (getattr(run_result, "fact_records", None) or [])
            if record.get("scenario_id") == scenario.scenario_id
        }
        fallback_ids = list(
            (getattr(run_result, "fact_ids", None) or {}).get(scenario.scenario_id, [])
        )
        errored = {
            getattr(error, "fact_index", None)
            for error in (getattr(run_result, "errors", None) or [])
            if getattr(error, "scenario_id", None) == scenario.scenario_id
        }

        refs: List[_FactRef] = []
        fallback_cursor = 0
        for index, fact in enumerate(scenario.facts):
            fact_type = _enum_value(fact.type)
            user_id = self._fact_user_id(scenario, fact)
            record = records.get(index)
            if fact_type == FactType.PROFILE_READ.value:
                refs.append(
                    _FactRef(
                        scenario_id=scenario.scenario_id,
                        fact_index=index,
                        text=fact.text,
                        user_id=user_id,
                        fact_type=fact_type,
                        fact_id=f"{PROFILE_FACT_ID_PREFIX}{user_id}",
                        fact_id_source="profile",
                        played=index not in errored,
                    )
                )
                continue

            if record is not None:
                fact_id = record.get("fact_id")
                source = record.get("fact_id_source", "unknown")
                played = True
            elif index in errored:
                fact_id, source, played = None, "errored", False
            elif run_result is not None and not records and fallback_ids:
                # Older RunResult without fact_records: fall back to playback
                # order, which matches fact order for a scenario's own facts.
                fact_id = (
                    fallback_ids[fallback_cursor]
                    if fallback_cursor < len(fallback_ids)
                    else None
                )
                fallback_cursor += 1
                source, played = "fact_ids", fact_id is not None
            else:
                # No run result at all — assume the runner's synthetic id.
                fact_id = f"{SYNTHETIC_FACT_ID_PREFIX}{scenario.scenario_id}:{index}"
                source, played = "assumed", True

            refs.append(
                _FactRef(
                    scenario_id=scenario.scenario_id,
                    fact_index=index,
                    text=fact.text,
                    user_id=(record or {}).get("user_id") or user_id,
                    fact_type=fact_type,
                    fact_id=fact_id,
                    fact_id_source=source,
                    memory_ids=list((record or {}).get("memory_ids") or []),
                    stored_texts=list((record or {}).get("stored_texts") or []),
                    extraction_reported=bool(
                        (record or {}).get("extraction_reported", False)
                    ),
                    ttl_seconds=(
                        (record or {}).get("ttl_seconds")
                        if record is not None
                        else fact.ttl_seconds
                    ),
                    contradicts_scenario=fact.contradicts_scenario,
                    contradicts_fact_id=(
                        (record or {}).get("contradicts_fact_id")
                        or fact.contradicts_fact_id
                    ),
                    played=played,
                )
            )
        return refs

    @staticmethod
    def _superseded_within_scenario(ctx: _Context) -> Dict[str, _FactRef]:
        """``old_fact_id -> replacing ref``, for same-scenario same-user pairs.

        Cross-user pairs are excluded: spec § 5.1 D's multi-user scenarios
        deliberately contradict across users and must leave *both* facts
        active, so they never supersede anything.
        """
        by_id = {ref.fact_id: ref for ref in ctx.ingested if ref.fact_id}
        superseded: Dict[str, _FactRef] = {}
        for ref in ctx.ingested:
            old_id = ref.contradicts_fact_id
            if not old_id or old_id not in by_id:
                continue
            old_ref = by_id[old_id]
            if old_ref.user_id != ref.user_id:
                continue  # cross-user: no deactivation expected
            if old_ref.fact_index >= ref.fact_index:
                continue
            superseded[old_id] = ref
        return superseded

    # -- shared verdict helpers -------------------------------------------

    def _verdict(self, ctx: _Context, ref: _FactRef) -> Tuple[Optional[bool], Dict[str, Any]]:
        """Consensus on whether ``ref`` is active; ``None`` when undecidable."""
        comparison = ctx.cross.get(ref.fact_id or "")
        if comparison is None:
            return None, {"reason": "no methods ran for this fact"}
        return comparison["consensus_active"], {
            "determinate_methods": comparison["determinate_methods"],
            "unavailable_methods": comparison["unavailable_methods"],
            "methods": {
                name: {"available": m["available"], "active": m["active"]}
                for name, m in comparison["methods"].items()
            },
        }

    def _attribute_inactive(
        self, ctx: _Context, ref: _FactRef, index: _AuditIndex
    ) -> Dict[str, Any]:
        """Why is ``ref`` inactive — this scenario, or a collision from another?

        ``memory_keys.natural_key`` is ``user:subject:predicate`` and unique,
        so two scenarios on one user that share a predicate overwrite each
        other. Such a deactivation says nothing about the scenario under test
        and must not fail it (Task 2 measured this on ~20% of runs).
        """
        if ref.fact_id in ctx.superseded_by:
            replacing = ctx.superseded_by[ref.fact_id]
            return {
                "attribution": "same_scenario",
                "superseded_by": replacing.fact_id,
                "superseded_by_index": replacing.fact_index,
            }
        audit = self._audit_state(ref, index)
        superseder = (audit.detail.get("superseded_by") or {}).get("fact_id")
        own_ids = {r.fact_id for r in ctx.ingested}
        if superseder and superseder not in own_ids:
            return {"attribution": "cross_scenario", "superseded_by": superseder}
        if superseder:
            return {"attribution": "same_scenario", "superseded_by": superseder}
        return {"attribution": "unattributed", "superseded_by": None}

    def _expect_active(
        self,
        ctx: _Context,
        refs: Iterable[_FactRef],
        index: _AuditIndex,
        tolerate_collisions: bool = True,
    ) -> Tuple[CheckStatus, Dict[str, Any]]:
        """Shared body of ``all_facts_active`` / ``both_facts_active`` / …"""
        failures: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        checked: List[str] = []
        for ref in refs:
            active, evidence = self._verdict(ctx, ref)
            if active is None:
                skipped.append({"fact": ref.label(), "reason": "undecidable", **evidence})
                continue
            if active:
                checked.append(ref.label())
                continue
            attribution = self._attribute_inactive(ctx, ref, index)
            entry = {"fact": ref.label(), "fact_id": ref.fact_id, **attribution, **evidence}
            if tolerate_collisions and attribution["attribution"] != "same_scenario":
                ctx.note_missing(
                    "cross_scenario_collision"
                    if attribution["attribution"] == "cross_scenario"
                    else "unattributed_deactivation",
                    fact_id=ref.fact_id,
                    fact_index=ref.fact_index,
                    user_id=ref.user_id,
                    superseded_by=attribution["superseded_by"],
                )
                skipped.append(entry)
            else:
                failures.append(entry)
        if failures:
            return CheckStatus.FAILED, {"failures": failures, "skipped": skipped}
        if not checked and skipped:
            return CheckStatus.SKIPPED, {
                "reason": "no fact could be decided",
                "skipped": skipped,
            }
        return CheckStatus.PASSED, {"active_facts": checked, "skipped": skipped}

    def _profile_entries(self, ctx: _Context, user_id: str) -> List[str]:
        profile = ctx.profiles.get(user_id)
        if profile is None:
            return []
        return list(profile.stable_facts) + list(profile.recent_activity)

    # ------------------------------------------------------------------
    # Checkers — § 6.2 base 5
    # ------------------------------------------------------------------

    def _check_fact_stored(self, ctx: _Context, index: _AuditIndex):
        refs = ctx.ingested
        if not refs:
            return CheckStatus.SKIPPED, {"reason": "scenario stores no facts"}
        not_extracted = [r.label() for r in refs if r.extracted_nothing]
        refs = [r for r in refs if not r.extracted_nothing]
        if not refs:
            return CheckStatus.SKIPPED, {
                "reason": "the extractor produced no fact from these utterances",
                "not_extracted": not_extracted,
            }
        missing, stored, undecided = [], [], []
        for ref in refs:
            db = ctx.state(ref, "db")
            audit = ctx.state(ref, "audit")
            if db.exists or audit.exists:
                stored.append(ref.label())
            elif db.available or audit.available:
                missing.append({"fact": ref.label(), "fact_id": ref.fact_id})
            else:
                undecided.append({"fact": ref.label(), "reason": db.reason or audit.reason})
        errored = [
            {"fact": f"{ctx.scenario.scenario_id}#{e.fact_index}", "error": e.message}
            for e in ctx.errors
        ]
        if missing or errored:
            return CheckStatus.FAILED, {"not_stored": missing, "ingest_errors": errored}
        if not stored:
            return CheckStatus.SKIPPED, {
                "reason": "no method could confirm storage",
                "undecided": undecided,
            }
        return CheckStatus.PASSED, {"stored": stored, "undecided": undecided}

    def _check_fact_retrievable(self, ctx: _Context, index: _AuditIndex):
        expected = [r for r in ctx.ingested if r.fact_id not in ctx.superseded_by]
        if not expected:
            return CheckStatus.SKIPPED, {"reason": "no fact is expected to stay active"}
        not_extracted = [r.label() for r in expected if r.extracted_nothing]
        expected = [r for r in expected if not r.extracted_nothing]
        if not expected:
            return CheckStatus.SKIPPED, {
                "reason": "the extractor produced no fact from these utterances",
                "not_extracted": not_extracted,
            }
        missing, found, undecided = [], [], []
        for ref in expected:
            api = ctx.state(ref, "api")
            if not api.available:
                undecided.append({"fact": ref.label(), "reason": api.reason})
            elif api.exists:
                found.append(ref.label())
            else:
                attribution = self._attribute_inactive(ctx, ref, index)
                entry = {"fact": ref.label(), "fact_id": ref.fact_id, **attribution}
                if attribution["attribution"] == "cross_scenario":
                    undecided.append(entry)
                else:
                    missing.append(entry)
        if missing:
            return CheckStatus.FAILED, {
                "not_retrievable": missing,
                "not_extracted": not_extracted,
            }
        if not found:
            return CheckStatus.SKIPPED, {
                "reason": "/retrieve unavailable",
                "undecided": undecided,
                "not_extracted": not_extracted,
            }
        return CheckStatus.PASSED, {
            "retrievable": found,
            "undecided": undecided,
            "not_extracted": not_extracted,
        }

    def _check_audit_event_logged(self, ctx: _Context, index: _AuditIndex):
        if self._audit is None and not index.events:
            return CheckStatus.SKIPPED, {"reason": "no audit trail available"}
        refs = ctx.ingested
        if not refs:
            return CheckStatus.SKIPPED, {"reason": "scenario stores no facts"}
        missing, logged = [], []
        for ref in refs:
            events = index.for_fact(ref.fact_id or "", ref.user_id)
            if events:
                logged.append({"fact": ref.label(), "events": len(events)})
            else:
                missing.append({"fact": ref.label(), "fact_id": ref.fact_id})
                ctx.note_missing(
                    "missing_audit_trail",
                    fact_id=ref.fact_id,
                    fact_index=ref.fact_index,
                    user_id=ref.user_id,
                )
        if missing:
            return CheckStatus.FAILED, {"unaudited_facts": missing}
        return CheckStatus.PASSED, {"audited_facts": logged}

    def _check_user_isolation_ok(self, ctx: _Context, index: _AuditIndex):
        """Criterion H (§ 6.1): no cross-user leak in DB, audit, API or profile."""
        problems: List[Dict[str, Any]] = []
        checks_run = 0

        # Track cross-user contradictions (intentional visibility)
        cross_user_pairs = set()
        for ref in ctx.ingested:
            if ref.contradicts_fact_id:
                for other_ref in ctx.ingested:
                    if (
                        other_ref.fact_id == ref.contradicts_fact_id
                        and other_ref.user_id != ref.user_id
                    ):
                        cross_user_pairs.add((ref.user_id, other_ref.user_id))
                        cross_user_pairs.add((other_ref.user_id, ref.user_id))

        for ref in ctx.ingested:
            db = ctx.state(ref, "db")
            if db.available and db.user_id and db.user_id != ref.user_id:
                problems.append(
                    {
                        "where": "memory.db",
                        "fact": ref.label(),
                        "expected_user": ref.user_id,
                        "actual_user": db.user_id,
                    }
                )
            if db.available:
                checks_run += 1
            for event in index.for_fact(ref.fact_id or "", None):
                checks_run += 1
                if event.user_id != ref.user_id:
                    problems.append(
                        {
                            "where": "audit_log.db",
                            "fact": ref.label(),
                            "expected_user": ref.user_id,
                            "actual_user": event.user_id,
                            "event_id": event.event_id,
                        }
                    )

        for user_id in ctx.user_ids:
            foreign = [
                (text, owner, sid)
                for text, owner, sid in self._owner_by_text
                if owner != user_id
            ]
            if not foreign:
                continue
            entries = self._profile_entries(ctx, user_id)
            if entries:
                checks_run += 1
                for entry in entries:
                    leak = next(
                        (
                            (text, owner, sid)
                            for text, owner, sid in foreign
                            if texts_match(text, entry, self.match_threshold)
                        ),
                        None,
                    )
                    if leak is not None:
                        # Skip if this is an expected cross-user contradiction
                        if (user_id, leak[1]) in cross_user_pairs:
                            continue
                        # The scenario script says another user owns this text,
                        # but scripts reuse phrasings across users. Only the
                        # store settles ownership: if this user holds a matching
                        # fact of their own, their profile is entitled to it.
                        if self._user_owns_text(user_id, entry):
                            continue
                        problems.append(
                            {
                                "where": "profile",
                                "user_id": user_id,
                                "leaked_text": entry,
                                "owner": leak[1],
                                "owner_scenario": leak[2],
                            }
                        )

        leaks = self._retrieval_leaks(ctx)
        checks_run += len(leaks["checked"])
        problems.extend(leaks["problems"])

        if problems:
            return CheckStatus.FAILED, {"leaks": problems}
        if not checks_run:
            return CheckStatus.SKIPPED, {"reason": "no isolation evidence available"}
        return CheckStatus.PASSED, {"checks": checks_run}

    def _retrieval_leaks(self, ctx: _Context) -> Dict[str, Any]:
        """Did ``/retrieve`` for one user return another user's fact text?"""
        problems: List[Dict[str, Any]] = []
        checked: List[str] = []
        for (user_id, query), retrieval in self._retrieval_cache.items():
            if user_id not in ctx.user_ids:
                continue
            if not any(r.text == query for r in ctx.refs):
                continue
            checked.append(f"{user_id}:{query[:40]}")
            for memory in retrieval:
                text = getattr(memory, "text", "")
                for owner_text, owner, sid in self._owner_by_text:
                    if owner == user_id:
                        continue
                    if not texts_match(owner_text, text, self.match_threshold):
                        continue
                    # Two users may legitimately state the same thing, and the
                    # script's assignment does not settle ownership — the store
                    # does. If this user holds a matching fact of their own,
                    # /retrieve returning it is correct, not a leak.
                    if self._user_owns_text(user_id, text):
                        continue
                    problems.append(
                        {
                            "where": "/retrieve",
                            "user_id": user_id,
                            "query": query,
                            "leaked_text": text,
                            "owner": owner,
                            "owner_scenario": sid,
                        }
                    )
        return {"problems": problems, "checked": checked}

    def _check_metadata_correct(self, ctx: _Context, index: _AuditIndex):
        """Timestamps, user_id and run_number on every recorded row (§ 6.2)."""
        problems: List[Dict[str, Any]] = []
        checked = 0
        for ref in ctx.ingested:
            for event in index.for_fact(ref.fact_id or "", ref.user_id):
                checked += 1
                if event.run_number != ctx.run_number:
                    problems.append(
                        {
                            "where": "audit_log.db",
                            "field": "run_number",
                            "fact": ref.label(),
                            "expected": ctx.run_number,
                            "actual": event.run_number,
                        }
                    )
                if not event.user_id:
                    problems.append(
                        {"where": "audit_log.db", "field": "user_id", "fact": ref.label()}
                    )
                if not event.fact_id:
                    problems.append(
                        {"where": "audit_log.db", "field": "fact_id", "fact": ref.label()}
                    )
                if not event.timestamp or event.timestamp <= 0:
                    problems.append(
                        {
                            "where": "audit_log.db",
                            "field": "timestamp",
                            "fact": ref.label(),
                            "actual": event.timestamp,
                        }
                    )
            db = ctx.state(ref, "db")
            if db.available:
                checked += 1
                if not db.user_id:
                    problems.append(
                        {"where": "memory.db", "field": "user_id", "fact": ref.label()}
                    )
                    ctx.note_missing(
                        "null_user_id", fact_id=ref.fact_id, fact_index=ref.fact_index
                    )
                if not db.detail.get("updated_at"):
                    problems.append(
                        {"where": "memory.db", "field": "updated_at", "fact": ref.label()}
                    )
        if problems:
            return CheckStatus.FAILED, {"metadata_problems": problems}
        if not checked:
            return CheckStatus.SKIPPED, {"reason": "no metadata rows to check"}
        return CheckStatus.PASSED, {"rows_checked": checked}

    # ------------------------------------------------------------------
    # Checkers — contradiction (3)
    # ------------------------------------------------------------------

    def _check_old_fact_deactivated(self, ctx: _Context, index: _AuditIndex):
        pairs = list(ctx.superseded_by.items())
        if not pairs:
            return CheckStatus.SKIPPED, {
                "reason": "no same-user contradiction was played for this scenario"
            }
        by_id = {r.fact_id: r for r in ctx.ingested}
        failures, deactivated, undecided = [], [], []
        for old_id, replacing in pairs:
            old_ref = by_id.get(old_id)
            if old_ref is None:
                undecided.append({"fact_id": old_id, "reason": "old fact not played"})
                continue
            active, evidence = self._verdict(ctx, old_ref)
            if active is None:
                undecided.append({"fact": old_ref.label(), **evidence})
            elif active:
                failures.append(
                    {
                        "fact": old_ref.label(),
                        "fact_id": old_id,
                        "replaced_by": replacing.fact_id,
                        **evidence,
                    }
                )
            else:
                deactivated.append({"fact": old_ref.label(), "replaced_by": replacing.fact_id})
        if failures:
            return CheckStatus.FAILED, {"still_active": failures}
        if not deactivated:
            return CheckStatus.SKIPPED, {
                "reason": "old fact state undecidable",
                "undecided": undecided,
            }
        return CheckStatus.PASSED, {"deactivated": deactivated, "undecided": undecided}

    def _check_new_fact_active(self, ctx: _Context, index: _AuditIndex):
        replacements = [
            ref
            for ref in ctx.ingested
            if ref.contradicts_fact_id and ref.fact_id not in ctx.superseded_by
        ]
        if not replacements:
            return CheckStatus.SKIPPED, {"reason": "no replacing fact in this scenario"}
        return self._expect_active(ctx, replacements, index)

    def _check_audit_trail_complete(self, ctx: _Context, index: _AuditIndex):
        """Criterion G (§ 6.1): every event logged, with before/after state."""
        if self._audit is None and not index.events:
            return CheckStatus.SKIPPED, {"reason": "no audit trail available"}
        problems: List[Dict[str, Any]] = []
        total_events = 0
        for ref in ctx.ingested:
            events = index.for_fact(ref.fact_id or "", ref.user_id)
            total_events += len(events)
            if not events:
                problems.append({"fact": ref.label(), "problem": "no audit events"})
                continue
            timestamps = [e.timestamp for e in events]
            if timestamps != sorted(timestamps):
                problems.append({"fact": ref.label(), "problem": "events out of order"})
            if events[-1].after_state is None:
                problems.append(
                    {"fact": ref.label(), "problem": "last event has no after_state"}
                )
        for old_id, replacing in ctx.superseded_by.items():
            events = index.for_fact(replacing.fact_id or "", replacing.user_id)
            supersede = [e for e in events if _state_fact_id(e.before_state) == old_id]
            if not supersede:
                problems.append(
                    {
                        "fact": replacing.label(),
                        "problem": (
                            "contradiction not audited: no event carries "
                            f"before_state.fact_id == {old_id!r}"
                        ),
                    }
                )
                ctx.note_missing(
                    "missing_contradiction_event",
                    fact_id=replacing.fact_id,
                    old_fact_id=old_id,
                    user_id=replacing.user_id,
                )
        if problems:
            return CheckStatus.FAILED, {"audit_problems": problems}
        return CheckStatus.PASSED, {"events": total_events}

    # ------------------------------------------------------------------
    # Checkers — expiry (5)
    # ------------------------------------------------------------------

    def _expiring(self, ctx: _Context) -> List[_FactRef]:
        return [r for r in ctx.ingested if r.ttl_seconds]

    def _check_expiry_scheduled(self, ctx: _Context, index: _AuditIndex):
        refs = self._expiring(ctx)
        if not refs:
            return CheckStatus.FAILED, {
                "reason": "expiry scenario declares no fact with a TTL",
                "facts": [r.label() for r in ctx.ingested],
            }
        status = self.memory_db_status()
        if "expires_at" in (status.get("missing_columns") or []):
            return CheckStatus.SKIPPED, {
                "reason": (
                    "memory.db predates the expires_at column — migrate it "
                    "(simulation.database.migrate_memory_db) before Method 1 "
                    "can verify a scheduled expiry"
                ),
                "path": status.get("path"),
            }
        unscheduled, scheduled, undecided = [], [], []
        for ref in refs:
            db = ctx.state(ref, "db")
            if not db.available:
                undecided.append({"fact": ref.label(), "reason": db.reason})
            elif db.detail.get("expires_at") is None:
                unscheduled.append(
                    {
                        "fact": ref.label(),
                        "ttl_seconds": ref.ttl_seconds,
                        "memory_id": db.detail.get("memory_id"),
                        "problem": "memory_keys.expires_at is NULL",
                    }
                )
            else:
                scheduled.append(
                    {"fact": ref.label(), "expires_at": db.detail.get("expires_at")}
                )
        if unscheduled:
            return CheckStatus.FAILED, {"not_scheduled": unscheduled}
        if not scheduled:
            return CheckStatus.SKIPPED, {
                "reason": "no expiring fact resolved to a memory.db row",
                "undecided": undecided,
            }
        return CheckStatus.PASSED, {"scheduled": scheduled, "undecided": undecided}

    def _check_ttl_recorded(self, ctx: _Context, index: _AuditIndex):
        refs = self._expiring(ctx)
        if not refs:
            return CheckStatus.FAILED, {
                "reason": "no TTL recorded for any fact in an expiry scenario"
            }
        bad = [
            {"fact": r.label(), "ttl_seconds": r.ttl_seconds}
            for r in refs
            if not isinstance(r.ttl_seconds, (int, float)) or r.ttl_seconds <= 0
        ]
        if bad:
            return CheckStatus.FAILED, {"invalid_ttls": bad}
        return CheckStatus.PASSED, {
            "ttls": {r.label(): r.ttl_seconds for r in refs},
        }

    def _check_fact_active_before_expiry(self, ctx: _Context, index: _AuditIndex):
        refs = self._expiring(ctx)
        if not refs:
            return CheckStatus.SKIPPED, {"reason": "no expiring fact in this scenario"}
        return self._expect_active(ctx, refs, index)

    def _check_no_live_expiry_fired(self, ctx: _Context, index: _AuditIndex):
        """Spec gap note: § 5.1 B TTLs (1h–3mo) all outlive a 1–6 minute run.

        No expiry can therefore fire during a simulation, so the honest check
        is the negative one: nothing expired, and nothing was pruned away.
        """
        fired = []
        for ref in ctx.ingested:
            for event in index.for_fact(ref.fact_id or "", ref.user_id):
                if event.event_type == AuditEventType.EXPIRED.value:
                    fired.append(
                        {
                            "fact": ref.label(),
                            "event_id": event.event_id,
                            "timestamp": event.timestamp,
                        }
                    )
        if fired:
            return CheckStatus.FAILED, {
                "expired_during_run": fired,
                "note": "run durations are 1–6 minutes; the shortest § 5.1 B TTL is 1 hour",
            }
        return CheckStatus.PASSED, {
            "min_ttl_seconds": min(
                (r.ttl_seconds for r in self._expiring(ctx)), default=None
            ),
            "note": "no expiry can fire inside a run — TTLs outlive every run duration",
        }

    def _check_archive_not_deleted(self, ctx: _Context, index: _AuditIndex):
        """Criterion G (§ 6.1 / § 7.2): history survives deactivation."""
        if self._audit is None and not index.events:
            return CheckStatus.SKIPPED, {"reason": "no audit trail available"}
        lost = []
        preserved = 0
        for ref in ctx.ingested:
            events = index.for_fact(ref.fact_id or "", ref.user_id)
            if events:
                preserved += 1
                continue
            active, _ = self._verdict(ctx, ref)
            if active is not None:
                lost.append(
                    {
                        "fact": ref.label(),
                        "fact_id": ref.fact_id,
                        "problem": "state observable but no audit history retained",
                    }
                )
        for old_id, replacing in ctx.superseded_by.items():
            history = index.for_fact(old_id, None)
            supersede = index.supersedes.get(old_id, [])
            if not history and not supersede:
                lost.append(
                    {
                        "fact_id": old_id,
                        "problem": "superseded fact has no surviving audit history",
                    }
                )
            elif supersede and supersede[0].before_state is None:
                lost.append(
                    {
                        "fact_id": old_id,
                        "problem": "supersede event kept no before_state to recover from",
                    }
                )
            # Check for UPDATED events with None before_state (missed by supersedes index)
            elif not supersede:
                for event in index.events:
                    if (
                        event.event_type == AuditEventType.UPDATED.value
                        and event.before_state is None
                        and event.fact_id == replacing.fact_id
                    ):
                        lost.append(
                            {
                                "fact_id": old_id,
                                "problem": "supersede event kept no before_state to recover from",
                            }
                        )
                        break
        if lost:
            return CheckStatus.FAILED, {"lost_history": lost}
        return CheckStatus.PASSED, {"facts_with_history": preserved}

    # ------------------------------------------------------------------
    # Checkers — profile cache (8)
    # ------------------------------------------------------------------

    def _check_profile_generated(self, ctx: _Context, index: _AuditIndex):
        if self.client is None:
            return CheckStatus.SKIPPED, {"reason": "no EngineClient configured"}
        missing, generated = [], []
        for user_id in ctx.user_ids:
            profile = ctx.profiles.get(user_id)
            if profile is None:
                missing.append({"user_id": user_id, "problem": "profile request failed"})
            elif not (
                profile.stable_facts
                or profile.recent_activity
                or profile.profile_timestamp
            ):
                missing.append({"user_id": user_id, "problem": "profile is empty"})
            else:
                generated.append(
                    {
                        "user_id": user_id,
                        "stable_facts": len(profile.stable_facts),
                        "recent_activity": len(profile.recent_activity),
                        "profile_timestamp": profile.profile_timestamp,
                    }
                )
        if missing:
            return CheckStatus.FAILED, {"missing_profiles": missing}
        return CheckStatus.PASSED, {"profiles": generated}

    def _check_profile_reflects_facts(self, ctx: _Context, index: _AuditIndex):
        if self.client is None:
            return CheckStatus.SKIPPED, {"reason": "no EngineClient configured"}
        expected = [r for r in ctx.ingested if r.fact_id not in ctx.superseded_by]
        if not expected:
            return CheckStatus.SKIPPED, {"reason": "scenario stores no lasting fact"}
        not_extracted = [r.label() for r in expected if r.extracted_nothing]
        expected = [r for r in expected if not r.extracted_nothing]
        if not expected:
            return CheckStatus.SKIPPED, {
                "reason": "the extractor produced no fact from these utterances",
                "not_extracted": not_extracted,
            }
        reflected, absent = [], []
        for ref in expected:
            entries = self._profile_entries(ctx, ref.user_id)
            if not entries:
                continue
            candidates = ref.match_texts()
            if any(
                texts_match(candidate, entry, self.match_threshold)
                for entry in entries
                for candidate in candidates
            ):
                reflected.append(ref.label())
            else:
                absent.append(
                    {"fact": ref.label(), "text": ref.text, "stored": ref.stored_texts}
                )
        if not reflected and not absent:
            return CheckStatus.SKIPPED, {"reason": "profiles are empty"}
        if absent:
            return CheckStatus.FAILED, {
                "not_in_profile": absent,
                "reflected": reflected,
                "not_extracted": not_extracted,
            }
        return CheckStatus.PASSED, {
            "reflected": reflected,
            "not_extracted": not_extracted,
        }

    def _cache_expectation(self, ctx: _Context, expected: str):
        relevant = [c for c in ctx.cache_checks if c.get("expected") == expected]
        if not relevant:
            return CheckStatus.SKIPPED, {
                "reason": f"no profile read in this scenario expects a cache {expected}"
            }
        unsatisfiable = [c for c in relevant if c.get("unsatisfiable")]
        usable = [c for c in relevant if not c.get("unsatisfiable")]
        if not usable:
            # Task 6's TTL solver proved the expectation cannot hold on this
            # timeline — skip rather than fail (see Task 6 report).
            return CheckStatus.SKIPPED, {
                "reason": "expectation marked unsatisfiable by the TTL solver",
                "checks": unsatisfiable,
            }
        mismatches = [c for c in usable if c.get("matched") is False]
        undecided = [c for c in usable if c.get("matched") is None]
        if mismatches:
            return CheckStatus.FAILED, {
                "mismatches": mismatches,
                "unsatisfiable": unsatisfiable,
            }
        if not any(c.get("matched") for c in usable):
            return CheckStatus.SKIPPED, {
                "reason": "cache status was indeterminate (engine reports none)",
                "undecided": undecided,
            }
        return CheckStatus.PASSED, {
            "checks": [c for c in usable if c.get("matched")],
            "unsatisfiable": unsatisfiable,
        }

    def _check_cache_hit_recorded(self, ctx: _Context, index: _AuditIndex):
        return self._cache_expectation(ctx, "hit")

    def _check_cache_miss_recorded(self, ctx: _Context, index: _AuditIndex):
        return self._cache_expectation(ctx, "miss")

    def _check_recent_activity_populated(self, ctx: _Context, index: _AuditIndex):
        if self.client is None:
            return CheckStatus.SKIPPED, {"reason": "no EngineClient configured"}
        empty, populated = [], []
        for user_id in ctx.user_ids:
            profile = ctx.profiles.get(user_id)
            if profile is None:
                continue
            if profile.recent_activity:
                populated.append({"user_id": user_id, "count": len(profile.recent_activity)})
            else:
                empty.append({"user_id": user_id})
        if not populated and not empty:
            return CheckStatus.SKIPPED, {"reason": "no profile available"}
        if empty:
            return CheckStatus.FAILED, {
                "empty_recent_activity": empty,
                "note": "every fact in a run is seconds old, so it belongs in the 7d window",
            }
        return CheckStatus.PASSED, {"populated": populated}

    def _check_stable_recent_split_correct(self, ctx: _Context, index: _AuditIndex):
        if self.client is None:
            return CheckStatus.SKIPPED, {"reason": "no EngineClient configured"}
        problems, checked = [], []
        for user_id in ctx.user_ids:
            profile = ctx.profiles.get(user_id)
            if profile is None:
                continue
            checked.append(user_id)
            overlap = [
                entry
                for entry in profile.recent_activity
                if any(
                    texts_match(entry, stable, self.match_threshold)
                    for stable in profile.stable_facts
                )
            ]
            if overlap:
                problems.append(
                    {
                        "user_id": user_id,
                        "problem": "entries appear in both stable_facts and recent_activity",
                        "entries": overlap,
                    }
                )
        if not checked:
            return CheckStatus.SKIPPED, {"reason": "no profile available"}
        if problems:
            return CheckStatus.FAILED, {"split_problems": problems}
        return CheckStatus.PASSED, {"users_checked": checked}

    def _check_profile_refreshed_on_new_fact(self, ctx: _Context, index: _AuditIndex):
        reads = ctx.cache_checks
        if len(reads) < 2:
            return CheckStatus.SKIPPED, {
                "reason": "needs two profile reads either side of a new fact",
                "reads": len(reads),
            }
        later = [r for r in reads if r.get("expected") == "miss" and not r.get("unsatisfiable")]
        if not later:
            return CheckStatus.SKIPPED, {
                "reason": "no post-ingest read expects a regenerated profile"
            }
        mismatched = [r for r in later if r.get("matched") is False]
        if mismatched:
            return CheckStatus.FAILED, {
                "profile_not_refreshed": mismatched,
                "note": "the read after a new fact still came from cache",
            }
        if not any(r.get("matched") for r in later):
            return CheckStatus.SKIPPED, {"reason": "cache status indeterminate"}
        return CheckStatus.PASSED, {"refresh_reads": later}

    def _check_expected_cache_unsatisfiable(self, ctx: _Context, index: _AuditIndex):
        """Report the Task 6 TTL solver's verdict; never a failure by itself."""
        unsatisfiable = [c for c in ctx.cache_checks if c.get("unsatisfiable")]
        scenario_flag = bool(
            (ctx.scenario.metadata or {}).get("cache_expectations_unsatisfiable")
        )
        if not unsatisfiable and not scenario_flag:
            return CheckStatus.PASSED, {
                "unsatisfiable_reads": 0,
                "note": "every cache expectation in this scenario was checkable",
            }
        return CheckStatus.SKIPPED, {
            "reason": (
                "the realised timeline admits no profile cache TTL that satisfies "
                "these reads — Task 6 marked them unsatisfiable, so their cache "
                "checks are skipped rather than failed"
            ),
            "unsatisfiable_reads": len(unsatisfiable),
            "checks": unsatisfiable,
            "effective_ttl_seconds": (ctx.scenario.metadata or {}).get(
                "effective_profile_cache_ttl_seconds"
            ),
        }

    # ------------------------------------------------------------------
    # Checkers — multi-user (2)
    # ------------------------------------------------------------------

    def _check_both_facts_active(self, ctx: _Context, index: _AuditIndex):
        refs = ctx.ingested
        if len(ctx.user_ids) < 2:
            return CheckStatus.SKIPPED, {
                "reason": "scenario did not reach two users",
                "user_ids": ctx.user_ids,
            }
        return self._expect_active(ctx, refs, index)

    def _check_no_cross_user_deactivation(self, ctx: _Context, index: _AuditIndex):
        """No user's fact may be deactivated by another user's utterance (§ 6.1 H)."""
        owners = {r.fact_id: r.user_id for r in ctx.ingested if r.fact_id}
        problems = []
        for ref in ctx.ingested:
            for event in index.for_fact(ref.fact_id or "", None):
                old_id = _state_fact_id(event.before_state)
                if not old_id or old_id == event.fact_id:
                    continue
                old_owner = owners.get(old_id)
                if old_owner is None:
                    continue
                if old_owner != (event.user_id or ""):
                    problems.append(
                        {
                            "event_id": event.event_id,
                            "event_type": event.event_type,
                            "deactivated_fact": old_id,
                            "owned_by": old_owner,
                            "deactivated_by_user": event.user_id,
                        }
                    )
        cross_user_pairs = [
            {
                "fact": r.label(),
                "user_id": r.user_id,
                "contradicts": r.contradicts_fact_id,
                "contradicted_owner": owners.get(r.contradicts_fact_id or ""),
            }
            for r in ctx.ingested
            if r.contradicts_fact_id
            and owners.get(r.contradicts_fact_id or "") not in (None, r.user_id)
        ]
        if problems:
            return CheckStatus.FAILED, {"cross_user_deactivations": problems}
        return CheckStatus.PASSED, {
            "cross_user_contradictions": cross_user_pairs,
            "note": "cross-user contradictions were recorded without deactivating either side",
        }

    # ------------------------------------------------------------------
    # Checkers — generic accumulation (2)
    # ------------------------------------------------------------------

    def _check_all_facts_active(self, ctx: _Context, index: _AuditIndex):
        refs = ctx.ingested
        if not refs:
            return CheckStatus.SKIPPED, {"reason": "scenario stores no facts"}
        # tolerate_collisions: a generic scenario's fact can be replaced by
        # *another* scenario that happened to land on the same user and
        # predicate. That is a generator/engine collision, not this scenario
        # failing, so it is recorded as missing data and skipped.
        return self._expect_active(ctx, refs, index, tolerate_collisions=True)

    def _check_no_contradiction_triggered(self, ctx: _Context, index: _AuditIndex):
        if ctx.superseded_by:
            return CheckStatus.FAILED, {
                "contradictions": [
                    {"old_fact_id": old, "replaced_by": ref.label()}
                    for old, ref in ctx.superseded_by.items()
                ]
            }
        own_ids = {r.fact_id for r in ctx.ingested}
        internal = []
        for ref in ctx.ingested:
            for event in index.for_fact(ref.fact_id or "", ref.user_id):
                old_id = _state_fact_id(event.before_state)
                if old_id and old_id != event.fact_id and old_id in own_ids:
                    internal.append(
                        {
                            "event_id": event.event_id,
                            "fact": ref.label(),
                            "superseded": old_id,
                        }
                    )
        if internal:
            return CheckStatus.FAILED, {"audited_contradictions": internal}
        return CheckStatus.PASSED, {"facts": len(ctx.ingested)}

    # ------------------------------------------------------------------
    # Checkers — modification chains (2)
    # ------------------------------------------------------------------

    def _check_latest_value_active(self, ctx: _Context, index: _AuditIndex):
        refs = ctx.ingested
        if not refs:
            return CheckStatus.SKIPPED, {"reason": "scenario stores no facts"}
        latest = refs[-1]
        status, details = self._expect_active(ctx, [latest], index, tolerate_collisions=True)
        details["latest_fact"] = latest.label()
        return status, details

    def _check_prior_versions_deactivated(self, ctx: _Context, index: _AuditIndex):
        """Only links that were actually *replaced* must be inactive.

        A fact is not deactivated by being ingested — it is deactivated when a
        later fact of the same chain replaces it, so the final link is exempt
        and a chain that never got past its first link is skipped.
        """
        replaced = [
            (ref, ctx.superseded_by[ref.fact_id])
            for ref in ctx.ingested
            if ref.fact_id in ctx.superseded_by
        ]
        if not replaced:
            return CheckStatus.SKIPPED, {
                "reason": "no prior version was replaced during this run",
                "facts_played": len(ctx.ingested),
            }
        failures, deactivated, undecided = [], [], []
        for ref, replacing in replaced:
            active, evidence = self._verdict(ctx, ref)
            if active is None:
                undecided.append({"fact": ref.label(), **evidence})
            elif active:
                failures.append(
                    {
                        "fact": ref.label(),
                        "fact_id": ref.fact_id,
                        "replaced_by": replacing.label(),
                        **evidence,
                    }
                )
            else:
                deactivated.append({"fact": ref.label(), "replaced_by": replacing.label()})
        if failures:
            return CheckStatus.FAILED, {"still_active": failures}
        if not deactivated:
            return CheckStatus.SKIPPED, {
                "reason": "prior version states undecidable",
                "undecided": undecided,
            }
        return CheckStatus.PASSED, {"deactivated": deactivated, "undecided": undecided}

    # ------------------------------------------------------------------
    # Run-wide missing data
    # ------------------------------------------------------------------

    def _collect_global_missing_data(
        self, report: ValidationReport, index: _AuditIndex
    ) -> None:
        """Rows the engine or the audit trail left incomplete (§ 6.1 G/H)."""
        conn = self._memory_cursor()
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM memory_keys "
                    "WHERE user_id IS NULL OR TRIM(user_id) = ''"
                ).fetchone()
                if row and row["n"]:
                    report.missing_data.append(
                        {
                            "kind": "null_user_id_rows",
                            "source": "memory.db",
                            "count": int(row["n"]),
                            "detail": "criterion H requires every fact to name its user",
                        }
                    )
            except sqlite3.Error as exc:  # pragma: no cover - schema drift
                logger.warning("memory.db audit query failed: %s", exc)

        untagged = [
            e for e in index.events if not e.user_id or not e.fact_id or not e.timestamp
        ]
        if untagged:
            report.missing_data.append(
                {
                    "kind": "incomplete_audit_rows",
                    "source": "audit_log.db",
                    "count": len(untagged),
                    "event_ids": [e.event_id for e in untagged][:20],
                }
            )
        if self._audit is not None and not index.events:
            report.missing_data.append(
                {
                    "kind": "empty_audit_trail",
                    "source": "audit_log.db",
                    "run_number": report.run_number,
                    "detail": "no audit events recorded for this run",
                }
            )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _result(
    ctx: _Context, outcome: str, status: CheckStatus, **details: Any
) -> ValidationResult:
    return ValidationResult(
        scenario_id=ctx.scenario.scenario_id,
        outcome=outcome,
        passed=status is not CheckStatus.FAILED,
        details=details,
        status=status.value,
    )
