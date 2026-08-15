# Agentic Memory Engine

A lightweight, hybrid long-term memory engine designed for LLM agents and assistant personalization. It extracts structured facts from conversations, stores them in vector space with semantic indexing, and retrieves relevant context with a time-decay algorithm.

## Features

- **Hybrid Storage**: Uses SQLite for structured relational fact keys and LanceDB for lightning-fast vector similarity search.
- **Asynchronous Ingestion**: Ingests paragraphs of text in a background thread to prevent blocking agent execution.
- **Time-Decay Ranking**: Ranks memories using a combination of vector similarity and memory age (half-life decay).
- **Auto-Injection Interceptor**: Includes a standard interceptor to automatically scan user queries, retrieve memories, and inject them into system prompts.
- **Multi-User Isolation**: Safely namespaces database storage and vector indexes by user ID to prevent cross-user context leakage.

---

## Installation

Install the package directly from source:

```bash
pip install .
```

---

## Getting Started

### 1. Launch the API Service
Run the FastAPI memory engine server to handle ingestion and retrieval:

```bash
python -m uvicorn agentic_memory.main_api:app --host 0.0.0.0 --port 8000
```

### 2. Auto-Hydrate Conversations (Prompt Injection)
Use the `MemoryInterceptor` to automatically retrieve relevant context and inject it into the LLM system prompt:

```python
from agentic_memory import MemoryInterceptor

# Initialize the interceptor
interceptor = MemoryInterceptor(memory_api_base="http://localhost:8000")

# Input messages from user session
input_messages = [
    {"role": "user", "content": "What is my location and focus area?"}
]

# Hydrate system prompt with relevant user memories
hydrated_messages = interceptor.inject_context(input_messages, user_id="user_4920")

# Send hydrated_messages directly to your LLM API
```

---

## Custom Tool Integration (LangChain Example)

Expose memory save and retrieve functions to your agent as LangChain tools:

```python
from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from agentic_memory import MemoryEngine

# Define the tools
@tool
def save_user_memory(user_id: str, text: str) -> str:
    """Saves a new fact about the user."""
    # (Send requests.post to http://localhost:8000/ingest)
    ...

@tool
def retrieve_user_memory(user_id: str, query: str) -> str:
    """Retrieves relevant facts about the user."""
    # (Send requests.post to http://localhost:8000/retrieve)
    ...

llm = ChatOllama(model="qwen2.5:14b-instruct")
tools = [save_user_memory, retrieve_user_memory]
llm_with_tools = llm.bind_tools(tools)
```

---

## Configuration

You can customize the memory parameters using the following environment variables:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MEMORY_DB_PATH` | `memory.db` | Path to SQLite database |
| `LANCEDB_PATH` | `./lancedb_data` | Directory containing LanceDB indexes |
| `OLLAMA_MODEL` | `qwen2.5:14b-instruct` | LLM model name used for fact extraction |

---

## Performance Benchmarks

Tested locally using `qwen2.5:14b-instruct` on Ollama:

*   **Ingestion Latency**: **~1.43 ms** (queued asynchronously)
*   **Retrieval Latency**: **~13.47 ms** ( LanceDB semantic search + decay ranking)
*   **Storage Footprint**: SQLite DB: ~20 KB, LanceDB Vector Index: ~441 KB (with 53 active rows)
