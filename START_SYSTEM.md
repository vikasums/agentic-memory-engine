# Start Full Simulation System

**Prerequisites:** Python 3.9+, Node.js 16+, npm

## Quick Start (3 Terminals)

### Terminal 1: Memory Engine API (Port 8000)
```bash
cd /Users/vikasanand/genAI/memory-framework/agentic_memory_engine
python3 -m uvicorn agentic_memory.main_api:app --host 0.0.0.0 --port 8000 --reload
```

Expected output:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Initializing Memory Engine & Pruner...
```

**Test:** `curl http://localhost:8000/docs` (Swagger UI)

---

### Terminal 2: Dashboard API (Port 8001)
```bash
cd /Users/vikasanand/genAI/memory-framework/agentic_memory_engine
PYTHONPATH=./src python3 -c "from simulation import dashboard_app; import uvicorn; uvicorn.run(dashboard_app, host='0.0.0.0', port=8001, log_level='info')"
```

Or use the startup script:
```bash
python3 run_dashboard.py
```

Expected output:
```
INFO:     Uvicorn running on http://0.0.0.0:8001
INFO:     Application startup complete
```

**Test:** `curl http://localhost:8001/health`

---

### Terminal 3: React Frontend (Port 3000)
```bash
cd /Users/vikasanand/genAI/memory-framework/agentic_memory_engine/frontend
npm run dev
```

Expected output:
```
  VITE v5.x.x  ready in XXX ms

  ➜  Local:   http://localhost:3000/
```

**Test:** Open http://localhost:3000 in browser

---

## Verification Checklist

**All 3 running?**
- [ ] Terminal 1: Memory Engine API on :8000
- [ ] Terminal 2: Dashboard API on :8001
- [ ] Terminal 3: React Frontend on :3000

**Browser check:**
1. Open http://localhost:3000
2. Should see Dashboard UI with 5 panels
3. Click "Start" button
4. Select 1-minute duration
5. Simulation should start (progress bar updates)
6. After ~1 second, run completes
7. View results in all 5 panels

**If any issues:**
- Check Terminal output for errors
- Verify ports are free: `lsof -i :8000` / `:8001` / `:3000`
- Kill if needed: `kill -9 <PID>`
- Restart service

---

## System Architecture

```
localhost:3000 (React)
    ↓ fetch /api/*
localhost:8001 (Dashboard API)
    ↓ EngineClient calls
localhost:8000 (Memory Engine API)
    ↓ memory.db / LanceDB
    └→ audit_log.db (persisted audit trail)
```

## Common Issues

| Issue | Fix |
|-------|-----|
| Port already in use | Kill process: `lsof -i :8001 \| grep LISTEN \| awk '{print $2}' \| xargs kill -9` |
| Module not found | Set `PYTHONPATH=./src` before running Dashboard API |
| CORS errors | Dashboard API has CORS enabled for localhost:3000 |
| React blank page | Check browser console (F12) for fetch errors to :8001 |

---

## Next: Manual Verification

Once all 3 are running:
1. Dashboard UI should load at http://localhost:3000
2. Click "Start Simulation"
3. Select duration (1 min recommended for testing)
4. Watch progress bar and panels update in real-time
5. After completion, view results across 5 tabs
