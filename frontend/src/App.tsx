/**
 * Main App Layout (spec § 3.7)
 *
 * Features:
 * - Header: "Simulation Dashboard" + branding
 * - Sidebar: navigation tabs (Control, Users, Audit, Validation, Metrics) + run status indicator
 * - Content area: active panel content (swap on tab click)
 * - Global polling: useEffect hook polling /status every 500ms when run_id active
 * - Error boundary: catch fetch errors + show toast notification
 * - Loading state: spinner during /start POST, "Run in progress" message
 */

import React, { useCallback, useState } from 'react';
import { usePolling } from './hooks/usePolling';
import { simulationAPI } from './api/client';
import { SimulationControl } from './components/SimulationControl';
import { UserStatePanel } from './components/UserStatePanel';
import { AuditLogViewer } from './components/AuditLogViewer';
import { ValidationDashboard } from './components/ValidationDashboard';
import { MetricsDisplay } from './components/MetricsDisplay';
import type { StatusResponse } from './types';

type TabType = 'control' | 'users' | 'audit' | 'validation' | 'metrics';

interface Toast {
  id: string;
  message: string;
  type: 'error' | 'success' | 'info';
}

export const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState<TabType>('control');
  const [runId, setRunId] = useState<string | null>(null);
  const [darkMode, setDarkMode] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);

  // Poll status when a run is active
  const [status, statusLoading, statusError] = usePolling(
    useCallback(() => {
      if (!runId) return Promise.reject(new Error('No run'));
      return simulationAPI.getStatus(runId);
    }, [runId]),
    {
      interval: 500,
      enabled: runId !== null,
      onError: (error) => {
        if (runId) {
          addToast(`Status polling failed: ${error.message}`, 'error');
        }
      },
    },
  );

  const addToast = (message: string, type: 'error' | 'success' | 'info' = 'info') => {
    const id = Date.now().toString();
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  };

  const handleRunStart = (newRunId: string) => {
    setRunId(newRunId);
    addToast('Simulation started', 'success');
  };

  const handleRunStop = () => {
    setRunId(null);
    addToast('Simulation stopped', 'info');
  };

  // Auto-switch to validation when run completes
  React.useEffect(() => {
    if (status?.status === 'completed') {
      addToast('Simulation completed', 'success');
      setTimeout(() => setActiveTab('validation'), 1000);
    } else if (status?.status === 'failed') {
      addToast('Simulation failed', 'error');
    }
  }, [status?.status]);

  const toggleDarkMode = () => {
    setDarkMode(!darkMode);
  };

  const tabs: Array<{ id: TabType; label: string; icon: string }> = [
    { id: 'control', label: 'Control', icon: '🎮' },
    { id: 'users', label: 'Users', icon: '👥' },
    { id: 'audit', label: 'Audit', icon: '📋' },
    { id: 'validation', label: 'Validation', icon: '✓' },
    { id: 'metrics', label: 'Metrics', icon: '📊' },
  ];

  return (
    <div className={darkMode ? 'dark' : ''}>
      <div className="min-h-screen bg-white dark:bg-gray-900 text-gray-900 dark:text-white">
        {/* Header */}
        <header className="bg-gray-100 dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 sticky top-0 z-50">
          <div className="max-w-7xl mx-auto px-4 py-4 flex items-center justify-between">
            <h1 className="text-3xl font-bold">Simulation Dashboard</h1>
            <div className="flex items-center gap-4">
              {status && runId && (
                <div className="text-sm">
                  <span className="font-semibold">Run:</span>
                  {' '}
                  <span className="font-mono bg-gray-200 dark:bg-gray-700 px-2 py-1 rounded">
                    {runId}
                  </span>
                  {' '}
                  <span
                    className={`ml-2 inline-block px-3 py-1 rounded-full text-xs font-semibold ${
                      status.status === 'in_progress'
                        ? 'bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-100'
                        : status.status === 'completed'
                          ? 'bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-100'
                          : 'bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-100'
                    }`}
                  >
                    {status.status}
                  </span>
                </div>
              )}
              <button
                onClick={toggleDarkMode}
                className="p-2 rounded-lg bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 transition-colors"
                title="Toggle dark mode"
              >
                {darkMode ? '☀️' : '🌙'}
              </button>
            </div>
          </div>
        </header>

        {/* Main Content */}
        <div className="max-w-7xl mx-auto px-4 py-6">
          <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
            {/* Sidebar - Navigation */}
            <nav className="lg:col-span-1">
              <div className="bg-white dark:bg-gray-800 rounded-lg shadow-md overflow-hidden sticky top-24">
                <div className="p-4 bg-gray-100 dark:bg-gray-700">
                  <h3 className="font-bold text-sm text-gray-600 dark:text-gray-300 uppercase tracking-wide">
                    Navigation
                  </h3>
                </div>
                <ul className="divide-y divide-gray-200 dark:divide-gray-700">
                  {tabs.map((tab) => (
                    <li key={tab.id}>
                      <button
                        onClick={() => setActiveTab(tab.id)}
                        className={`w-full text-left px-4 py-3 font-medium transition-colors flex items-center gap-2 ${
                          activeTab === tab.id
                            ? 'bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400 border-l-4 border-blue-600 dark:border-blue-400'
                            : 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50'
                        }`}
                      >
                        <span>{tab.icon}</span>
                        {tab.label}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>

              {/* Run Status Widget */}
              {runId && status && (
                <div className="mt-6 bg-white dark:bg-gray-800 rounded-lg shadow-md p-4">
                  <h4 className="font-bold text-sm mb-3 text-gray-700 dark:text-gray-300">
                    Run Status
                  </h4>
                  <div className="space-y-2 text-xs text-gray-600 dark:text-gray-400">
                    <p>
                      <span className="font-semibold text-gray-900 dark:text-white">
                        Progress:
                      </span>
                      {' '}
                      {Math.round(status.progress.percent_complete)}%
                    </p>
                    <p>
                      <span className="font-semibold text-gray-900 dark:text-white">
                        Events:
                      </span>
                      {' '}
                      {status.progress.events_played}/{status.events_total}
                    </p>
                    <p>
                      <span className="font-semibold text-gray-900 dark:text-white">
                        Time Remaining:
                      </span>
                      {' '}
                      {Math.round(status.progress.remaining_seconds)}s
                    </p>
                  </div>
                </div>
              )}
            </nav>

            {/* Main Content Area */}
            <main className="lg:col-span-3">
              {activeTab === 'control' && (
                <SimulationControl
                  status={status}
                  loading={statusLoading}
                  error={statusError}
                  onRunStart={handleRunStart}
                  onRunStop={handleRunStop}
                />
              )}

              {activeTab === 'users' && <UserStatePanel />}

              {activeTab === 'audit' && <AuditLogViewer />}

              {activeTab === 'validation' && <ValidationDashboard />}

              {activeTab === 'metrics' && <MetricsDisplay />}
            </main>
          </div>
        </div>

        {/* Toast Notifications */}
        <div className="fixed bottom-4 right-4 space-y-2 max-w-sm">
          {toasts.map((toast) => (
            <div
              key={toast.id}
              className={`p-4 rounded-lg shadow-lg text-white font-medium animate-in fade-in slide-in-from-right-4 ${
                toast.type === 'error'
                  ? 'bg-red-500'
                  : toast.type === 'success'
                    ? 'bg-green-500'
                    : 'bg-blue-500'
              }`}
            >
              {toast.message}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

export default App;
