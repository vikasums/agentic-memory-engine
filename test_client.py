import time
import requests
from agentic_memory import MemoryInterceptor

BASE_URL = "http://localhost:8000"
USER_ID = "user_4920"

def run_test():
    print("1. Ingesting paragraph...")
    requests.post(f"{BASE_URL}/ingest", json={
        "user_id": USER_ID,
        "text": "User moved to Bangalore and works on Kubernetes infrastructure."
    })

    print("2. Waiting for Ollama SLM extraction (3s)...")
    time.sleep(3)

    print("3. Testing context auto-injection...")
    interceptor = MemoryInterceptor()
    input_messages = [{"role": "user", "content": "What is my location and focus area?"}]
    hydrated = interceptor.inject_context(input_messages, user_id=USER_ID)
    
    print("\n--- Hydrated Prompt Payload ---")
    for m in hydrated:
        print(f"[{m['role'].upper()}]: {m['content']}")

if __name__ == "__main__":
    run_test()