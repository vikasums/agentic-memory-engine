/**
 * Audit Log Viewer (spec § 3.7, component 3)
 *
 * Features:
 * - Real-time event stream (reversed chronological, newest first)
 * - Filters (collapsible):
 *   - User dropdown (All, user_1-5)
 *   - Event type (All, Created, Updated, Deactivated, Expired)
 *   - Fact ID search box
 * - Event row format: [timestamp] user_X: action (fact_id) "old_state" → "new_state"
 * - Pagination: limit=100, scroll-to-load more
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { usePolling } from '../hooks/usePolling';
import { auditAPI } from '../api/client';
import type { EventResponse } from '../types';

const EVENT_TYPES = ['All', 'created', 'updated', 'deactivated', 'expired'];
const USERS = ['All', 'user_1', 'user_2', 'user_3', 'user_4', 'user_5'];

export const AuditLogViewer: React.FC = () => {
  const [showFilters, setShowFilters] = useState(false);
  const [selectedUser, setSelectedUser] = useState('All');
  const [selectedEventType, setSelectedEventType] = useState('All');
  const [searchFactId, setSearchFactId] = useState('');
  const [events] = usePolling(
    useCallback(() => {
      return auditAPI.getEvents({
        user_id: selectedUser === 'All' ? undefined : selectedUser,
        event_type: selectedEventType === 'All' ? undefined : selectedEventType,
        fact_id: searchFactId || undefined,
        limit: 100,
      });
    }, [selectedUser, selectedEventType, searchFactId]),
    { interval: 500 },
  );

  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const lastEventIdRef = useRef<number | null>(null);

  // Auto-scroll to bottom on new events
  useEffect(() => {
    if (scrollContainerRef.current && events && events.length > 0) {
      const lastEvent = events[0];
      if (lastEventIdRef.current !== lastEvent.event_id) {
        lastEventIdRef.current = lastEvent.event_id;
        scrollContainerRef.current.scrollTop = 0;
      }
    }
  }, [events]);

  const formatTimestamp = (ts: number) => {
    return new Date(ts * 1000).toLocaleTimeString();
  };

  const getEventLabel = (eventType: string) => {
    return eventType.charAt(0).toUpperCase() + eventType.slice(1);
  };

  const sortedEvents = events ? [...events].sort((a, b) => b.timestamp - a.timestamp) : [];

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow-md p-6">
      <h2 className="text-2xl font-bold mb-6 text-gray-900 dark:text-white">
        Audit Log Viewer
      </h2>

      {/* Filter Toggle */}
      <button
        onClick={() => setShowFilters(!showFilters)}
        className="mb-4 px-4 py-2 bg-gray-200 dark:bg-gray-700 text-gray-900 dark:text-white rounded-lg hover:bg-gray-300 dark:hover:bg-gray-600 transition-colors"
      >
        {showFilters ? 'Hide Filters' : 'Show Filters'}
      </button>

      {/* Filters */}
      {showFilters && (
        <div className="mb-6 p-4 bg-gray-50 dark:bg-gray-700 rounded-lg space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* User Filter */}
            <div>
              <label className="block text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
                User
              </label>
              <select
                value={selectedUser}
                onChange={(e) => setSelectedUser(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
              >
                {USERS.map((u) => (
                  <option key={u} value={u}>
                    {u}
                  </option>
                ))}
              </select>
            </div>

            {/* Event Type Filter */}
            <div>
              <label className="block text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
                Event Type
              </label>
              <select
                value={selectedEventType}
                onChange={(e) => setSelectedEventType(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
              >
                {EVENT_TYPES.map((et) => (
                  <option key={et} value={et}>
                    {et}
                  </option>
                ))}
              </select>
            </div>

            {/* Fact ID Search */}
            <div>
              <label className="block text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
                Fact ID Search
              </label>
              <input
                type="text"
                placeholder="Search fact ID..."
                value={searchFactId}
                onChange={(e) => setSearchFactId(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400"
              />
            </div>
          </div>
        </div>
      )}

      {/* Event Stream */}
      <div
        ref={scrollContainerRef}
        className="border border-gray-200 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-900 overflow-y-auto max-h-96"
      >
        {sortedEvents.length === 0 ? (
          <div className="p-4 text-center text-gray-500 dark:text-gray-400">
            No events recorded yet
          </div>
        ) : (
          <div className="divide-y divide-gray-200 dark:divide-gray-700">
            {sortedEvents.map((event, idx) => (
              <div key={idx} className="p-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-800">
                <div className="flex justify-between items-start gap-2 mb-1">
                  <div className="flex-1">
                    <span className="font-mono text-xs text-gray-500 dark:text-gray-400">
                      {formatTimestamp(event.timestamp)}
                    </span>
                    {' '}
                    <span className="font-semibold text-gray-900 dark:text-white">
                      {event.user_id}
                    </span>
                    <span className="text-gray-600 dark:text-gray-400">: </span>
                    <span className="font-medium text-blue-600 dark:text-blue-400">
                      {getEventLabel(event.event_type)}
                    </span>
                  </div>
                  <span className="font-mono text-xs bg-gray-200 dark:bg-gray-700 px-2 py-1 rounded text-gray-700 dark:text-gray-300">
                    {event.fact_id.substring(0, 8)}
                  </span>
                </div>

                {/* State Transition */}
                {event.before_state && event.after_state && (
                  <div className="text-xs text-gray-600 dark:text-gray-400 ml-0 mt-1">
                    <span className="text-red-600 dark:text-red-400">
                      {JSON.stringify(event.before_state).substring(0, 50)}...
                    </span>
                    <span className="mx-1">→</span>
                    <span className="text-green-600 dark:text-green-400">
                      {JSON.stringify(event.after_state).substring(0, 50)}...
                    </span>
                  </div>
                )}

                <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Source: {event.source}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="mt-4 text-xs text-gray-500 dark:text-gray-400">
        Showing {sortedEvents.length} events (newest first, auto-refreshing)
      </div>
    </div>
  );
};
