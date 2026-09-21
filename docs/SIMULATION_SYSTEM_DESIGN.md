# Agentic Memory Engine — Simulation & Monitoring System Design

**Date:** 2026-09-19  
**Status:** Design Phase (Pre-Implementation)  
**Phase:** 1 (Deterministic Scenarios)  
**Approved Architecture:** Modular Service Architecture (Single Server)

---

## 1. Requirements Summary

### 1.1 Simulation Scope
- **Type A:** Realistic user behavior (multiple simulated users, natural conversations, organic fact corrections)
- **Type B:** Scenario-based test cases (predetermined test scenarios for specific validation)
- **Phase 2 (Future):** Stress/load testing, edge cases, Claude API integration

### 1.2 User Behavior Specification
- **Number of users:** 5 simulated users
- **Fact types:** Mix of generic ("I like pizza", "I code in Python") + domain-specific facts
- **Conversation duration:** Progressive validation
  - Run 1: 1 minute (end-to-end check)
  - Run 2: 2 minutes
  - Run 3: 4 minutes
  - Run 4: 6 minutes
- **Test scenarios:** 50+ predetermined scenarios, randomly distributed across users/times
- **Scenario playback:** Deterministic (no LLM calls in Phase 1)

### 1.3 Monitoring & Observability
- **Full dashboard:** Real-time simulation state + audit log + live validation
- **Audit trail:** When each fact ingested, who said it, when modified/deactivated, TTL status, change history
- **Verification state:** Real-time checks (✓/✗) for expected outcomes
- **Auto-validation:** Test cases auto-verify monitoring data matches actual state
- **Cross-validation:** All verification methods (DB, API, Profile API, Audit) must agree

### 1.4 Success Criteria
- **(G) Audit completeness:** Every fact event logged with timestamp + user + before/after state, full audit trail recoverable
- **(H) User isolation:** User A's facts never leak to User B, even with same fact subjects
- **Data persistence:** Data persists across runs, tagged with run_number for traceability
- **Manual verification:** Historical data retained for future auditing

### 1.5 Tech Stack
- **Deployment:** Single server, modular Python backend
- **Frontend:** React-based web UI (real-time dashboard)
- **Database:**
  - `memory.db` (SQLite) — Existing memory engine storage
  - `audit_log.db` (SQLite) — New audit trail (separate DB)
- **Data flow:** Deterministic scenario playback (Phase 1)

---

## 2. Approach: Modular Service Architecture

**Rationale:** Single server but modular design allows each service to have one responsibility, enables independent testing, and extensible to Phase 2 (Claude API, stress testing).

```
┌─────────────────────────────────────────────────────────────┐
│                    SIMULATION SYSTEM                         │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌─────────────────┐  ┌─────────────────┐                   │
│  │  React Frontend │  │   REST API      │                   │
│  │   (Dashboard)   │  │  (Monitoring)   │                   │
│  └────────┬────────┘  └────────┬────────┘                   │
│           │                    │                             │
│  ┌────────┴────────────────────┴─────────┐                  │
│  │   Simulation Backend (Python)          │                  │
│  │                                         │                  │
│  ├──────────────────────────────────────┤                  │
│  │ 1. ScenarioGenerator                  │                  │
│  │    - 50+ fact scenarios               │                  │
│  │    - Random distribution              │                  │
│  │    - Run tagging                      │                  │
│  ├──────────────────────────────────────┤                  │
│  │ 2. SimulationRunner                   │                  │
│  │    - Orchestrates scenario playback   │                  │
│  │    - Timer-based execution (1/2/4/6m) │                  │
│  │    - Progress tracking                │                  │
│  ├──────────────────────────────────────┤                  │
│  │ 3. EngineClient                       │                  │
│  │    - Calls memory engine API          │                  │
│  │    - /ingest, /retrieve, /profile     │                  │
│  ├──────────────────────────────────────┤                  │
│  │ 4. MonitoringService                  │                  │
│  │    - Tracks state changes             │                  │
│  │    - Real-time metrics                │                  │
│  │    - Cache hits/misses                │                  │
│  ├──────────────────────────────────────┤                  │
│  │ 5. AuditLogger                        │                  │
│  │    - Writes to audit_log.db           │                  │
│  │    - Event: fact_id, user, action,    │                  │
│  │      before_state, after_state,       │                  │
│  │      timestamp, run_number            │                  │
│  ├──────────────────────────────────────┤                  │
│  │ 6. ValidatorService                   │                  │
│  │    - Auto-check monitoring            │                  │
│  │    - Cross-validate methods           │                  │
│  │    - Pass/fail criteria               │                  │
│  │                                        │                  │
│  └──────────────────────────────────────┘                  │
│           │                 │                │               │
└───────────┼─────────────────┼────────────────┼───────────────┘
            │                 │                │
    ┌───────▼───┐    ┌────────▼────────┐   ┌──▼─────────────┐
    │ memory.db │    │  audit_log.db   │   │ Existing       │
    │ (Engine)  │    │  (Audit Trail)  │   │ Memory Engine  │
    └───────────┘    └─────────────────┘   │ API (FastAPI)  │
                                            └────────────────┘
```

