"""ValidatorService — auto-verification and cross-validation (spec § 3.6, § 6).

Skeleton only. Task 7 implements the four validation methods (direct memory.db
query, API retrieval, audit trail replay, profile API) and the cross-validation
that requires all four to agree before a scenario is marked PASS.
"""

from typing import Any, Dict, List, Optional

from .database import DEFAULT_AUDIT_DB_PATH
from .engine_client import EngineClient
from .models import ValidationResult

DEFAULT_MEMORY_DB_PATH = "memory.db"


class ValidatorService:
    """Verifies that monitoring data matches the engine's actual state."""

    def __init__(
        self,
        memory_db_path: str = DEFAULT_MEMORY_DB_PATH,
        audit_db_path: str = DEFAULT_AUDIT_DB_PATH,
        client: Optional[EngineClient] = None,
    ) -> None:
        self.memory_db_path = memory_db_path
        self.audit_db_path = audit_db_path
        self.client = client

    def validate_via_db(self, fact_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Method 1: direct memory.db query. Implemented in Task 7."""
        raise NotImplementedError("validate_via_db is implemented in Task 7")

    async def validate_via_api(self, user_id: str, query_text: str) -> Dict[str, Any]:
        """Method 2: API /retrieve. Implemented in Task 7."""
        raise NotImplementedError("validate_via_api is implemented in Task 7")

    def validate_via_audit(self, fact_id: str, user_id: str) -> Dict[str, Any]:
        """Method 3: replay audit_log.db events. Implemented in Task 7."""
        raise NotImplementedError("validate_via_audit is implemented in Task 7")

    async def validate_via_profile(self, user_id: str) -> Dict[str, Any]:
        """Method 4: API /profile. Implemented in Task 7."""
        raise NotImplementedError("validate_via_profile is implemented in Task 7")

    async def cross_validate(self, fact_id: str, user_id: str) -> List[ValidationResult]:
        """Assert all four methods agree. Implemented in Task 7."""
        raise NotImplementedError("cross_validate is implemented in Task 7")
