/**
 * Regression tests for usePolling.
 *
 * The hook previously cleared its "is mounted" flag in a cleanup that never ran
 * again on remount. Under React StrictMode (which mounts, unmounts, then
 * remounts in development) that left the flag false forever, so every fetch
 * resolved but no state update was ever applied and consumers saw null data.
 */

import React, { StrictMode } from 'react';
import { render, screen, waitFor, act } from '@testing-library/react';
import { usePolling } from '../hooks/usePolling';

const Probe: React.FC<{ fetchFn: () => Promise<string>; interval?: number }> = ({
  fetchFn,
  interval = 50,
}) => {
  const [data, , error] = usePolling(fetchFn, { interval });
  return (
    <div>
      <span data-testid="data">{data ?? 'null'}</span>
      <span data-testid="error">{error ? error.message : 'none'}</span>
    </div>
  );
};

describe('usePolling', () => {
  it('applies fetched data when rendered inside StrictMode', async () => {
    const fetchFn = jest.fn().mockResolvedValue('payload');

    render(
      <StrictMode>
        <Probe fetchFn={fetchFn} />
      </StrictMode>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('data')).toHaveTextContent('payload');
    });
  });

  it('applies fetched data outside StrictMode', async () => {
    const fetchFn = jest.fn().mockResolvedValue('plain');

    render(<Probe fetchFn={fetchFn} />);

    await waitFor(() => {
      expect(screen.getByTestId('data')).toHaveTextContent('plain');
    });
  });

  it('exposes errors and keeps polling', async () => {
    const fetchFn = jest.fn().mockRejectedValue(new Error('boom'));

    render(
      <StrictMode>
        <Probe fetchFn={fetchFn} />
      </StrictMode>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('error')).toHaveTextContent('boom');
    });
  });

  it('stops fetching once enabled turns false', async () => {
    const fetchFn = jest.fn().mockResolvedValue('tick');

    const Toggleable: React.FC<{ enabled: boolean }> = ({ enabled }) => {
      const [data] = usePolling<string>(fetchFn, { interval: 20, enabled });
      return <span data-testid="data">{data ?? 'null'}</span>;
    };

    const { rerender } = render(<Toggleable enabled />);
    await waitFor(() => expect(fetchFn).toHaveBeenCalled());

    rerender(<Toggleable enabled={false} />);
    const callsWhenDisabled = fetchFn.mock.calls.length;

    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(fetchFn).toHaveBeenCalledTimes(callsWhenDisabled);
  });

  it('does not restart the interval when the caller passes new inline callbacks', async () => {
    jest.useFakeTimers();
    const fetchFn = jest.fn().mockResolvedValue('tick');

    const Wrapper: React.FC = () => {
      const [, force] = React.useState(0);
      // A new inline onError identity on every render used to invalidate the
      // polling effect and trigger an extra immediate fetch each time.
      const [data] = usePolling<string>(fetchFn, { interval: 1000, onError: () => {} });
      React.useEffect(() => {
        force((n) => (n === 0 ? 1 : n));
      }, [data]);
      return <span data-testid="data">{data ?? 'null'}</span>;
    };

    render(<Wrapper />);

    await act(async () => {
      await Promise.resolve();
    });

    const callsAfterMount = fetchFn.mock.calls.length;
    expect(callsAfterMount).toBeLessThanOrEqual(2);

    jest.useRealTimers();
  });
});
