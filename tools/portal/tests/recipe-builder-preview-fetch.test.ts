/**
 * ADR-027 §5: the would-fire preview is asked of tenant-api under /api/ —
 * the browser never reaches the recipe-preview container, which reads no
 * identity and has no network port. tenant-api authorizes on the path's
 * tenant, so the tenant goes in the path and not in the body.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { defaultPreviewFetch } from '../src/interactive/tools/recipe-builder.jsx';

const RECIPE = { recipe: 'threshold', metric: 'q', threshold: '1:warning' };

function stubFetch(status: number, body: unknown) {
  const f = vi.fn(() => Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    statusText: 'x',
    json: () => Promise.resolve(body),
  } as any));
  vi.stubGlobal('fetch', f);
  return f;
}

afterEach(() => { vi.unstubAllGlobals(); });

describe('defaultPreviewFetch', () => {
  it('POSTs {recipe, scenario} to /api/v1/tenants/{id}/recipe-preview', async () => {
    const f = stubFetch(200, { supported: true, states: [], warnings: [] });
    const out = await defaultPreviewFetch('shop a/b', RECIPE, { value: 5 }, undefined);
    expect(out).toEqual({ supported: true, states: [], warnings: [] });
    const [url, init] = f.mock.calls[0] as any[];
    expect(url).toBe('/api/v1/tenants/shop%20a%2Fb/recipe-preview');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ recipe: RECIPE, scenario: { value: 5 } });
  });

  it('a refusal throws with the server text and the status', async () => {
    stubFetch(429, { error: 'the recipe preview gave no verdict this time; retry shortly', code: 'PREVIEW_BUSY', reason: 'busy' });
    await expect(defaultPreviewFetch('t', RECIPE, {}, undefined))
      .rejects.toMatchObject({ status: 429, message: expect.stringContaining('no verdict') });
  });

  it('401 sends the browser to sign in', async () => {
    stubFetch(401, { error: 'missing identity', code: 'UNAUTHORIZED' });
    window.sessionStorage.clear();
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, pathname: '/interactive/', search: '', hash: '', assign });
    await expect(defaultPreviewFetch('t', RECIPE, {}, undefined)).rejects.toMatchObject({ status: 401 });
    expect(assign).toHaveBeenCalledTimes(1);
  });
});
