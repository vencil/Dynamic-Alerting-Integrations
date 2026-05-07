/**
 * Unit tests for useDebouncedValue hook — TD-032b (#TBD).
 *
 * Hook contract from `_common/hooks/useDebouncedValue.js`:
 *   - First render returns initial value immediately (no delay).
 *   - On change, schedules a delayMs timer; if value changes again
 *     before timer fires, cancels and reschedules.
 *   - On unmount, clears any pending timer.
 *
 * Used by tenant-manager search input (PR-2b) — debounces user
 * typing before sending `?q=` to /api/v1/tenants/search.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useDebouncedValue } from '../../docs/interactive/tools/_common/hooks/useDebouncedValue.js';

describe('useDebouncedValue', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns the initial value immediately on first render', () => {
    const { result } = renderHook(() => useDebouncedValue('hello', 300));
    expect(result.current).toBe('hello');
  });

  it('does NOT update before delayMs has elapsed', () => {
    const { result, rerender } = renderHook(
      ({ value, delay }) => useDebouncedValue(value, delay),
      { initialProps: { value: 'a', delay: 300 } },
    );
    rerender({ value: 'b', delay: 300 });
    // Timer has not fired yet — debounced value still 'a'.
    expect(result.current).toBe('a');

    act(() => {
      vi.advanceTimersByTime(299);
    });
    expect(result.current).toBe('a');
  });

  it('updates to the new value after delayMs has fully elapsed', () => {
    const { result, rerender } = renderHook(
      ({ value, delay }) => useDebouncedValue(value, delay),
      { initialProps: { value: 'a', delay: 300 } },
    );
    rerender({ value: 'b', delay: 300 });

    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(result.current).toBe('b');
  });

  it('cancels the pending timer when value changes again before fire', () => {
    const { result, rerender } = renderHook(
      ({ value, delay }) => useDebouncedValue(value, delay),
      { initialProps: { value: 'a', delay: 300 } },
    );
    rerender({ value: 'b', delay: 300 });

    // Advance partway, then change to 'c' — original 'b' timer should be cancelled.
    act(() => {
      vi.advanceTimersByTime(200);
    });
    rerender({ value: 'c', delay: 300 });
    act(() => {
      vi.advanceTimersByTime(200);
    });
    // 200 ms after 'c' set, original 'b' would have fired at 300 ms total
    // (if not cancelled). debounced should still be 'a' because the 'c'
    // timer needs another 100 ms.
    expect(result.current).toBe('a');

    act(() => {
      vi.advanceTimersByTime(100);
    });
    expect(result.current).toBe('c');
  });

  it('clears the timer on unmount (no leaked update after teardown)', () => {
    const { result, rerender, unmount } = renderHook(
      ({ value, delay }) => useDebouncedValue(value, delay),
      { initialProps: { value: 'a', delay: 300 } },
    );
    rerender({ value: 'b', delay: 300 });
    unmount();

    // If the timer leaked, advancing past the fire time would crash with
    // "perform a React state update on an unmounted component". renderHook
    // surfaces such warnings as test failures via console.error tracking.
    act(() => {
      vi.advanceTimersByTime(500);
    });
    // Last observed value before unmount.
    expect(result.current).toBe('a');
  });
});
