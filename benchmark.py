import time
import requests
import statistics
import numpy as np

BASE_URL = "http://localhost:8000"
USER_ID = "benchmark_user"

# Sample facts to ingest
paragraphs = [
    "User loves drinking fresh espresso in the morning.",
    "User works as a Principal Infrastructure Architect at a cloud tech firm.",
    "User prefers using macOS over Linux for local development.",
    "User has a pet golden retriever dog named Buddy.",
    "User enjoys hiking in Yosemite National Park during summer seasons.",
    "User is allergic to peanuts and sesame seeds.",
    "User plays the acoustic guitar in a local hobby band.",
    "User lives in Seattle, Washington.",
    "User recently started learning Rust programming language.",
    "User dislikes cold winter weather and prefers tropical climates."
]

# Sample queries to test retrieval
queries = [
    "What is my preferred coffee drink?",
    "Where do I work and what is my role?",
    "Which operating system do I use for coding?",
    "What is my pet's name and breed?",
    "Where do I like to go hiking?",
    "Do I have any allergies?",
    "Do I play any musical instruments?",
    "Where do I live?",
    "What programming languages am I learning?",
    "What kind of weather do I dislike?"
]

def run_ingestion_benchmark():
    print(f"=== Running Ingestion Benchmark (10 paragraphs, sequential) ===")
    latencies = []
    
    for i, text in enumerate(paragraphs, start=1):
        start_time = time.perf_counter()
        response = requests.post(
            f"{BASE_URL}/ingest",
            json={"user_id": f"{USER_ID}_{i}", "text": text}
        )
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        
        if response.status_code == 202:
            latencies.append(duration_ms)
            print(f"  Ingest {i}/10: {duration_ms:.2f} ms")
        else:
            print(f"  Ingest {i}/10 Failed: Status {response.status_code}")
            
    return latencies

def run_retrieval_benchmark():
    print(f"\n=== Running Retrieval Benchmark (50 queries) ===")
    latencies = []
    
    # We will query with multiple parameters to get a good statistical spread
    for i in range(50):
        query = queries[i % len(queries)]
        user_idx = (i % 10) + 1
        
        start_time = time.perf_counter()
        response = requests.post(
            f"{BASE_URL}/retrieve",
            json={"user_id": f"{USER_ID}_{user_idx}", "query": query, "top_k": 3}
        )
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        
        if response.status_code == 200:
            latencies.append(duration_ms)
        else:
            print(f"  Query {i+1} Failed: Status {response.status_code}")
            
    print(f"  Completed 50 queries.")
    return latencies

def print_stats(name: str, latencies: list):
    if not latencies:
        print(f"No successful operations for {name}")
        return
    print(f"\nStats for {name}:")
    print(f"  Count:  {len(latencies)}")
    print(f"  Min:    {min(latencies):.2f} ms")
    print(f"  Max:    {max(latencies):.2f} ms")
    print(f"  Mean:   {statistics.mean(latencies):.2f} ms")
    print(f"  Median: {statistics.median(latencies):.2f} ms")
    print(f"  p95:    {np.percentile(latencies, 95):.2f} ms")
    print(f"  p99:    {np.percentile(latencies, 99):.2f} ms")

if __name__ == "__main__":
    print("Initializing Benchmark Suite...")
    
    # Check if server is running
    try:
        requests.get(f"{BASE_URL}/metrics")
    except Exception:
        print(f"Error: API Server is not running on {BASE_URL}. Start it first!")
        exit(1)
        
    ingest_latencies = run_ingestion_benchmark()
    
    print("\nSleeping 5s to allow background Ollama extractions to finish...")
    time.sleep(5)
    
    retrieve_latencies = run_retrieval_benchmark()
    
    print_stats("POST /ingest (Network Roundtrip to Queue)", ingest_latencies)
    print_stats("POST /retrieve (LanceDB Vector Search + Decay Ranking)", retrieve_latencies)
    
    # Get storage footprint
    footprint = requests.get(f"{BASE_URL}/metrics").json()
    print("\n=== Storage Footprint ===")
    for k, v in footprint.items():
        print(f"  {k}: {v}")
