/**
 * Validation Dashboard (spec § 3.7, component 4)
 *
 * Features:
 * - Top: summary row (X passed / Y failed / all_methods_agree: Y/N)
 * - Below: test case table
 *   - Columns: Scenario ID | Status (✓/✗) | Expected Outcomes | Actual Outcomes | Errors
 *   - Green row = pass, red row = fail
 * - Data from GET /validation/results, /validation/summary
 */

import React, { useCallback, useEffect, useState } from 'react';
import { usePolling } from '../hooks/usePolling';
import { validationAPI } from '../api/client';
import type { ValidationResultResponse, ValidationSummaryResponse } from '../types';

export const ValidationDashboard: React.FC = () => {
  // A validation report only exists once a run has finished, and it never
  // changes afterwards, so polling stops as soon as one arrives.
  const [reportLoaded, setReportLoaded] = useState(false);

  const [summary] = usePolling(useCallback(() => validationAPI.getSummary(), []), {
    interval: 1000,
    enabled: !reportLoaded,
    onError: () => {
      // 400 until a run completes; the placeholder below covers that state.
    },
  });

  const [results] = usePolling(useCallback(() => validationAPI.getResults(), []), {
    interval: 1000,
    enabled: !reportLoaded,
    onError: () => {
      // Same as above.
    },
  });

  useEffect(() => {
    if (summary && results && results.length > 0) setReportLoaded(true);
  }, [summary, results]);

  const statusColor = (passed: boolean) => {
    return passed ? 'bg-green-50 dark:bg-green-900/20' : 'bg-red-50 dark:bg-red-900/20';
  };

  const statusIcon = (passed: boolean) => {
    return passed ? '✓' : '✗';
  };

  const statusTextColor = (passed: boolean) => {
    return passed
      ? 'text-green-600 dark:text-green-400'
      : 'text-red-600 dark:text-red-400';
  };

  if (!summary) {
    return (
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-md p-6">
        <h2 className="text-2xl font-bold mb-6 text-gray-900 dark:text-white">
          Validation Dashboard
        </h2>
        <div className="text-center py-8 text-gray-500 dark:text-gray-400">
          Waiting for validation results...
        </div>
      </div>
    );
  }

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow-md p-6 space-y-6">
      <h2 className="text-2xl font-bold text-gray-900 dark:text-white">
        Validation Dashboard
      </h2>

      {/* Summary Row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-gradient-to-br from-green-100 to-green-50 dark:from-green-900/40 dark:to-green-900/20 p-4 rounded-lg">
          <p className="text-sm text-gray-600 dark:text-gray-400">Passed</p>
          <p className="text-3xl font-bold text-green-600 dark:text-green-400">
            {summary.passed}
          </p>
        </div>

        <div className="bg-gradient-to-br from-red-100 to-red-50 dark:from-red-900/40 dark:to-red-900/20 p-4 rounded-lg">
          <p className="text-sm text-gray-600 dark:text-gray-400">Failed</p>
          <p className="text-3xl font-bold text-red-600 dark:text-red-400">
            {summary.failed}
          </p>
        </div>

        <div className="bg-gradient-to-br from-blue-100 to-blue-50 dark:from-blue-900/40 dark:to-blue-900/20 p-4 rounded-lg">
          <p className="text-sm text-gray-600 dark:text-gray-400">Pass Rate</p>
          <p className="text-3xl font-bold text-blue-600 dark:text-blue-400">
            {summary.total_scenarios > 0
              ? Math.round((summary.passed / summary.total_scenarios) * 100)
              : 0}
            %
          </p>
        </div>

        <div
          className={`p-4 rounded-lg ${
            summary.all_methods_agree
              ? 'bg-gradient-to-br from-green-100 to-green-50 dark:from-green-900/40 dark:to-green-900/20'
              : 'bg-gradient-to-br from-yellow-100 to-yellow-50 dark:from-yellow-900/40 dark:to-yellow-900/20'
          }`}
        >
          <p className="text-sm text-gray-600 dark:text-gray-400">Cross-Validation</p>
          <p
            className={`text-lg font-bold ${
              summary.all_methods_agree
                ? 'text-green-600 dark:text-green-400'
                : 'text-yellow-600 dark:text-yellow-400'
            }`}
          >
            {summary.all_methods_agree ? 'Agree' : 'Disagree'}
          </p>
        </div>
      </div>

      {/* Checks Summary */}
      <div className="p-4 bg-gray-100 dark:bg-gray-700 rounded-lg">
        <h3 className="font-semibold text-gray-900 dark:text-white mb-2">
          Overall Checks
        </h3>
        <div className="flex gap-6 text-sm">
          <span className="text-gray-700 dark:text-gray-300">
            Passed: <span className="font-bold text-green-600 dark:text-green-400">
              {summary.checks_passed}
            </span>
          </span>
          <span className="text-gray-700 dark:text-gray-300">
            Failed: <span className="font-bold text-red-600 dark:text-red-400">
              {summary.checks_failed}
            </span>
          </span>
          <span className="text-gray-700 dark:text-gray-300">
            Skipped: <span className="font-bold text-gray-600 dark:text-gray-400">
              {summary.checks_skipped}
            </span>
          </span>
          <span className="text-gray-700 dark:text-gray-300">
            Total: <span className="font-bold">{summary.checks_total}</span>
          </span>
        </div>
      </div>

      {/* Results Table */}
      {results && results.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-100 dark:bg-gray-700 border-b border-gray-300 dark:border-gray-600">
              <tr>
                <th className="text-left px-4 py-3 font-semibold text-gray-900 dark:text-white">
                  Scenario ID
                </th>
                <th className="text-center px-4 py-3 font-semibold text-gray-900 dark:text-white">
                  Status
                </th>
                <th className="text-left px-4 py-3 font-semibold text-gray-900 dark:text-white">
                  Outcome
                </th>
                <th className="text-left px-4 py-3 font-semibold text-gray-900 dark:text-white">
                  Details
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
              {results.map((result: ValidationResultResponse, index: number) => (
                <tr
                  key={`${result.scenario_id}:${result.outcome}:${index}`}
                  className={`${statusColor(result.passed)} hover:opacity-75 transition-opacity`}
                >
                  <td className="px-4 py-3 font-mono text-xs text-gray-700 dark:text-gray-300">
                    {result.scenario_id}
                  </td>
                  <td className={`text-center px-4 py-3 font-bold text-lg ${statusTextColor(result.passed)}`}>
                    {statusIcon(result.passed)}
                  </td>
                  <td className="px-4 py-3 text-gray-700 dark:text-gray-300">
                    {result.outcome}
                  </td>
                  <td className="px-4 py-3 text-gray-600 dark:text-gray-400 text-xs">
                    {result.status === 'skipped' ? (
                      <span className="text-gray-500">Skipped</span>
                    ) : Object.entries(result.details).length > 0 ? (
                      <code className="text-xs">{JSON.stringify(result.details).substring(0, 50)}...</code>
                    ) : (
                      'No details'
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="text-center py-8 text-gray-500 dark:text-gray-400">
          No validation results yet. Start a simulation to run validation.
        </div>
      )}
    </div>
  );
};
