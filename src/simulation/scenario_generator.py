"""ScenarioGenerator — deterministic test scenario construction (spec § 3.1, § 5).

Skeleton only. Task 2 fills in the 50+ scenario catalogue (10 contradiction,
8 expiry, 6 profile cache, 10 multi-user, 10 generic, 6 modification) and the
random-but-seeded distribution of scenarios across users and run timeline.

Phase 1 is deterministic: no LLM calls anywhere in this module.
"""

from typing import List, Optional

from .models import Scenario

DEFAULT_USER_COUNT = 5
DEFAULT_USER_IDS = [f"user_{i}" for i in range(1, DEFAULT_USER_COUNT + 1)]


class ScenarioGenerator:
    """Builds the deterministic scenario set for a simulation run.

    Args:
        seed: RNG seed so a given run number reproduces the same distribution.
        user_ids: Simulated users to spread scenarios across (default: 5).
    """

    def __init__(self, seed: int = 0, user_ids: Optional[List[str]] = None) -> None:
        self.seed = seed
        self.user_ids = list(user_ids) if user_ids else list(DEFAULT_USER_IDS)

    def generate(self, duration_seconds: float) -> List[Scenario]:
        """Return the scenario set, timed within ``0..duration_seconds``.

        Implemented in Task 2.
        """
        raise NotImplementedError("ScenarioGenerator.generate is implemented in Task 2")