---

## 3. Component Specifications

### 3.1 ScenarioGenerator
**Purpose:** Generate 50+ deterministic test scenarios

**Scenarios types:**
- **Contradiction scenarios** (10): User says X, then contradicts with Y
  - "I live in NYC" → "Actually moved to SF"
  - "I code in Python" → "Now using Go"
- **Expiry scenarios** (8): Time-sensitive facts
  - "Meeting tomorrow at 2pm" (expires 1 day)
  - "Exam next Friday" (expires at deadline)
- **Profile cache scenarios** (6): Cache hit/miss/TTL
  - "Generate profile" → "Retrieve immediately" (cache hit)
  - "Generate profile" → "Wait TTL" → "Retrieve" (cache miss)
- **Multi-user contradiction scenarios** (10): Different users saying different things about same fact
  - User A: "Team lead is John"
  - User B: "Team lead is Sarah"
- **Generic fact accumulation** (10): Normal conversation facts
  - "I like coffee", "I work on Kubernetes", etc.
- **Fact modification scenarios** (6): Same fact updated multiple times
  - "I have 5 years exp" → "7 years exp" → "10 years exp"

**Output:** Scenario list with:
```json
{
  "scenario_id": "contradiction_001",
  "user_id": "user_2",
  "facts": [
    {
      "timestamp": 15,
      "text": "I live in New York",
      "type": "primary_fact"
    },
    {
      "timestamp": 45,
      "text": "Actually I moved to San Francisco",
      "type": "contradiction",
      "contradicts_scenario": "contradiction_001",
      "contradicts_fact_id": "fact_xyz"
    }
  ],
  "expected_outcomes": [
    "old_fact_deactivated",
    "new_fact_active",
    "audit_trail_complete"
  ]
}
```

**Random distribution:** Scenarios randomly assigned to users and times within 0-T seconds of simulation run.

### 3.2 SimulationRunner
**Purpose:** Orchestrate scenario playback for 1/2/4/6 minute runs

**Process:**
1. Load scenario set for this run
2. Randomly distribute scenarios across 5 users
3. For each scenario fact:
   - Wait until scheduled_time
   - Call EngineClient.ingest()
   - Record MonitoringService metrics
   - Log to AuditLogger
4. After run completes:
   - Call ValidatorService.validate()
   - Generate run report
   - Tag all data with run_number

**Timing:** Use event queue with timestamps; clock advances as scenarios process.

### 3.3 EngineClient
**Purpose:** Call memory engine API endpoints

**Interface:**
```python
class EngineClient:
    async def ingest(user_id, text, scope='user') -> IngestionResult
    async def retrieve(user_id, query, top_k=5) -> RetrievalResult
    async def get_profile(user_id, recent_days=7) -> ProfileResult
    async def get_metrics() -> StorageMetrics
```

**Result tracking:** Every call logged with:
- Timestamp
- User ID
- Request parameters
- Response (facts returned, scores, etc.)
- Latency

### 3.4 MonitoringService
**Purpose:** Track real-time state changes during simulation

**Tracks:**
- Fact ingestion events
- Contradiction detection (old fact deactivated, new one active)
- Cache hits/misses (via profile API calls)
- TTL/expiry status
- Memory counts (active/inactive/total)
- API latencies
- User isolation (no cross-user leaks)

