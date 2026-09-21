# Release Notes — v0.3.0

## 🧠 Agentic Memory Engine — Self-Maintaining Memory, and Proof That It Works

Version **0.3.0** does two things for you. It makes stored memory maintain itself — facts
correct themselves when the user changes their mind, and they expire when they stop being
true — and it ships the instrumentation that shows you, run by run, whether the engine is
actually behaving that way on your data.

---

## 🌟 What's New and Why It Helps

### 1. Memory that corrects itself (contradiction resolution)
When a user says something that conflicts with what the engine already stored, the older
fact is deactivated and the new one takes its place. Every change is written to an audit
trail, so you can see what was replaced and when.

**Helps you:** no stale preferences leaking into prompts, and no hand-written reconciliation
logic in your application. "I moved to Berlin" simply supersedes "I live in Seattle."

### 2. Facts that expire on schedule (auto-expiry)
`FactRecord` now carries an `expires_at` timestamp, the SQLite schema tracks it, and
`MemoryPruner` purges expired facts automatically. TTLs are set per fact.

**Helps you:** short-lived context (a travel date, a temporary project, a one-off request)
ages out on its own, so long-lived users don't accumulate an ever-growing pile of
irrelevant facts — which keeps both retrieval quality and storage cost in check.

### 3. Fast user profiles (`POST /profile` + profile cache)
A profile splits a user into `stable_facts` and `recent_activity`, and is cached with a
configurable TTL. Cached retrieval benchmarks under 50ms.

**Helps you:** a single call gives you a prompt-ready summary of a user instead of a
retrieval query per turn — cheap enough to put on the hot path of a chat loop.

### 4. Command-line tool (`agentic-memory`)
A console entry point with `ingest`, `retrieve`, `profile`, `metrics`, `cleanup`, and
`set-expiry` commands. Startup is under 500ms.

**Helps you:** inspect and repair a memory store, load fixtures, or check storage footprint
without writing a script or standing up the API.

### 5. Simulation and validation system
A deterministic 50-scenario catalogue (contradiction, expiry, cache, multi-user, modify and
generic cases) plays back against a running engine. Every state change is recorded in
`audit_log.db`, and `ValidatorService` cross-checks the outcome three independent ways —
the database, the API, and the audit trail — across 27 checkers.

**Helps you:** a repeatable answer to "did my last change break memory correctness?" that
does not depend on reading logs by hand. Run reports are generated from the run data, so
the numbers in them reflect what actually happened.

### 6. Monitoring dashboard (Dashboard API + React frontend)
Start, watch, and stop a simulation run from the browser: live progress, cache hit rate,
latency, fact counts, audit events, and validation results.

**Helps you:** see contradiction handling, expiry and cache behaviour as they happen, which
makes tuning TTLs and diagnosing a bad run a matter of looking rather than guessing.

---

## 📊 Measured Results

From `VALIDATION_RESULTS.md`, generated from the JSON run reports in `reports/`:

| Run | Duration | Scenarios passed | Checks passed | Failed |
|-----|----------|------------------|---------------|--------|
| 1 | 1 minute | 43/50 | 290 | 3 |
| 2 | 2 minutes | 42/50 | 293 | 4 |
| 3 | 4 minutes | 44/50 | 291 | 2 |
| 4 | 6 minutes | 43/50 | 299 | 3 |

- **Criterion G — audit completeness: PASS** on all four runs (76 passed, 0 failed each).
- **Criterion H — user isolation: PASS** on all four runs (50 passed, 0 failed each).
- Test suite: **383 tests passing**.

### Known limitations

- The engine's extractor returns no fact for a large share of utterances (66–69 of 111 per
  run) and is not deterministic across identical inputs. Presence checks skip those
  utterances rather than reporting a storage failure, which is why the run reports show a
  high skip count. Extraction coverage is the main open item.
- Cross-validation still reports ~10 disagreements per run between the three validation
  methods.

---

## 🐛 Fixes in This Release

- `GET /simulate/{run_id}/status` returned HTTP 500 after a run was stopped; a cancelled run
  now reports `stopped`.
- The validator no longer reports extractor silence or a phrasing shared between users as a
  correctness failure — both were false negatives that understated the pass rate.
- Run reports are derived from the runs themselves instead of carrying hardcoded verdicts.
- The React dashboard renders run data correctly (polling, CORS, and unmount fixes).

---

## 🛠 Upgrading from v0.2.0

No breaking API changes, but the fact schema gained an `expires_at` column and the engine
does not add it to an existing database on startup. A `memory.db` created before v0.3.0
has no `expires_at`, and the pruner raises against it. Migrate it once — the statements are
additive `ALTER TABLE ... ADD COLUMN`, so no row is rewritten and nothing is deleted:

```python
from simulation.database import migrate_memory_db

migrate_memory_db("memory.db")  # returns the columns it added; [] if already current
```

A fresh database needs nothing. Facts ingested without a TTL have `expires_at` unset and
never expire, so existing behaviour is preserved.

---

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
