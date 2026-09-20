/**
 * User State Panel (spec § 3.7, component 2)
 *
 * Features:
 * - 5 tabs (one per user: user_1 through user_5)
 * - Per-tab display:
 *   - Current facts list (text, timestamp, is_active)
 *   - Recent contradictions (old_fact → new_fact with arrows)
 *   - Memory count (active/inactive/total pills)
 *   - Profile cache status (Hit/Miss/TTL with countdown)
 */

import React, { useCallback, useState } from 'react';
import { usePolling } from '../hooks/usePolling';
import type { FactResponse, MemoryCountResponse, ProfileCacheResponse } from '../types';
import { userAPI } from '../api/client';

const USERS = ['user_1', 'user_2', 'user_3', 'user_4', 'user_5'];

const USER_POLL_INTERVAL_MS = 2000;

interface UserTabProps {
  user_id: string;
  isActive: boolean;
}

const UserTab: React.FC<UserTabProps> = ({ user_id, isActive }) => {
  const [facts] = usePolling(
    useCallback(() => userAPI.getFacts(user_id), [user_id]),
    { enabled: isActive, interval: USER_POLL_INTERVAL_MS },
  );

  const [memoryCount] = usePolling(
    useCallback(() => userAPI.getMemoryCount(user_id), [user_id]),
    { enabled: isActive, interval: USER_POLL_INTERVAL_MS },
  );

  const [profileCache] = usePolling(
    useCallback(() => userAPI.getProfileCache(user_id), [user_id]),
    { enabled: isActive, interval: USER_POLL_INTERVAL_MS },
  );

  const activeFacts = facts?.filter((f) => f.is_active) ?? [];
  const inactiveFacts = facts?.filter((f) => !f.is_active) ?? [];
  const recentContradictions = activeFacts.filter((f) => f.contradictions.length > 0);

  const formatTimestamp = (ts: number) => {
    return new Date(ts * 1000).toLocaleTimeString();
  };

  const formatCacheStatus = () => {
    if (profileCache?.status === 'hit') {
      return `Hit`;
    } else if (profileCache?.status === 'miss') {
      return 'Miss';
    } else if (profileCache?.status === 'ttl' && profileCache?.ttl_remaining_seconds) {
      return `TTL (${profileCache.ttl_remaining_seconds.toFixed(1)}s)`;
    }
    return 'Unknown';
  };

  return (
    <div className="space-y-4">
      {/* Memory Count Pills */}
      {memoryCount && (
        <div className="flex gap-3 flex-wrap">
          <div className="bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-100 px-3 py-1 rounded-full text-sm font-semibold">
            Active: {memoryCount.active}
          </div>
          <div className="bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-100 px-3 py-1 rounded-full text-sm font-semibold">
            Inactive: {memoryCount.inactive}
          </div>
          <div className="bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-100 px-3 py-1 rounded-full text-sm font-semibold">
            Total: {memoryCount.total}
          </div>
        </div>
      )}

      {/* Profile Cache Status */}
      {profileCache && (
        <div className="text-sm text-gray-600 dark:text-gray-400">
          Cache: <span className="font-semibold">{formatCacheStatus()}</span>
        </div>
      )}

      {/* Current Facts */}
      {activeFacts.length > 0 && (
        <div>
          <h4 className="font-semibold text-gray-900 dark:text-white mb-2">
            Current Facts ({activeFacts.length})
          </h4>
          <ul className="space-y-2">
            {activeFacts.map((fact, idx) => (
              <li key={idx} className="bg-gray-50 dark:bg-gray-700 p-3 rounded text-sm">
                <p className="text-gray-900 dark:text-white">{fact.text}</p>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  {formatTimestamp(fact.timestamp)}
                </p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Recent Contradictions */}
      {recentContradictions.length > 0 && (
        <div>
          <h4 className="font-semibold text-gray-900 dark:text-white mb-2">
            Contradictions ({recentContradictions.length})
          </h4>
          <ul className="space-y-2">
            {recentContradictions.map((fact, idx) => (
              <li key={idx} className="bg-yellow-50 dark:bg-yellow-900/20 p-3 rounded text-sm">
                <p className="text-gray-700 dark:text-gray-300">
                  {inactiveFacts.find((f) => fact.contradictions.includes(f.text))?.text ||
                    'Previous'}
                  {' → '}
                  {fact.text}
                </p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Empty State */}
      {!activeFacts.length && !inactiveFacts.length && (
        <div className="text-center py-8 text-gray-500 dark:text-gray-400">
          No facts recorded yet
        </div>
      )}
    </div>
  );
};

export const UserStatePanel: React.FC = () => {
  const [activeUser, setActiveUser] = useState('user_1');

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow-md p-6">
      <h2 className="text-2xl font-bold mb-6 text-gray-900 dark:text-white">
        User State Panel
      </h2>

      {/* Tabs */}
      <div className="flex border-b border-gray-200 dark:border-gray-700 mb-6 overflow-x-auto">
        {USERS.map((user) => (
          <button
            key={user}
            onClick={() => setActiveUser(user)}
            className={`px-4 py-2 font-medium whitespace-nowrap transition-colors ${
              activeUser === user
                ? 'border-b-2 border-blue-500 text-blue-600 dark:text-blue-400'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-300'
            }`}
          >
            {user}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      <div className="min-h-96">
        {USERS.map((user) => (
          <div key={user} className={activeUser === user ? 'block' : 'hidden'}>
            <UserTab user_id={user} isActive={activeUser === user} />
          </div>
        ))}
      </div>
    </div>
  );
};