**Data structure:**
```python
@dataclass
class MonitoringEvent:
    timestamp: float
    run_number: int
    event_type: str  # "fact_ingested", "contradiction_resolved", "cache_hit", "expiry_fired"
    user_id: str
    fact_id: str
    old_state: dict  # For updates
    new_state: dict
    metadata: dict  # Cache info, TTL remaining, etc.
```

### 3.5 AuditLogger
**Purpose:** Persistent audit trail in `audit_log.db`

**Schema:**
```sql
CREATE TABLE audit_events (
    event_id INTEGER PRIMARY KEY,
    run_number INTEGER,
    timestamp REAL,
    user_id TEXT,
    fact_id TEXT,
    event_type TEXT,  -- "created", "updated", "deactivated", "expired"
    before_state TEXT,  -- JSON
    after_state TEXT,   -- JSON
    source TEXT,  -- "scenario_playback", "user_interaction", "ttl_pruning"
    created_at REAL
);

CREATE TABLE run_metadata (
    run_number INTEGER PRIMARY KEY,
    start_time REAL,
    end_time REAL,
    duration_seconds REAL,
    user_count INTEGER,
    scenario_count INTEGER,
    status TEXT  -- "in_progress", "completed", "failed"
);
```

**Guarantees:**
- Every fact event (create/update/deactivate/expire) logged
- Before/after states recorded (for full audit trail)
- Run metadata tracked for traceability
- Data never deleted (only archived if needed)

### 3.6 ValidatorService
**Purpose:** Auto-verify monitoring data matches actual state

**Validation methods (must cross-validate):**

**Method 1: Direct DB Query**
```python
def validate_via_db(fact_id, user_id):
    cursor = memory_db.cursor()
    result = cursor.execute(
        "SELECT * FROM memory_keys WHERE memory_id = ? AND user_id = ?",
        (fact_id, user_id)
    )
    return result.fetchone()  # Returns memory record if active
```

**Method 2: API Retrieval**
```python
def validate_via_api(user_id, query_text):
    results = engine_client.retrieve(user_id, query_text, top_k=10)
    # Verify expected fact in results
    return any(r.source == fact_id for r in results)
```

**Method 3: Audit Trail Query**
```python
def validate_via_audit(fact_id, user_id):
    cursor = audit_db.cursor()
    events = cursor.execute(
        "SELECT * FROM audit_events WHERE fact_id = ? AND user_id = ? ORDER BY timestamp",
        (fact_id, user_id)
    )
    # Reconstruct fact state from event history
    return reconstruct_state_from_events(events)
```

**Method 4: Profile API**
```python
def validate_via_profile(user_id):
    profile = engine_client.get_profile(user_id)
    return profile.stable_facts, profile.recent_activity
```

**Cross-validation checks:**
```python
def cross_validate(fact_id, user_id):
    db_state = validate_via_db(fact_id, user_id)
    api_state = validate_via_api(user_id, fact_text)
    audit_state = validate_via_audit(fact_id, user_id)
    profile_state = validate_via_profile(user_id)
    
    # All must agree
    assert db_state == api_state == audit_state == profile_state, \
        "Validation methods disagree!"
    
    return True
```

**Test case auto-checks:**
```python
class TestCase:
    scenario_id: str
    expected_outcomes: List[str]  # ["old_deactivated", "new_active", "audit_logged", ...]
    
    def verify(self):
        for outcome in self.expected_outcomes:
            if outcome == "old_deactivated":
                assert not validate_via_db(old_fact_id)
            elif outcome == "new_active":
                assert validate_via_db(new_fact_id)
            elif outcome == "audit_logged":
                assert event_exists_in_audit(fact_id)
            # ... more checks
```

**Pass/fail criteria:**
- ✅ PASS: All cross-validations succeed, all test case checks pass
- ❌ FAIL: Any validation method disagrees, any test case fails

### 3.7 React Dashboard
**Purpose:** Real-time visualization of simulation progress

**Components:**

1. **Simulation Control Panel**
   - Start/stop/pause buttons
   - Run duration selector (1/2/4/6 min)
   - Progress bar (% complete)
   - Elapsed time display

2. **User State Panel**
   - 5 tabs (one per user)
   - Current facts (with timestamps)
   - Recent contradictions (old → new)
   - Memory count (active/inactive)
   - Profile cache status (hit/miss/TTL remaining)

