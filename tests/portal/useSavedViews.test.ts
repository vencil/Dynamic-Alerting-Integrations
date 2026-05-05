/**
 * Unit tests for `useSavedViews` hook — TECH-DEBT-030b first-batch.
 *
 * The hook wraps /api/v1/views CRUD. Tests use Vitest's `vi.fn()` to
 * stub `global.fetch`, exercise initial load / 404 (demo mode) / save
 * / remove. Real network never touched.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useSavedViews } from '../../docs/interactive/tools/tenant-manager/hooks/useSavedViews.js';

function mockFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    return handler(String(url), init);
  }) as unknown as typeof fetch;
}

describe('useSavedViews', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('initial load: fetches /api/v1/views and exposes returned views', async () => {
    mockFetch(() =>
      new Response(
        JSON.stringify({
          views: {
            'prod-finance': { label: 'Production Finance', filters: { environment: 'production' } },
          },
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ),
    );

    const { result } = renderHook(() => useSavedViews());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.reachable).toBe(true);
    expect(Object.keys(result.current.views)).toEqual(['prod-finance']);
  });

  it('404 → reachable=false (demo mode contract)', async () => {
    mockFetch(() => new Response('not found', { status: 404 }));

    const { result } = renderHook(() => useSavedViews());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.reachable).toBe(false);
    expect(result.current.views).toEqual({});
  });

  it('save() PUTs JSON body and reloads on success', async () => {
    let callCount = 0;
    let putBody: unknown = null;
    mockFetch((url, init) => {
      callCount += 1;
      if (init?.method === 'PUT') {
        putBody = init.body ? JSON.parse(String(init.body)) : null;
        return new Response('{}', { status: 200 });
      }
      // GET reload — empty on first call, populated after save
      const body = callCount === 1
        ? { views: {} }
        : { views: { 'my-view': { label: 'My View', filters: {} } } };
      return new Response(JSON.stringify(body), { status: 200 });
    });

    const { result } = renderHook(() => useSavedViews());
    await waitFor(() => expect(result.current.loading).toBe(false));

    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.save('my-view', 'My View', '', { environment: 'prod' });
    });

    expect(ok).toBe(true);
    expect((putBody as { label: string }).label).toBe('My View');
    expect((putBody as { filters: Record<string, string> }).filters).toEqual({ environment: 'prod' });
    await waitFor(() => expect(Object.keys(result.current.views)).toContain('my-view'));
  });

  it('save() rejects invalid id charset and calls onError', async () => {
    mockFetch(() => new Response(JSON.stringify({ views: {} }), { status: 200 }));

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

  it('remove() sends DELETE and reloads', async () => {
    let deleteCalled = false;
    mockFetch((url, init) => {
      if (init?.method === 'DELETE') {
        deleteCalled = true;
        return new Response(null, { status: 204 });
      }
      return new Response(JSON.stringify({ views: {} }), { status: 200 });
    });

    const { result } = renderHook(() => useSavedViews());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.remove('some-view');
    });

    expect(deleteCalled).toBe(true);
  });
});
