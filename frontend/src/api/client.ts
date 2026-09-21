/**
 * API client for Dashboard API (spec § 3.7)
 *
 * Provides fetch wrapper with error handling and polling logic.
 */

import type {
  EventResponse,
  FactResponse,
  MemoryCountResponse,
  MetricsResponse,
  ProfileCacheResponse,
  StartRunRequest,
  StartRunResponse,
  StatusResponse,
  ValidationResultResponse,
  ValidationSummaryResponse,
} from '../types';

declare const __API_BASE_URL__: string | undefined;

// Replaced at build time by Vite's `define`; falls back for test runners.
const API_BASE =
  typeof __API_BASE_URL__ !== 'undefined' ? __API_BASE_URL__ : 'http://localhost:8001';

class APIError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function fetchAPI<T>(endpoint: string, options?: RequestInit): Promise<T> {
  const url = `${API_BASE}${endpoint}`;

  try {
    const response = await fetch(url, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(options?.headers || {}),
      },
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
      // Not logged here: several endpoints answer 4xx for ordinary states, such
      // as /validation/summary before a run finishes. Callers decide what is noise.
      throw new APIError(response.status, error.detail || response.statusText);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof APIError) throw error;
    throw new APIError(500, `Failed to fetch ${endpoint}`);
  }
}

/**
 * Simulation Control API
 */
export const simulationAPI = {
  startRun: async (duration_seconds: number): Promise<StartRunResponse> => {
    return fetchAPI<StartRunResponse>('/simulate/start', {
      method: 'POST',
      body: JSON.stringify({ duration_seconds }),
    });
  },

  getStatus: async (run_id: string): Promise<StatusResponse> => {
    return fetchAPI<StatusResponse>(`/simulate/${run_id}/status`);
  },

  stopRun: async (run_id: string): Promise<{ status: string; run_id: string }> => {
    return fetchAPI<{ status: string; run_id: string }>(`/simulate/${run_id}/stop`, {
      method: 'POST',
    });
  },
};

/**
 * User State API
 */
export const userAPI = {
  getFacts: async (user_id: string): Promise<FactResponse[]> => {
    return fetchAPI<FactResponse[]>(`/users/${user_id}/facts`);
  },

  getMemoryCount: async (user_id: string): Promise<MemoryCountResponse> => {
    return fetchAPI<MemoryCountResponse>(`/users/${user_id}/memory-count`);
  },

  getProfileCache: async (user_id: string): Promise<ProfileCacheResponse> => {
    return fetchAPI<ProfileCacheResponse>(`/users/${user_id}/profile-cache`);
  },
};

/**
 * Audit Log API
 */
export const auditAPI = {
  getEvents: async (params?: {
    user_id?: string;
    event_type?: string;
    fact_id?: string;
    limit?: number;
  }): Promise<EventResponse[]> => {
    const searchParams = new URLSearchParams();
    if (params?.user_id) searchParams.append('user_id', params.user_id);
    if (params?.event_type) searchParams.append('event_type', params.event_type);
    if (params?.fact_id) searchParams.append('fact_id', params.fact_id);
    if (params?.limit) searchParams.append('limit', String(params.limit));

    const query = searchParams.toString() ? `?${searchParams.toString()}` : '';
    return fetchAPI<EventResponse[]>(`/audit/events${query}`);
  },
};

/**
 * Validation API
 */
export const validationAPI = {
  getResults: async (): Promise<ValidationResultResponse[]> => {
    return fetchAPI<ValidationResultResponse[]>('/validation/results');
  },

  getSummary: async (): Promise<ValidationSummaryResponse> => {
    return fetchAPI<ValidationSummaryResponse>('/validation/summary');
  },
};

/**
 * Metrics API
 */
export const metricsAPI = {
  getMetrics: async (): Promise<MetricsResponse> => {
    return fetchAPI<MetricsResponse>('/metrics');
  },
};

/**
 * Health Check API
 */
export const healthAPI = {
  check: async (): Promise<{ status: string; [key: string]: string }> => {
    return fetchAPI<{ status: string; [key: string]: string }>('/health');
  },
};