3. **Audit Log Viewer**
   - Real-time event stream
   - Filters: by user, event type, fact_id, timestamp range
   - Each event shows: timestamp, user, action, before→after state

4. **Validation Dashboard**
   - Live test case results (✓/✗)
   - Cross-validation status (all methods agree Y/N)
   - Pass/fail summary
   - Error details if validation fails

5. **Metrics Display**
   - Cache hit rate
   - Average retrieval latency
   - API call count
   - Facts created/updated/deactivated/expired counts
   - Storage size (memory.db, audit_log.db)

**Data flow:** Dashboard polls Monitoring REST API every 500ms for updates.

---

## 4. Data Flow: Scenario Playback to Validation

```
Scenario Fact (t=15s, user_2, "I live in NYC")
        │
        ▼
SimulationRunner waits until t=15s
        │
        ▼
EngineClient.ingest(user_id="user_2", text="I live in NYC")
        │
        ▼
Memory Engine API POST /ingest
        │
        ├─► memory.db: INSERT INTO memory_keys (natural_key, memory_id, object_value, ...)
        │
        ├─► LanceDB: INSERT INTO memories (id, user_id, text, vector, timestamp, is_active)
        │
        ├─► Engine returns: MemoryID = "mem_xyz"
        │
        ▼
MonitoringService.log_event("fact_ingested", user_id="user_2", fact_id="mem_xyz", ...)
        │
        ▼
AuditLogger.write(
    run_number=1,
    timestamp=T.15s,
    user_id="user_2",
    fact_id="mem_xyz",
    event_type="created",
    before_state=null,
    after_state={"subject": "user", "predicate": "location", "object": "NYC", ...}
)
        │
        ▼
Dashboard updates: user_2 facts panel, audit log stream

===== Later: Contradiction (t=45s) =====

Scenario Fact (t=45s, user_2, "I moved to San Francisco")
        │
        ▼
EngineClient.ingest(user_id="user_2", text="I moved to San Francisco")
        │
        ▼
Memory Engine: deactivate old "NYC" fact, insert new "SF" fact
        │
        ├─► memory.db: UPDATE memory_keys SET is_active=0 WHERE natural_key="user_2:user:location"
        ├─► memory.db: INSERT OR REPLACE INTO memory_keys (natural_key, memory_id="mem_abc", object_value="SF", is_active=1)
        │
        ├─► LanceDB: UPDATE (id=mem_xyz, is_active=False)
        ├─► LanceDB: INSERT (id=mem_abc, text="user location San Francisco", is_active=True)
        │
        ▼
MonitoringService.log_event("contradiction_resolved", user_id="user_2", old_fact_id="mem_xyz", new_fact_id="mem_abc", ...)
        │
        ▼
AuditLogger.write TWO events:
    Event 1: event_type="deactivated", fact_id="mem_xyz", after_state={..., is_active: false}
    Event 2: event_type="created", fact_id="mem_abc", after_state={..., object: "SF", is_active: true}
        │
        ▼
Dashboard updates: user_2 facts panel shows "NYC" crossed out, "SF" active; audit log shows contradiction

===== After Run Completes (t=T_end) =====

ValidatorService.run_full_validation():
    For each fact in scenario:
        ├─ validate_via_db(): Query memory.db
        ├─ validate_via_api(): Call /retrieve
        ├─ validate_via_audit(): Query audit_log.db
        ├─ validate_via_profile(): Call /profile
        └─ cross_validate(): Assert all agree

    For each test case:
        └─ Run expected_outcomes checks

Generate Report:
    ├─ Summary: X scenarios, Y contradictions, Z expirations
    ├─ Validation: PASS/FAIL + details
    ├─ Audit trail: Download full event log
    └─ Data tag: run_number=1, timestamp=..., status=completed
```

---

## 5. Test Scenarios (50+)

### 5.1 Scenario Categories

**A. Contradiction Scenarios (10)**
1. `contradiction_001`: Location change (NYC → SF)
2. `contradiction_002`: Programming language (Python → Go)
3. `contradiction_003`: Job role (Engineer → Manager)
4. `contradiction_004`: Company (Google → Apple)
5. `contradiction_005`: Expertise level (Junior → Senior)
6. `contradiction_006`: Project status (Active → Completed)
7. `contradiction_007`: Experience years (5 → 10)
8. `contradiction_008`: Team assignment (Team A → Team B)
9. `contradiction_009`: Certification (None → AWS Certified)
10. `contradiction_010`: Skill proficiency (Intermediate → Expert)

