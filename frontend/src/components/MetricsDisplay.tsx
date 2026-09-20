/**
 * Metrics Display (spec § 3.7, component 5)
 *
 * Features:
 * - Grid of stat tiles:
 *   - Cache hit rate: X.Y%
 *   - Avg latency: Z.Z ms
 *   - API calls: N
 *   - Facts created/updated/deactivated/expired: N each
 *   - Storage: memory.db: X.XX MB, audit_log.db: Y.YY MB
 * - Data from GET /metrics
 * - Auto-refresh every 500ms alongside polling
 */

import React, { useCallback } from 'react';
import { usePolling } from '../hooks/usePolling';
import { metricsAPI } from '../api/client';
import type { MetricsResponse } from '../types';

interface StatTileProps {
  label: string;
  value: string | number;
  unit?: string;
  icon?: string;
  trend?: 'up' | 'down' | 'neutral';
}

const StatTile: React.FC<StatTileProps> = ({ label, value, unit, icon, trend = 'neutral' }) => {
  const trendColor = {
    up: 'text-green-600 dark:text-green-400',
    down: 'text-red-600 dark:text-red-400',
    neutral: 'text-gray-600 dark:text-gray-400',
  }[trend];

  return (
    <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-4 border border-gray-200 dark:border-gray-600">
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-2 flex items-center gap-2">
        {icon && <span className="text-lg">{icon}</span>}
        {label}
      </p>
      <p className={`text-3xl font-bold text-gray-900 dark:text-white ${trendColor}`}>
        {value}
        {unit && <span className="text-sm ml-1 text-gray-600 dark:text-gray-400">{unit}</span>}
      </p>
    </div>
  );
};

export const MetricsDisplay: React.FC = () => {
  const [metrics] = usePolling(useCallback(() => metricsAPI.getMetrics(), []), {
    interval: 500,
  });

  if (!metrics) {
    return (
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-md p-6">
        <h2 className="text-2xl font-bold mb-6 text-gray-900 dark:text-white">
          Metrics
        </h2>
        <div className="text-center py-8 text-gray-500 dark:text-gray-400">
          Waiting for metrics...
        </div>
      </div>
    );
  }

  const cacheHitPercent = (metrics.cache_hit_rate * 100).toFixed(1);
  const avgLatency = metrics.avg_latency_ms.toFixed(2);

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow-md p-6">
      <h2 className="text-2xl font-bold mb-6 text-gray-900 dark:text-white">
        Metrics
      </h2>

      {/* Main Metrics Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-6">
        <StatTile
          label="Cache Hit Rate"
          value={cacheHitPercent}
          unit="%"
          icon="📊"
          trend={metrics.cache_hit_rate > 0.7 ? 'up' : 'neutral'}
        />

        <StatTile
          label="Avg Latency"
          value={avgLatency}
          unit="ms"
          icon="⏱️"
          trend={metrics.avg_latency_ms < 100 ? 'up' : 'down'}
        />

        <StatTile
          label="API Calls"
          value={metrics.api_calls}
          icon="📡"
          trend="neutral"
        />

        <StatTile
          label="Facts Created"
          value={metrics.facts.created}
          icon="✨"
          trend="up"
        />

        <StatTile
          label="Facts Updated"
          value={metrics.facts.updated}
          icon="🔄"
          trend="neutral"
        />

        <StatTile
          label="Facts Expired"
          value={metrics.facts.expired}
          icon="⏳"
          trend="neutral"
        />
      </div>

      {/* Storage Metrics */}
      <div className="mb-6">
        <h3 className="text-lg font-semibold mb-4 text-gray-900 dark:text-white">
          Storage Usage
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="bg-gradient-to-br from-blue-100 to-blue-50 dark:from-blue-900/40 dark:to-blue-900/20 p-4 rounded-lg">
            <p className="text-sm text-gray-700 dark:text-gray-300 mb-2">memory.db</p>
            <p className="text-2xl font-bold text-blue-600 dark:text-blue-400">
              {metrics.storage_mb.memory_db.toFixed(2)}
              {' '}
              <span className="text-sm">MB</span>
            </p>
          </div>

          <div className="bg-gradient-to-br from-purple-100 to-purple-50 dark:from-purple-900/40 dark:to-purple-900/20 p-4 rounded-lg">
            <p className="text-sm text-gray-700 dark:text-gray-300 mb-2">audit_log.db</p>
            <p className="text-2xl font-bold text-purple-600 dark:text-purple-400">
              {metrics.storage_mb.audit_log_db.toFixed(2)}
              {' '}
              <span className="text-sm">MB</span>
            </p>
          </div>
        </div>
      </div>

      {/* Events Summary */}
      <div className="p-4 bg-gray-100 dark:bg-gray-700 rounded-lg">
        <h3 className="font-semibold text-gray-900 dark:text-white mb-3">
          Event Summary
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div>
            <span className="text-gray-600 dark:text-gray-400">Created:</span>
            {' '}
            <span className="font-bold text-gray-900 dark:text-white">
              {metrics.facts.created}
            </span>
          </div>
          <div>
            <span className="text-gray-600 dark:text-gray-400">Updated:</span>
            {' '}
            <span className="font-bold text-gray-900 dark:text-white">
              {metrics.facts.updated}
            </span>
          </div>
          <div>
            <span className="text-gray-600 dark:text-gray-400">Deactivated:</span>
            {' '}
            <span className="font-bold text-gray-900 dark:text-white">
              {metrics.facts.deactivated}
            </span>
          </div>
          <div>
            <span className="text-gray-600 dark:text-gray-400">Expired:</span>
            {' '}
            <span className="font-bold text-gray-900 dark:text-white">
              {metrics.facts.expired}
            </span>
          </div>
        </div>
      </div>

      {/* Auto-refresh indicator */}
      <div className="mt-4 text-xs text-gray-500 dark:text-gray-400">
        Auto-refreshing every 500ms
      </div>
    </div>
  );
};
