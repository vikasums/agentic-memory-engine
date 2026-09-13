from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

class Scope(str, Enum):
    USER = "user"
    GLOBAL = "global"

@dataclass(frozen=True, slots=True)
class FactRecord:
    """A single extracted fact from natural language input."""
    subject: str
    predicate: str
    object_value: str
    confidence: float = 1.0
    scope: Scope = Scope.USER

@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """A retrieved memory with evidence metadata."""
    text: str
    score: float
    similarity: float
    decay_factor: float
    source: str
    timestamp: float
    scope: Scope
    age_days: float

@dataclass(frozen=True, slots=True)
class StoreFilter:
    """Filter criteria for vector and relational memory queries."""
    user_id: str
    is_active: bool = True
    include_global: bool = True

@dataclass(slots=True)
class ScoredMemory:
    """Raw search result item from storage engine prior to decay ranking."""
    id: str
    user_id: str
    scope: Scope
    text: str
    vector: List[float]
    is_active: bool
    timestamp: float
    distance: float = 0.0
