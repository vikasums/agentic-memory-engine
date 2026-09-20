/**
 * Tests for API client (spec § 3.7)
 */

import {
  simulationAPI,
  userAPI,
  auditAPI,
  validationAPI,
  metricsAPI,
} from '../api/client';
import type {
  StartRunResponse,
  StatusResponse,
  FactResponse,
  EventResponse,
  MetricsResponse,
} from '../types';

describe('API Client', () => {
  beforeEach(() => {
    (global.fetch as jest.Mock).mockClear();
  });

  describe('simulationAPI', () => {
    test('startRun should POST to /simulate/start', async () => {
      const mockResponse: StartRunResponse = {
        run_id: 'test123',
        run_number: 1,
        status: 'in_progress',
      };

      (global.fetch as jest.Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => mockResponse,
      });

      const result = await simulationAPI.startRun(60);

      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:8000/simulate/start',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ duration_seconds: 60 }),
        }),
      );
      expect(result).toEqual(mockResponse);
    });

    test('getStatus should GET /simulate/{run_id}/status', async () => {
      const mockStatus: StatusResponse = {
        run_id: 'test123',
        run_number: 1,
        status: 'in_progress',
        progress: {
          events_played: 5,
          events_skipped: 0,
          errors: 0,
          percent_complete: 50,
          elapsed_seconds: 30,
          remaining_seconds: 30,
        },
        duration_seconds: 60,
        events_total: 10,
      };

      (global.fetch as jest.Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => mockStatus,
      });

      const result = await simulationAPI.getStatus('test123');

      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:8000/simulate/test123/status',
        expect.any(Object),
      );
      expect(result).toEqual(mockStatus);
    });

    test('stopRun should POST to /simulate/{run_id}/stop', async () => {
      (global.fetch as jest.Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: 'stopped', run_id: 'test123' }),
      });

      await simulationAPI.stopRun('test123');

      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:8000/simulate/test123/stop',
        expect.objectContaining({ method: 'POST' }),
      );
    });
  });

  describe('userAPI', () => {
    test('getFacts should return array of facts', async () => {
      const mockFacts: FactResponse[] = [
        {
          text: 'I live in NYC',
          timestamp: 1234567890,
          is_active: true,
          contradictions: [],
        },
      ];

      (global.fetch as jest.Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => mockFacts,
      });

      const result = await userAPI.getFacts('user_1');

      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:8000/users/user_1/facts',
        expect.any(Object),
      );
      expect(result).toEqual(mockFacts);
    });

    test('getMemoryCount should return counts', async () => {
      (global.fetch as jest.Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ active: 5, inactive: 2, total: 7 }),
      });

      const result = await userAPI.getMemoryCount('user_1');

      expect(result).toEqual({ active: 5, inactive: 2, total: 7 });
    });

    test('getProfileCache should return cache status', async () => {
      (global.fetch as jest.Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          status: 'hit',
          ttl_remaining_seconds: 30,
          cache_timestamp: 1234567890,
        }),
      });

      const result = await userAPI.getProfileCache('user_1');

      expect(result.status).toBe('hit');
    });
  });

  describe('auditAPI', () => {
    test('getEvents should query with filters', async () => {
      const mockEvents: EventResponse[] = [
        {
          event_id: 1,
          run_number: 1,
          timestamp: 1234567890,
          user_id: 'user_1',
          fact_id: 'fact_1',
          event_type: 'created',
          source: 'scenario_playback',
        },
      ];

      (global.fetch as jest.Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => mockEvents,
      });

      const result = await auditAPI.getEvents({
        user_id: 'user_1',
        event_type: 'created',
        limit: 100,
      });

      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('http://localhost:8000/audit/events?'),
        expect.any(Object),
      );
      expect(result).toEqual(mockEvents);
    });
  });

  describe('validationAPI', () => {
    test('getSummary should return validation summary', async () => {
      (global.fetch as jest.Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          total_scenarios: 10,
          passed: 8,
          failed: 2,
          all_methods_agree: true,
          checks_total: 50,
          checks_passed: 45,
          checks_failed: 5,
          checks_skipped: 0,
          cross_validation_errors: 0,
        }),
      });

      const result = await validationAPI.getSummary();

      expect(result.total_scenarios).toBe(10);
      expect(result.all_methods_agree).toBe(true);
    });

    test('getResults should return validation results', async () => {
      (global.fetch as jest.Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => [
          {
            scenario_id: 'contradiction_001',
            outcome: 'old_deactivated',
            status: 'passed',
            passed: true,
            details: {},
          },
        ],
      });

      const result = await validationAPI.getResults();

      expect(result).toHaveLength(1);
      expect(result[0].passed).toBe(true);
    });
  });

  describe('metricsAPI', () => {
    test('getMetrics should return metrics', async () => {
      const mockMetrics: MetricsResponse = {
        cache_hit_rate: 0.8,
        avg_latency_ms: 50,
        api_calls: 100,
        facts: { created: 50, updated: 10, deactivated: 5, expired: 2 },
        storage_mb: { memory_db: 1.5, audit_log_db: 0.8 },
      };

      (global.fetch as jest.Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => mockMetrics,
      });

      const result = await metricsAPI.getMetrics();

      expect(result.cache_hit_rate).toBe(0.8);
      expect(result.facts.created).toBe(50);
    });
  });

  describe('error handling', () => {
    test('should throw APIError on failed request', async () => {
      (global.fetch as jest.Mock).mockResolvedValueOnce({
        ok: false,
        status: 404,
        statusText: 'Not Found',
        json: async () => ({ detail: 'Run not found' }),
      });

      await expect(simulationAPI.getStatus('nonexistent')).rejects.toThrow();
    });

    test('should handle network errors', async () => {
      (global.fetch as jest.Mock).mockRejectedValueOnce(
        new Error('Network error'),
      );

      await expect(simulationAPI.startRun(60)).rejects.toThrow();
    });
  });
});
