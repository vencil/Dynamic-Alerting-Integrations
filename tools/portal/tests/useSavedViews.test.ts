/**
 * Unit tests for `useSavedViews` hook — TECH-DEBT-030b first-batch.
 *
 * The hook wraps /api/v1/views CRUD. Tests use Vitest's `vi.fn()` to
 * stub `global.fetch`, exercise initial load / 404 (demo mode) / 403 / save
 * / remove. Real network never touched.
 *
 * Every GET /api/v1/views body goes through `apiFixture('ViewResponse[]', …)`
 * (#2833): the list is an array of ViewResponse, and a fixture written in any
 * other shape fails at the fixture. The old `{ views: { [id]: … } }` fixtures
 * agreed only with the hook that read them, never with the API (#3231).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useSavedViews } from '../src/interactive/tools/tenant-manager/hooks/useSavedViews.js';
import { apiFixture } from './helpers/api-fixture';

// Built outside the fetch stubs on purpose: a fixture that does not fit
// must throw in the test body, not inside fetch (where the hook would
// swallow it as a network failure).
const listBody = (views: unknown[]) => JSON.stringify(apiFixture('ViewResponse[]', views));
const EMPTY_LIST = listBody([]);

type FetchCall = { method: string; url: string };

/** Stubs fetch and returns the list of calls it receives, as `{ method, url }`. */
function mockFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>): FetchCall[] {
  const calls: FetchCall[] = [];
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ method: init?.method || 'GET', url: String(url) });
    return handler(String(url), init);
  }) as unknown as typeof fetch;
  return calls;
}

describe('useSavedViews', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('initial load: fetches /api/v1/views and exposes returned views', async () => {
    const body = listBody([
      { id: 'prod-finance', label: 'Production Finance', filters: { environment: 'production' } },
      { id: 'critical-silent', label: 'Critical + Silent', filters: { tier: 'tier-1' } },
    ]);
    mockFetch(() =>
      new Response(body, { status: 200, headers: { 'content-type': 'application/json' } }),
    );

    const { result } = renderHook(() => useSavedViews());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.reachable).toBe(true);
    // The ids are the views' own ids, not the array positions '0' / '1'.
    expect(result.current.views.map((v: { id: string }) => v.id)).toEqual(['prod-finance', 'critical-silent']);
  });

  it('a 200 that is not a ViewResponse[] is reported, not read as an empty list (#3231)', async () => {
    // The shape the hook used to accept besides the array.
    mockFetch(() => new Response(JSON.stringify({ views: {} }), { status: 200 }));
    const onError = vi.fn();

    const { result } = renderHook(() => useSavedViews(onError));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.views).toEqual([]);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('not a list of views'));
  });

  it('404 → reachable=false (demo mode contract)', async () => {
    mockFetch(() => new Response('not found', { status: 404 }));

    const { result } = renderHook(() => useSavedViews());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.reachable).toBe(false);
    expect(result.current.views).toEqual([]);
  });

  it('403 → reachable=false, no onError (caller may not read saved views, #3160)', async () => {
    // An org-scoped "*" rule is refused by design: the panel hides
    // itself exactly as for a 404, and no error toast is raised.
    mockFetch(() => new Response(JSON.stringify({ error: 'forbidden' }), { status: 403 }));
    const onError = vi.fn();

    const { result } = renderHook(() => useSavedViews(onError));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.reachable).toBe(false);
    expect(result.current.views).toEqual([]);
    expect(onError).not.toHaveBeenCalled();
  });

  it('500 on list → onError called, panel stays reachable (contrast to 403)', async () => {
    mockFetch(() => new Response('boom', { status: 500 }));
    const onError = vi.fn();

    const { result } = renderHook(() => useSavedViews(onError));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.reachable).toBe(true);
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it('save() PUTs JSON body and reloads on success', async () => {
    let callCount = 0;
    let putBody: unknown = null;
    const afterSave = listBody([{ id: 'my-view', label: 'My View', filters: {} }]);
    const calls = mockFetch((url, init) => {
      callCount += 1;
      if (init?.method === 'PUT') {
        putBody = init.body ? JSON.parse(String(init.body)) : null;
        return new Response(JSON.stringify({ status: 'ok', view_id: 'my-view' }), { status: 200 });
      }
      // GET reload — empty on first call, populated after save
      return new Response(callCount === 1 ? EMPTY_LIST : afterSave, { status: 200 });
    });

    const { result } = renderHook(() => useSavedViews());
    await waitFor(() => expect(result.current.loading).toBe(false));

    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.save('my-view', 'My View', '', { environment: 'prod' });
    });

    expect(ok).toBe(true);
    // The PUT goes to the view's own id (#3231: not a list position such as /0).
    // The id charset ([a-zA-Z0-9_-]) leaves encodeURIComponent nothing to encode.
    expect(calls.filter(c => c.method === 'PUT')).toEqual([{ method: 'PUT', url: '/api/v1/views/my-view' }]);
    expect((putBody as { label: string }).label).toBe('My View');
    expect((putBody as { filters: Record<string, string> }).filters).toEqual({ environment: 'prod' });
    await waitFor(() => expect(result.current.views.map((v: { id: string }) => v.id)).toContain('my-view'));
  });

  it('save() rejects invalid id charset and calls onError', async () => {
    mockFetch(() => new Response(EMPTY_LIST, { status: 200 }));

    const onError = vi.fn();
    const { result } = renderHook(() => useSavedViews(onError));
    await waitFor(() => expect(result.current.loading).toBe(false));

    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.save('has spaces!', 'Label', '', {});
    });

    expect(ok).toBe(false);
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('letters, digits'));
  });

  it('remove() sends DELETE to /api/v1/views/<id> and reloads', async () => {
    const calls = mockFetch((url, init) => {
      if (init?.method === 'DELETE') {
        return new Response(JSON.stringify({ status: 'ok', view_id: 'some-view' }), { status: 200 });
      }
      return new Response(EMPTY_LIST, { status: 200 });
    });

    const { result } = renderHook(() => useSavedViews());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.remove('some-view');
    });

    // Initial list, the DELETE on the view's own id (#3231: not /0), the reload.
    expect(calls).toEqual([
      { method: 'GET', url: '/api/v1/views' },
      { method: 'DELETE', url: '/api/v1/views/some-view' },
      { method: 'GET', url: '/api/v1/views' },
    ]);
  });
});
