/**
 * Unit tests for useCopyToClipboard hook — portal ROI refactor Wave 4.
 *
 * Hook contract from `_common/hooks/useCopyToClipboard.js`:
 *   - copy(text, key?) writes `text` to the clipboard. ON SUCCESS flips
 *     `copied` to true (and `copiedKey` to `key` for keyed buttons),
 *     then auto-resets after `timeout` ms (default 2000). Returns the
 *     write promise (await-safe).
 *   - A rejected / unavailable clipboard is swallowed: `copied` stays
 *     false and no unhandled rejection escapes.
 *   - reset() clears the indicator + timer immediately.
 *   - The pending reset timer is cleared on unmount (no setState leak).
 *
 * Replaces the hand-rolled writeText + setCopied + setTimeout block that
 * had been re-implemented across 18 portal tools.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useCopyToClipboard } from '../src/interactive/tools/_common/hooks/useCopyToClipboard.js';

// jsdom ships no navigator.clipboard — install a mock writeText we can
// assert on and swap between resolve / reject per test.
let writeText: ReturnType<typeof vi.fn>;

function installClipboard(fn: ReturnType<typeof vi.fn>) {
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: fn },
    configurable: true,
    writable: true,
  });
}

describe('useCopyToClipboard', () => {
  beforeEach(() => {
    // Only fake the timer functions the hook uses — leaves Promise
    // microtasks real so `await copy(...)` resolves normally.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
    writeText = vi.fn().mockResolvedValue(undefined);
    installClipboard(writeText);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  // ─────────────────────────────────────────────────────────────────
  // Happy path — boolean indicator
  // ─────────────────────────────────────────────────────────────────

  it('starts with copied=false and copiedKey=null', () => {
    const { result } = renderHook(() => useCopyToClipboard());
    expect(result.current.copied).toBe(false);
    expect(result.current.copiedKey).toBe(null);
  });

  it('writes the given text to the clipboard', async () => {
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy('da-tools init');
    });
    expect(writeText).toHaveBeenCalledWith('da-tools init');
  });

  it('flips copied=true after a successful write', async () => {
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy('hello');
    });
    expect(result.current.copied).toBe(true);
  });

  it('resets copied=false after the default 2000ms window', async () => {
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy('hello');
    });
    expect(result.current.copied).toBe(true);

    act(() => {
      vi.advanceTimersByTime(1999);
    });
    expect(result.current.copied).toBe(true);

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current.copied).toBe(false);
  });

  it('honours a custom timeout (e.g. 2500ms, playground share-link)', async () => {
    const { result } = renderHook(() => useCopyToClipboard(2500));
    await act(async () => {
      await result.current.copy('link');
    });

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current.copied).toBe(true); // still on at 2000

    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(result.current.copied).toBe(false);
  });

  // ─────────────────────────────────────────────────────────────────
  // Keyed variant (template-gallery per-item buttons)
  // ─────────────────────────────────────────────────────────────────

  it('tracks copiedKey for keyed copies and resets it after timeout', async () => {
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy('yaml-for-tpl-1', 'tpl-1');
    });
    expect(result.current.copiedKey).toBe('tpl-1');
    expect(result.current.copied).toBe(true);

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current.copiedKey).toBe(null);
    expect(result.current.copied).toBe(false);
  });

  it('moves copiedKey to the latest item when a different key is copied', async () => {
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy('a', 'tpl-1');
    });
    expect(result.current.copiedKey).toBe('tpl-1');

    await act(async () => {
      await result.current.copy('b', 'tpl-2');
    });
    expect(result.current.copiedKey).toBe('tpl-2');
  });

  // ─────────────────────────────────────────────────────────────────
  // reset() + re-copy rescheduling
  // ─────────────────────────────────────────────────────────────────

  it('reset() clears the indicator immediately and cancels the timer', async () => {
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy('x', 'k');
    });
    expect(result.current.copied).toBe(true);

    act(() => {
      result.current.reset();
    });
    expect(result.current.copied).toBe(false);
    expect(result.current.copiedKey).toBe(null);

    // Advancing past the old window must not flip anything back.
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current.copied).toBe(false);
  });

  it('re-copying before the window ends reschedules the reset', async () => {
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy('a');
    });
    act(() => {
      vi.advanceTimersByTime(1500);
    });
    expect(result.current.copied).toBe(true);

    // Second copy restarts the 2000ms window.
    await act(async () => {
      await result.current.copy('b');
    });
    act(() => {
      vi.advanceTimersByTime(1500);
    });
    // Only 1500ms into the fresh window — still on (the first timer,
    // which would have fired at 2000ms total, must have been cancelled).
    expect(result.current.copied).toBe(true);

    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(result.current.copied).toBe(false);
  });

  // ─────────────────────────────────────────────────────────────────
  // Unmount cleanup — no setState-on-unmounted leak
  // ─────────────────────────────────────────────────────────────────

  it('clears the pending reset timer on unmount', async () => {
    const { result, unmount } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy('x');
    });
    // One pending reset timer is scheduled.
    expect(vi.getTimerCount()).toBe(1);

    unmount();
    // Cleanup must have cleared it — no leaked timer to fire setState
    // on the unmounted component.
    expect(vi.getTimerCount()).toBe(0);

    // Advancing past the window must not throw.
    expect(() => {
      act(() => {
        vi.advanceTimersByTime(2000);
      });
    }).not.toThrow();
  });

  // ─────────────────────────────────────────────────────────────────
  // Rejection safety
  // ─────────────────────────────────────────────────────────────────

  it('stays copied=false and does not throw when writeText rejects', async () => {
    installClipboard(vi.fn().mockRejectedValue(new Error('permission denied')));
    const { result } = renderHook(() => useCopyToClipboard());

    await act(async () => {
      await expect(result.current.copy('x', 'k')).resolves.toBeUndefined();
    });
    expect(result.current.copied).toBe(false);
    expect(result.current.copiedKey).toBe(null);
  });

  it('swallows a missing clipboard API (insecure context / jsdom)', async () => {
    // Remove the clipboard entirely — writeText access throws synchronously.
    Object.defineProperty(navigator, 'clipboard', {
      value: undefined,
      configurable: true,
      writable: true,
    });
    const { result } = renderHook(() => useCopyToClipboard());

    await act(async () => {
      await expect(result.current.copy('x')).resolves.toBeUndefined();
    });
    expect(result.current.copied).toBe(false);
  });
});
