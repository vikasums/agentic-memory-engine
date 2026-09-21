"""ScenarioGenerator — deterministic test scenario construction (spec § 3.1, § 5).

Builds the 50-scenario catalogue from spec § 5.1 (10 contradiction, 8 expiry,
6 profile cache, 10 multi-user, 10 generic accumulation, 6 modification) and
spreads it randomly — but reproducibly, given a seed — across the simulated
users and the run timeline.

Phase 1 is deterministic: no LLM calls anywhere in this module. The fact texts
are fixed; only the user assignment and the fact timestamps are randomised.

Scenario JSON shape (spec § 3.1)::

    {
      "scenario_id": "contradiction_001",
      "user_id": "user_2",
      "facts": [
        {"timestamp": 15, "text": "I live in New York", "type": "primary_fact"},
        {"timestamp": 45, "text": "Actually I moved to San Francisco",
         "type": "contradiction", "contradicts_scenario": "contradiction_001",
         "contradicts_fact_id": null}
      ],
      "expected_outcomes": ["old_fact_deactivated", "new_fact_active",
                            "audit_trail_complete"]
    }

``contradicts_fact_id`` is always ``null`` here: the generator does not know the
engine-assigned fact ids, so the SimulationRunner (Task 6) fills them in during
playback and the ValidatorService (Task 7) reads them back.
"""

import json
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import FactType, Scenario, ScenarioCategory, ScenarioFact

DEFAULT_USER_COUNT = 5
DEFAULT_USER_IDS = [f"user_{i}" for i in range(1, DEFAULT_USER_COUNT + 1)]

#: Longest progressive validation run in spec § 1.2 (6 minutes).
DEFAULT_MAX_DURATION_SECONDS = 360.0

#: Default minimum spacing between two facts of the same scenario, so that a
#: contradiction or modification is always ingested after the fact it replaces.
DEFAULT_MIN_FACT_GAP_SECONDS = 3.0

#: Spec § 5 mandates at least 50 scenarios.
MIN_SCENARIO_COUNT = 50

# --- TTL constants (spec § 5.1 B) --------------------------------------------
_HOUR = 3600.0
_DAY = 24 * _HOUR
_WEEK = 7 * _DAY

# --- Expected-outcome vocabulary (spec § 3.1, § 6.2) -------------------------
# Every scenario carries the § 6.2 base checks; each category adds its own.
BASE_OUTCOMES: Tuple[str, ...] = (
    "fact_stored",
    "fact_retrievable",
    "audit_event_logged",
    "user_isolation_ok",
    "metadata_correct",
)

CATEGORY_OUTCOMES: Dict[ScenarioCategory, Tuple[str, ...]] = {
    ScenarioCategory.CONTRADICTION: (
        "old_fact_deactivated",
        "new_fact_active",
        "audit_trail_complete",
    ),
    ScenarioCategory.EXPIRY: (
        "expiry_scheduled",
        "ttl_recorded",
        "fact_active_before_expiry",
    ),
    ScenarioCategory.CACHE: (
        "profile_generated",
        "profile_reflects_facts",
    ),
    ScenarioCategory.MULTI_USER: (
        "both_facts_active",
        "no_cross_user_deactivation",
        "audit_trail_complete",
    ),
    ScenarioCategory.GENERIC: (
        "all_facts_active",
        "no_contradiction_triggered",
    ),
    ScenarioCategory.MODIFY: (
        "latest_value_active",
        "prior_versions_deactivated",
        "audit_trail_complete",
    ),
}

#: Extra outcomes used by individual cache scenarios.
_CACHE_HIT = "cache_hit_recorded"
_CACHE_MISS = "cache_miss_recorded"
_RECENT_ACTIVITY = "recent_activity_populated"
_STABLE_SPLIT = "stable_recent_split_correct"
_PROFILE_REFRESH = "profile_refreshed_on_new_fact"

