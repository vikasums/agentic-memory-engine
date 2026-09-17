from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

from .models import Scope
from .engine import MemoryEngine, create_engine
from .pruner import MemoryPruner
from .metrics import logger

engine: Optional[MemoryEngine] = None
pruner: Optional[MemoryPruner] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, pruner
    logger.info("Initializing Memory Engine & Pruner...")
    engine = create_engine()
    pruner = MemoryPruner(engine)
    pruner.start()
    yield
    if pruner:
        pruner.stop()
    if engine:
        engine.close()

app = FastAPI(title="Agentic Memory Engine API", lifespan=lifespan)

class IngestRequest(BaseModel):
    user_id: str
    text: str
    scope: Scope = Scope.USER

class RetrieveRequest(BaseModel):
    user_id: str
    query: str
    top_k: int = 5
    half_life_days: float = 30.0
    scope: Scope = Scope.USER

class ProfileRequest(BaseModel):
    user_id: str
    recent_days: int = 7

@app.post("/ingest", status_code=202)
async def ingest(payload: IngestRequest, bg_tasks: BackgroundTasks):
    if not engine:
        raise HTTPException(500, "Engine uninitialized")
    bg_tasks.add_task(engine.process_paragraph_async, payload.text, payload.user_id, payload.scope)
    return {"status": "queued", "user_id": payload.user_id, "scope": payload.scope.value}

@app.post("/retrieve")
async def retrieve(payload: RetrieveRequest):
    if not engine:
        raise HTTPException(500, "Engine uninitialized")
    records = engine.retrieve_memories(
        query=payload.query,
        user_id=payload.user_id,
        top_k=payload.top_k,
        half_life_days=payload.half_life_days,
        scope=payload.scope
    )
    formatted_memories = [
        {
            "text": r.text,
            "score": round(r.score, 4),
            "similarity": round(r.similarity, 4),
            "decay_factor": round(r.decay_factor, 4),
            "source": r.source,
            "timestamp": r.timestamp,
            "scope": r.scope.value,
            "age_days": round(r.age_days, 1)
        }
        for r in records
    ]
    return {
        "user_id": payload.user_id,
        "query": payload.query,
        "memories": formatted_memories
    }

@app.post("/profile")
async def profile(payload: ProfileRequest):
    if not engine:
        raise HTTPException(500, "Engine uninitialized")
    # Try cached profile first
    cached = engine.get_user_profile(payload.user_id)
    if cached:
        return cached
    # Generate new profile
    profile_data = engine.generate_user_profile(payload.user_id, payload.recent_days)
    return profile_data

@app.get("/metrics")
async def metrics():
    if not engine:
        raise HTTPException(500, "Engine uninitialized")
    return engine.get_storage_footprint()

@app.post("/prune")
async def prune(inactive_days: float = 7.0, max_age_days: float = 180.0):
    if not pruner:
        raise HTTPException(500, "Pruner uninitialized")
    return pruner.prune_now(inactive_days, max_age_days)
