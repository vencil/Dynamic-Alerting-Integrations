/**
 * ADR-027 §5, §9 item 4: every request the portal makes to the backend goes
 * through apiFetch, which refuses — before any request leaves — a path that
 * does not resolve, the way the browser will send it, to this origin under
 * /api/. oauth2-proxy routes /api/ to tenant-api and everything else to the
 * static files, so a backend path outside /api/ never reaches a backend.
 *
 * A 401 is handled in the same place: apiFetch sends the browser to sign in
 * (redirectToSignIn: the hold and the retry window apply), and the caller
 * asks leftForSignIn(resp) whether the page is leaving.
 *
 * fetchPageData is the one other way to fetch: a data file next to the page
 * (a bare file name), never anything under /api/.
 *
 * That nothing else calls fetch (or XMLHttpRequest, EventSource, WebSocket,
 * sendBeacon) is checked by network-calls-only-in-api-fetch.test.ts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  apiFetch, fetchPageData, leftForSignIn, ApiPathError,
} from '../src/interactive/tools/_common/api/api-fetch.js';
import { holdSignIn, SIGN_IN_ATTEMPT_KEY } from '../src/interactive/tools/_common/auth/sign-in.js';

const takePathRefusals = () => (globalThis as any).takePathRefusals() as Error[];

function stubFetch(status = 200) {
  const f = vi.fn(() => Promise.resolve({ ok: status >= 200 && status < 300, status } as any));
  vi.stubGlobal('fetch', f);
  return f;
}

let assign: ReturnType<typeof vi.fn>;
beforeEach(() => {
  window.sessionStorage.clear();
  assign = vi.fn();
  vi.stubGlobal('location', { ...window.location, pathname: '/interactive/', search: '?tool=x', hash: '', assign });
});
afterEach(() => { vi.unstubAllGlobals(); });

describe('apiFetch: the path', () => {
  it.each([
    '/api/v1/me',
    '/api/v1/tenants/a%2Fb/custom-alerts',
    '/api/v1/tenants/search?page_size=500&q=../..',
    '/api/v1/tenants/../groups', // stays under /api/ after the browser normalises it
  ])('sends %s as given', async (path) => {
    const f = stubFetch();
    const init = { method: 'PUT', body: '{}' };
    await apiFetch(path, init);
    expect(f).toHaveBeenCalledTimes(1);
    expect(f).toHaveBeenCalledWith(path, init);
  });

  it.each([
    ['outside /api/', '/preview'],
    ['/api without the slash', '/api'],
    ['a longer first segment', '/apix/v1/me'],
    ['dot segments out of /api/', '/api/../preview'],
    ['encoded dot segments', '/api/%2e%2e/preview'],
    ['mixed-case encoded dot segments', '/api/%2E%2e/preview'],
    ['relative to the page', 'api/v1/me'],
    ['another origin', 'https://evil.example/api/v1/me'],
    ['protocol-relative', '//evil.example/api/v1/me'],
    ['backslash protocol-relative', '/\\evil.example/api/v1/me'],
    ['this origin, absolute', 'http://localhost:3000/api/v1/me'],
    ['empty', ''],
  ])('refuses %s (%s) before any request, and reports it', async (_why, path) => {
    const f = stubFetch();
    await expect(apiFetch(path)).rejects.toBeInstanceOf(ApiPathError);
    expect(f).not.toHaveBeenCalled();
    expect(takePathRefusals()).toHaveLength(1);
  });

  it('a refusal the caller swallows still reaches the refusal handler', async () => {
    stubFetch();
    await apiFetch('/preview').catch(() => null); // what most callers do
    const [err] = takePathRefusals();
    expect(err).toBeInstanceOf(ApiPathError);
    expect(err.message).toContain('/preview');
  });

  it.each([
    ['undefined', undefined],
    ['a URL object', new URL('http://localhost:3000/api/v1/me')],
    ['a Request-like object', { url: '/api/v1/me' }],
  ])('refuses a non-string path (%s)', async (_why, path) => {
    const f = stubFetch();
    await expect(apiFetch(path as any)).rejects.toBeInstanceOf(ApiPathError);
    expect(f).not.toHaveBeenCalled();
    takePathRefusals();
  });

  it('refuses when the document base URL is another origin (<base href>)', async () => {
    const f = stubFetch();
    const base = document.createElement('base');
    base.href = 'https://evil.example/';
    document.head.appendChild(base);
    try {
      await expect(apiFetch('/api/v1/me')).rejects.toBeInstanceOf(ApiPathError);
    } finally {
      base.remove();
    }
    expect(f).not.toHaveBeenCalled();
    takePathRefusals();
  });

  it('a network failure is not a path refusal', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))));
    await expect(apiFetch('/api/v1/me')).rejects.toBeInstanceOf(TypeError);
    await expect(apiFetch('/api/v1/me')).rejects.not.toBeInstanceOf(ApiPathError);
  });
});

describe('apiFetch: a 401', () => {
  it('sends the browser to sign in, returning here, and returns the answer', async () => {
    stubFetch(401);
    const resp = await apiFetch('/api/v1/me');
    expect(resp.status).toBe(401);
    expect(assign).toHaveBeenCalledWith('/oauth2/start?rd=' + encodeURIComponent('/interactive/?tool=x'));
    expect(leftForSignIn(resp)).toBe(true);
  });

  it('stays while a hold is in place (a one-time token on screen)', async () => {
    stubFetch(401);
    const release = holdSignIn();
    try {
      const resp = await apiFetch('/api/v1/federation/tokens');
      expect(assign).not.toHaveBeenCalled();
      expect(leftForSignIn(resp)).toBe(false);
    } finally {
      release();
    }
  });

  it('stays when this tab left for sign-in moments ago (no loop through the IdP)', async () => {
    stubFetch(401);
    window.sessionStorage.setItem(SIGN_IN_ATTEMPT_KEY, String(Date.now()));
    const resp = await apiFetch('/api/v1/me');
    expect(assign).not.toHaveBeenCalled();
    expect(leftForSignIn(resp)).toBe(false);
  });

  it.each([200, 403, 404, 500])('does not sign in on %i', async (status) => {
    stubFetch(status);
    const resp = await apiFetch('/api/v1/me');
    expect(assign).not.toHaveBeenCalled();
    expect(leftForSignIn(resp)).toBe(false);
  });

  it('a second 401 while the page is already leaving is not a refusal', async () => {
    stubFetch(401);
    const [a, b] = await Promise.all([apiFetch('/api/v1/prs'), apiFetch('/api/v1/me')]);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(leftForSignIn(a)).toBe(true);
    expect(leftForSignIn(b)).toBe(true);
  });

  it('leftForSignIn is false for anything apiFetch did not return', () => {
    expect(leftForSignIn({ status: 401 } as any)).toBe(false);
    expect(leftForSignIn(null as any)).toBe(false);
  });
});

describe('fetchPageData', () => {
  it.each(['platform-data.json', 'template-data.json', 'recommendation-data.json', 'x.yaml'])(
    'fetches %s next to the page', async (name) => {
      const f = stubFetch();
      await fetchPageData(name);
      expect(f).toHaveBeenCalledWith(name);
    },
  );

  it.each([
    '/platform-data.json', '../platform-data.json', 'data/platform-data.json',
    'https://evil.example/x.json', '//evil.example/x.json', 'x.js', 'platform-data.json?x=1', '',
  ])('refuses %s before any request', async (name) => {
    const f = stubFetch();
    await expect(fetchPageData(name)).rejects.toBeInstanceOf(ApiPathError);
    expect(f).not.toHaveBeenCalled();
    expect(takePathRefusals()).toHaveLength(1);
  });

  it('refuses a data file that would resolve under /api/ (a page served from there)', async () => {
    const f = stubFetch();
    vi.stubGlobal('location', { ...window.location, pathname: '/api/', assign });
    const base = document.createElement('base');
    base.href = 'http://localhost:3000/api/';
    document.head.appendChild(base);
    try {
      await expect(fetchPageData('platform-data.json')).rejects.toBeInstanceOf(ApiPathError);
    } finally {
      base.remove();
    }
    expect(f).not.toHaveBeenCalled();
    takePathRefusals();
  });
});
