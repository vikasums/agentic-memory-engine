/**
 * TypeScript types mirroring Dashboard API responses (spec § 3.7)
 */

export interface ProgressInfo {
  events_played: number;
  events_skipped: number;
  errors: number;
  percent_complete: number;
  elapsed_seconds: number;
  remaining_seconds: number;
  last_scenario_id?: string | null;
}

export interface StatusResponse {
  run_id: string;
  run_number: number;
  status: 'in_progress' | 'completed' | 'failed';
  progress: ProgressInfo;
  duration_seconds: number;
  events_total: number;
}

export interface FactResponse {
  text: string;
  timestamp: number;
  is_active: boolean;
  contradictions: string[];
}

export interface MemoryCountResponse {
  active: number;
  inactive: number;
  total: number;
}

export interface ProfileCacheResponse {
  status: 'hit' | 'miss' | 'ttl' | 'unknown';
  ttl_remaining_seconds?: number | null;
  cache_timestamp?: number | null;
}

export interface EventResponse {
  event_id: number;
  run_number: number;
  timestamp: number;
  user_id: string;
  fact_id: string;
  event_type: string;
  before_state?: Record<string, unknown> | null;
  after_state?: Record<string, unknown> | null;
  source: string;
}

export interface ValidationResultResponse {
  scenario_id: string;
  outcome: string;
  status: 'passed' | 'failed' | 'skipped';
  passed: boolean;
  details: Record<string, unknown>;
}

export interface ValidationSummaryResponse {
  total_scenarios: number;
  passed: number;
  failed: number;
  all_methods_agree: boolean;
  checks_total: number;
  checks_passed: number;
  checks_failed: number;
  checks_skipped: number;
  cross_validation_errors: number;
}

export interface MetricsResponse {
  cache_hit_rate: number;
  avg_latency_ms: number;
  api_calls: number;
  facts: {
    created: number;
    updated: number;
    deactivated: number;
    expired: number;
  };
  storage_mb: {
    memory_db: number;
    audit_log_db: number;
  };
}

export interface StartRunRequest {
  duration_seconds: number;
  scenarios?: unknown[];
}

export interface StartRunResponse {
  run_id: string;
  run_number: number;
  status: string;
}
