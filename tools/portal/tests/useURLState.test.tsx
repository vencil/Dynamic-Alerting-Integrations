/**
 * Unit tests for useURLState hook — Vitest next-batch (after TRK-232b).
 *
 * Hook contract from `_common/hooks/useURLState.js`:
 *   - Initial state read from window.location.search; keys not present
 *     default to "".
 *   - setKey(key, value) updates React state AND writes to URL via
 *     history.replaceState (no new history entry, no scroll jump).
 *   - Empty values remove the key from the URL.
 *   - reset() clears all tracked keys.
 *   - popstate event re-syncs state from URL.
 *
 * Used by tenant-manager filter UI (PR-2b) so users can bookmark and
 * share filtered views.
 *
 * jsdom provides window.location / URLSearchParams / history APIs;
 * each test resets the URL via history.replaceState in beforeEach
 * so tests are isolated.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useURLState } from '../src/interactive/tools/_common/hooks/useURLState.js';

const ORIGINAL_PATH = '/test/page';

describe('useURLState', () => {
  beforeEach(() => {
    // Start every test from a known-clean URL: /test/page (no query, no
    // hash). Without this, state from a previous test's history.replaceState
    // would leak into the next.
    window.history.replaceState(null, '', ORIGINAL_PATH);
  });

  afterEach(() => {
    window.history.replaceState(null, '', ORIGINAL_PATH);
  });

  // ─────────────────────────────────────────────────────────────────
  // Initial state
  // ─────────────────────────────────────────────────────────────────

  it('returns empty strings for tracked keys not present in URL', () => {
    const { result } = renderHook(() => useURLState(['q', 'env']));
    expect(result.current.state).toEqual({ q: '', env: '' });
  });

  it('reads initial state from URL on mount', () => {
    window.history.replaceState(null, '', ORIGINAL_PATH + '?q=mariadb&env=prod');
    const { result } = renderHook(() => useURLState(['q', 'env']));
    expect(result.current.state).toEqual({ q: 'mariadb', env: 'prod' });
  });

  it('defaults missing keys to empty string when URL has only some', () => {
    window.history.replaceState(null, '', ORIGINAL_PATH + '?q=hello');
    const { result } = renderHook(() => useURLState(['q', 'env', 'tier']));
    expect(result.current.state).toEqual({ q: 'hello', env: '', tier: '' });
  });

  it('ignores URL keys not in the tracked list', () => {
    window.history.replaceState(null, '', ORIGINAL_PATH + '?q=a&unrelated=b');
    const { result } = renderHook(() => useURLState(['q']));
    expect(result.current.state).toEqual({ q: 'a' });
  });

  // ─────────────────────────────────────────────────────────────────
  // setKey
  // ─────────────────────────────────────────────────────────────────

  it('setKey updates the React state', () => {
    const { result } = renderHook(() => useURLState(['q']));
    act(() => result.current.setKey('q', 'hello'));
    expect(result.current.state).toEqual({ q: 'hello' });
  });

  it('setKey writes to the URL via replaceState', () => {
    const { result } = renderHook(() => useURLState(['q', 'env']));
    act(() => result.current.setKey('env', 'staging'));
    expect(window.location.search).toBe('?env=staging');
  });

  it('setKey with empty string removes the key from the URL', () => {
    window.history.replaceState(null, '', ORIGINAL_PATH + '?q=hello&env=prod');
    const { result } = renderHook(() => useURLState(['q', 'env']));
    act(() => result.current.setKey('q', ''));
    // URLSearchParams normalisation may reorder keys; assert presence/absence
    // rather than exact substring.
    const params = new URLSearchParams(window.location.search);
    expect(params.has('q')).toBe(false);
    expect(params.get('env')).toBe('prod');
  });

  it('setKey preserves the pathname', () => {
    window.history.replaceState(null, '', '/some/deep/page');
    const { result } = renderHook(() => useURLState(['q']));
    act(() => result.current.setKey('q', 'foo'));
    expect(window.location.pathname).toBe('/some/deep/page');
  });

  it('setKey preserves the hash', () => {
    window.history.replaceState(null, '', ORIGINAL_PATH + '#section-2');
    const { result } = renderHook(() => useURLState(['q']));
    act(() => result.current.setKey('q', 'foo'));
    expect(window.location.hash).toBe('#section-2');
  });

  it('uses replaceState (not pushState) so history depth does not grow', () => {
    const { result } = renderHook(() => useURLState(['q']));
    const lengthBefore = window.history.length;
    act(() => result.current.setKey('q', 'a'));
    act(() => result.current.setKey('q', 'b'));
    act(() => result.current.setKey('q', 'c'));
    // pushState would add 3 entries; replaceState adds none.
    expect(window.history.length).toBe(lengthBefore);
  });

  // ─────────────────────────────────────────────────────────────────
  // reset
  // ─────────────────────────────────────────────────────────────────

  it('reset clears all tracked keys from state', () => {
    window.history.replaceState(null, '', ORIGINAL_PATH + '?q=a&env=b&tier=c');
    const { result } = renderHook(() => useURLState(['q', 'env', 'tier']));
    act(() => result.current.reset());
    expect(result.current.state).toEqual({ q: '', env: '', tier: '' });
  });

  it('reset removes all tracked keys from URL', () => {
    window.history.replaceState(null, '', ORIGINAL_PATH + '?q=a&env=b');
    const { result } = renderHook(() => useURLState(['q', 'env']));
    act(() => result.current.reset());
    expect(window.location.search).toBe('');
  });

  it('reset preserves URL params NOT in the tracked list', () => {
    window.history.replaceState(null, '', ORIGINAL_PATH + '?q=a&keepme=yes');
    const { result } = renderHook(() => useURLState(['q']));
    act(() => result.current.reset());
    const params = new URLSearchParams(window.location.search);
    expect(params.has('q')).toBe(false);
    expect(params.get('keepme')).toBe('yes');
  });

  // ─────────────────────────────────────────────────────────────────
  // popstate sync (back/forward button)
  // ─────────────────────────────────────────────────────────────────

  it('re-reads state from URL when popstate fires', () => {
    const { result } = renderHook(() => useURLState(['q']));
    expect(result.current.state).toEqual({ q: '' });

    // Simulate back-button: URL changes externally, then popstate fires.
    act(() => {
      window.history.replaceState(null, '', ORIGINAL_PATH + '?q=from-history');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });

    expect(result.current.state).toEqual({ q: 'from-history' });
  });

  it('removes popstate listener on unmount', () => {
    const { result, unmount } = renderHook(() => useURLState(['q']));
    unmount();
    // After unmount, dispatching popstate must not crash; if the listener
    // wasn't removed, setState on an unmounted component would log a React
    // warning (and in stricter modes, fail).
    act(() => {
      window.history.replaceState(null, '', ORIGINAL_PATH + '?q=after-unmount');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    // result is the LAST render's value before unmount — should still be
    // the empty initial state.
    expect(result.current.state).toEqual({ q: '' });
  });

  // ─────────────────────────────────────────────────────────────────
  // Edge cases
  // ─────────────────────────────────────────────────────────────────

  it('handles empty keys array without crashing', () => {
    const { result } = renderHook(() => useURLState([]));
    expect(result.current.state).toEqual({});
  });

  it('handles URL-encoded values correctly', () => {
    window.history.replaceState(null, '', ORIGINAL_PATH + '?q=hello%20world');
    const { result } = renderHook(() => useURLState(['q']));
    // URLSearchParams.get() decodes automatically.
    expect(result.current.state.q).toBe('hello world');
  });
});
