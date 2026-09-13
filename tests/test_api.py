import pytest
from fastapi.testclient import TestClient
from agentic_memory.main_api import app

def test_api_endpoints():
    with TestClient(app) as client:
        # Test metrics endpoint
        response = client.get("/metrics")
        assert response.status_code == 200
        metrics = response.json()
        assert "active_memories" in metrics
        assert "sqlite_file_size_kb" in metrics

        # Test prune endpoint
        response = client.post("/prune?inactive_days=7.0&max_age_days=180.0")
        assert response.status_code == 200
        prune_res = response.json()
        assert "sqlite_rows_deleted" in prune_res
