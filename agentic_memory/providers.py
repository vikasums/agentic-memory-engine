import json
import asyncio
from typing import List, Protocol, Optional, Dict, Any
import requests
from .models import FactRecord, Scope

class Embedder(Protocol):
    """Protocol for vector embedding generation."""
    def embed(self, texts: List[str]) -> List[List[float]]:
        ...
    @property
    def dimension(self) -> int:
        ...

class Extractor(Protocol):
    """Protocol for extracting structured facts from natural language."""
    async def extract_facts(self, text: str) -> List[FactRecord]:
        ...


class FastEmbedEmbedder:
    """Local embedding provider using FastEmbed library."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        self.model_name = model_name
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise ImportError(
                "fastembed is required for FastEmbedEmbedder. "
                "Install with `pip install agentic_memory[local]`"
            ) from e
        self._model = TextEmbedding(model_name)
        # Test embed to determine dimension dynamically or fallback to 384
        sample = list(self._model.embed(["test"]))
        self._dim = len(sample[0]) if sample else 384

    def embed(self, texts: List[str]) -> List[List[float]]:
        embeddings = list(self._model.embed(texts))
        return [e.tolist() if hasattr(e, "tolist") else list(e) for e in embeddings]

    @property
    def dimension(self) -> int:
        return self._dim


class OpenAICompatibleEmbedder:
    """Embedding provider supporting OpenAI-compatible proxies / endpoints."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        model: str = "text-embedding-3-small",
        api_key: str = "token",
        dimension: int = 384
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._dim = dimension

    def embed(self, texts: List[str]) -> List[List[float]]:
        url = f"{self.base_url}/embeddings"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "input": texts,
            "model": self.model
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        
        # Sort embeddings by index
        sorted_data = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in sorted_data]

    @property
    def dimension(self) -> int:
        return self._dim


class OllamaExtractor:
    """Local fact extractor using Ollama."""

    def __init__(self, model_name: str = "qwen2.5:14b-instruct"):
        self.model_name = model_name
        try:
            import ollama
        except ImportError as e:
            raise ImportError(
                "ollama package is required for OllamaExtractor. "
                "Install with `pip install agentic_memory[local]`"
            ) from e
        self.ollama = ollama

    async def extract_facts(self, text: str) -> List[FactRecord]:
        prompt = f"""You are an expert system designed to extract clear, atomic facts from user input.
Identify any facts about the user's name, profession, hobbies, interests, location, preferences, or other personal details.
For each fact, extract:
- subject: Who or what the fact is about (typically 'user')
- predicate: The relationship (e.g. 'works_as', 'enjoys', 'specializes_in', 'lives_in', 'name_is')
- object_value: The detail or value (e.g. 'Software Engineer', 'tennis', 'Generative AI', 'Vikas')
- confidence: A confidence score between 0.0 and 1.0

Return ONLY JSON matching this format:
{{
  "facts": [
    {{"subject": "user", "predicate": "lives_in", "object_value": "Seattle", "confidence": 0.95}}
  ]
}}
Text: {text}"""

        response = await asyncio.to_thread(
            self.ollama.chat,
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            format="json"
        )

        content = response.get('message', {}).get('content', '{}')
        data = json.loads(content)
        raw_facts = data.get("facts", [])

        facts = []
        for f in raw_facts:
            facts.append(
                FactRecord(
                    subject=f.get("subject", "user"),
                    predicate=f.get("predicate", ""),
                    object_value=f.get("object_value", ""),
                    confidence=float(f.get("confidence", 1.0)),
                    scope=Scope.USER  # Caller sets scope, model does not set scope
                )
            )
        return facts


class OpenAICompatibleExtractor:
    """Fact extractor supporting OpenAI-compatible proxies / endpoints."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        model: str = "gpt-4o-mini",
        api_key: str = "token"
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    async def extract_facts(self, text: str) -> List[FactRecord]:
        prompt = f"""You are an expert system designed to extract clear, atomic facts from user input.
Identify any facts about the user's name, profession, hobbies, interests, location, preferences, or other personal details.
For each fact, extract:
- subject: Who or what the fact is about (typically 'user')
- predicate: The relationship (e.g. 'works_as', 'enjoys', 'specializes_in', 'lives_in', 'name_is')
- object_value: The detail or value (e.g. 'Software Engineer', 'tennis', 'Generative AI', 'Vikas')
- confidence: A confidence score between 0.0 and 1.0

Return ONLY JSON matching this format:
{{
  "facts": [
    {{"subject": "user", "predicate": "lives_in", "object_value": "Seattle", "confidence": 0.95}}
  ]
}}
Text: {text}"""

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}
        }

        loop = asyncio.get_running_loop()
        def _call_api():
            resp = requests.post(url, json=payload, headers=headers, timeout=30.0)
            resp.raise_for_status()
            return resp.json()

        data_raw = await loop.run_in_executor(None, _call_api)
        content = data_raw.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        data = json.loads(content)
        raw_facts = data.get("facts", [])

        facts = []
        for f in raw_facts:
            facts.append(
                FactRecord(
                    subject=f.get("subject", "user"),
                    predicate=f.get("predicate", ""),
                    object_value=f.get("object_value", ""),
                    confidence=float(f.get("confidence", 1.0)),
                    scope=Scope.USER
                )
            )
        return facts
