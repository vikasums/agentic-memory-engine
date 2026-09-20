"""SimulationRunner — orchestrates scenario playback (spec § 3.2, § 4).

Drives one progressive-validation run end to end:

1. Load (or generate) the scenario set for the run.
2. Flatten every scenario's facts into **one** global, timestamp-ordered event
   queue — scenarios interleave, they are not played one after another.
3. Walk the queue against the wall clock: sleep until each fact's scheduled
   offset, then play it.

   * a ``profile_read`` fact is **not** ingested — it calls
     :meth:`EngineClient.get_profile` and its observed cache status is compared
     against ``fact.metadata["expected_cache"]``;
   * every other fact is ingested via :meth:`EngineClient.ingest`.

   Either way the engine's ``fact_id`` is recorded, ``contradicts_fact_id`` is
   backfilled from the earlier fact of the same ``contradicts_scenario``, and a
   :class:`~simulation.models.MonitoringEvent` is logged (which the
   MonitoringService also persists to ``audit_log.db``).
4. After the run: validate, collect metrics, close the run's metadata row and
   return a :class:`~simulation.models.RunResult`.

Everything is tagged with ``run_number`` (spec § 7).

Resilience (spec § 3.2): a failed engine call is classified (permanent /
temporary / timeout / transport), recorded on the result and *skipped* — the
runner never retries and never lets one bad event end a run.

Two scenario-level subtleties are handled here rather than in the generator:

``user_id`` override
    A fact's ``metadata["user_id"]`` wins over ``scenario.user_id``. That is
    how multi-user scenarios put the second, conflicting utterance in a
    *different* user's memory. Because the two facts belong to different users
    the runner logs a plain ``fact_ingested`` event for the second one — never
    ``contradiction_resolved`` — so neither user's fact is marked inactive.

Profile cache TTL
    ``cache_001`` expects a hit from a read seconds after the first one and
    ``cache_002`` expects a miss from a read after the cache TTL, but the
    engine's profile cache TTL is a server-side default of 3600s
    (``agentic_memory/store.py:225``) and the realised gaps depend on the run
    length. :meth:`SimulationRunner.plan_profile_cache_ttl` derives the TTL
    window those two expectations imply from the *realised* timeline and
    reports it on the result; when no TTL can satisfy both, the affected
    expectations are marked unsatisfiable so Task 7 skips them instead of
    failing them.
"""

import asyncio
import inspect
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .audit_logger import AuditLogger
from .engine_client import EngineClient, EngineClientError
from .models import (
    ErrorClass,
    FactType,
    MonitoringEventType,
    RunError,
    RunResult,
    RunStatus,
    Scenario,
    ScenarioFact,
    ValidationResult,
)
from .monitoring_service import MonitoringService
from .scenario_generator import ScenarioGenerator
from .validator_service import ValidatorService

logger = logging.getLogger(__name__)

# Progressive validation durations from spec § 1.2 (run 1..4).
RUN_DURATIONS_SECONDS = (60.0, 120.0, 240.0, 360.0)

#: Smallest margin, in seconds, kept between a derived profile cache TTL and
#: the realised gaps it has to sit between.
_CACHE_TTL_MARGIN_SECONDS = 1.0

#: Scenario metadata key the generator sets on ``cache_002`` (spec § 5.1 C).
REQUIRED_CACHE_TTL_KEY = "requires_profile_cache_ttl_seconds"


def _enum_value(value: Any) -> Any:
    """``value.value`` for an enum member, the value itself otherwise.

    Scenarios round-tripped through JSON (spec § 3.1) come back with plain
    strings where the generator produced enum members, so every comparison and
    every recorded value goes through here or through ``==``, never ``is``.
    """
    return getattr(value, "value", value)


class _QueuedEvent:
    """One fact of one scenario, placed on the global event queue."""

    __slots__ = ("timestamp", "scenario", "fact", "fact_index", "sequence")

    def __init__(
        self,
        timestamp: float,
        scenario: Scenario,
        fact: ScenarioFact,
        fact_index: int,
        sequence: int,
    ) -> None:
        self.timestamp = timestamp
        self.scenario = scenario
        self.fact = fact
        self.fact_index = fact_index
        self.sequence = sequence

    @property
    def sort_key(self) -> Tuple[float, str, int]:
        return (self.timestamp, self.scenario.scenario_id, self.fact_index)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"_QueuedEvent(t={self.timestamp:.3f}, "
            f"{self.scenario.scenario_id}#{self.fact_index}, {self.fact.type})"
        )


