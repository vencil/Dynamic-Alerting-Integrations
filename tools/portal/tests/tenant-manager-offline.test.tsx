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
import { render, screen, waitFor } from '@testing-library/react';
import TenantManager from '../src/interactive/tools/tenant-manager.jsx';
import { DEMO_TENANTS } from '../src/interactive/tools/tenant-manager/fixtures/demo-tenants.js';
import { signInUrl } from '../src/interactive/tools/_common/auth/sign-in.js';

function stubAssign() {
  const assign = vi.fn();
  vi.stubGlobal('location', { ...window.location, pathname: '/interactive/', search: '?tool=tenant-manager', hash: '#x', assign });
  return assign;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('signInUrl', () => {
  it('returns to the page it was on', () => {
    expect(signInUrl({ pathname: '/interactive/', search: '?a=1&b=2', hash: '#t' } as any))
      .toBe('/oauth2/start?rd=' + encodeURIComponent('/interactive/?a=1&b=2#t'));
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
});