#: Full vocabulary the ValidatorService (Task 7) must know how to check.
EXPECTED_OUTCOME_VOCABULARY = frozenset(
    set(BASE_OUTCOMES)
    | {outcome for outcomes in CATEGORY_OUTCOMES.values() for outcome in outcomes}
    | {_CACHE_HIT, _CACHE_MISS, _RECENT_ACTIVITY, _STABLE_SPLIT, _PROFILE_REFRESH}
)


# --- Catalogue specs ---------------------------------------------------------


@dataclass(frozen=True)
class _FactSpec:
    """Timeless description of one fact; the generator adds the timestamp."""

    text: str
    type: FactType = FactType.PRIMARY_FACT
    ttl_seconds: Optional[float] = None
    #: True when this fact is uttered by the scenario's peer user (multi-user).
    peer: bool = False
    #: Marks the fact as conflicting with an earlier fact of the same scenario.
    conflicts: bool = False
    metadata: Tuple[Tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class _ScenarioSpec:
    """Timeless description of one scenario in the spec § 5.1 catalogue."""

    scenario_id: str
    category: ScenarioCategory
    description: str
    facts: Tuple[_FactSpec, ...]
    extra_outcomes: Tuple[str, ...] = ()
    min_gap_seconds: float = DEFAULT_MIN_FACT_GAP_SECONDS
    metadata: Tuple[Tuple[str, Any], ...] = ()

    @property
    def needs_peer_user(self) -> bool:
        return any(f.peer for f in self.facts)

    @property
    def expected_outcomes(self) -> List[str]:
        outcomes = list(BASE_OUTCOMES) + list(CATEGORY_OUTCOMES[self.category])
        for outcome in self.extra_outcomes:
            if outcome not in outcomes:
                outcomes.append(outcome)
        return outcomes


def _primary(text: str) -> _FactSpec:
    return _FactSpec(text=text, type=FactType.PRIMARY_FACT)


def _contradiction(text: str, peer: bool = False, **meta: Any) -> _FactSpec:
    return _FactSpec(
        text=text,
        type=FactType.CONTRADICTION,
        peer=peer,
        conflicts=True,
        metadata=tuple(sorted(meta.items())),
    )


def _modification(text: str) -> _FactSpec:
    return _FactSpec(text=text, type=FactType.MODIFICATION, conflicts=True)


def _expiring(text: str, ttl_seconds: float) -> _FactSpec:
    return _FactSpec(text=text, type=FactType.EXPIRING_FACT, ttl_seconds=ttl_seconds)


def _profile_read(text: str, **meta: Any) -> _FactSpec:
    return _FactSpec(
        text=text,
        type=FactType.PROFILE_READ,
        metadata=tuple(sorted(meta.items())),
    )


# --- A. Contradiction scenarios (10) — spec § 5.1 A --------------------------

_CONTRADICTION_PAIRS: Sequence[Tuple[str, str, str]] = (
    ("Location change (NYC -> SF)", "I live in New York", "Actually I moved to San Francisco"),
    ("Programming language (Python -> Go)", "I code in Python", "I have switched to Go as my main language"),
    ("Job role (Engineer -> Manager)", "My role is software engineer", "I was promoted to engineering manager"),
    ("Company (Google -> Apple)", "I work at Google", "I now work at Apple"),
    ("Expertise level (Junior -> Senior)", "I am a junior developer", "I am now a senior developer"),
    ("Project status (Active -> Completed)", "The Atlas project is active", "The Atlas project is completed"),
    ("Experience years (5 -> 10)", "I have 5 years of experience", "Correction, I have 10 years of experience"),
    ("Team assignment (Team A -> Team B)", "I am on Team A", "I moved to Team B"),
    ("Certification (None -> AWS Certified)", "I have no cloud certifications", "I am now AWS certified"),
    ("Skill proficiency (Intermediate -> Expert)", "My Kubernetes skill is intermediate", "My Kubernetes skill is expert level"),
)

# --- B. Expiry scenarios (8) — spec § 5.1 B ----------------------------------

_EXPIRY_SPECS: Sequence[Tuple[str, str, float]] = (
    ("Meeting tomorrow", "I have a meeting tomorrow at 2pm", _DAY),
    ("Exam next Friday", "My certification exam is next Friday", _WEEK),
    ("Temporary task", "I am running a temporary data migration task this hour", _HOUR),
    ("Sprint deadline", "Our sprint deadline is in two weeks", 2 * _WEEK),
    ("Short-term goal", "My short-term goal is to ship the search revamp this month", 30 * _DAY),
    ("Out of office notice", "I am out of office for the next two weeks", 2 * _WEEK),
    ("Upcoming vacation", "I am going on vacation in three months", 90 * _DAY),
    ("Blocked on issue", "I am blocked on issue AME-412", 5 * _DAY),
)

# --- D. Multi-user contradiction scenarios (10) — spec § 5.1 D ---------------

_MULTI_USER_PAIRS: Sequence[Tuple[str, str, str]] = (
    ("Team lead disagreement", "Our team lead is John", "Our team lead is Sarah"),
    ("Project status", "The Helios project is active", "The Helios project is on hold"),
    ("Deadline", "The launch deadline is March 15", "The launch deadline is March 20"),
    ("Technology choice", "We are building the dashboard in React", "We are building the dashboard in Vue"),
    ("Performance metric", "Our test suite pass rate is 95 percent", "Our test suite pass rate is 92 percent"),
    ("Resource allocation", "Two developers are allocated to the migration", "Three developers are allocated to the migration"),
    ("Bug severity", "Bug AME-77 is critical severity", "Bug AME-77 is major severity"),
    ("Customer feedback", "Customer feedback on the beta was positive", "Customer feedback on the beta was mixed"),
    ("Next release date", "The next release ships in Q3", "The next release ships in Q4"),
    ("Code review status", "The payments pull request is approved", "The payments pull request needs changes"),
)

# --- E. Generic fact accumulation (10) — spec § 5.1 E ------------------------

_GENERIC_GROUPS: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("Preference facts", ("I like coffee", "I listen to jazz while coding", "I play squash on weekends")),
    ("Work environment", ("I use VS Code as my editor", "I write most services in Python", "I use FastAPI for web APIs")),
    ("Personal projects", ("I maintain an open source CLI tool", "I am building a side project for recipe planning", "I contribute to the LanceDB community")),
    ("Learning goals", ("I am taking a distributed systems course", "I am studying for the CKA certification", "I want to learn Rust this year")),
    ("Team info", ("I am on the Memory Platform team", "My team has six engineers", "My team focuses on retrieval quality")),
    ("Responsibility areas", ("I own the ingestion pipeline", "I maintain the retrieval ranking module", "I am responsible for the audit subsystem")),
    ("Communication style", ("I prefer async communication", "I am in the GMT+4 timezone", "I keep synchronous meetings on Tuesdays")),
    ("Availability", ("I work full time", "I am available between 9am and 6pm", "I keep Fridays as focus days")),
    ("Previous experience", ("I worked at Stripe before this", "I was a backend engineer for four years", "I previously led a data platform team")),
    ("Interests", ("I am interested in AI and machine learning", "I follow DevOps practices closely", "I care about application security")),
)

