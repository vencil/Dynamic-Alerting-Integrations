/**
 * Unit tests for the `TenantManager` orchestrator — last-mile
 * activation (wiring BUILT-but-unconnected capabilities).
 *
 * Scope (intentionally narrow — the orchestrator's data-loading,
 * 429-retry, virtualization etc. are covered by useTenantData /
 * e2e specs; here we lock the four activation fixes):
 *
 *   1. GroupSidebar actually RENDERS (was imported, never mounted →
 *      the whole group select/create/delete feature was dead).
 *   2. The domain / db-type filter dropdowns reflect the computed
 *      `filterOptions` (derived from live tenant data) instead of a
 *      hardcoded option list that drifts from reality.
 *   3. The dead "Compare Mode" toggle button is gone.
 *
 * The component fires several fetch() calls on mount (/api/v1/me,
 * /api/v1/prs, the tenants data-chain). We stub fetch to fail every
 * request so the data layer falls all the way through to DEMO_TENANTS
 * / DEMO_GROUPS (registered on window as an import side-effect of the
 * fixtures module) — a deterministic, backend-free render.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import TenantManager from '../src/interactive/tools/tenant-manager.jsx';
// Import the fixtures for their window.__DEMO_* registration side-effect
// (the orchestrator + useTenantData read window.__DEMO_TENANTS /
// window.__DEMO_GROUPS at effect time).
import { DEMO_TENANTS, DEMO_GROUPS } from '../src/interactive/tools/tenant-manager/fixtures/demo-tenants.js';

// Demo fixture distinct domains / db_types, sorted — what the wired
// dropdowns should render (the old hardcoded lists happened to match
// in demo mode, but these are now derived from the data).
const EXPECTED_DOMAINS = [...new Set(Object.values(DEMO_TENANTS).map((t: any) => t.domain).filter(Boolean))].sort();
const EXPECTED_DBTYPES = [...new Set(Object.values(DEMO_TENANTS).map((t: any) => t.db_type).filter(Boolean))].sort();

beforeEach(() => {
  // Every fetch fails → API path returns null → platform-data.json
  // path throws → demo fallback. Keeps the test fully offline + the
  // group-read best-effort lands on DEMO_GROUPS.
  vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('offline test'))));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderAndSettle() {
  const utils = render(<TenantManager />);
  // The orchestrator shows a <Loading> spinner until the data chain
  // resolves; wait for the real UI (the search box) to appear.
  await waitFor(() => expect(screen.getByLabelText('Search')).toBeInTheDocument());
  return utils;
}

describe('TenantManager — last-mile activation', () => {
  it('renders the GroupSidebar (group feature is no longer dead)', async () => {
    await renderAndSettle();
    // GroupSidebar is role="complementary" with its group-management label.
    const sidebar = screen.getByRole('complementary', { name: 'Group management sidebar' });
    expect(sidebar).toBeInTheDocument();
    // It always renders an "All Tenants" affordance + the group-count title.
    expect(within(sidebar).getByText('All Tenants')).toBeInTheDocument();
    // Demo groups should be listed (production-dba + staging-all).
    for (const id of Object.keys(DEMO_GROUPS)) {
      const label = (DEMO_GROUPS as any)[id].label;
      expect(within(sidebar).getByText(new RegExp(label))).toBeInTheDocument();
    }
  });

  it('selecting a group filters the tenant TABLE to its members', async () => {
    await renderAndSettle();
    // Before selection a non-staging tenant is on screen.
    expect(screen.getAllByText('prod-mariadb-01').length).toBeGreaterThan(0);
    const sidebar = screen.getByRole('complementary', { name: 'Group management sidebar' });
    // staging-all has exactly one member (staging-pg-01). Click it.
    const stagingBtn = within(sidebar).getByRole('button', { name: /Select group: All Staging/ });
    const { fireEvent } = await import('@testing-library/react');
    fireEvent.click(stagingBtn);
    // After selecting "All Staging" the TABLE is filtered to its sole
    // member — non-members disappear. Proves the selection drives the
    // tenant filter, not just the active-group panel: if onSelectGroup
    // stopped filtering, prod-mariadb-01 would still render and this fails.
    await waitFor(() => expect(screen.queryAllByText('prod-mariadb-01').length).toBe(0));
    expect(screen.getAllByText('staging-pg-01').length).toBeGreaterThan(0);
    // …and the active-group panel still surfaces the selection.
    expect(screen.getByRole('region', { name: 'Group: All Staging' })).toBeInTheDocument();
  });

  it('domain dropdown options are derived from filterOptions (live data), not hardcoded', async () => {
    await renderAndSettle();
    const domainSelect = document.getElementById('filter-domain') as HTMLSelectElement;
    expect(domainSelect).not.toBeNull();
    const optionValues = Array.from(domainSelect.options)
      .map((o) => o.value)
      .filter((v) => v !== ''); // drop the leading "All Domains"
    expect(optionValues).toEqual(EXPECTED_DOMAINS);
  });

  it('db-type dropdown options are derived from filterOptions (live data), not hardcoded', async () => {
    await renderAndSettle();
    const dbSelect = document.getElementById('filter-dbtype') as HTMLSelectElement;
    expect(dbSelect).not.toBeNull();
    const optionValues = Array.from(dbSelect.options)
      .map((o) => o.value)
      .filter((v) => v !== ''); // drop the leading "All DB Types"
    expect(optionValues).toEqual(EXPECTED_DBTYPES);
  });

  it('dropdowns are truly DATA-DRIVEN — a sentinel value absent from the old hardcoded lists appears', async () => {
    // The old hardcoded <option> lists were [finance,cache,analytics,
    // mobile,streaming] / [mariadb,redis,postgresql,mongodb,kafka] — which
    // happen to equal the demo distinct set, so the EXPECTED_* assertions
    // above cannot tell a regression-to-hardcoded apart. Override the data
    // with a SENTINEL domain/db_type the legacy lists never contained: only
    // a filterOptions-driven dropdown can surface it.
    vi.stubGlobal('__DEMO_TENANTS', {
      'sentinel-01': {
        environment: 'production', region: 'x', tier: 'tier-1',
        domain: 'zzz-sentinel-domain', db_type: 'zzz-sentinel-db',
        rule_packs: [], owner: 'x', routing_channel: 'x',
        operational_mode: 'normal', metric_count: 0,
        last_config_commit: 'x', tags: [], groups: [],
      },
    });
    await renderAndSettle();
    const domainOpts = Array.from((document.getElementById('filter-domain') as HTMLSelectElement).options).map((o) => o.value);
    const dbOpts = Array.from((document.getElementById('filter-dbtype') as HTMLSelectElement).options).map((o) => o.value);
    expect(domainOpts).toContain('zzz-sentinel-domain');
    expect(dbOpts).toContain('zzz-sentinel-db');
    // A legacy hardcoded value cannot appear (that tenant no longer exists).
    expect(domainOpts).not.toContain('finance');
  });

  it('still renders the fixed-enum filters (env/tier/mode) untouched', async () => {
    await renderAndSettle();
    // These stay hardcoded by design — guard against an over-broad refactor.
    const envSelect = document.getElementById('filter-env') as HTMLSelectElement;
    expect(Array.from(envSelect.options).map((o) => o.value)).toEqual([
      '', 'production', 'staging', 'development',
    ]);
  });

  it('the dead "Compare Mode" toggle button has been removed', async () => {
    await renderAndSettle();
    expect(screen.queryByRole('button', { name: 'Compare Mode' })).toBeNull();
    expect(screen.queryByText('Compare Mode')).toBeNull();
    expect(screen.queryByText('Exit Compare Mode')).toBeNull();
  });
});
