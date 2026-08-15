import json
import logging
import time
from functools import wraps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("MemoryEngine")

def time_operation(metric_name: str):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                duration_ms = (time.perf_counter() - start) * 1000.0
                logger.info(json.dumps({
                    "metric": metric_name,
                    "status": "success",
                    "latency_ms": round(duration_ms, 2)
                }))
                return result
            except Exception as e:
                duration_ms = (time.perf_counter() - start) * 1000.0
                logger.error(json.dumps({
                    "metric": metric_name,
                    "status": "error",
                    "error": str(e),
                    "latency_ms": round(duration_ms, 2)
                }))
                raise
        return wrapper
    return decorator