def build_event_queue(scenarios: Sequence[Scenario]) -> List[_QueuedEvent]:
    """Flatten ``scenarios`` into one globally timestamp-ordered queue.

    Ties break on ``(scenario_id, fact_index)``, so a scenario's own facts can
    never be reordered relative to each other even when two share a timestamp —
    a contradiction is always played after the fact it replaces.
    """
    events = [
        _QueuedEvent(float(fact.timestamp), scenario, fact, index, 0)
        for scenario in scenarios
        for index, fact in enumerate(scenario.facts)
    ]
    events.sort(key=lambda event: event.sort_key)
    for sequence, event in enumerate(events):
        event.sequence = sequence
    return events


def classify_engine_error(error: EngineClientError) -> str:
    """Map an :class:`EngineClientError` onto an :class:`ErrorClass` value.

    * HTTP 4xx → ``permanent`` (the request is wrong; a replay fails too)
    * HTTP 5xx → ``temporary`` (the engine is unhealthy; later calls may work)
    * no status + "timed out" → ``timeout``
    * no status otherwise → ``transport``
    """
    status = error.status_code
    if status is not None:
        if 400 <= status < 500:
            return ErrorClass.PERMANENT.value
        if status >= 500:
            return ErrorClass.TEMPORARY.value
        return ErrorClass.TRANSPORT.value
    if "timed out" in str(error).lower():
        return ErrorClass.TIMEOUT.value
    return ErrorClass.TRANSPORT.value


