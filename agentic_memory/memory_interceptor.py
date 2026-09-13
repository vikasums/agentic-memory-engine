import copy
import requests
from typing import List, Dict, Any, Optional

class MemoryInterceptor:
    """Intercepts LLM chat payloads to inject relevant long-term memory context hints."""

    def __init__(self, memory_api_base: str = "http://localhost:8000"):
        self.memory_api_base = memory_api_base.rstrip("/")

    def _fetch_memories(self, user_id: str, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Queries the memory API for relevant active facts with evidence metadata."""
        try:
            response = requests.post(
                f"{self.memory_api_base}/retrieve",
                json={
                    "user_id": user_id,
                    "query": query,
                    "top_k": top_k,
                    "half_life_days": 30.0
                },
                timeout=2.0
            )
            if response.status_code == 200:
                return response.json().get("memories", [])
        except Exception:
            # Graceful fallback if memory API is temporarily unreachable
            pass
        return []

    def inject_context(
        self,
        messages: List[Dict[str, str]],
        user_id: str,
        top_k: int = 3
    ) -> List[Dict[str, str]]:
        """Intercepts messages payload and updates system prompt with user memory context hints."""
        # 1. Extract the latest user message to use as the query
        user_query = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_query = msg.get("content", "")
                break

        if not user_query:
            return messages

        # 2. Retrieve memories from the FastAPI engine
        memories = self._fetch_memories(user_id=user_id, query=user_query, top_k=top_k)
        if not memories:
            return messages

        # 3. Format memories into an evidence hint block
        formatted_hints = []
        for m in memories:
            if isinstance(m, dict):
                text = m.get("text", "")
                score = m.get("score", 1.0)
                age_days = m.get("age_days", 0.0)
                formatted_hints.append(f"- {text} (confidence: {score:.0%}, {age_days:.0f}d ago)")
            else:
                formatted_hints.append(f"- {m}")

        memory_block = "\n".join(formatted_hints)
        system_instruction = (
            f"\n\n[RELEVANT USER CONTEXT (Hints - Not database ground truth)]\n"
            f"{memory_block}\n"
            f"Use the above hints to personalize your answer if relevant, without explicitly claiming certainty."
        )

        # 4. Inject into existing system prompt or insert a new system message
        messages_copy = copy.deepcopy(messages)
        system_msg_idx = next(
            (i for i, m in enumerate(messages_copy) if m.get("role") == "system"),
            None
        )

        if system_msg_idx is not None:
            messages_copy[system_msg_idx]["content"] += system_instruction
        else:
            messages_copy.insert(0, {
                "role": "system",
                "content": f"You are a helpful assistant.{system_instruction}"
            })

        return messages_copy