**B. Expiry Scenarios (8)**
1. `expiry_001`: Meeting tomorrow (TTL: 1 day)
2. `expiry_002`: Exam next Friday (TTL: 1 week)
3. `expiry_003`: Temporary task (TTL: 1 hour)
4. `expiry_004`: Sprint deadline (TTL: 2 weeks)
5. `expiry_005`: Short-term goal (TTL: 30 days)
6. `expiry_006`: Out of office notice (TTL: 2 weeks)
7. `expiry_007`: Upcoming vacation (TTL: 3 months)
8. `expiry_008`: Blocked on issue (TTL: 5 days)

**C. Profile Cache Scenarios (6)**
1. `cache_001`: Immediate cache hit (generate, retrieve <1s)
2. `cache_002`: Cache miss after TTL (generate, wait TTL, retrieve)
3. `cache_003`: Stable facts accumulation (5 facts, check profile reflects all)
4. `cache_004`: Recent activity window (facts within 7d in recent_activity)
5. `cache_005`: Stable vs recent split (old facts → stable, new → recent)
6. `cache_006`: Profile refresh on new fact (add fact, profile regenerates)

**D. Multi-User Contradiction Scenarios (10)**
1. `multi_001`: Team lead disagreement (User A: John, User B: Sarah)
2. `multi_002`: Project status (User A: Active, User B: On Hold)
3. `multi_003`: Deadline (User A: March 15, User B: March 20)
4. `multi_004`: Technology choice (User A: React, User B: Vue)
5. `multi_005`: Performance metric (User A: 95% pass, User B: 92% pass)
6. `multi_006`: Resource allocation (User A: 2 devs, User B: 3 devs)
7. `multi_007`: Bug severity (User A: Critical, User B: Major)
8. `multi_008`: Customer feedback (User A: Positive, User B: Mixed)
9. `multi_009`: Next release date (User A: Q3, User B: Q4)
10. `multi_010`: Code review status (User A: Approved, User B: Needs Changes)

**E. Generic Fact Accumulation (10)**
1. `generic_001`: Preference facts (coffee, music, sports)
2. `generic_002`: Work environment (tools, languages, frameworks)
3. `generic_003`: Personal projects (side hustles, open source)
4. `generic_004`: Learning goals (courses, certifications, skills)
5. `generic_005`: Team info (team name, members, focus areas)
6. `generic_006`: Responsibility areas (features, modules, domains)
7. `generic_007`: Communication style (async, synchronous, timezone)
8. `generic_008`: Availability (full-time, part-time, contractor)
9. `generic_009`: Previous experience (companies, roles, years)
10. `generic_010`: Interests (AI/ML, DevOps, Security, Frontend)

**F. Fact Modification Scenarios (6)**
1. `modify_001`: Experience progression (3y → 5y → 8y)
2. `modify_002`: Project evolution (Planning → Development → Testing → Released)
3. `modify_003`: Skill improvement (Beginner → Intermediate → Advanced → Expert)
4. `modify_004`: Team growth (2 people → 5 people → 8 people)
5. `modify_005`: Salary history (100k → 120k → 150k) [if tracking this]
6. `modify_006`: Performance score (3.2 → 3.5 → 3.8)

---

## 6. Success Criteria & Validation

### 6.1 Criteria (G + H)

**Criterion G: Audit Completeness + Data Recoverability**
- Every fact event (create/update/deactivate/expire) logged in audit_log.db
- Each event includes: timestamp, user, fact_id, event_type, before_state, after_state
- Full audit trail reconstructable (can replay events to recover state)
- Data never deleted (only archived if needed)

**Criterion H: User Isolation**
- User A's facts queried from DB: only User A's facts returned
- User A's profile: only includes User A's stable facts + recent activity
- API /retrieve with user_A's context: no User B facts leaked
- Audit log: facts tagged with correct user_id

### 6.2 Validation Approach

