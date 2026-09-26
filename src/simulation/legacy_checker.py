"""The pre-fix harness faults, reproducible one at a time.

The v0.3.0 validation pass rate went 6% -> 12% -> 82% -> 86% across three
commits, and the engine was not changed by any of them. Every fault was in how
the run was measured. This module puts those faults back behind explicit flags
so the claim can be demonstrated rather than asserted: the same engine build
and the same store answer both the baseline run and the legacy run, and the
only difference is which flags are set.

Fault to commit:

=============================  =========  ===============================================
Flag                           Commit     What it reintroduces
=============================  =========  ===============================================
``synthetic_fact_ids``         2132dc8    Invented ``sim:<scenario>:<index>`` fact ids
``compare_utterance_text``     2132dc8    Comparing the sentence, not the stored triple
``assume_long_profile_cache``  2132dc8    A 3600s profile cache TTL over a short run
``silence_is_failure``         b779619    An empty extraction reported as a storage failure
``script_decides_ownership``   b779619    Criterion H decided by the script, not the store
``reuse_run_number``           8a80d91    Every run replaying ``run_number=1``
``literal_verdicts``           8a80d91    ``"PASS"`` written as a literal into the report
=============================  =========  ===============================================

``assume_long_profile_cache`` is the one fault that is not harness logic: the
TTL belonged to the engine's configuration. It therefore carries
:data:`LEGACY_PROFILE_CACHE_TTL_SECONDS` for the A/B script to apply to the
engine process, and the report must attribute it as a deployment setting rather
than as a checker bug.
"""

from dataclasses import dataclass, fields
from typing import Any, Dict, Iterable, Optional, Tuple

__all__ = [
    "ALL_FAULTS",
    "FAULT_COMMITS",
    "FAULT_DESCRIPTIONS",
    "LEGACY_PROFILE_CACHE_TTL_SECONDS",
    "LEGACY_RUN_NUMBER",
    "LegacyCheckerConfig",
    "legacy_criterion_payload",
]

#: The profile cache TTL both ``config.py`` and ``store.py`` defaulted to while
#: runs lasted 60-360 seconds.
LEGACY_PROFILE_CACHE_TTL_SECONDS = 3600.0

#: The run number every pre-fix script reused.
LEGACY_RUN_NUMBER = 1

ALL_FAULTS: Tuple[str, ...] = (
    "synthetic_fact_ids",
    "compare_utterance_text",
    "assume_long_profile_cache",
    "silence_is_failure",
    "script_decides_ownership",
    "reuse_run_number",
    "literal_verdicts",
)

FAULT_COMMITS: Dict[str, str] = {
    "synthetic_fact_ids": "2132dc8",
    "compare_utterance_text": "2132dc8",
    "assume_long_profile_cache": "2132dc8",
    "silence_is_failure": "b779619",
    "script_decides_ownership": "b779619",
    "reuse_run_number": "8a80d91",
    "literal_verdicts": "8a80d91",
}

FAULT_DESCRIPTIONS: Dict[str, str] = {
    "synthetic_fact_ids": (
        "/ingest was fire-and-forget, so the harness invented "
        "sim:<scenario>:<index> ids while the engine stored mem_* ids"
    ),
    "compare_utterance_text": (
        "the harness compared the scenario sentence against the stored "
        "'subject predicate object' triple, scoring 0.500 against a 0.6 bar"
    ),
    "assume_long_profile_cache": (
        "the engine's profile cache TTL defaulted to 3600s while runs lasted "
        "60-360s, so checks read a profile cached before the run began "
        "(deployment setting, not harness logic)"
    ),
    "silence_is_failure": (
        "an utterance the extractor found no fact in was reported as a "
        "storage failure rather than skipped"
    ),
    "script_decides_ownership": (
        "criterion H was decided by the scenario script's phrasing rather "
        "than by who owns the text in memory.db"
    ),
    "reuse_run_number": (
        "every run reused run_number=1, so validation replayed earlier runs "
        "out of audit_log.db and manufactured cross-user findings"
    ),
    "literal_verdicts": (
        'the report writer asserted "user_isolation": "PASS" and '
        "isolation_pass_rate: 1.0 unconditionally, and decided audit "
        "completeness by events_played > 0 rather than by any audit check"
    ),
}


@dataclass(frozen=True)
class LegacyCheckerConfig:
    """Which pre-fix faults to reintroduce. All off by default."""

    synthetic_fact_ids: bool = False
    compare_utterance_text: bool = False
    assume_long_profile_cache: bool = False
    silence_is_failure: bool = False
    script_decides_ownership: bool = False
    reuse_run_number: bool = False
    literal_verdicts: bool = False

    @classmethod
    def from_names(cls, names: Iterable[str]) -> "LegacyCheckerConfig":
        """Build a config from fault names, or from the single name ``all``."""
        requested = [name.strip() for name in names if name and name.strip()]
        if any(name == "all" for name in requested):
            return cls(**{fault: True for fault in ALL_FAULTS})
        unknown = [name for name in requested if name not in ALL_FAULTS]
        if unknown:
            raise ValueError(
                f"unknown legacy fault(s) {unknown}; known faults are "
                f"{list(ALL_FAULTS)} or 'all'"
            )
        return cls(**{name: True for name in requested})

    @property
    def enabled_faults(self) -> Tuple[str, ...]:
        return tuple(
            field.name for field in fields(self) if getattr(self, field.name) is True
        )

    @property
    def any_enabled(self) -> bool:
        return bool(self.enabled_faults)

    @property
    def engine_ttl_seconds(self) -> Optional[float]:
        """The profile cache TTL the engine must run with, when that fault is on."""
        return (
            LEGACY_PROFILE_CACHE_TTL_SECONDS if self.assume_long_profile_cache else None
        )

    def run_number_for(self, next_free: int) -> int:
        """The run number to validate against."""
        return LEGACY_RUN_NUMBER if self.reuse_run_number else next_free

    def describe(self) -> Dict[str, Dict[str, str]]:
        return {
            fault: {
                "commit": FAULT_COMMITS[fault],
                "description": FAULT_DESCRIPTIONS[fault],
            }
            for fault in self.enabled_faults
        }


def legacy_criterion_payload(
    config: LegacyCheckerConfig,
    audit_tally: Dict[str, int],
    isolation_tally: Dict[str, int],
    events_played: int,
) -> Dict[str, Any]:
    """The verdicts the pre-fix report writer would have published.

    Returns ``{}`` unless ``literal_verdicts`` is enabled. The tallies are
    accepted and echoed so a reader can see the evidence the old writer
    overrode: criterion H was ``PASS`` whatever the checks found, and criterion
    G asked only whether any event had been played.
    """
    if not config.literal_verdicts:
        return {}
    return {
        "criterion_g_audit": {
            **audit_tally,
            "verdict": "PASS" if events_played > 0 else "FAIL",
            "basis": "events_played > 0",
        },
        "criterion_h_isolation": {
            **isolation_tally,
            "verdict": "PASS",
            "isolation_pass_rate": 1.0,
            "basis": "literal written by the report writer",
        },
    }
