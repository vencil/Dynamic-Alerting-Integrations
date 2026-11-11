/**
 * Tenant Manager — signed out, offline and demo data (ADR-027 §5, #3306).
 *
 *   - /api/ answers 401 without a session (oauth2-proxy --api-route): the
 *     page sends the browser to sign in, back to where it was, and never
 *     shows the offline or demo copy instead;
 *   - the offline copy (platform-data.json) is shown as it is — no demo
 *     tenant mixed in — and marked offline; nothing can be written over it.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import TenantManager from '../src/interactive/tools/tenant-manager.jsx';
import { DEMO_TENANTS } from '../src/interactive/tools/tenant-manager/fixtures/demo-tenants.js';
import {
  signInUrl, redirectToSignIn, SIGN_IN_ATTEMPT_KEY, SIGN_IN_RETRY_WINDOW_MS,
} from '../src/interactive/tools/_common/auth/sign-in.js';

function stubAssign() {
  const assign = vi.fn();
  vi.stubGlobal('location', { ...window.location, pathname: '/interactive/', search: '?tool=tenant-manager', hash: '#x', assign });
  return assign;
}

afterEach(() => {
  window.sessionStorage.removeItem(SIGN_IN_ATTEMPT_KEY);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('signInUrl', () => {
  it('returns to the page it was on', () => {
    expect(signInUrl({ pathname: '/interactive/', search: '?a=1&b=2', hash: '#t' } as any))
      .toBe('/oauth2/start?rd=' + encodeURIComponent('/interactive/?a=1&b=2#t'));
  });

  it('collapses leading slashes: //host is not a return target', () => {
    expect(signInUrl({ pathname: '//evil.example/x', search: '', hash: '' } as any))
      .toBe('/oauth2/start?rd=' + encodeURIComponent('/evil.example/x'));
  });
});

describe('redirectToSignIn', () => {
  it('does not leave again for a 401 right after signing in', () => {
    const assign = vi.fn();
    const win = { location: { pathname: '/p', search: '', hash: '', assign }, sessionStorage: window.sessionStorage } as any;
    expect(redirectToSignIn(win, 1_000_000)).toBe(true);
    expect(redirectToSignIn(win, 1_000_000 + SIGN_IN_RETRY_WINDOW_MS - 1)).toBe(false);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(redirectToSignIn(win, 1_000_000 + SIGN_IN_RETRY_WINDOW_MS)).toBe(true);
    expect(assign).toHaveBeenCalledTimes(2);
  });
});

describe('TenantManager — signed out (401)', () => {
  it('a 401 on the tenant search sends the browser to sign in and reads no offline data', async () => {
    const assign = stubAssign();
    const fetched: string[] = [];
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      fetched.push(String(url));
      if (String(url).startsWith('/api/')) {
        return Promise.resolve({ ok: false, status: 401, headers: new Headers(), json: () => Promise.resolve({}) } as any);
      }
      return Promise.reject(new Error('must not be read'));
    }));
    render(<TenantManager />);
    await waitFor(() => expect(assign).toHaveBeenCalledWith(
      '/oauth2/start?rd=' + encodeURIComponent('/interactive/?tool=tenant-manager#x')));
    expect(fetched.filter(u => u.includes('platform-data.json'))).toEqual([]);
    for (const name of Object.keys(DEMO_TENANTS)) {
      expect(screen.queryByText(name)).toBeNull();
    }
  });
});

describe('TenantManager — session ends before the groups call', () => {
  it('a 401 on /api/v1/groups sends the browser to sign in and reads no offline groups', async () => {
    const assign = stubAssign();
    const fetched: string[] = [];
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      const u = String(url);
      fetched.push(u);
      if (u.startsWith('/api/v1/tenants/search')) {
        return Promise.resolve({ ok: true, status: 200, headers: new Headers(),
          json: () => Promise.resolve({ items: [{ id: 't-live' }], total_matched: 1 }) } as any);
      }
      if (u.startsWith('/api/v1/groups')) {
        return Promise.resolve({ ok: false, status: 401, headers: new Headers(), json: () => Promise.resolve({}) } as any);
      }
      if (u.startsWith('/api/')) {
        return Promise.resolve({ ok: false, status: 404, headers: new Headers(), json: () => Promise.resolve({}) } as any);
      }
      return Promise.reject(new Error('must not be read'));
    }));
    render(<TenantManager />);
    await waitFor(() => expect(assign).toHaveBeenCalled());
    expect(fetched.filter(u => u.includes('platform-data.json'))).toEqual([]);
  });
});

describe('TenantManager — offline copy (#3306)', () => {
  it('shows platform-data.json as it is, marked offline, without demo tenants, read-only', async () => {
    stubAssign();
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      const u = String(url);
      if (u.includes('platform-data.json')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
          tenant_metadata: { 't-real': { environment: 'production' } },
        }) } as any);
      }
      return Promise.reject(new Error('no API on this host'));
    }));
    render(<TenantManager />);
    expect(await screen.findByTestId('offline-data-notice')).toHaveTextContent(/Offline data/);
    await waitFor(() => expect(screen.getAllByText('t-real').length).toBeGreaterThan(0));
    for (const name of Object.keys(DEMO_TENANTS)) {
      expect(screen.queryByText(name)).toBeNull();
    }
    const sidebar = screen.getByRole('complementary', { name: 'Group management sidebar' });
    expect(sidebar.querySelector('[aria-label="Create new group"]')).toBeNull();
  });

  it('offers no group delete over the offline copy, even with /api/v1/me granting write', async () => {
    stubAssign();
    const deletes: string[] = [];
    vi.stubGlobal('fetch', vi.fn((url: string, init?: any) => {
      const u = String(url);
      if (init?.method === 'DELETE') deletes.push(u);
      if (u.includes('platform-data.json')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
          tenant_metadata: { 't-real': { environment: 'production' } },
          custom_groups: { 'g-real': { label: 'Real group', members: ['t-real'] } },
        }) } as any);
      }
      if (u === '/api/v1/me') {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
          email: 'a@example.com', permissions: { 't-real': ['write'] },
        }) } as any);
      }
      return Promise.reject(new Error('no API on this host'));
    }));
    render(<TenantManager />);
    fireEvent.click(await screen.findByRole('button', { name: /Select group: Real group/ }));
    expect(screen.queryByRole('button', { name: /^Delete Group$/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Delete group: Real group/ })).toBeNull();
    expect(deletes).toEqual([]);
  });
});