# --- F. Fact modification scenarios (6) — spec § 5.1 F -----------------------

_MODIFY_CHAINS: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("Experience progression (3y -> 5y -> 8y)", ("I have 3 years of experience", "I now have 5 years of experience", "I now have 8 years of experience")),
    ("Project evolution (Planning -> Released)", ("The Orion project is in planning", "The Orion project is in development", "The Orion project is in testing", "The Orion project is released")),
    ("Skill improvement (Beginner -> Expert)", ("My Rust skill is beginner level", "My Rust skill is intermediate level", "My Rust skill is advanced level", "My Rust skill is expert level")),
    ("Team growth (2 -> 5 -> 8)", ("My team has 2 people", "My team has 5 people", "My team has 8 people")),
    ("Salary history (100k -> 120k -> 150k)", ("My salary is 100k", "My salary is 120k", "My salary is 150k")),
    ("Performance score (3.2 -> 3.5 -> 3.8)", ("My performance score is 3.2", "My performance score is 3.5", "My performance score is 3.8")),
)


def _build_catalogue() -> Tuple[_ScenarioSpec, ...]:
    """Assemble the 50 scenarios of spec § 5.1 in category order."""

    specs: List[_ScenarioSpec] = []

    for index, (description, first, second) in enumerate(_CONTRADICTION_PAIRS, start=1):
        specs.append(
            _ScenarioSpec(
                scenario_id=f"contradiction_{index:03d}",
                category=ScenarioCategory.CONTRADICTION,
                description=description,
                facts=(_primary(first), _contradiction(second)),
            )
        )

    for index, (description, text, ttl_seconds) in enumerate(_EXPIRY_SPECS, start=1):
        specs.append(
            _ScenarioSpec(
                scenario_id=f"expiry_{index:03d}",
                category=ScenarioCategory.EXPIRY,
                description=description,
                facts=(_expiring(text, ttl_seconds),),
                metadata=(("ttl_seconds", ttl_seconds),),
            )
        )

    specs.extend(_build_cache_specs())

    for index, (description, first, second) in enumerate(_MULTI_USER_PAIRS, start=1):
        specs.append(
            _ScenarioSpec(
                scenario_id=f"multi_{index:03d}",
                category=ScenarioCategory.MULTI_USER,
                description=description,
                facts=(
                    _primary(first),
                    _contradiction(second, peer=True, cross_user=True),
                ),
            )
        )

    for index, (description, texts) in enumerate(_GENERIC_GROUPS, start=1):
        specs.append(
            _ScenarioSpec(
                scenario_id=f"generic_{index:03d}",
                category=ScenarioCategory.GENERIC,
                description=description,
                facts=tuple(_primary(text) for text in texts),
            )
        )

    for index, (description, texts) in enumerate(_MODIFY_CHAINS, start=1):
        facts = (_primary(texts[0]),) + tuple(_modification(text) for text in texts[1:])
        specs.append(
            _ScenarioSpec(
                scenario_id=f"modify_{index:03d}",
                category=ScenarioCategory.MODIFY,
                description=description,
                facts=facts,
            )
        )

    return tuple(specs)


