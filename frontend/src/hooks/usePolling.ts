/**
 * Custom hook for polling data at regular intervals (500ms default per spec)
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export interface UsePollingOptions {
  interval?: number;
  enabled?: boolean;
  onError?: (error: Error) => void;
}

export function usePolling<T>(
  fetchFn: () => Promise<T>,
  options: UsePollingOptions = {},
): [T | null, boolean, Error | null] {
  const { interval = 500, enabled = true, onError } = options;
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const isMountedRef = useRef(true);
  const fetchFnRef = useRef(fetchFn);
  const onErrorRef = useRef(onError);

  // Keep the latest callbacks without making `poll` a new function each render,
  // which would tear down and restart the interval on every state update.
  fetchFnRef.current = fetchFn;
  onErrorRef.current = onError;

  // StrictMode mounts, unmounts, then remounts in dev; the flag must be set on
  // every mount, not just initialised once, or updates are dropped after remount.
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const poll = useCallback(async () => {
    try {
      setLoading(true);
      const result = await fetchFnRef.current();
      if (!isMountedRef.current) return;
      setData(result);
      setError(null);
    } catch (err) {
      if (!isMountedRef.current) return;
      const pollError = err instanceof Error ? err : new Error(String(err));
      setError(pollError);
      if (onErrorRef.current) onErrorRef.current(pollError);
    } finally {
      if (isMountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;

    poll();
    const intervalId = setInterval(poll, interval);

    return () => clearInterval(intervalId);
  }, [poll, interval, enabled]);

  return [data, loading, error];
}
