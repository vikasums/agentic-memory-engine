import time
import math
import numpy as np
from typing import List, Optional, Tuple
from .models import Scope, FactRecord, MemoryRecord, StoreFilter, ScoredMemory
from .store import MemoryStore, SQLiteLanceDBStore
from .providers import Embedder, Extractor, FastEmbedEmbedder, OllamaExtractor, OpenAICompatibleEmbedder, OpenAICompatibleExtractor
from .config import MemorySettings, default_settings
from .metrics import logger, time_operation

class MemoryEngine:
    """Core memory engine responsible for fact extraction, storage, and decay-ranked retrieval."""

    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        embedder: Optional[Embedder] = None,
        extractor: Optional[Extractor] = None,
        settings: Optional[MemorySettings] = None
    ):
        self.settings = settings or MemorySettings()
        
        # Default initialization if dependencies not injected
        if store is not None:
            self.store = store
        else:
            if self.settings.storage_backend == "sqlite":
                self.store = SQLiteLanceDBStore(
                    db_path=self.settings.db_path,
                    lancedb_path=self.settings.lancedb_path
                )
            else:
                raise ValueError(f"Unsupported storage backend: {self.settings.storage_backend}")

        if embedder is not None:
            self.embedder = embedder
        else:
            if self.settings.embedder_provider == "fastembed":
                self.embedder = FastEmbedEmbedder(model_name=self.settings.embedder_model)
            elif self.settings.embedder_provider == "openai":
                self.embedder = OpenAICompatibleEmbedder(
                    base_url=self.settings.embedder_base_url or "http://localhost:11434/v1",
                    model=self.settings.embedder_model,
                    api_key=self.settings.embedder_api_key
                )
            else:
                raise ValueError(f"Unsupported embedder provider: {self.settings.embedder_provider}")

        if extractor is not None:
            self.extractor = extractor
        else:
            if self.settings.extractor_provider == "ollama":
                self.extractor = OllamaExtractor(model_name=self.settings.extractor_model)
            elif self.settings.extractor_provider == "openai":
                self.extractor = OpenAICompatibleExtractor(
                    base_url=self.settings.extractor_base_url or "http://localhost:11434/v1",
                    model=self.settings.extractor_model,
                    api_key=self.settings.extractor_api_key
                )
            else:
                raise ValueError(f"Unsupported extractor provider: {self.settings.extractor_provider}")

    async def process_paragraph_detailed(
        self,
        text: str,
        user_id: str,
        scope: Scope = Scope.USER
    ) -> List[Tuple[str, str]]:
        """Extract facts and upsert them, returning ``(memory_id, stored_text)``.

        The stored text is the normalised ``subject predicate object`` triple,
        which is what retrieval ranks and returns — not the caller's prose.
        """
        facts = await self.extractor.extract_facts(text)
        extracted: List[Tuple[str, str]] = []

        for fact in facts:
            # Enforce scope set by caller
            fact_str = f"{fact.subject} {fact.predicate} {fact.object_value}"
            mem_id = self.store.upsert_fact(fact=fact, user_id=user_id, scope=scope)

            # Embed fact text
            vectors = self.embedder.embed([fact_str])
            vector = vectors[0] if vectors else [0.0] * self.embedder.dimension

            if hasattr(self.store, "add_vector_record"):
                self.store.add_vector_record(
                    memory_id=mem_id,
                    user_id=user_id,
                    scope=scope,
                    text=fact_str,
                    vector=vector,
                    timestamp=time.time()
                )
            extracted.append((mem_id, fact_str))

        return extracted

    async def process_paragraph_async(
        self,
        text: str,
        user_id: str,
        scope: Scope = Scope.USER
    ) -> List[str]:
        """Extracts facts from natural language text and upserts them into storage."""
        return [
            mem_id
            for mem_id, _ in await self.process_paragraph_detailed(text, user_id, scope)
        ]

    @time_operation("retrieve_memories")
    def retrieve_memories(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        half_life_days: float = 30.0,
        scope: Scope = Scope.USER
    ) -> List[MemoryRecord]:
        """Retrieves and ranks memories based on vector similarity and half-life decay."""
        query_vectors = self.embedder.embed([query])
        query_vector = query_vectors[0] if query_vectors else [0.0] * self.embedder.dimension

        fetch_limit = top_k * self.settings.candidate_overfetch_factor
        filter_params = StoreFilter(user_id=user_id, is_active=True, include_global=(scope != Scope.USER or True))

        scored_candidates = self.store.search_vectors(
            vector=query_vector,
            filter_params=filter_params,
            limit=fetch_limit
        )

        if not scored_candidates:
            return []

        now = time.time()
        effective_half_life = max(0.001, half_life_days)
        lambda_decay = math.log(2) / effective_half_life

        records: List[MemoryRecord] = []
        for cand in scored_candidates:
            similarity = max(0.0, min(1.0, 1.0 - cand.distance))
            age_days = (now - cand.timestamp) / 86400.0
            decay_factor = math.exp(-lambda_decay * age_days)
            final_score = similarity * decay_factor

            records.append(
                MemoryRecord(
                    text=cand.text,
                    score=final_score,
                    similarity=similarity,
                    decay_factor=decay_factor,
                    source=cand.id,
                    timestamp=cand.timestamp,
                    scope=cand.scope,
                    age_days=age_days
                )
            )

        # Sort by final decayed score descending
        records.sort(key=lambda r: r.score, reverse=True)
        return records[:top_k]

    def generate_user_profile(self, user_id: str, recent_days: int = 7) -> dict:
        """Generates user profile: stable facts + recent activity."""
        now = time.time()
        recent_cutoff = now - (recent_days * 86400)

        # Get all active facts for user
        from .models import StoreFilter
        filter_params = StoreFilter(user_id=user_id, is_active=True, include_global=True)

        # Query for recent activity (last N days) - fetch all available memories
        all_memories = self.store.search_vectors([0.0] * self.embedder.dimension, filter_params, limit=10000)

        stable_facts = []
        recent_activity = []

        for mem in all_memories:
            if mem.timestamp >= recent_cutoff:
                recent_activity.append(mem.text)
            else:
                stable_facts.append(mem.text)

        # Cache profile for fast retrieval (~50ms)
        if hasattr(self.store, "cache_user_profile"):
            self.store.cache_user_profile(user_id, stable_facts, recent_activity)

        return {
            "user_id": user_id,
            "stable_facts": stable_facts,
            "recent_activity": recent_activity,
            "profile_timestamp": now
        }

    def get_user_profile(self, user_id: str) -> Optional[dict]:
        """Retrieves cached user profile if available."""
        if hasattr(self.store, "get_cached_profile"):
            return self.store.get_cached_profile(user_id)
        return None

    def get_storage_footprint(self) -> dict:
        """Returns storage metrics and row counts from underlying store."""
        return self.store.get_footprint()

    def close(self):
        """Closes memory store resources."""
        if hasattr(self.store, "close"):
            self.store.close()


def create_engine(settings: Optional[MemorySettings] = None) -> MemoryEngine:
    """Factory helper to instantiate MemoryEngine with configured defaults."""
    return MemoryEngine(settings=settings)
