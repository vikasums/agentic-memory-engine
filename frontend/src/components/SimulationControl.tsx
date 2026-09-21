/**
 * Simulation Control Panel (spec § 3.7, component 1)
 *
 * Features:
 * - Start/Stop/Pause buttons
 * - Duration selector (1/2/4/6 minute radio buttons)
 * - Progress bar (percent complete + elapsed/remaining time)
 */

import React, { useState } from 'react';
import { simulationAPI } from '../api/client';
import type { StatusResponse } from '../types';

interface SimulationControlProps {
  status?: StatusResponse | null;
  loading?: boolean;
  error?: Error | null;
  onRunStart?: (run_id: string) => void;
  onRunStop?: () => void;
}

const DURATIONS = [
  { label: '1 minute', seconds: 60 },
  { label: '2 minutes', seconds: 120 },
  { label: '4 minutes', seconds: 240 },
  { label: '6 minutes', seconds: 360 },
];

export const SimulationControl: React.FC<SimulationControlProps> = ({
  status,
  loading = false,
  error,
  onRunStart,
  onRunStop,
}) => {
  const [duration, setDuration] = useState(60);
  const [isStarting, setIsStarting] = useState(false);
  const [isStopping, setIsStopping] = useState(false);

  const handleStart = async () => {
    if (loading || isStarting) return;
    setIsStarting(true);

    try {
      const result = await simulationAPI.startRun(duration);
      if (onRunStart) onRunStart(result.run_id);
    } catch (err) {
      console.error('Failed to start simulation:', err);
    } finally {
      setIsStarting(false);
    }
  };

  const handleStop = async () => {
    if (!status?.run_id || isStopping) return;
    setIsStopping(true);

    try {
      await simulationAPI.stopRun(status.run_id);
      if (onRunStop) onRunStop();
    } catch (err) {
      console.error('Failed to stop simulation:', err);
    } finally {
      setIsStopping(false);
    }
  };

  const isRunning = status?.status === 'in_progress';
  const progress = status?.progress;
  const percentComplete = progress?.percent_complete ?? 0;

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}m ${secs}s`;
  };

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow-md p-6">
      <h2 className="text-2xl font-bold mb-6 text-gray-900 dark:text-white">
        Simulation Control
      </h2>

      {error && (
        <div className="mb-4 p-3 bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-100 rounded">
          {error.message}
        </div>
      )}

      {/* Duration Selector */}
      <div className="mb-6">
        <h3 className="text-lg font-semibold mb-3 text-gray-700 dark:text-gray-300">
          Select Duration
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {DURATIONS.map((d) => (
            <label
              key={d.seconds}
              className="flex items-center cursor-pointer p-3 border-2 rounded-lg transition-colors"
              style={{
                borderColor: duration === d.seconds ? '#3b82f6' : '#e5e7eb',
                backgroundColor:
                  duration === d.seconds
                    ? 'rgba(59, 130, 246, 0.1)'
                    : 'transparent',
              }}
            >
              <input
                type="radio"
                name="duration"
                value={d.seconds}
                checked={duration === d.seconds}
                onChange={(e) => setDuration(Number(e.target.value))}
                disabled={isRunning}
                className="mr-2"
              />
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {d.label}
              </span>
            </label>
          ))}
        </div>
      </div>

      {/* Progress Bar */}
      {progress && (
        <div className="mb-6 space-y-3">
          <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3 overflow-hidden">
            <div
              className="bg-blue-500 h-3 transition-all duration-300 ease-out"
              style={{ width: `${percentComplete}%` }}
            />
          </div>
          <div className="flex justify-between text-sm text-gray-600 dark:text-gray-400">
            <span>{Math.round(percentComplete)}% Complete</span>
            <span>
              {formatTime(progress.elapsed_seconds)} / {formatTime(progress.remaining_seconds)}
            </span>
          </div>
          <div className="text-xs text-gray-500 dark:text-gray-400">
            Events: {progress.events_played} played, {progress.errors} errors
          </div>
        </div>
      )}

      {/* Control Buttons */}
      <div className="flex gap-3">
        <button
          onClick={handleStart}
          disabled={isRunning || isStarting || loading}
          className="flex-1 bg-green-500 hover:bg-green-600 disabled:bg-gray-400 text-white font-semibold py-2 px-4 rounded-lg transition-colors"
        >
          {isStarting ? 'Starting...' : 'Start'}
        </button>

        <button
          onClick={handleStop}
          disabled={!isRunning || isStopping}
          className="flex-1 bg-red-500 hover:bg-red-600 disabled:bg-gray-400 text-white font-semibold py-2 px-4 rounded-lg transition-colors"
        >
          {isStopping ? 'Stopping...' : 'Stop'}
        </button>
      </div>

      {/* Status Display */}
      {status && (
        <div className="mt-6 p-3 bg-gray-100 dark:bg-gray-700 rounded-lg text-sm text-gray-700 dark:text-gray-300">
          <p>
            Run ID: <span className="font-mono font-semibold">{status.run_id}</span>
          </p>
          <p>Status: {status.status}</p>
        </div>
      )}
    </div>
  );
};