Each test case has:
```python
expected_outcomes = [
    "fact_stored",           # Verify via DB query
    "fact_retrievable",      # Verify via API /retrieve
    "audit_event_logged",    # Verify in audit_log.db
    "user_isolation_ok",     # Verify no cross-user leaks
    "metadata_correct",      # Verify timestamps, user_id, run_number
]
```

Validator runs 4 methods for each expected outcome:
- Method 1 (DB): Query memory.db
- Method 2 (API): Call /retrieve or /profile
- Method 3 (Audit): Query audit_log.db
- Method 4 (Cross-check): Compare all methods, flag discrepancies

**PASS:** All methods agree on outcome  
**FAIL:** Any method disagrees, or expected outcome not met

---

## 7. Data Persistence & Run Tagging

### 7.1 Run Metadata
Every simulation run tagged with:
```
run_number: 1, 2, 3, 4, ...
start_time: timestamp
end_time: timestamp
duration: 1/2/4/6 minutes
status: "in_progress" / "completed" / "failed"
scenario_count: 50
user_count: 5
notes: "Initial end-to-end check" / "Progressive validation" / ...
```

### 7.2 Data Retention
- **memory.db:** Accumulates across runs (facts, audit trail)
- **audit_log.db:** All events tagged with run_number
- **run_metadata table:** Track each run's progress/status
- **No deletion:** Data persists for manual verification, future analysis

### 7.3 Manual Verification
After each run:
```
SELECT * FROM audit_log.audit_events WHERE run_number = 1 ORDER BY timestamp;
SELECT * FROM run_metadata WHERE run_number = 1;
```

Can reconstruct exact sequence of events for any run.

---

## 8. Technology Stack (Final)

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Backend | Python 3.12+ | SimulationRunner, services |
| Framework | FastAPI | REST API for monitoring, engine API client |
| Frontend | React 18+ | Real-time dashboard, visualization |
| Database 1 | SQLite | memory.db (existing engine storage) |
| Database 2 | SQLite | audit_log.db (new audit trail) |
| HTTP Client | httpx | Call memory engine API |
| Real-time | WebSockets | Dashboard ← monitoring updates |
| Async | asyncio | Concurrent API calls, timing |
| Logging | Python logging | Local logs + audit trail |
| Containerization | (Phase 2) | Docker Compose for reproducibility |

---

## 9. Next Steps

**This design phase complete.** Remaining phases:

1. **Approval:** User reviews design, requests changes (if any)
2. **Implementation planning:** Invoke writing-plans skill for detailed task breakdown
3. **Coding:** Build components in order (ScenarioGenerator → Runner → Client → Monitoring → Validator → Dashboard)
4. **Testing:** Run 1/2/4/6 minute simulations, verify all criteria
5. **Phase 2:** Add stress/load testing, Claude API integration, edge cases

---

## Appendix A: Example Scenario Execution

**Scenario: `contradiction_001` (NYC → SF)**

```
Time t=0s:      Simulation starts, run_number=1 assigned
Time t=15s:     User_2 ingests "I live in New York"
                - memory.db: INSERT (natural_key="user_2:user:location", object_value="NYC", is_active=1)
                - audit_log.db: INSERT event (event_type="created", fact_id="mem_xyz", ...)
                - monitoring: fact_ingested event
                - dashboard: updates user_2 panel

Time t=45s:     User_2 ingests "I moved to San Francisco"
                - memory.db: UPDATE (natural_key="user_2:user:location", object_value="SF", is_active=1)
                - memory.db: Previous record marked is_active=0
                - audit_log.db: INSERT event (event_type="deactivated", fact_id="mem_xyz")
                - audit_log.db: INSERT event (event_type="created", fact_id="mem_abc", object_value="SF")
                - monitoring: contradiction_resolved event
                - dashboard: shows contradiction flow

Time t=60s (or T_end): Simulation completes

Validation:
    ✓ DB query: NYC inactive, SF active
    ✓ API /retrieve: returns SF, not NYC
    ✓ Audit events: 2 events (deactivated + created) logged
    ✓ Profile API: SF in profile, NYC not
    ✓ Cross-validation: all methods agree
    ✓ Test case check: "old_deactivated" ✓, "new_active" ✓

Result: PASS
```

---

**Document Status:** Complete, ready for user review and approval before implementation planning.