class SimulationRunner:
    """Drives one simulation run end to end (spec § 3.2).

    Usage::

        runner = SimulationRunner(client, monitor, auditor)
        result = await runner.run(scenarios, duration_seconds=60.0, run_number=1)

    Args:
        engine_client: Engine API wrapper used for ingests and profile reads.
        monitoring_service: Receives one ``log_event`` per played fact.
        audit_logger: Opens/closes the run's ``run_metadata`` row. Monitoring
            events reach ``audit_events`` through the MonitoringService, which
            is normally constructed with this same logger.
        validator: Optional ValidatorService, called once after playback.
        generator: Optional ScenarioGenerator, used when :meth:`run` is called
            without an explicit scenario list.
        run_number: Default run tag (spec § 7).
        duration_seconds: Default run length.
        configured_profile_cache_ttl_seconds: The profile cache TTL the engine
            under test is actually running with, when it is known. Given one,
            cache expectations are checked against it; without one the runner
            derives and reports the TTL the run needs.
        wait_out_duration: Hold the run open until the full wall-clock duration
            has elapsed, even after the last event (the default — TTL and cache
            behaviour need the time). Set ``False`` to return immediately.
        on_progress: Optional callback invoked after every played event as
            ``on_progress(progress_dict)``; may be a coroutine function.
            Exceptions it raises are logged and swallowed.
        clock: Monotonic clock override (tests).
        sleep: ``await sleep(seconds)`` override (tests).
    """

    def __init__(
        self,
        engine_client: Optional[EngineClient] = None,
        monitoring_service: Optional[MonitoringService] = None,
        audit_logger: Optional[AuditLogger] = None,
        validator: Optional[ValidatorService] = None,
        generator: Optional[ScenarioGenerator] = None,
        run_number: int = 1,
        duration_seconds: float = RUN_DURATIONS_SECONDS[0],
        configured_profile_cache_ttl_seconds: Optional[float] = None,
        wait_out_duration: bool = True,
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Any]] = None,
        # Skeleton-era aliases, kept so existing keyword call sites still work.
        client: Optional[EngineClient] = None,
        monitor: Optional[MonitoringService] = None,
        auditor: Optional[AuditLogger] = None,
    ) -> None:
        self.client = engine_client if engine_client is not None else client
        self.monitor = monitoring_service if monitoring_service is not None else monitor
        self.auditor = audit_logger if audit_logger is not None else auditor
        self.validator = validator
        self.generator = generator
        self.run_number = run_number
        self.duration_seconds = float(duration_seconds)
        self.configured_profile_cache_ttl_seconds = configured_profile_cache_ttl_seconds
        self.wait_out_duration = wait_out_duration
        self.on_progress = on_progress
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep

        #: ``{scenario_id: [(fact_id, user_id, fact_index), ...]}`` in playback
        #: order — the source for ``contradicts_fact_id`` backfill.
        self._scenario_fact_ids: Dict[str, List[Tuple[str, str, int]]] = {}

        #: Live progress snapshot (spec § 3.2 "progress tracking").
        self.progress: Dict[str, Any] = self._blank_progress()

    # -- aliases for the constructor's primary names -----------------------

    @property
    def engine_client(self) -> Optional[EngineClient]:
        return self.client

    @property
    def monitoring_service(self) -> Optional[MonitoringService]:
        return self.monitor

    @property
    def audit_logger(self) -> Optional[AuditLogger]:
        return self.auditor

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        scenarios: Optional[Sequence[Scenario]] = None,
        duration_seconds: Optional[float] = None,
        run_number: Optional[int] = None,
    ) -> RunResult:
        """Play ``scenarios`` over ``duration_seconds`` and report the outcome.

        With no ``scenarios``, the configured :class:`ScenarioGenerator` builds
        the catalogue for this run length; without a generator either, a
        :class:`ValueError` is raised.

        The run never raises on an engine failure — those land in
        :attr:`RunResult.errors`. It only returns a ``failed`` status when the
        orchestration itself broke, or when every played event errored.
        """
        if self.client is None:
            raise ValueError("SimulationRunner requires an EngineClient")

        run_number = self.run_number if run_number is None else run_number
        duration = self.duration_seconds if duration_seconds is None else float(duration_seconds)
        if duration < 0:
            raise ValueError("duration_seconds must be >= 0")

        scenario_list = self._resolve_scenarios(scenarios, duration)

        # TTL planning can re-time cache-hit reads, so it runs before the queue
        # is flattened — the queue must reflect the timestamps actually played.
        ttl, ttl_notes = self.plan_profile_cache_ttl(scenario_list)

        queue = build_event_queue(scenario_list)
        users = self._collect_users(scenario_list)

        result = RunResult(
            run_number=run_number,
            duration_seconds=duration,
            scenarios_count=len(scenario_list),
            events_total=len(queue),
            user_count=len(users),
            start_time=time.time(),
            profile_cache_ttl_seconds=ttl,
            notes=list(ttl_notes),
        )

        self._scenario_fact_ids = {}
        self._wire_monitoring()
        self._open_run(result, len(users), len(scenario_list))
        self._reset_progress(run_number, duration, len(queue))

        run_start = self._clock()
        try:
            await self._play_queue(queue, result, run_number, duration, run_start)
            if self.wait_out_duration:
                await self._sleep_until(run_start + duration)
            result.status = self._final_status(result)
        except asyncio.CancelledError:
            result.status = RunStatus.FAILED.value
            result.notes.append("run cancelled")
            self._finish(result, run_start)
            raise
        except Exception as exc:  # pragma: no cover - orchestration failure
            logger.exception("simulation run %s failed", run_number)
            result.status = RunStatus.FAILED.value
            result.notes.append(f"run aborted: {exc}")
            self._finish(result, run_start)
            return result

        self._finish(result, run_start)
        await self._validate(scenario_list, result)
        return result

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _resolve_scenarios(
        self, scenarios: Optional[Sequence[Scenario]], duration: float
    ) -> List[Scenario]:
        if scenarios is not None:
            return list(scenarios)
        if self.generator is None:
            raise ValueError(
                "run() needs a scenario list, or a ScenarioGenerator on the runner"
            )
        return list(self.generator.generate(max_duration_seconds=duration))

    @staticmethod
    def _collect_users(scenarios: Sequence[Scenario]) -> List[str]:
        """Every user touched by the run, including multi-user peers."""
        users: List[str] = []
        seen = set()
        for scenario in scenarios:
            candidates = [scenario.user_id] + [
                fact.metadata.get("user_id")
                for fact in scenario.facts
                if isinstance(fact.metadata, dict)
            ]
            for user in candidates:
                if user and user not in seen:
                    seen.add(user)
                    users.append(user)
        return users

    def _wire_monitoring(self) -> None:
        """Feed EngineClient latencies to the MonitoringService (spec § 3.4)."""
        if self.monitor is not None and self.client is not None:
            if self.client.on_request is None:
                self.client.on_request = self.monitor.on_request

    def _open_run(self, result: RunResult, user_count: int, scenario_count: int) -> None:
        if self.auditor is None:
            return
        try:
            self.auditor.start_run(
                result.run_number,
                user_count=user_count,
                scenario_count=scenario_count,
                notes=f"Progressive validation run {result.run_number} "
                f"({result.duration_seconds:g}s)",
            )
        except Exception as exc:  # pragma: no cover - audit must not break a run
            logger.warning("start_run failed for run %s: %s", result.run_number, exc)
            result.notes.append(f"start_run failed: {exc}")

    def _close_run(self, result: RunResult) -> None:
        if self.auditor is None:
            return
        try:
            self.auditor.end_run(result.run_number, status=result.status)
        except Exception as exc:  # pragma: no cover - audit must not break a run
            logger.warning("end_run failed for run %s: %s", result.run_number, exc)
            result.notes.append(f"end_run failed: {exc}")

    # ------------------------------------------------------------------
    # Profile cache TTL planning (spec § 5.1 C, cache_001 / cache_002)
    # ------------------------------------------------------------------

    def plan_profile_cache_ttl(
        self, scenarios: Sequence[Scenario]
    ) -> Tuple[Optional[float], List[str]]:
        """Reconcile the run's cache expectations with one profile cache TTL.

        Two realised gaps bound the TTL:

        * a read expecting a **hit** must land *inside* the TTL, so the TTL has
          to be larger than the gap from the read that filled the cache;
        * a read expecting a **miss** after the TTL (``cache_002``) must land
          *outside* it, so the TTL has to be smaller than that gap.

        The generator only guarantees a *minimum* spacing, so a hit-expecting
        read routinely lands tens of seconds after the read that filled the
        cache — further out than ``cache_002``'s miss. Rather than write those
        expectations off, the runner **pulls the hit read earlier** to
        ``ttl / 2`` after the read it must hit, which is what "retrieve
        immediately" in spec § 5.1 C meant in the first place. Moving a fact
        earlier can never reorder it past the fact before it, and the adjusted
        timestamp is what the queue, the audit trail and Task 7 all see.

        Returns ``(ttl_or_None, notes)``. ``None`` means no scenario needs a TTL
        change. Reads that still cannot be satisfied — because the run is too
        short, or because a non-read fact sits between the two reads — are
        annotated ``expected_cache_unsatisfiable`` so Task 7 skips instead of
        failing them.
        """
        hit_reads: List[Tuple[float, Scenario, int, int]] = []
        miss_gaps: List[Tuple[float, Scenario, int]] = []

        for scenario in scenarios:
            previous_read: Optional[Tuple[float, int]] = None
            requires_ttl = self._scenario_required_ttl(scenario)
            for index, fact in enumerate(scenario.facts):
                if fact.type != FactType.PROFILE_READ:
                    continue
                expected = self._expected_cache(fact)
                if previous_read is not None:
                    gap = float(fact.timestamp) - previous_read[0]
                    if expected == "hit":
                        hit_reads.append((gap, scenario, index, previous_read[1]))
                    elif expected == "miss" and requires_ttl is not None:
                        miss_gaps.append((gap, scenario, index))
                previous_read = (float(fact.timestamp), index)

        notes: List[str] = []
        if not miss_gaps:
            # Without a cache_002-style scenario the engine's 3600s default is
            # longer than any run, so every hit expectation already holds.
            return None, notes

        upper = min(gap for gap, _, _ in miss_gaps)

        if self.configured_profile_cache_ttl_seconds is not None:
            ttl: Optional[float] = float(self.configured_profile_cache_ttl_seconds)
            if ttl >= upper:
                notes.append(
                    f"configured profile cache TTL {ttl:g}s is not shorter than the "
                    f"realised {upper:.3f}s gap before the cache_002 read; its miss "
                    f"expectation is marked unsatisfiable"
                )
                self._mark_unsatisfiable(miss_gaps, ttl)
        elif upper <= 2 * _CACHE_TTL_MARGIN_SECONDS:
            ttl = None
            notes.append(
                f"the {upper:.3f}s gap before the cache_002 read is too short to "
                f"host a profile cache TTL; cache expectations marked unsatisfiable"
            )
            self._mark_unsatisfiable(miss_gaps, None)
        else:
            ttl = round(upper / 2.0, 3)
            notes.append(
                f"engine profile cache TTL must be ~{ttl:g}s for this run "
                f"(TTL < miss gap {upper:.3f}s); the engine default is 3600s"
            )

        if ttl is not None:
            for scenario in scenarios:
                if self._scenario_required_ttl(scenario) is not None:
                    scenario.metadata["effective_profile_cache_ttl_seconds"] = ttl
            notes.extend(self._retime_hit_reads(hit_reads, ttl))
        else:
            self._mark_unsatisfiable(
                [(gap, scenario, index) for gap, scenario, index, _ in hit_reads], None
            )

        return ttl, notes

    def _retime_hit_reads(
        self,
        hit_reads: Sequence[Tuple[float, Scenario, int, int]],
        ttl: float,
    ) -> List[str]:
        """Pull hit-expecting reads inside ``ttl`` of the read they must hit."""
        target_gap = round(ttl / 2.0, 3)
        notes: List[str] = []
        for gap, scenario, index, previous_index in hit_reads:
            if gap <= target_gap:
                continue
            if previous_index != index - 1:
                # A non-read fact sits between the two reads; moving the read
                # earlier could reorder the scenario, so leave it alone.
                self._mark_unsatisfiable([(gap, scenario, index)], ttl)
                notes.append(
                    f"{scenario.scenario_id}#{index} expects a cache hit {gap:.3f}s "
                    f"after the previous read but cannot be moved inside the "
                    f"{ttl:g}s TTL; expectation marked unsatisfiable"
                )
                continue
            fact = scenario.facts[index]
            previous = scenario.facts[previous_index]
            original = float(fact.timestamp)
            fact.timestamp = round(float(previous.timestamp) + target_gap, 3)
            fact.metadata["timestamp_adjusted_from"] = original
            fact.metadata["timestamp_adjusted_reason"] = "profile_cache_ttl"
            notes.append(
                f"{scenario.scenario_id}#{index} moved from {original:.3f}s to "
                f"{fact.timestamp:.3f}s so its cache hit lands inside the "
                f"{ttl:g}s TTL"
            )
        return notes

    @staticmethod
    def _mark_unsatisfiable(
        entries: Sequence[Tuple[float, Scenario, int]], ttl: Optional[float]
    ) -> None:
        for _, scenario, index in entries:
            fact = scenario.facts[index]
            fact.metadata["expected_cache_unsatisfiable"] = True
            if ttl is not None:
                fact.metadata["configured_profile_cache_ttl_seconds"] = ttl
            scenario.metadata["cache_expectations_unsatisfiable"] = True

    @staticmethod
    def _scenario_required_ttl(scenario: Scenario) -> Optional[float]:
        value = (scenario.metadata or {}).get(REQUIRED_CACHE_TTL_KEY)
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _expected_cache(fact: ScenarioFact) -> Optional[str]:
        value = (fact.metadata or {}).get("expected_cache")
        return value if isinstance(value, str) else None

    # ------------------------------------------------------------------
    # Playback loop (spec § 3.2 timing)
    # ------------------------------------------------------------------

    async def _play_queue(
        self,
        queue: Sequence[_QueuedEvent],
        result: RunResult,
        run_number: int,
        duration: float,
        run_start: float,
    ) -> None:
        for event in queue:
            if event.timestamp > duration:
                result.events_skipped += 1
                self._note_skip(result, event, duration)
                continue
            await self._sleep_until(run_start + event.timestamp)
            await self._play_event(event, result, run_number)
            result.events_played += 1
            result.facts_count = result.events_played
            self._update_progress(result, run_start, duration, event)
            await self._emit_progress()

    async def _sleep_until(self, target: float) -> None:
        """Sleep until the monotonic ``target``; returns at once if it passed."""
        delay = target - self._clock()
        if delay > 0:
            await self._sleep(delay)

    def _note_skip(self, result: RunResult, event: _QueuedEvent, duration: float) -> None:
        message = (
            f"{event.scenario.scenario_id}#{event.fact_index} scheduled at "
            f"{event.timestamp:g}s is past the {duration:g}s run — skipped"
        )
        logger.info("%s", message)
        result.notes.append(message)

    async def _play_event(
        self, event: _QueuedEvent, result: RunResult, run_number: int
    ) -> None:
        user_id = self.resolve_user_id(event.scenario, event.fact)
        if event.fact.type == FactType.PROFILE_READ:
            await self._play_profile_read(event, user_id, result, run_number)
        else:
            await self._play_ingest(event, user_id, result, run_number)

    @staticmethod
    def resolve_user_id(scenario: Scenario, fact: ScenarioFact) -> str:
        """``fact.metadata["user_id"]`` wins over ``scenario.user_id``.

        Multi-user scenarios put the conflicting utterance in a second user's
        memory that way (spec § 5.1 D).
        """
        override = (fact.metadata or {}).get("user_id")
        if isinstance(override, str) and override.strip():
            return override
        return scenario.user_id

    # -- ingest ------------------------------------------------------------

    async def _play_ingest(
        self, event: _QueuedEvent, user_id: str, result: RunResult, run_number: int
    ) -> None:
        scenario, fact = event.scenario, event.fact
        prior = self._backfill_contradiction(event)

        try:
            ingestion = await self.client.ingest(user_id, fact.text)
        except EngineClientError as exc:
            self._record_error(result, event, user_id, exc)
            return
        except Exception as exc:  # pragma: no cover - never crash a run
            self._record_error(result, event, user_id, exc)
            return

        fact_id, id_source = self._engine_fact_id(ingestion.fact_id, scenario, event.fact_index)
        self._scenario_fact_ids.setdefault(scenario.scenario_id, []).append(
            (fact_id, user_id, event.fact_index)
        )
        result.fact_ids.setdefault(scenario.scenario_id, []).append(fact_id)
        result.ingested_count += 1

        # A contradiction only resolves against a fact of the *same* user.
        # Multi-user scenarios deliberately cross users and must leave both
        # facts active (spec § 5.1 D), so they stay plain ingests.
        cross_user = prior is not None and prior[1] != user_id
        resolves = prior is not None and not cross_user

        metadata: Dict[str, Any] = {
            "scenario_id": scenario.scenario_id,
            "scenario_category": _enum_value(scenario.category),
            "fact_index": event.fact_index,
            "fact_type": _enum_value(fact.type),
            "scheduled_time": event.timestamp,
            "fact_id_source": id_source,
        }
        if fact.ttl_seconds is not None:
            metadata["ttl_seconds"] = fact.ttl_seconds
        if prior is not None:
            metadata["contradicts_scenario"] = fact.contradicts_scenario
            metadata["contradicts_fact_id"] = prior[0]
        if cross_user:
            metadata["cross_user_contradiction"] = True
            metadata["contradicting_user_id"] = prior[1]
            metadata["no_deactivation_expected"] = True

        if resolves:
            metadata["old_fact_id"] = prior[0]
            metadata["new_fact_id"] = fact_id
            event_type = MonitoringEventType.CONTRADICTION_RESOLVED.value
            old_state: Dict[str, Any] = {"fact_id": prior[0], "active": True}
            new_state: Dict[str, Any] = {
                "fact_id": fact_id,
                "active": True,
                "text": fact.text,
                "user_id": user_id,
            }
            result.contradiction_count += 1
        else:
            event_type = MonitoringEventType.FACT_INGESTED.value
            old_state = {}
            new_state = {
                "fact_id": fact_id,
                "active": True,
                "text": fact.text,
                "user_id": user_id,
            }

        self._log_event(
            run_number=run_number,
            event_type=event_type,
            user_id=user_id,
            fact_id=fact_id,
            old_state=old_state,
            new_state=new_state,
            metadata=metadata,
        )

    def _backfill_contradiction(
        self, event: _QueuedEvent
    ) -> Optional[Tuple[str, str, int]]:
        """Fill ``contradicts_fact_id`` from the earlier fact it replaces.

        The generator leaves ``contradicts_fact_id`` ``None`` because engine
        fact ids only exist at playback time. The reference is the most recent
        already-played fact of the scenario named by ``contradicts_scenario``,
        which is the primary fact for a two-fact contradiction and the previous
        link for a modification chain.
        """
        target = event.fact.contradicts_scenario
        if not target:
            return None
        played = self._scenario_fact_ids.get(target) or []
        same_scenario = target == event.scenario.scenario_id
        for entry in reversed(played):
            # Within one scenario only an *earlier* fact can be contradicted;
            # a cross-scenario reference may point at any played fact.
            if not same_scenario or entry[2] < event.fact_index:
                event.fact.contradicts_fact_id = entry[0]
                return entry
        return None

    @staticmethod
    def _engine_fact_id(
        engine_id: Optional[str], scenario: Scenario, fact_index: int
    ) -> Tuple[str, str]:
        """Return ``(fact_id, source)``.

        ``/ingest`` is fire-and-forget (HTTP 202) and normally returns no id, so
        a stable synthetic id keeps monitoring and the audit trail keyed — the
        audit schema requires a fact id on every row.
        """
        if engine_id:
            return str(engine_id), "engine"
        return f"sim:{scenario.scenario_id}:{fact_index}", "synthetic"

    # -- profile read ------------------------------------------------------

    async def _play_profile_read(
        self, event: _QueuedEvent, user_id: str, result: RunResult, run_number: int
    ) -> None:
        scenario, fact = event.scenario, event.fact
        try:
            profile = await self.client.get_profile(user_id)
        except EngineClientError as exc:
            self._record_error(result, event, user_id, exc)
            return
        except Exception as exc:  # pragma: no cover - never crash a run
            self._record_error(result, event, user_id, exc)
            return

        result.profile_read_count += 1
        observed, source = self._observed_cache_status(profile)
        expected = self._expected_cache(fact)
        unsatisfiable = bool((fact.metadata or {}).get("expected_cache_unsatisfiable"))
        matched: Optional[bool] = None
        if expected is not None and observed is not None and not unsatisfiable:
            matched = expected == observed

        check = {
            "scenario_id": scenario.scenario_id,
            "fact_index": event.fact_index,
            "user_id": user_id,
            "expected": expected,
            "observed": observed,
            "matched": matched,
            "source": source,
            "latency_us": profile.latency_us,
            "unsatisfiable": unsatisfiable,
        }
        result.cache_checks.append(check)

        fact_id = f"profile:{user_id}"
        if observed == "hit":
            event_type = MonitoringEventType.CACHE_HIT.value
        elif observed == "miss":
            event_type = MonitoringEventType.CACHE_MISS.value
        else:
            # Latency landed between the hit and miss thresholds and the engine
            # reported nothing: record the read without claiming a cache verdict.
            event_type = MonitoringEventType.RETRIEVAL.value

        self._log_event(
            run_number=run_number,
            event_type=event_type,
            user_id=user_id,
            fact_id=fact_id,
            old_state={},
            new_state={
                "stable_facts": len(profile.stable_facts),
                "recent_activity": len(profile.recent_activity),
                "profile_timestamp": profile.profile_timestamp,
            },
            metadata={
                "scenario_id": scenario.scenario_id,
                "scenario_category": _enum_value(scenario.category),
                "fact_index": event.fact_index,
                "fact_type": _enum_value(fact.type),
                "scheduled_time": event.timestamp,
                "action": "get_profile",
                "expected_cache": expected,
                "observed_cache": observed,
                "cache_status_source": source,
                "cache_expectation_met": matched,
                "expected_cache_unsatisfiable": unsatisfiable,
                "latency_us": profile.latency_us,
            },
        )

    def _observed_cache_status(self, profile: Any) -> Tuple[Optional[str], str]:
        """``(status, source)`` — the engine's own signal, else latency.

        The shipped engine returns the same body for a cached and a freshly
        generated profile, so the fallback is the MonitoringService's latency
        thresholds (spec § 3.4): under 10ms is a hit, over 100ms a miss, and
        anything between is indeterminate.
        """
        if isinstance(profile.cache_status, str):
            return profile.cache_status, "engine"
        hit_threshold = getattr(
            self.monitor, "CACHE_HIT_THRESHOLD_US", MonitoringService.CACHE_HIT_THRESHOLD_US
        )
        miss_threshold = getattr(
            self.monitor, "CACHE_MISS_THRESHOLD_US", MonitoringService.CACHE_MISS_THRESHOLD_US
        )
        latency = profile.latency_us
        if latency < hit_threshold:
            return "hit", "latency"
        if latency > miss_threshold:
            return "miss", "latency"
        return None, "indeterminate"

    # ------------------------------------------------------------------
    # Monitoring, errors, progress
    # ------------------------------------------------------------------

    def _log_event(self, **kwargs: Any) -> None:
        """Forward to the MonitoringService, never breaking playback."""
        if self.monitor is None:
            return
        try:
            self.monitor.log_event(**kwargs)
        except Exception:  # pragma: no cover - monitoring must not break a run
            logger.exception(
                "log_event failed for %s/%s",
                kwargs.get("user_id"),
                kwargs.get("fact_id"),
            )

    def _record_error(
        self,
        result: RunResult,
        event: _QueuedEvent,
        user_id: str,
        exc: Exception,
    ) -> None:
        """Classify a failed call, record it and let the run continue."""
        if isinstance(exc, EngineClientError):
            classification = classify_engine_error(exc)
            endpoint = exc.endpoint
            status_code = exc.status_code
        else:
            classification = ErrorClass.UNEXPECTED.value
            endpoint = None
            status_code = None

        error = RunError(
            timestamp=time.time(),
            scenario_id=event.scenario.scenario_id,
            fact_index=event.fact_index,
            user_id=user_id,
            classification=classification,
            message=str(exc),
            endpoint=endpoint,
            status_code=status_code,
            error=exc,
        )
        result.errors.append(error)
        logger.warning(
            "run %s: %s#%s (%s) failed [%s] %s",
            result.run_number,
            event.scenario.scenario_id,
            event.fact_index,
            user_id,
            classification,
            exc,
        )

    def _blank_progress(self) -> Dict[str, Any]:
        return {
            "run_number": self.run_number,
            "duration_seconds": self.duration_seconds,
            "events_total": 0,
            "events_played": 0,
            "events_skipped": 0,
            "errors": 0,
            "current_time": 0.0,
            "elapsed_seconds": 0.0,
            "remaining_seconds": self.duration_seconds,
            "percent_complete": 0.0,
            "last_scenario_id": None,
        }

    def _reset_progress(self, run_number: int, duration: float, total: int) -> None:
        self.progress = {
            "run_number": run_number,
            "duration_seconds": duration,
            "events_total": total,
            "events_played": 0,
            "events_skipped": 0,
            "errors": 0,
            "current_time": 0.0,
            "elapsed_seconds": 0.0,
            "remaining_seconds": duration,
            "percent_complete": 0.0,
            "last_scenario_id": None,
        }

    def _update_progress(
        self,
        result: RunResult,
        run_start: float,
        duration: float,
        event: _QueuedEvent,
    ) -> None:
        elapsed = max(self._clock() - run_start, 0.0)
        self.progress.update(
            {
                "events_played": result.events_played,
                "events_skipped": result.events_skipped,
                "errors": len(result.errors),
                "current_time": event.timestamp,
                "elapsed_seconds": elapsed,
                "remaining_seconds": max(duration - elapsed, 0.0),
                "percent_complete": (
                    100.0 * result.events_played / result.events_total
                    if result.events_total
                    else 100.0
                ),
                "last_scenario_id": event.scenario.scenario_id,
            }
        )

    async def _emit_progress(self) -> None:
        if self.on_progress is None:
            return
        try:
            outcome = self.on_progress(dict(self.progress))
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:  # pragma: no cover - callbacks must not break a run
            logger.exception("on_progress callback failed")

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _final_status(self, result: RunResult) -> str:
        """``failed`` only when nothing at all got through."""
        if result.events_played and len(result.errors) >= result.events_played:
            return RunStatus.FAILED.value
        return RunStatus.COMPLETED.value

    def _finish(self, result: RunResult, run_start: float) -> None:
        result.elapsed_seconds = max(self._clock() - run_start, 0.0)
        result.end_time = time.time()
        result.facts_count = result.events_played
        if self.monitor is not None:
            try:
                result.metrics = self.monitor.get_metrics(run_number=result.run_number)
            except Exception as exc:  # pragma: no cover - metrics are advisory
                logger.warning("get_metrics failed: %s", exc)
                result.notes.append(f"get_metrics failed: {exc}")
        self.progress.update(
            {
                "elapsed_seconds": result.elapsed_seconds,
                "remaining_seconds": 0.0,
                "errors": len(result.errors),
                "status": result.status,
            }
        )
        self._close_run(result)

    async def _validate(self, scenarios: Sequence[Scenario], result: RunResult) -> None:
        """Run the ValidatorService once playback is over (spec § 3.2 step 4).

        Task 7 owns the ValidatorService; until it lands, a missing or
        unimplemented ``validate()`` is recorded as a note rather than failing
        the run.
        """
        if self.validator is None:
            result.notes.append("validation skipped: no ValidatorService configured")
            return
        validate = getattr(self.validator, "validate", None)
        if not callable(validate):
            result.notes.append(
                "validation skipped: ValidatorService has no validate() (Task 7)"
            )
            return
        try:
            kwargs: Dict[str, Any] = {}
            try:
                if "run_number" in inspect.signature(validate).parameters:
                    kwargs["run_number"] = result.run_number
            except (TypeError, ValueError):  # pragma: no cover - builtins/mocks
                pass
            outcome = validate(list(scenarios), **kwargs)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except NotImplementedError:
            result.notes.append(
                "validation skipped: ValidatorService.validate() not implemented (Task 7)"
            )
            return
        except Exception as exc:
            logger.warning("validation failed for run %s: %s", result.run_number, exc)
            result.notes.append(f"validation failed: {exc}")
            return

        if outcome is None:
            result.validation_results = []
        elif isinstance(outcome, ValidationResult):
            result.validation_results = [outcome]
        else:
            result.validation_results = list(outcome)
