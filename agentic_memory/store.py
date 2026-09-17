import os
import sqlite3
import time
import threading
from typing import List, Optional, Protocol, Dict, Any
from .models import Scope, FactRecord, StoreFilter, ScoredMemory

class MemoryStore(Protocol):
    """Abstract protocol for memory persistence storage engines."""

    def upsert_fact(self, fact: FactRecord, user_id: str, scope: Scope) -> str:
        ...

    def deactivate_by_key(self, natural_key: str) -> Optional[str]:
        ...

    def search_vectors(self, vector: List[float], filter_params: StoreFilter, limit: int) -> List[ScoredMemory]:
        ...

    def delete_expired(self, inactive_cutoff: float, max_age_cutoff: float) -> int:
        ...

    def get_footprint(self) -> Dict[str, Any]:
        ...

    def close(self) -> None:
        ...


class SQLiteLanceDBStore:
    """Local storage engine using SQLite for metadata and LanceDB for vector search."""

    def __init__(self, db_path: str = "memory.db", lancedb_path: str = "./lancedb_data"):
        self.db_path = db_path
        self.lancedb_path = lancedb_path
        self._lock = threading.RLock()
        
        # Deferred import of optional lancedb & pyarrow dependencies
        try:
            import lancedb
            import pyarrow as pa
        except ImportError as e:
            raise ImportError(
                "lancedb and pyarrow are required for SQLiteLanceDBStore. "
                "Install them with `pip install agentic_memory[local]`"
            ) from e

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_sqlite()

        self.vector_db = lancedb.connect(self.lancedb_path)
        self.table_name = "memories"

        try:
            self.table = self.vector_db.open_table(self.table_name)
        except Exception:
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("user_id", pa.string()),
                pa.field("scope", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), 384)),
                pa.field("is_active", pa.bool_()),
                pa.field("timestamp", pa.float64())
            ])
            self.table = self.vector_db.create_table(self.table_name, schema=schema, exist_ok=True)

    def _init_sqlite(self):
        with self._lock:
            with self.conn:
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory_keys (
                        natural_key TEXT PRIMARY KEY,
                        memory_id TEXT,
                        user_id TEXT,
                        subject TEXT,
                        predicate TEXT,
                        object_value TEXT,
                        scope TEXT,
                        is_active INTEGER,
                        updated_at REAL,
                        expires_at REAL
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_profiles (
                        user_id TEXT PRIMARY KEY,
                        stable_facts TEXT,
                        recent_activity TEXT,
                        profile_timestamp REAL,
                        ttl_seconds REAL
                    )
                """)

    def deactivate_by_key(self, natural_key: str) -> Optional[str]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT memory_id FROM memory_keys WHERE natural_key = ? AND is_active = 1",
                (natural_key,)
            )
            existing = cursor.fetchone()
            if existing:
                old_id = existing[0]
                self.conn.execute(
                    "UPDATE memory_keys SET is_active = 0 WHERE natural_key = ?",
                    (natural_key,)
                )
                # Escape id for LanceDB query string
                safe_old_id = old_id.replace("'", "''")
                self.table.update(where=f"id = '{safe_old_id}'", values={"is_active": False})
                return old_id
            return None

    def upsert_fact(self, fact: FactRecord, user_id: str, scope: Scope) -> str:
        subj = fact.subject.lower().strip()
        pred = fact.predicate.lower().strip()
        obj = fact.object_value.strip()

        target_user = "GLOBAL" if scope == Scope.GLOBAL else user_id
        natural_key = f"{target_user}:{subj}:{pred}"
        memory_id = f"mem_{time.time_ns()}"
        fact_str = f"{subj} {pred} {obj}"
        now = time.time()

        with self._lock:
            self.deactivate_by_key(natural_key)
            with self.conn:
                self.conn.execute("""
                    INSERT OR REPLACE INTO memory_keys
                    (natural_key, memory_id, user_id, subject, predicate, object_value, scope, is_active, updated_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """, (natural_key, memory_id, target_user, subj, pred, obj, scope.value, now, fact.expires_at))

        return memory_id

    def add_vector_record(
        self,
        memory_id: str,
        user_id: str,
        scope: Scope,
        text: str,
        vector: List[float],
        timestamp: float
    ):
        target_user = "GLOBAL" if scope == Scope.GLOBAL else user_id
        with self._lock:
            self.table.add([{
                "id": memory_id,
                "user_id": target_user,
                "scope": scope.value,
                "text": text,
                "vector": vector,
                "is_active": True,
                "timestamp": timestamp
            }])

    def search_vectors(self, vector: List[float], filter_params: StoreFilter, limit: int) -> List[ScoredMemory]:
        # Escape user_id to prevent string injection into LanceDB SQL filter
        safe_user_id = filter_params.user_id.replace("'", "''")
        active_str = "true" if filter_params.is_active else "false"

        if filter_params.include_global:
            filter_expr = f"is_active = {active_str} AND (scope = 'global' OR user_id = '{safe_user_id}')"
        else:
            filter_expr = f"is_active = {active_str} AND user_id = '{safe_user_id}'"

        with self._lock:
            results_df = (
                self.table.search(vector)
                .where(filter_expr)
                .limit(limit)
                .to_pandas()
            )

        if results_df.empty:
            return []

        scored_memories = []
        for _, row in results_df.iterrows():
            distance = float(row['_distance']) if '_distance' in row else 0.0
            scored_memories.append(
                ScoredMemory(
                    id=str(row['id']),
                    user_id=str(row['user_id']),
                    scope=Scope(row['scope']) if row['scope'] in [s.value for s in Scope] else Scope.USER,
                    text=str(row['text']),
                    vector=list(row['vector']) if 'vector' in row and row['vector'] is not None else [],
                    is_active=bool(row['is_active']),
                    timestamp=float(row['timestamp']),
                    distance=distance
                )
            )
        return scored_memories

    def delete_expired(self, inactive_cutoff: float, max_age_cutoff: float) -> int:
        now = time.time()
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                DELETE FROM memory_keys
                WHERE (is_active = 0 AND updated_at < ?)
                   OR (updated_at < ?)
                   OR (expires_at IS NOT NULL AND expires_at < ?)
            """, (inactive_cutoff, max_age_cutoff, now))
            sql_deleted = cursor.rowcount

            delete_filter = (
                f"(is_active = false AND timestamp < {inactive_cutoff}) OR "
                f"(timestamp < {max_age_cutoff})"
            )
            self.table.delete(delete_filter)
            try:
                if hasattr(self.table, "optimize"):
                    self.table.optimize()
                else:
                    self.table.compact_files()
                    if hasattr(self.table, "cleanup_old_versions"):
                        self.table.cleanup_old_versions()
            except Exception:
                pass

        return sql_deleted

    def cache_user_profile(self, user_id: str, stable_facts: List[str], recent_activity: List[str], ttl_seconds: float = 3600.0) -> None:
        import json
        now = time.time()
        with self._lock:
            with self.conn:
                self.conn.execute("""
                    INSERT OR REPLACE INTO user_profiles
                    (user_id, stable_facts, recent_activity, profile_timestamp, ttl_seconds)
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, json.dumps(stable_facts), json.dumps(recent_activity), now, ttl_seconds))

    def get_cached_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        import json
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT stable_facts, recent_activity, profile_timestamp, ttl_seconds
                FROM user_profiles WHERE user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            if not row:
                return None

            stable_facts, recent_activity, profile_ts, ttl = row
            now = time.time()
            if now - profile_ts > ttl:
                cursor.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
                return None

            return {
                "user_id": user_id,
                "stable_facts": json.loads(stable_facts),
                "recent_activity": json.loads(recent_activity),
                "profile_timestamp": profile_ts
            }

    def resolve_contradiction(self, natural_key: str, old_value: str, new_value: str) -> str:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT memory_id, subject, predicate, user_id, scope FROM memory_keys
                WHERE natural_key = ? AND is_active = 1
            """, (natural_key,))
            existing = cursor.fetchone()
            if existing:
                old_id, subject, predicate, user_id, scope = existing
                # Deactivate old memory
                self.conn.execute(
                    "UPDATE memory_keys SET is_active = 0 WHERE natural_key = ?",
                    (natural_key,)
                )
                safe_old_id = old_id.replace("'", "''")
                self.table.update(where=f"id = '{safe_old_id}'", values={"is_active": False})

                # Create new memory with resolved value
                new_memory_id = f"mem_{time.time_ns()}"
                self.conn.execute("""
                    INSERT INTO memory_keys
                    (natural_key, memory_id, user_id, subject, predicate, object_value, scope, is_active, updated_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, NULL)
                """, (natural_key, new_memory_id, user_id, subject, predicate, new_value, scope, time.time()))

                return f"Resolved: '{old_value}' → '{new_value}' (new memory: {new_memory_id})"
            return f"No active memory found for {natural_key}"

    def get_footprint(self) -> Dict[str, Any]:
        with self._lock:
            sqlite_bytes = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
            lancedb_bytes = sum(
                os.path.getsize(os.path.join(dirpath, filename))
                for dirpath, _, filenames in os.walk(self.lancedb_path)
                for filename in filenames
            ) if os.path.exists(self.lancedb_path) else 0

            cursor = self.conn.cursor()
            cursor.execute("SELECT is_active, COUNT(*) FROM memory_keys GROUP BY is_active")
            counts = dict(cursor.fetchall())

            return {
                "sqlite_file_size_kb": round(sqlite_bytes / 1024, 2),
                "lancedb_folder_size_kb": round(lancedb_bytes / 1024, 2),
                "active_memories": counts.get(1, 0),
                "inactive_memories": counts.get(0, 0),
                "lancedb_total_rows": self.table.count_rows()
            }

    def close(self) -> None:
        with self._lock:
            if self.conn:
                self.conn.close()


class MariaDBStore:
    """MariaDB storage engine for shared/distributed container environments."""

    def __init__(self, connection_url: str):
        self.connection_url = connection_url
        # Connection pooling and initialization logic for MariaDB
        try:
            import pymysql
        except ImportError:
            raise ImportError(
                "pymysql (or aiomysql) is required for MariaDBStore. "
                "Install it with `pip install agentic_memory[mariadb]`"
            )
        # Placeholder for full MariaDB implementation
        raise NotImplementedError("MariaDB storage backend initialization requiring connection parameters.")
