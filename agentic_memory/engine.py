import asyncio
import json
import os
import sqlite3
import time
from typing import List, Literal
import lancedb
import numpy as np
import ollama
import pandas as pd
from fastembed import TextEmbedding
from pydantic import BaseModel

import pyarrow as pa

from . import config
from .metrics import logger, time_operation

class MemoryEngine:
    def __init__(self, db_path=config.DB_PATH, lancedb_path=config.LANCEDB_PATH):
        self.db_path = db_path
        self.lancedb_path = lancedb_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_sqlite()
        
        self.vector_db = lancedb.connect(self.lancedb_path)
        self.embed_model = TextEmbedding(config.EMBEDDING_MODEL)
        self.table_name = "memories"
        
        if self.table_name not in self.vector_db.table_names():
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("user_id", pa.string()),
                pa.field("scope", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), 384)),
                pa.field("is_active", pa.bool_()),
                pa.field("timestamp", pa.float64())
            ])
            self.table = self.vector_db.create_table(
                self.table_name,
                schema=schema
            )
        else:
            self.table = self.vector_db.open_table(self.table_name)

    def _init_sqlite(self):
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
                    updated_at REAL
                )
            """)

    async def process_paragraph_async(self, text: str, user_id: str):
        prompt = f"""You are an expert system designed to extract clear, atomic facts from user input.
Identify any facts about the user's name, profession, hobbies, interests, location, preferences, or other personal details.
For each fact, extract:
- subject: Who or what the fact is about (typically 'user')
- predicate: The relationship (e.g. 'works_as', 'enjoys', 'specializes_in', 'lives_in', 'name_is')
- object_value: The detail or value (e.g. 'Software Engineer', 'tennis', 'Generative AI', 'Vikas')
- scope: 'user' (default) or 'global'
- confidence: A confidence score between 0.0 and 1.0

Return ONLY JSON matching this format:
{{
  "facts": [
    {{"subject": "user", "predicate": "lives_in", "object_value": "Seattle", "scope": "user", "confidence": 0.95}}
  ]
}}
Text: {text}"""

        response = await asyncio.to_thread(
            ollama.chat,
            model=config.OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            format="json"
        )
        
        data = json.loads(response['message']['content'])
        facts = data.get("facts", [])
        
        for fact in facts:
            self._upsert_fact(fact, user_id)

    def _upsert_fact(self, fact: dict, user_id: str):
        scope = fact.get("scope", "user")
        subj = fact.get("subject", "").lower()
        pred = fact.get("predicate", "").lower()
        obj = fact.get("object_value", "")
        
        target_user = "GLOBAL" if scope == "global" else user_id
        natural_key = f"{target_user}:{subj}:{pred}"
        memory_id = f"mem_{time.time_ns()}"
        fact_str = f"{subj} {pred} {obj}"
        
        cursor = self.conn.cursor()
        cursor.execute("SELECT memory_id FROM memory_keys WHERE natural_key = ? AND is_active = 1", (natural_key,))
        existing = cursor.fetchone()
        
        if existing:
            old_id = existing[0]
            self.conn.execute("UPDATE memory_keys SET is_active = 0 WHERE natural_key = ?", (natural_key,))
            self.table.update(where=f"id = '{old_id}'", values={"is_active": False})
            
        now = time.time()
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO memory_keys 
                (natural_key, memory_id, user_id, subject, predicate, object_value, scope, is_active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """, (natural_key, memory_id, target_user, subj, pred, obj, scope, now))
            
        vector = list(self.embed_model.embed([fact_str]))[0]
        self.table.add([{
            "id": memory_id,
            "user_id": target_user,
            "scope": scope,
            "text": fact_str,
            "vector": vector,
            "is_active": True,
            "timestamp": now
        }])

    @time_operation("retrieve_memories")
    def retrieve_memories(
        self, 
        query: str, 
        user_id: str, 
        top_k: int = 5,
        half_life_days: float = config.DEFAULT_HALF_LIFE_DAYS
    ) -> List[str]:
        query_vector = list(self.embed_model.embed([query]))[0]
        filter_expr = f"is_active = true AND (scope = 'global' OR user_id = '{user_id}')"
        
        fetch_limit = top_k * config.CANDIDATE_OVERFETCH_FACTOR
        results_df = (
            self.table.search(query_vector)
            .where(filter_expr)
            .limit(fetch_limit)
            .to_pandas()
        )
        
        if results_df.empty:
            return []

        if '_distance' in results_df.columns:
            results_df['similarity'] = 1.0 - results_df['_distance'].clip(lower=0.0, upper=1.0)
        else:
            results_df['similarity'] = 1.0

        now = time.time()
        lambda_decay = np.log(2) / half_life_days
        results_df['age_days'] = (now - results_df['timestamp']) / 86400.0
        results_df['decay_factor'] = np.exp(-lambda_decay * results_df['age_days'])
        results_df['final_score'] = results_df['similarity'] * results_df['decay_factor']
        
        ranked_df = results_df.sort_values(by='final_score', ascending=False).head(top_k)
        return ranked_df['text'].tolist()

    def get_storage_footprint(self) -> dict:
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
