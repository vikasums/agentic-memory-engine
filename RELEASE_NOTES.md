# Release Notes — v0.2.0

## 🚀 Agentic Memory Engine — Production Architecture Refactor

Version **0.2.0** is a major architectural release transforming the Agentic Memory Engine into a production-ready, highly modular, and secure long-term memory framework for AI agents.

---

## 🌟 Key Improvements

### 1. Pluggable Storage Abstraction (`MemoryStore`)
- Introduced the `MemoryStore` protocol allowing hot-swappable persistence backends.
- **`SQLiteLanceDBStore`**: Default zero-config storage combining SQLite for relational fact keys and LanceDB for vector search.
- **`MariaDBStore`**: Backend interface designed for multi-container job environments where memory must persist and be shared across job runners.
- **Thread Safety**: Fixed thread locking using reentrant locks (`threading.RLock()`), preventing lock deadlocks during async extractions.

### 2. Provider-Agnostic Model Interface (`Embedder` & `Extractor`)
- Abstracted model dependencies behind `Embedder` and `Extractor` protocols.
- **Local Models**: FastEmbed for local vector embeddings and Ollama for local fact extraction.
- **OpenAI-Compatible Proxies**: `OpenAICompatibleEmbedder` and `OpenAICompatibleExtractor` allowing direct connection to team proxies, custom gateways, Azure, or OpenAI API endpoints (`base_url`, `model`, `api_key`).

### 3. Lean Core & Modular Dependencies
- **Minimal Core Dependencies**: Core installation reduced to 5 lightweight packages (`fastapi`, `uvicorn`, `pydantic`, `numpy`, `requests`).
- **Modular Extras**: Heavy libraries moved to optional extras:
  - `pip install "agentic_memory[local]"` (LanceDB, FastEmbed, Ollama)
  - `pip install "agentic_memory[openai]"` (OpenAI-compatible proxy support)
  - `pip install "agentic_memory[mariadb]"` (MariaDB driver)
- **LangChain Purged**: Removed heavy LangChain framework dependencies in favor of standard async Python and Pydantic validation.

### 4. Secure Tenant & Scope Isolation
- **Parameterized Queries**: Applied SQL parameter binding (`?`) and string sanitization across LanceDB filter expressions to prevent SQL and vector filter injection attacks.
- **Caller-Controlled Scope**: LLM extractions no longer determine memory scope (`user` vs `global`). Scope is strictly specified by the caller/API request.

### 5. Memory as Evidence Metadata
- **Structured Evidence Records**: `/retrieve` now returns `MemoryRecord` objects containing similarity, time-decay factor, source ID, timestamp, scope, and age in days.
- **Context Interceptor**: Formats retrieved memories as contextual evidence hints (`[RELEVANT USER CONTEXT (Hints - Not database ground truth)]`) with confidence percentages and age metadata, letting LLMs weigh relevance appropriately.

### 6. Automated Test Suite & Quality Assurance
- Added 14 unit and integration test cases in `tests/` covering end-to-end ingestion, retrieval, prompt interception, isolation, model providers, and configuration defaults.
- All 14 tests pass in under **1 second** (0.98s) with 0 errors and 0 warnings.

---

## 🛠 Breaking Changes & Migration

- `/retrieve` response shape changed from `{"memories": ["string"]}` to structured evidence objects:
  ```json
  {
    "user_id": "user_123",
    "query": "What are my hobbies?",
    "memories": [
      {
        "text": "user enjoys playing guitar",
        "score": 0.92,
        "similarity": 0.92,
        "decay_factor": 1.0,
        "source": "mem_1741829102",
        "timestamp": 1741829102.12,
        "scope": "user",
        "age_days": 0.0
      }
    ]
  }
  ```
- Package dependencies now require specifying extras for local dev (`pip install ".[local]"`).