def _build_cache_specs() -> List[_ScenarioSpec]:
    """C. Profile cache scenarios (6) — spec § 5.1 C.

    ``profile_read`` facts are not ingested: the SimulationRunner calls
    ``EngineClient.get_profile()`` for them and the MonitoringService records a
    ``cache_hit`` / ``cache_miss`` event. ``expected_cache`` in the fact
    metadata says which one the scenario expects.
    """

    # Profile cache TTL in the engine store defaults to 3600s, far longer than a
    # 6 minute run, so cache_002 declares the shorter TTL the runner must
    # configure for the miss to be observable.
    cache_miss_ttl = 30.0

    return [
        _ScenarioSpec(
            scenario_id="cache_001",
            category=ScenarioCategory.CACHE,
            description="Immediate cache hit (generate, retrieve < 1s)",
            facts=(
                _primary("I lead the platform reliability guild"),
                _profile_read("Generate profile", expected_cache="miss"),
                _profile_read("Retrieve profile immediately", expected_cache="hit"),
            ),
            extra_outcomes=(_CACHE_HIT,),
            min_gap_seconds=1.0,
        ),
        _ScenarioSpec(
            scenario_id="cache_002",
            category=ScenarioCategory.CACHE,
            description="Cache miss after TTL (generate, wait TTL, retrieve)",
            facts=(
                _primary("I run the on-call rotation for the ingestion service"),
                _profile_read("Generate profile", expected_cache="miss"),
                _profile_read("Retrieve profile after cache TTL", expected_cache="miss"),
            ),
            extra_outcomes=(_CACHE_MISS,),
            min_gap_seconds=cache_miss_ttl + 15.0,
            metadata=(("requires_profile_cache_ttl_seconds", cache_miss_ttl),),
        ),
        _ScenarioSpec(
            scenario_id="cache_003",
            category=ScenarioCategory.CACHE,
            description="Stable facts accumulation (5 facts, profile reflects all)",
            facts=(
                _primary("I am a staff engineer"),
                _primary("I specialise in distributed storage"),
                _primary("I work from Dubai"),
                _primary("I mentor two junior engineers"),
                _primary("I own the retrieval service roadmap"),
                _profile_read("Generate profile after five facts", expected_cache="miss"),
            ),
            min_gap_seconds=2.0,
        ),
        _ScenarioSpec(
            scenario_id="cache_004",
            category=ScenarioCategory.CACHE,
            description="Recent activity window (facts within 7d in recent_activity)",
            facts=(
                _primary("I started reviewing the sharding proposal today"),
                _primary("I filed three bugs against the indexer this week"),
                _profile_read("Check recent activity window", expected_cache="miss"),
            ),
            extra_outcomes=(_RECENT_ACTIVITY,),
        ),
        _ScenarioSpec(
            scenario_id="cache_005",
            category=ScenarioCategory.CACHE,
            description="Stable vs recent split (old facts stable, new facts recent)",
            facts=(
                _primary("I have worked in backend engineering for nine years"),
                _primary("I joined the memory platform team this week"),
                _profile_read("Check stable vs recent split", expected_cache="miss"),
            ),
            extra_outcomes=(_STABLE_SPLIT,),
        ),
        _ScenarioSpec(
            scenario_id="cache_006",
            category=ScenarioCategory.CACHE,
            description="Profile refresh on new fact (add fact, profile regenerates)",
            facts=(
                _primary("I use Postgres for analytics workloads"),
                _profile_read("Generate profile", expected_cache="miss"),
                _primary("I also use ClickHouse for event analytics"),
                _profile_read("Retrieve profile after new fact", expected_cache="miss"),
            ),
            extra_outcomes=(_PROFILE_REFRESH,),
        ),
    ]


