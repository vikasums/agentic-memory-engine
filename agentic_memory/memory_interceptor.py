import json
import requests
from typing import List, Dict, Any, Optional

class MemoryInterceptor:
    def __init__(self, memory_api_base: str = "http://localhost:8000"):
        self.memory_api_base = memory_api_base

    def _fetch_memories(self, user_id: str, query: str, top_k: int = 3) -> List[str]:
        """Queries the local memory API for relevant active facts."""
        try:
            response = requests.post(
                f"{self.memory_api_base}/retrieve",
                json={
                    "user_id": user_id,
                    "query": query,
                    "top_k": top_k,
                    "half_life_days": 30.0
                },
                timeout=2.0  # Fast sub-20ms lookup fallback
            )
            if response.status_code == 200:
                return response.json().get("memories", [])
        except Exception:
            # Fallback gracefully if memory API is unreachable
            pass
        return []

    def inject_context(
        self, 
        messages: List[Dict[str, str]], 
        user_id: str, 
        top_k: int = 3
    ) -> List[Dict[str, str]]:
        """Intercepts messages payload and updates system prompt with user memories."""
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

        # 3. Format memories into a system block
        memory_block = "\n".join([f"- {m}" for m in memories])
        system_instruction = (
            f"\n\n[RELEVANT USER MEMORIES (Ground Truth)]\n"
            f"{memory_block}\n"
            f"Use the above facts to personalize your answer without explicitly mentioning you retrieved them."
        )

        # 4. Inject into existing system prompt or insert a new system message
        messages_copy = [dict(m) for m in messages]
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
