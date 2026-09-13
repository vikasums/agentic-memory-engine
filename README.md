# Agentic Memory Engine

A lightweight, pluggable long-term memory framework designed for LLM agents, personalized assistants, and multi-tenant applications.

The engine extracts structured atomic facts from natural language, indexes them in vector storage with relational metadata key-versioning, and retrieves context ranked with exponential time decay.

---

## Key Features & Architecture

- **Pluggable Storage Abstraction (`MemoryStore`)**:
  - `SQLiteLanceDBStore` — Zero-config local storage combining SQLite for fact key-versioning and LanceDB for vector search.
  - `MariaDBStore` — Multi-container shared persistent backend for distributed production job runs.
- **Provider-Agnostic Model Interface (`Embedder` & `Extractor`)**:
  - Local model execution via `FastEmbed` and `Ollama`.
  - Production-ready OpenAI-compatible endpoints (`base_url`, `model`, `api_key`) routing through team proxies or custom deployments.
- **Strict Tenant & Scope Isolation**:
  - Fully parameterized SQL and vector filter queries.
  - Scope (`user` vs `global`) is caller-controlled to prevent untrusted prompt injection.
- **Memory as Evidence**:
  - Returns structured `MemoryRecord` instances containing similarity, decay factor, source ID, timestamp, and composite score.
  - System context interceptor treats retrieved memories as contextual hints rather than database ground truth.
- **Lean Dependency Core**:
  - Minimal 5-package core (`fastapi`, `uvicorn`, `pydantic`, `numpy`, `requests`).
  - Modular optional extras (`[local]`, `[openai]`, `[mariadb]`).

---

## Installation

### Core Installation (Lean)
```bash
pip install agentic_memory
```

### Local Dev Installation (LanceDB + FastEmbed + Ollama)
```bash
pip install "agentic_memory[local]"
```

### Production Installation (OpenAI-compatible Proxy + MariaDB)
```bash
pip install "agentic_memory[openai,mariadb]"
```

---

## Quickstart

### 1. Direct Python API Usage

```python
import asyncio
from agentic_memory import create_engine, Scope

async def main():
    # Initialize engine with configured defaults
    engine = create_engine()

    # Ingest facts
    user_id = "user_9012"
    await engine.process_paragraph_async(
        text="User is a Senior Systems Engineer based in Zurich working on Kubernetes.",
        user_id=user_id,
        scope=Scope.USER
    )

    # Retrieve decay-ranked evidence records
    memories = engine.retrieve_memories(
        query="What is my location and engineering focus?",
        user_id=user_id,
        top_k=3
    )

    for m in memories:
        print(f"Fact: {m.text} | Score: {m.score:.2f} | Source: {m.source} | Age: {m.age_days:.1f}d")

if __name__ == "__main__":
    asyncio.run(main())
```

---

### 2. Launch FastAPI Server

Start the REST API server:

```bash
python -m uvicorn agentic_memory.main_api:app --host 0.0.0.0 --port 8000
```

#### API Endpoints:

- `POST /ingest`: Ingests free text asynchronously.
  ```json
  {
    "user_id": "user_123",
    "text": "User loves playing acoustic guitar.",
    "scope": "user"
  }
  ```
- `POST /retrieve`: Retrieves decay-ranked evidence records.
  ```json
  {
    "user_id": "user_123",
    "query": "What instruments do I play?",
    "top_k": 3,
    "half_life_days": 30.0,
    "scope": "user"
  }
  ```
  **Response**:
  ```json
  {
    "user_id": "user_123",
    "query": "What instruments do I play?",
    "memories": [
      {
        "text": "user plays acoustic guitar",
        "score": 0.8912,
        "similarity": 0.8912,
        "decay_factor": 1.0,
        "source": "mem_1741829102",
        "timestamp": 1741829102.12,
        "scope": "user",
        "age_days": 0.0
      }
    ]
  }
  ```

---

### 3. Prompt Interceptor

Automatically inject context hints into chat payloads:

```python
from agentic_memory import MemoryInterceptor

interceptor = MemoryInterceptor(memory_api_base="http://localhost:8000")

messages = [
    {"role": "user", "content": "Suggest a weekend itinerary for me."}
]

hydrated_messages = interceptor.inject_context(messages, user_id="user_123")
```

---

## Configuration Reference

Configure using environment variables prefixed with `MEMORY_`:

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `MEMORY_STORAGE_BACKEND` | `sqlite` | Persistence engine (`sqlite`, `mariadb`) |
| `MEMORY_DB_PATH` | `memory.db` | Path to local SQLite metadata database |
| `LANCEDB_PATH` | `./lancedb_data` | Directory containing LanceDB vector tables |
| `MEMORY_MARIADB_URL` | `None` | MariaDB connection string for shared storage |
| `MEMORY_EMBEDDER_PROVIDER` | `fastembed` | Embedding provider (`fastembed`, `openai`) |
| `MEMORY_EMBEDDER_MODEL` | `BAAI/bge-small-en-v1.5` | Embedding model identifier |
| `MEMORY_EMBEDDER_BASE_URL` | `http://localhost:11434/v1` | Base URL for OpenAI-compatible embedding proxy |
| `MEMORY_EXTRACTOR_PROVIDER` | `ollama` | Fact extraction provider (`ollama`, `openai`) |
| `MEMORY_EXTRACTOR_MODEL` | `qwen2.5:14b-instruct` | LLM model identifier for extraction |
| `MEMORY_EXTRACTOR_BASE_URL` | `http://localhost:11434/v1` | Base URL for OpenAI-compatible extraction proxy |
| `MEMORY_DEFAULT_HALF_LIFE_DAYS`| `30.0` | Half-life in days for exponential time-decay ranking |
