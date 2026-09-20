/**
 * Integration tests for the complete simulation flow
 *
 * Tests the flow: SimulationControl → start → poll status → validation results
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { App } from '../App';

jest.mock('../api/client', () => ({
  simulationAPI: {
    startRun: jest.fn(),
    stopRun: jest.fn(),
    getStatus: jest.fn(),
  },
  userAPI: {
    getFacts: jest.fn(),
    getMemoryCount: jest.fn(),
    getProfileCache: jest.fn(),
  },
  auditAPI: {
    getEvents: jest.fn(),
  },
  validationAPI: {
    getSummary: jest.fn(),
    getResults: jest.fn(),
  },
  metricsAPI: {
    getMetrics: jest.fn(),
  },
}));

describe('Integration: Simulation Flow', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('should start a simulation and poll status', async () => {
    const {
      simulationAPI,
      userAPI,
      auditAPI,
      validationAPI,
      metricsAPI,
    } = require('../api/client');

    // Mock the start response
    simulationAPI.startRun.mockResolvedValueOnce({
      run_id: 'test123',
      run_number: 1,
      status: 'in_progress',
    });

    // Mock status polling (simulate running)
    simulationAPI.getStatus.mockResolvedValueOnce({
      run_id: 'test123',
      run_number: 1,
      status: 'in_progress',
      progress: {
        events_played: 0,
        events_skipped: 0,
        errors: 0,
        percent_complete: 0,
        elapsed_seconds: 0,
        remaining_seconds: 60,
      },
      duration_seconds: 60,
      events_total: 10,
    });

    // Mock other API calls
    userAPI.getFacts.mockResolvedValue([]);
    userAPI.getMemoryCount.mockResolvedValue({
      active: 0,
      inactive: 0,
      total: 0,
    });
    userAPI.getProfileCache.mockResolvedValue({ status: 'miss' });
    auditAPI.getEvents.mockResolvedValue([]);
    validationAPI.getSummary.mockResolvedValue({
      total_scenarios: 10,
      passed: 0,
      failed: 0,
      all_methods_agree: true,
      checks_total: 0,
      checks_passed: 0,
      checks_failed: 0,
      checks_skipped: 0,
      cross_validation_errors: 0,
    });
    validationAPI.getResults.mockResolvedValue([]);
    metricsAPI.getMetrics.mockResolvedValue({
      cache_hit_rate: 0,
      avg_latency_ms: 0,
      api_calls: 0,
      facts: {
        created: 0,
        updated: 0,
        deactivated: 0,
        expired: 0,
      },
      storage_mb: {
        memory_db: 0,
        audit_log_db: 0,
      },
    });

    render(<App />);

    // Verify the app renders
    expect(screen.getByText('Simulation Dashboard')).toBeInTheDocument();

    // Click the Start button
    const startButton = screen.getByRole('button', { name: /start/i });
    fireEvent.click(startButton);

    // Wait for the run to start
    await waitFor(() => {
      expect(simulationAPI.startRun).toHaveBeenCalledWith(60);
    });

    // Verify run ID is displayed
    await waitFor(() => {
      expect(screen.getByText(/Run:/)).toBeInTheDocument();
    });

    // Verify status polling was called
    await waitFor(() => {
      expect(simulationAPI.getStatus).toHaveBeenCalledWith('test123');
    }, { timeout: 2000 });
  });

  it('should display simulation progress', async () => {
    const { simulationAPI, userAPI, auditAPI, validationAPI, metricsAPI } =
      require('../api/client');

    simulationAPI.startRun.mockResolvedValueOnce({
      run_id: 'test123',
      run_number: 1,
      status: 'in_progress',
    });

    simulationAPI.getStatus.mockResolvedValueOnce({
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
    });

    userAPI.getFacts.mockResolvedValue([]);
    userAPI.getMemoryCount.mockResolvedValue({
      active: 0,
      inactive: 0,
      total: 0,
    });
    userAPI.getProfileCache.mockResolvedValue({ status: 'miss' });
    auditAPI.getEvents.mockResolvedValue([]);
    validationAPI.getSummary.mockResolvedValue({
      total_scenarios: 10,
      passed: 0,
      failed: 0,
      all_methods_agree: true,
      checks_total: 0,
      checks_passed: 0,
      checks_failed: 0,
      checks_skipped: 0,
      cross_validation_errors: 0,
    });
    validationAPI.getResults.mockResolvedValue([]);
    metricsAPI.getMetrics.mockResolvedValue({
      cache_hit_rate: 0,
      avg_latency_ms: 0,
      api_calls: 5,
      facts: {
        created: 5,
        updated: 0,
        deactivated: 0,
        expired: 0,
      },
      storage_mb: {
        memory_db: 0,
        audit_log_db: 0,
      },
    });

    render(<App />);

    const startButton = screen.getByRole('button', { name: /start/i });
    fireEvent.click(startButton);

    await waitFor(() => {
      expect(screen.getByText(/50%/)).toBeInTheDocument();
    });
  });

  it('should show validation results when run completes', async () => {
    const { simulationAPI, userAPI, auditAPI, validationAPI, metricsAPI } =
      require('../api/client');

    simulationAPI.startRun.mockResolvedValueOnce({
      run_id: 'test123',
      run_number: 1,
      status: 'in_progress',
    });

    // First call returns in_progress
    simulationAPI.getStatus.mockResolvedValueOnce({
      run_id: 'test123',
      run_number: 1,
      status: 'in_progress',
      progress: {
        events_played: 10,
        events_skipped: 0,
        errors: 0,
        percent_complete: 100,
        elapsed_seconds: 60,
        remaining_seconds: 0,
      },
      duration_seconds: 60,
      events_total: 10,
    });

    // Second call returns completed
    simulationAPI.getStatus.mockResolvedValueOnce({
      run_id: 'test123',
      run_number: 1,
      status: 'completed',
      progress: {
        events_played: 10,
        events_skipped: 0,
        errors: 0,
        percent_complete: 100,
        elapsed_seconds: 60,
        remaining_seconds: 0,
      },
      duration_seconds: 60,
      events_total: 10,
    });

    userAPI.getFacts.mockResolvedValue([]);
    userAPI.getMemoryCount.mockResolvedValue({
      active: 5,
      inactive: 2,
      total: 7,
    });
    userAPI.getProfileCache.mockResolvedValue({ status: 'hit' });
    auditAPI.getEvents.mockResolvedValue([]);
    validationAPI.getSummary.mockResolvedValue({
      total_scenarios: 10,
      passed: 9,
      failed: 1,
      all_methods_agree: true,
      checks_total: 50,
      checks_passed: 48,
      checks_failed: 2,
      checks_skipped: 0,
      cross_validation_errors: 0,
    });
    validationAPI.getResults.mockResolvedValue([
      {
        scenario_id: 'contradiction_001',
        outcome: 'old_deactivated',
        status: 'passed',
        passed: true,
        details: {},
      },
    ]);
    metricsAPI.getMetrics.mockResolvedValue({
      cache_hit_rate: 0.9,
      avg_latency_ms: 25,
      api_calls: 50,
      facts: {
        created: 10,
        updated: 5,
        deactivated: 2,
        expired: 1,
      },
      storage_mb: {
        memory_db: 1.2,
        audit_log_db: 0.5,
      },
    });

    render(<App />);

    const startButton = screen.getByRole('button', { name: /start/i });
    fireEvent.click(startButton);

    // Wait for the simulation to complete
    await waitFor(
      () => {
        expect(screen.getByText(/Simulation completed/)).toBeInTheDocument();
      },
      { timeout: 3000 },
    );

    // Verify validation tab is active
    const validationTab = screen.getByRole('button', { name: /validation/i });
    expect(validationTab.className).toContain('blue');
  });

  it('should filter audit events by user', async () => {
    const { simulationAPI, userAPI, auditAPI, validationAPI, metricsAPI } =
      require('../api/client');

    simulationAPI.startRun.mockResolvedValue({
      run_id: 'test123',
      run_number: 1,
      status: 'in_progress',
    });

    simulationAPI.getStatus.mockResolvedValue({
      run_id: 'test123',
      run_number: 1,
      status: 'in_progress',
      progress: {
        events_played: 0,
        events_skipped: 0,
        errors: 0,
        percent_complete: 0,
        elapsed_seconds: 0,
        remaining_seconds: 60,
      },
      duration_seconds: 60,
      events_total: 10,
    });

    userAPI.getFacts.mockResolvedValue([]);
    userAPI.getMemoryCount.mockResolvedValue({
      active: 0,
      inactive: 0,
      total: 0,
    });
    userAPI.getProfileCache.mockResolvedValue({ status: 'miss' });
    auditAPI.getEvents.mockResolvedValue([
      {
        event_id: 1,
        run_number: 1,
        timestamp: 1234567890,
        user_id: 'user_1',
        fact_id: 'fact_1',
        event_type: 'created',
        source: 'scenario_playback',
      },
    ]);
    validationAPI.getSummary.mockResolvedValue({
      total_scenarios: 10,
      passed: 0,
      failed: 0,
      all_methods_agree: true,
      checks_total: 0,
      checks_passed: 0,
      checks_failed: 0,
      checks_skipped: 0,
      cross_validation_errors: 0,
    });
    validationAPI.getResults.mockResolvedValue([]);
    metricsAPI.getMetrics.mockResolvedValue({
      cache_hit_rate: 0,
      avg_latency_ms: 0,
      api_calls: 0,
      facts: {
        created: 0,
        updated: 0,
        deactivated: 0,
        expired: 0,
      },
      storage_mb: {
        memory_db: 0,
        audit_log_db: 0,
      },
    });

    render(<App />);

    // Navigate to Audit tab
    const auditTab = screen.getByRole('button', { name: /audit/i });
    fireEvent.click(auditTab);

    // Open filters
    const filterButton = await screen.findByText(/Show Filters/i);
    fireEvent.click(filterButton);

    // Verify event is displayed
    await waitFor(() => {
      expect(screen.getByText('user_1')).toBeInTheDocument();
    });
  });
});
