from contextlib import asynccontextmanager
from typing import List, Optional
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from .engine import MemoryEngine
from .pruner import MemoryPruner
from .metrics import logger

engine: Optional[MemoryEngine] = None
pruner: Optional[MemoryPruner] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, pruner
    logger.info("Initializing Memory Engine & Pruner...")
    engine = MemoryEngine()
    pruner = MemoryPruner(engine)
    pruner.start()
    yield
    if pruner:
        pruner.stop()

app = FastAPI(title="Agentic Memory Engine API", lifespan=lifespan)

class IngestRequest(BaseModel):
    user_id: str
    text: str

class RetrieveRequest(BaseModel):
    user_id: str
    query: str
    top_k: int = 5
    half_life_days: float = 30.0

@app.post("/ingest", status_code=202)
async def ingest(payload: IngestRequest, bg_tasks: BackgroundTasks):
    if not engine:
        raise HTTPException(500, "Engine uninitialized")
    bg_tasks.add_task(engine.process_paragraph_async, payload.text, payload.user_id)
    return {"status": "queued", "user_id": payload.user_id}

@app.post("/retrieve")
async def retrieve(payload: RetrieveRequest):
    if not engine:
        raise HTTPException(500, "Engine uninitialized")
    memories = engine.retrieve_memories(
        query=payload.query,
        user_id=payload.user_id,
        top_k=payload.top_k,
        half_life_days=payload.half_life_days
    )
    return {"user_id": payload.user_id, "query": payload.query, "memories": memories}

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
