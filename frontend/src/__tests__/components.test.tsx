/**
 * Component tests (spec § 3.7)
 *
 * Tests for React components: render, interaction, polling, filtering
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { SimulationControl } from '../components/SimulationControl';
import { UserStatePanel } from '../components/UserStatePanel';
import { AuditLogViewer } from '../components/AuditLogViewer';
import { ValidationDashboard } from '../components/ValidationDashboard';
import { MetricsDisplay } from '../components/MetricsDisplay';
import type { StatusResponse } from '../types';

// Mock the API module
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

describe('SimulationControl Component', () => {
  it('should render control buttons', () => {
    render(<SimulationControl />);

    expect(screen.getByRole('button', { name: /start/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /stop/i })).toBeInTheDocument();
  });

  it('should display duration options', () => {
    render(<SimulationControl />);

    expect(screen.getByText('1 minute')).toBeInTheDocument();
    expect(screen.getByText('2 minutes')).toBeInTheDocument();
    expect(screen.getByText('4 minutes')).toBeInTheDocument();
    expect(screen.getByText('6 minutes')).toBeInTheDocument();
  });

  it('should show progress bar when running', () => {
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

    render(<SimulationControl status={mockStatus} />);

    expect(screen.getByText('50%')).toBeInTheDocument();
    expect(screen.getByText(/50%/)).toBeInTheDocument();
  });

  it('should call onRunStart callback', async () => {
    const mockOnRunStart = jest.fn();
    const { simulationAPI } = require('../api/client');

    simulationAPI.startRun.mockResolvedValueOnce({
      run_id: 'test123',
      run_number: 1,
      status: 'in_progress',
    });

    render(<SimulationControl onRunStart={mockOnRunStart} />);

    const startButton = screen.getByRole('button', { name: /start/i });
    fireEvent.click(startButton);

    await waitFor(() => {
      expect(mockOnRunStart).toHaveBeenCalledWith('test123');
    });
  });

  it('should disable buttons when running', () => {
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

    render(<SimulationControl status={mockStatus} />);

    const startButton = screen.getByRole('button', { name: /start/i });
    expect(startButton).toBeDisabled();
  });
});

describe('UserStatePanel Component', () => {
  it('should render user tabs', () => {
    render(<UserStatePanel />);

    expect(screen.getByText('user_1')).toBeInTheDocument();
    expect(screen.getByText('user_2')).toBeInTheDocument();
    expect(screen.getByText('user_5')).toBeInTheDocument();
  });

  it('should switch tabs on click', async () => {
    const { userAPI } = require('../api/client');
    userAPI.getFacts.mockResolvedValue([]);
    userAPI.getMemoryCount.mockResolvedValue({
      active: 0,
      inactive: 0,
      total: 0,
    });
    userAPI.getProfileCache.mockResolvedValue({ status: 'miss' });

    render(<UserStatePanel />);

    const user2Tab = screen.getByText('user_2');
    fireEvent.click(user2Tab);

    await waitFor(() => {
      expect(userAPI.getFacts).toHaveBeenCalledWith('user_2');
    });
  });

  it('should display memory count pills', async () => {
    const { userAPI } = require('../api/client');
    userAPI.getFacts.mockResolvedValue([]);
    userAPI.getMemoryCount.mockResolvedValue({
      active: 5,
      inactive: 2,
      total: 7,
    });
    userAPI.getProfileCache.mockResolvedValue({ status: 'hit' });

    render(<UserStatePanel />);

    await waitFor(() => {
      expect(screen.getByText(/Active: 5/)).toBeInTheDocument();
      expect(screen.getByText(/Inactive: 2/)).toBeInTheDocument();
      expect(screen.getByText(/Total: 7/)).toBeInTheDocument();
    });
  });

  it('should display facts', async () => {
    const { userAPI } = require('../api/client');
    userAPI.getFacts.mockResolvedValue([
      {
        text: 'I live in NYC',
        timestamp: 1234567890,
        is_active: true,
        contradictions: [],
      },
    ]);
    userAPI.getMemoryCount.mockResolvedValue({
      active: 1,
      inactive: 0,
      total: 1,
    });
    userAPI.getProfileCache.mockResolvedValue({ status: 'miss' });

    render(<UserStatePanel />);

    await waitFor(() => {
      expect(screen.getByText('I live in NYC')).toBeInTheDocument();
    });
  });
});

describe('AuditLogViewer Component', () => {
  it('should render filter button', () => {
    const { auditAPI } = require('../api/client');
    auditAPI.getEvents.mockResolvedValue([]);

    render(<AuditLogViewer />);

    expect(screen.getByText(/Show Filters/i)).toBeInTheDocument();
  });

  it('should show filters when toggle clicked', async () => {
    const { auditAPI } = require('../api/client');
    auditAPI.getEvents.mockResolvedValue([]);

    render(<AuditLogViewer />);

    const toggleButton = screen.getByText(/Show Filters/i);
    fireEvent.click(toggleButton);

    await waitFor(() => {
      expect(screen.getByText(/User/i)).toBeInTheDocument();
    });
  });

  it('should filter events by user', async () => {
    const { auditAPI } = require('../api/client');
    auditAPI.getEvents.mockResolvedValue([]);

    render(<AuditLogViewer />);

    const toggleButton = screen.getByText(/Show Filters/i);
    fireEvent.click(toggleButton);

    await waitFor(() => {
      const userSelect = screen.getByDisplayValue('All');
      fireEvent.change(userSelect, { target: { value: 'user_1' } });
    });

    await waitFor(() => {
      expect(auditAPI.getEvents).toHaveBeenCalledWith(
        expect.objectContaining({ user_id: 'user_1' }),
      );
    });
  });

  it('should display events', async () => {
    const { auditAPI } = require('../api/client');
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

    render(<AuditLogViewer />);

    await waitFor(() => {
      expect(screen.getByText('user_1')).toBeInTheDocument();
      expect(screen.getByText(/Created/i)).toBeInTheDocument();
    });
  });
});

describe('ValidationDashboard Component', () => {
  it('should display validation summary', async () => {
    const { validationAPI } = require('../api/client');
    validationAPI.getSummary.mockResolvedValue({
      total_scenarios: 10,
      passed: 8,
      failed: 2,
      all_methods_agree: true,
      checks_total: 50,
      checks_passed: 45,
      checks_failed: 5,
      checks_skipped: 0,
      cross_validation_errors: 0,
    });
    validationAPI.getResults.mockResolvedValue([]);

    render(<ValidationDashboard />);

    await waitFor(() => {
      expect(screen.getByText('8')).toBeInTheDocument();
      expect(screen.getByText('2')).toBeInTheDocument();
    });
  });

  it('should display pass rate percentage', async () => {
    const { validationAPI } = require('../api/client');
    validationAPI.getSummary.mockResolvedValue({
      total_scenarios: 10,
      passed: 8,
      failed: 2,
      all_methods_agree: true,
      checks_total: 50,
      checks_passed: 45,
      checks_failed: 5,
      checks_skipped: 0,
      cross_validation_errors: 0,
    });
    validationAPI.getResults.mockResolvedValue([]);

    render(<ValidationDashboard />);

    await waitFor(() => {
      expect(screen.getByText('80')).toBeInTheDocument();
    });
  });

  it('should display results table', async () => {
    const { validationAPI } = require('../api/client');
    validationAPI.getSummary.mockResolvedValue({
      total_scenarios: 1,
      passed: 1,
      failed: 0,
      all_methods_agree: true,
      checks_total: 5,
      checks_passed: 5,
      checks_failed: 0,
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

    render(<ValidationDashboard />);

    await waitFor(() => {
      expect(screen.getByText('contradiction_001')).toBeInTheDocument();
    });
  });
});

describe('MetricsDisplay Component', () => {
  it('should display cache hit rate', async () => {
    const { metricsAPI } = require('../api/client');
    metricsAPI.getMetrics.mockResolvedValue({
      cache_hit_rate: 0.85,
      avg_latency_ms: 50,
      api_calls: 100,
      facts: { created: 50, updated: 10, deactivated: 5, expired: 2 },
      storage_mb: { memory_db: 1.5, audit_log_db: 0.8 },
    });

    render(<MetricsDisplay />);

    await waitFor(() => {
      expect(screen.getByText(/85/)).toBeInTheDocument();
    });
  });

  it('should display average latency', async () => {
    const { metricsAPI } = require('../api/client');
    metricsAPI.getMetrics.mockResolvedValue({
      cache_hit_rate: 0.85,
      avg_latency_ms: 50.5,
      api_calls: 100,
      facts: { created: 50, updated: 10, deactivated: 5, expired: 2 },
      storage_mb: { memory_db: 1.5, audit_log_db: 0.8 },
    });

    render(<MetricsDisplay />);

    await waitFor(() => {
      expect(screen.getByText(/50.50/)).toBeInTheDocument();
    });
  });

  it('should display facts counts', async () => {
    const { metricsAPI } = require('../api/client');
    metricsAPI.getMetrics.mockResolvedValue({
      cache_hit_rate: 0.85,
      avg_latency_ms: 50,
      api_calls: 100,
      facts: { created: 50, updated: 10, deactivated: 5, expired: 2 },
      storage_mb: { memory_db: 1.5, audit_log_db: 0.8 },
    });

    render(<MetricsDisplay />);

    await waitFor(() => {
      expect(screen.getByText('50')).toBeInTheDocument();
    });
  });

  it('should display storage usage', async () => {
    const { metricsAPI } = require('../api/client');
    metricsAPI.getMetrics.mockResolvedValue({
      cache_hit_rate: 0.85,
      avg_latency_ms: 50,
      api_calls: 100,
      facts: { created: 50, updated: 10, deactivated: 5, expired: 2 },
      storage_mb: { memory_db: 1.5, audit_log_db: 0.8 },
    });

    render(<MetricsDisplay />);

    await waitFor(() => {
      expect(screen.getByText(/1.50/)).toBeInTheDocument();
      expect(screen.getByText(/0.80/)).toBeInTheDocument();
    });
  });
});
