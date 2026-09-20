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
  const intervalIdRef = useRef<NodeJS.Timeout | null>(null);
  const isMountedRef = useRef(true);

  const poll = useCallback(async () => {
    if (!enabled) return;

    try {
      setLoading(true);
      const result = await fetchFn();
      if (isMountedRef.current) {
        setData(result);
        setError(null);
      }
    } catch (err) {
      if (isMountedRef.current) {
        const error = err instanceof Error ? err : new Error(String(err));
        setError(error);
        if (onError) onError(error);
      }
    } finally {
      if (isMountedRef.current) {
        setLoading(false);
      }
    }
  }, [fetchFn, enabled, onError]);

  useEffect(() => {
    if (!enabled) return;

    // Poll immediately
    poll();

    // Set up interval
    intervalIdRef.current = setInterval(poll, interval);

    return () => {
      if (intervalIdRef.current) {
        clearInterval(intervalIdRef.current);
      }
    };
  }, [poll, interval, enabled]);

  useEffect(() => {
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  return [data, loading, error];
}
