import os

# DB_PATH: Local SQLite path for metadata, facts and transaction logs.
DB_PATH = os.getenv("MEMORY_DB_PATH", "memory.db")

# LANCEDB_PATH: Directory path containing the LanceDB vector table.
LANCEDB_PATH = os.getenv("LANCEDB_PATH", "./lancedb_data")

# OLLAMA_MODEL: Local model used for extracting structured facts from text inputs.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b-instruct")

# EMBEDDING_MODEL: Local embedding model from FastEmbed library.
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# DEFAULT_HALF_LIFE_DAYS: Time-decay half-life used to penalize old memories.
DEFAULT_HALF_LIFE_DAYS = 30.0

# CANDIDATE_OVERFETCH_FACTOR: Multiplier for initial vector scan count to account for decay ranking.
CANDIDATE_OVERFETCH_FACTOR = 3

# INACTIVE_RETENTION_DAYS: Days to retain deactivated memory records before pruning.
INACTIVE_RETENTION_DAYS = 7.0

# MAX_MEMORY_AGE_DAYS: Upper limit of memory age; older memories are automatically pruned.
MAX_MEMORY_AGE_DAYS = 180.0

# PRUNE_INTERVAL_HOURS: Frequency of the background database pruner execution.
PRUNE_INTERVAL_HOURS = 24.0