SCENARIO_CATALOGUE: Tuple[_ScenarioSpec, ...] = _build_catalogue()

#: Scenario count per category, for tests and run reports (spec § 5.1).
CATEGORY_COUNTS: Dict[ScenarioCategory, int] = {
    category: sum(1 for spec in SCENARIO_CATALOGUE if spec.category is category)
    for category in ScenarioCategory
}


class ScenarioGenerator:
    """Builds the deterministic scenario set for a simulation run.

    Args:
        seed: RNG seed so a given run number reproduces the same distribution.
        user_ids: Simulated users to spread scenarios across (default: 5).
    """

    def __init__(self, seed: int = 0, user_ids: Optional[List[str]] = None) -> None:
        self.seed = seed
        self._explicit_user_ids: Optional[List[str]] = list(user_ids) if user_ids else None
        self.user_ids = self._explicit_user_ids or list(DEFAULT_USER_IDS)

    # -- public API ----------------------------------------------------------

    def generate(
        self,
        num_scenarios: int = MIN_SCENARIO_COUNT,
        num_users: int = DEFAULT_USER_COUNT,
        max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS,
        duration_seconds: Optional[float] = None,
    ) -> List[Scenario]:
        """Return the scenario set, timed within ``0..max_duration_seconds``.

        Args:
            num_scenarios: How many scenarios to return. ``50`` (the default)
                returns the whole spec § 5.1 catalogue. More than 50 repeats the
                catalogue with ``_r2``/``_r3`` suffixed ids; fewer takes a
                round-robin slice that keeps every category represented.
            num_users: Number of simulated users (``user_1``..``user_N``).
                Ignored when explicit ``user_ids`` were passed to ``__init__``.
            max_duration_seconds: Fact timestamps are drawn from
                ``[0, max_duration_seconds]``.
            duration_seconds: Alias for ``max_duration_seconds`` kept for the
                SimulationRunner's call style; takes precedence when given.

        Returns:
            Scenarios ordered by their first fact's timestamp. Facts inside a
            scenario are ordered ascending with a minimum gap, so a
            contradiction is always played back after the fact it replaces.
        """

        if duration_seconds is not None:
            max_duration_seconds = duration_seconds
        if not isinstance(num_scenarios, int) or isinstance(num_scenarios, bool):
            # Guards the old skeleton call style, generate(<duration>).
            raise TypeError(
                "num_scenarios must be an int; pass a run length as "
                "max_duration_seconds=..."
            )
        if num_scenarios < 1:
            raise ValueError("num_scenarios must be >= 1")
        if max_duration_seconds < 0:
            raise ValueError("max_duration_seconds must be >= 0")

        users = self._resolve_users(num_users)
        rng = random.Random(self.seed)
        specs = self._select_specs(num_scenarios)

        scenarios = [
            self._materialise(spec, suffix, users, float(max_duration_seconds), rng)
            for spec, suffix in specs
        ]
        scenarios.sort(key=lambda s: (s.facts[0].timestamp if s.facts else 0.0, s.scenario_id))
        return scenarios

    # -- internals -----------------------------------------------------------

    def _resolve_users(self, num_users: int) -> List[str]:
        if self._explicit_user_ids is not None:
            return list(self._explicit_user_ids)
        if num_users < 1:
            raise ValueError("num_users must be >= 1")
        return [f"user_{i}" for i in range(1, num_users + 1)]

    @staticmethod
    def _select_specs(num_scenarios: int) -> List[Tuple[_ScenarioSpec, str]]:
        """Pick ``num_scenarios`` catalogue entries with an id suffix for repeats."""

        catalogue = SCENARIO_CATALOGUE
        size = len(catalogue)

        if num_scenarios == size:
            return [(spec, "") for spec in catalogue]

        if num_scenarios < size:
            # Round-robin over the categories so a short run still exercises all
            # six of them.
            by_category: Dict[ScenarioCategory, List[_ScenarioSpec]] = {}
            for spec in catalogue:
                by_category.setdefault(spec.category, []).append(spec)
            picked: List[_ScenarioSpec] = []
            depth = 0
            while len(picked) < num_scenarios:
                progressed = False
                for specs in by_category.values():
                    if depth < len(specs):
                        picked.append(specs[depth])
                        progressed = True
                        if len(picked) == num_scenarios:
                            break
                if not progressed:  # pragma: no cover - guarded by num < size
                    break
                depth += 1
            picked.sort(key=lambda spec: catalogue.index(spec))
            return [(spec, "") for spec in picked]

        selected: List[Tuple[_ScenarioSpec, str]] = []
        for position in range(num_scenarios):
            repeat, offset = divmod(position, size)
            suffix = "" if repeat == 0 else f"_r{repeat + 1}"
            selected.append((catalogue[offset], suffix))
        return selected

    def _materialise(
        self,
        spec: _ScenarioSpec,
        suffix: str,
        users: List[str],
        max_duration_seconds: float,
        rng: random.Random,
    ) -> Scenario:
        scenario_id = f"{spec.scenario_id}{suffix}"
        primary_user = rng.choice(users)
        peer_user = self._pick_peer(primary_user, users, rng) if spec.needs_peer_user else None

        timestamps = _draw_timestamps(
            len(spec.facts), max_duration_seconds, spec.min_gap_seconds, rng
        )

        facts: List[ScenarioFact] = []
        for fact_spec, timestamp in zip(spec.facts, timestamps):
            fact_metadata: Dict[str, Any] = dict(fact_spec.metadata)
            if fact_spec.peer and peer_user is not None:
                fact_metadata["user_id"] = peer_user
            if fact_spec.type is FactType.PROFILE_READ:
                fact_metadata.setdefault("action", "get_profile")
            facts.append(
                ScenarioFact(
                    timestamp=timestamp,
                    text=fact_spec.text,
                    type=fact_spec.type,
                    # contradicts_fact_id stays None: engine fact ids are only
                    # known at playback time (filled in by Task 6 / Task 7).
                    contradicts_scenario=scenario_id if fact_spec.conflicts else None,
                    ttl_seconds=fact_spec.ttl_seconds,
                    metadata=fact_metadata,
                )
            )

        scenario_metadata: Dict[str, Any] = dict(spec.metadata)
        scenario_metadata["description"] = spec.description
        scenario_metadata["catalogue_id"] = spec.scenario_id
        if peer_user is not None:
            scenario_metadata["peer_user_id"] = peer_user
            scenario_metadata["user_ids"] = [primary_user, peer_user]

        return Scenario(
            scenario_id=scenario_id,
            user_id=primary_user,
            category=spec.category,
            facts=facts,
            expected_outcomes=spec.expected_outcomes,
            metadata=scenario_metadata,
        )

    @staticmethod
    def _pick_peer(primary_user: str, users: List[str], rng: random.Random) -> Optional[str]:
        """Return a different user, or ``None`` when only one user exists."""

        others = [user for user in users if user != primary_user]
        if not others:
            return None
        return rng.choice(others)


