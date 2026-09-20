#!/usr/bin/env python3
"""Start Dashboard API server on port 8001."""
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from simulation import dashboard_app
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        dashboard_app,
        host="0.0.0.0",
        port=8001,
        log_level="info"
    )