def _draw_timestamps(
    count: int,
    max_duration_seconds: float,
    min_gap_seconds: float,
    rng: random.Random,
) -> List[float]:
    """Draw ``count`` ascending timestamps inside ``[0, max_duration_seconds]``.

    Uses stick-breaking: sample uniformly from the duration left after reserving
    ``(count - 1) * gap``, sort, then push each sample out by ``i * gap``. That
    keeps the distribution uniform-ish while guaranteeing the ordering a
    contradiction or modification chain depends on. The gap shrinks when the run
    is too short to hold the requested spacing.
    """

    if count <= 0:
        return []
    if max_duration_seconds <= 0:
        return [0.0] * count
    if count == 1:
        return [round(rng.uniform(0.0, max_duration_seconds), 3)]

    gap = min(min_gap_seconds, max_duration_seconds / count)
    usable = max(max_duration_seconds - gap * (count - 1), 0.0)
    draws = sorted(rng.uniform(0.0, usable) for _ in range(count))
    return [round(value + index * gap, 3) for index, value in enumerate(draws)]


# --- Serialisation (spec § 3.1 JSON) -----------------------------------------


def fact_to_dict(fact: ScenarioFact) -> Dict[str, Any]:
    """Serialise one fact to the spec § 3.1 JSON shape."""

    payload: Dict[str, Any] = {
        "timestamp": fact.timestamp,
        "text": fact.text,
        "type": fact.type.value if isinstance(fact.type, FactType) else fact.type,
    }
    if fact.contradicts_scenario is not None:
        payload["contradicts_scenario"] = fact.contradicts_scenario
        # Always emitted, always null here — the runner fills it during playback.
        payload["contradicts_fact_id"] = fact.contradicts_fact_id
    if fact.ttl_seconds is not None:
        payload["ttl_seconds"] = fact.ttl_seconds
    if fact.metadata:
        payload["metadata"] = fact.metadata
    return payload


def scenario_to_dict(scenario: Scenario) -> Dict[str, Any]:
    """Serialise one scenario to the spec § 3.1 JSON shape."""

    category = scenario.category
    return {
        "scenario_id": scenario.scenario_id,
        "user_id": scenario.user_id,
        "category": category.value if isinstance(category, ScenarioCategory) else category,
        "facts": [fact_to_dict(fact) for fact in scenario.facts],
        "expected_outcomes": list(scenario.expected_outcomes),
        "metadata": dict(scenario.metadata),
    }


def scenarios_to_json(scenarios: Sequence[Scenario], indent: Optional[int] = 2) -> str:
    """Serialise a scenario list to a JSON string."""

    return json.dumps([scenario_to_dict(s) for s in scenarios], indent=indent, sort_keys=False)
