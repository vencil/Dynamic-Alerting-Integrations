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
 * request so the data layer falls all the way through to the DEMO_TENANTS
 * / DEMO_GROUPS fixtures (ESM-imported by useTenantData) — a
 * deterministic, backend-free render.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import TenantManager from '../src/interactive/tools/tenant-manager.jsx';
import { DEMO_TENANTS, DEMO_GROUPS } from '../src/interactive/tools/tenant-manager/fixtures/demo-tenants.js';

// useTenantData ESM-imports DEMO_TENANTS / DEMO_GROUPS (TRK-230z Wave 2 — the
// former window.__DEMO_* registration is gone). Mock the fixtures module so the
// one "data-driven dropdown" test can inject a sentinel tenant per-test without
// disturbing the real fixture the other tests assert against. A `null` override
// makes the getter fall through to the real export.
const demoOverride = vi.hoisted(() => ({ tenants: null as any, groups: null as any }));
vi.mock('../src/interactive/tools/tenant-manager/fixtures/demo-tenants.js', async (importOriginal) => {
  const actual = (await importOriginal()) as any;
  return {
    get DEMO_TENANTS() { return demoOverride.tenants ?? actual.DEMO_TENANTS; },
    get DEMO_GROUPS() { return demoOverride.groups ?? actual.DEMO_GROUPS; },
  };
});

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
  demoOverride.tenants = null;
  demoOverride.groups = null;
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
    demoOverride.tenants = {
      'sentinel-01': {
        environment: 'production', region: 'x', tier: 'tier-1',
        domain: 'zzz-sentinel-domain', db_type: 'zzz-sentinel-db',
        rule_packs: [], owner: 'x', routing_channel: 'x',
        operational_mode: 'normal', metric_count: 0,
        last_config_commit: 'x', tags: [], groups: [],
      },
    };
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

  it('YAML modal restores focus to the「Maintenance YAML」opener on close (TRK-335, WCAG 2.4.3)', async () => {
    await renderAndSettle();
    const { fireEvent } = await import('@testing-library/react');
    // Select tenants so the batch-action bar (with the YAML buttons) appears.
    fireEvent.click(screen.getByRole('button', { name: 'Select All Filtered' }));
    const opener = await screen.findByRole('button', { name: 'Maintenance YAML' });
    opener.focus();
    expect(document.activeElement).toBe(opener);

    fireEvent.click(opener);
    // the maintenance modal opens as a labelled dialog and steals focus into it
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    // Esc closes (hook → setModalType(null)) → focus returns to the opener.
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(document.activeElement).toBe(opener);
  });
});

/**
 * LD-7 (#962): IdentityStrip — legible identity/view + soft empty-state.
 *
 * Unlike the block above (which fails EVERY fetch → demo mode → authUser
 * stays null), these tests resolve /api/v1/me with a real MeResponse so
 * `authUser` is populated, while /api/v1/prs and the tenant data-chain
 * still fall through to the offline DEMO fixtures. The stub keys on URL.
 */
describe('TenantManager — LD-7 IdentityStrip', () => {
  // Build a fetch stub that returns `meBody` (200) for /api/v1/me and
  // rejects everything else (→ DEMO fixtures + no pending PRs).
  function stubFetchWithMe(meBody: any) {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        const u = String(url);
        if (u.includes('/api/v1/me')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve(meBody),
          } as any);
        }
        return Promise.reject(new Error('offline test'));
      })
    );
  }

  const ME_WITH_ACCESS = {
    email: 'alice@example.com',
    user: 'alice',
    groups: ['production-dba'],
    accessible_tenants: ['prod-mariadb-01'],
    accessible_domains: ['finance'],
    permissions: { 'production-dba': ['read', 'write'] },
  };

  const ME_EMPTY_PERMISSIONS = {
    // Real-bug shape: a mistyped group name → non-empty `groups` but the
    // RBAC lookup found nothing → empty `permissions` → maps to no tenants.
    email: 'bob@example.com',
    user: 'bob',
    groups: ['dba-typoo'],
    accessible_tenants: [],
    accessible_domains: [],
    permissions: {},
  };

  it('does NOT render the identity strip in demo mode (authUser == null)', async () => {
    // Every fetch fails → no /api/v1/me body → authUser stays null.
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('offline test'))));
    await renderAndSettle();
    expect(screen.queryByTestId('identity-strip')).toBeNull();
  });

  it('renders email + view summary when authenticated', async () => {
    stubFetchWithMe(ME_WITH_ACCESS);
    await renderAndSettle();
    const strip = await screen.findByTestId('identity-strip');
    expect(strip).toBeInTheDocument();
    // identity (email) is shown
    expect(within(strip).getByText('alice@example.com')).toBeInTheDocument();
    // current-view label is present; with no active filters/group it reads
    // "All tenants" (the neutral default, not a blank).
    expect(within(strip).getByText(/Current view:/)).toBeInTheDocument();
    expect(within(strip).getByText('All tenants')).toBeInTheDocument();
    // groups shown as a NEUTRAL fact — no "authorized"/green-check semantics.
    expect(within(strip).getByText(/Your groups:/)).toBeInTheDocument();
    expect(within(strip).queryByText(/authorized/i)).toBeNull();
    // no empty-state notice when the user has permissions
    expect(screen.queryByTestId('identity-no-access')).toBeNull();
  });

  it('empty permissions → soft warning banner, but functions stay visible (SOFT)', async () => {
    stubFetchWithMe(ME_EMPTY_PERMISSIONS);
    await renderAndSettle();
    // the advisory banner appears…
    const notice = await screen.findByTestId('identity-no-access');
    expect(notice).toBeInTheDocument();
    expect(notice.getAttribute('role')).toBe('status');
    expect(notice.textContent).toMatch(/bob@example.com/);
    // …and NOTHING is hard-hidden: the group sidebar and the search box
    // remain in the DOM (soft notice, not a lockout). NOTE: the CREATE
    // button is absent here — that's the pre-existing canWrite=false
    // gating (empty permissions), not the strip hiding anything.
    const sidebar = screen.getByRole('complementary', { name: 'Group management sidebar' });
    expect(within(sidebar).getByText('All Tenants')).toBeInTheDocument();
    expect(screen.getByLabelText('Search')).toBeInTheDocument();
  });

  it('search text joins the view summary (list is narrowed → strip must say so)', async () => {
    stubFetchWithMe(ME_WITH_ACCESS);
    await renderAndSettle();
    const strip = await screen.findByTestId('identity-strip');
    expect(within(strip).getByText('All tenants')).toBeInTheDocument();

    const { fireEvent } = await import('@testing-library/react');
    fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'mariadb' } });

    // The strip reflects the narrowing search instead of claiming
    // "All tenants" (which would be a wrong fact about the visible list).
    expect(within(strip).getByText(/Search: "mariadb"/)).toBeInTheDocument();
    expect(within(strip).queryByText('All tenants')).toBeNull();
  });

  it('identity display does not touch canWrite — write controls follow permissions only', async () => {
    // ME_WITH_ACCESS grants write → the group-create form (canWrite-gated)
    // is available. The identity strip must not gate this either way.
    stubFetchWithMe(ME_WITH_ACCESS);
    await renderAndSettle();
    await screen.findByTestId('identity-strip');
    const sidebar = screen.getByRole('complementary', { name: 'Group management sidebar' });
    // GroupSidebar shows its create affordance (aria-label "Create new
    // group") only when canWrite=true — proves write access survived.
    expect(within(sidebar).getByRole('button', { name: 'Create new group' })).toBeInTheDocument();
  });
});

/**
 * LD-6 P7 (#962): org badges + AccessScopePanel + first-visit callout.
 *
 * Same fetch-stub shape as the LD-7 block above: /api/v1/me resolves with
 * a MeResponse (now carrying the P7 `org_claim_keys` + `claims` fields),
 * everything else stays offline → DEMO fixtures. The callout persists its
 * dismissal in localStorage, so each test starts from a cleared store.
 */
describe('TenantManager — LD-6 P7 org badges / access scope / callout', () => {
  const SCOPE_CALLOUT_KEY = 'da_tm_scope_callout_v1';

  function stubFetchWithMe(meBody: any) {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        const u = String(url);
        if (u.includes('/api/v1/me')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve(meBody),
          } as any);
        }
        return Promise.reject(new Error('offline test'));
      })
    );
  }

  const ME_BASE = {
    email: 'alice@example.com',
    user: 'alice',
    groups: ['production-dba'],
    accessible_tenants: ['prod-mariadb-01'],
    accessible_domains: ['finance'],
    permissions: { 'production-dba': ['read', 'write'] },
  };

  // The server surfaced ONE caller-relative org axis; a second verified
  // claim exists but is NOT an org axis and must never badge (the strip
  // shows org axes only — the full claims table belongs to the panel).
  const ME_WITH_ORG = {
    ...ME_BASE,
    claims: {
      'x-auth-request-team': 'payments-squad',
      'x-auth-request-region': 'apac',
    },
    org_claim_keys: ['x-auth-request-team'],
  };

  beforeEach(() => {
    window.localStorage.clear();
  });

  function orgBadgeNodes() {
    return document.querySelectorAll('[data-testid^="org-badge"]');
  }

  async function openScopePanel() {
    const { fireEvent } = await import('@testing-library/react');
    fireEvent.click(await screen.findByTestId('view-access-scope'));
    return screen.findByTestId('access-scope-panel');
  }

  it('renders an org badge (org: <value>) for surfaced org axes only', async () => {
    stubFetchWithMe(ME_WITH_ORG);
    await renderAndSettle();
    const badge = await screen.findByTestId('org-badge-x-auth-request-team');
    expect(badge.textContent).toBe('org: payments-squad');
    // tooltip carries the key so the value isn't a floating mystery
    expect(badge.getAttribute('title')).toBe('x-auth-request-team: payments-squad');
    // the non-org claim must NOT badge — nor leak its value into the strip
    expect(screen.queryByTestId('org-badge-x-auth-request-region')).toBeNull();
    const strip = screen.getByTestId('identity-strip');
    expect(within(strip).queryByText(/apac/)).toBeNull();
  });

  it('org_claim_keys absent (older server / zero org rules) → no badge segment', async () => {
    stubFetchWithMe(ME_BASE);
    await renderAndSettle();
    await screen.findByTestId('identity-strip');
    expect(orgBadgeNodes().length).toBe(0);
    expect(screen.queryByTestId('org-badges-collapsed')).toBeNull();
  });

  it('org_claim_keys present but claims missing the values → no badges (no empty state)', async () => {
    stubFetchWithMe({ ...ME_BASE, org_claim_keys: ['x-auth-request-team'] });
    await renderAndSettle();
    await screen.findByTestId('identity-strip');
    expect(orgBadgeNodes().length).toBe(0);
  });

  it('a drifted prototype key in org_claim_keys resolves to nothing (own-property guard)', async () => {
    // Defense in depth: org_claim_keys is server-supplied. A key like
    // "constructor" is NOT an own property of the claims object, so it must
    // not render an inherited prototype value as a badge.
    stubFetchWithMe({ ...ME_BASE, claims: {}, org_claim_keys: ['constructor', 'toString'] });
    await renderAndSettle();
    await screen.findByTestId('identity-strip');
    expect(orgBadgeNodes().length).toBe(0);
    expect(screen.queryByTestId('org-badges-collapsed')).toBeNull();
  });

  it('more than 3 org axes collapse into a count with a full title tooltip', async () => {
    const claims: Record<string, string> = {
      'x-org-a': 'v-a', 'x-org-b': 'v-b', 'x-org-c': 'v-c', 'x-org-d': 'v-d',
    };
    stubFetchWithMe({ ...ME_BASE, claims, org_claim_keys: Object.keys(claims) });
    await renderAndSettle();
    const collapsed = await screen.findByTestId('org-badges-collapsed');
    expect(collapsed.textContent).toContain('4');
    for (const [k, v] of Object.entries(claims)) {
      expect(collapsed.getAttribute('title')).toContain(`${k}: ${v}`);
    }
    // no individual badges alongside the collapsed count
    expect(screen.queryByTestId('org-badge-x-org-a')).toBeNull();
  });

  it('「檢視存取範圍」 opens the panel rendering the full /me scope (zero extra fetches)', async () => {
    stubFetchWithMe(ME_WITH_ORG);
    await renderAndSettle();
    const fetchMock = window.fetch as ReturnType<typeof vi.fn>;
    const callsBefore = fetchMock.mock.calls.length;
    const panel = await openScopePanel();

    expect(panel.getAttribute('role')).toBe('dialog');
    expect(panel.getAttribute('aria-modal')).toBe('true');
    // permissions: rule → granted table
    const perms = within(panel).getByTestId('scope-permissions');
    expect(within(perms).getByText('production-dba')).toBeInTheDocument();
    expect(within(perms).getByText('read, write')).toBeInTheDocument();
    // accessible_tenants shown WITH the "rule patterns, not an expanded
    // list" label — the one fact users misread most.
    expect(within(panel).getByTestId('scope-tenants').textContent).toBe('prod-mariadb-01');
    expect(within(panel).getByText(/rule patterns, not an expanded tenant list/)).toBeInTheDocument();
    // omitempty semantics: absent environments → "All"; domains present
    expect(within(panel).getByTestId('scope-environments').textContent).toBe('All');
    expect(within(panel).getByTestId('scope-domains').textContent).toBe('finance');
    expect(within(panel).getByTestId('scope-groups').textContent).toBe('production-dba');
    // full claims table: org axis gets the pill, the other claim doesn't
    const claimsTable = within(panel).getByTestId('scope-claims');
    expect(within(claimsTable).getByText('payments-squad')).toBeInTheDocument();
    expect(within(claimsTable).getByText('apac')).toBeInTheDocument();
    expect(within(panel).getByTestId('scope-org-key-x-auth-request-team')).toBeInTheDocument();
    expect(within(panel).queryByTestId('scope-org-key-x-auth-request-region')).toBeNull();
    // everything above came from the already-fetched /me body
    expect(fetchMock.mock.calls.length).toBe(callsBefore);

    const { fireEvent } = await import('@testing-library/react');
    fireEvent.click(within(panel).getByTestId('scope-close'));
    await waitFor(() => expect(screen.queryByTestId('access-scope-panel')).toBeNull());
  });

  it('panel traps focus (auto-focus + Tab wrap) and Esc closes it', async () => {
    stubFetchWithMe(ME_WITH_ORG);
    await renderAndSettle();
    const panel = await openScopePanel();
    const content = panel.querySelector('[tabindex="-1"]') as HTMLElement;
    await waitFor(() => expect(document.activeElement).toBe(content));

    const { fireEvent } = await import('@testing-library/react');
    // single focusable (the close button): Tab from the last focusable
    // wraps to the first — proves the trap is live on this modal
    const closeBtn = within(panel).getByTestId('scope-close');
    closeBtn.focus();
    // fireEvent returns false when the handler preventDefault'ed — the
    // trap intercepted the Tab at the edge (jsdom never moves focus on
    // its own, so asserting activeElement alone would pass vacuously).
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(false);
    expect(document.activeElement).toBe(closeBtn);

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByTestId('access-scope-panel')).toBeNull());
  });

  it('restores focus to the「檢視存取範圍」opener when the panel closes (TRK-335, WCAG 2.4.3)', async () => {
    stubFetchWithMe(ME_WITH_ORG);
    await renderAndSettle();
    const opener = await screen.findByTestId('view-access-scope');
    // Mirror a real click, which focuses the button before the modal opens —
    // the element the hook must return focus to on close.
    opener.focus();
    expect(document.activeElement).toBe(opener);

    const { fireEvent } = await import('@testing-library/react');
    fireEvent.click(opener);
    const panel = await screen.findByTestId('access-scope-panel');
    // the panel steals focus into its content on open
    await waitFor(() => expect(panel.contains(document.activeElement)).toBe(true));

    // Esc closes → focus returns to the launching trigger, not document.body.
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByTestId('access-scope-panel')).toBeNull());
    expect(document.activeElement).toBe(opener);
  });

  it('backdrop mousedown closes; content mousedown and a stray click do not (Reef 8)', async () => {
    stubFetchWithMe(ME_WITH_ORG);
    await renderAndSettle();
    const panel = await openScopePanel();
    const { fireEvent } = await import('@testing-library/react');
    // A press that starts inside the content must not close.
    fireEvent.mouseDown(panel.querySelector('[tabindex="-1"]') as HTMLElement);
    expect(screen.getByTestId('access-scope-panel')).toBeInTheDocument();
    // A stray click on the backdrop (the mouseup of a text-selection drag that
    // began in the content) must NOT close — this is the bug the Reef-8
    // mousedown+target guard fixes.
    fireEvent.click(panel);
    expect(screen.getByTestId('access-scope-panel')).toBeInTheDocument();
    // A press that starts on the backdrop itself closes.
    fireEvent.mouseDown(panel);
    await waitFor(() => expect(screen.queryByTestId('access-scope-panel')).toBeNull());
  });

  it('first authed visit shows the callout; dismissal persists to localStorage', async () => {
    stubFetchWithMe(ME_BASE);
    await renderAndSettle();
    const callout = await screen.findByTestId('scope-callout');
    expect(callout.getAttribute('role')).toBe('status');

    const { fireEvent } = await import('@testing-library/react');
    fireEvent.click(screen.getByTestId('scope-callout-dismiss'));
    await waitFor(() => expect(screen.queryByTestId('scope-callout')).toBeNull());
    expect(window.localStorage.getItem(SCOPE_CALLOUT_KEY)).toBe('1');
  });

  it('callout is not shown again once the dismissal flag is set', async () => {
    window.localStorage.setItem(SCOPE_CALLOUT_KEY, '1');
    stubFetchWithMe(ME_BASE);
    await renderAndSettle();
    await screen.findByTestId('identity-strip');
    expect(screen.queryByTestId('scope-callout')).toBeNull();
  });

  it("callout's open button opens the panel AND dismisses the callout", async () => {
    stubFetchWithMe(ME_BASE);
    await renderAndSettle();
    await screen.findByTestId('scope-callout');
    const { fireEvent } = await import('@testing-library/react');
    fireEvent.click(screen.getByTestId('scope-callout-open'));
    await screen.findByTestId('access-scope-panel');
    expect(screen.queryByTestId('scope-callout')).toBeNull();
    expect(window.localStorage.getItem(SCOPE_CALLOUT_KEY)).toBe('1');
  });

  it('opening the panel from the strip button also dismisses the callout', async () => {
    // The strip button and the callout both point at the panel; using either
    // means the callout has served its purpose, so both must dismiss it.
    stubFetchWithMe(ME_BASE);
    await renderAndSettle();
    await screen.findByTestId('scope-callout');
    const { fireEvent } = await import('@testing-library/react');
    fireEvent.click(screen.getByTestId('view-access-scope'));
    await screen.findByTestId('access-scope-panel');
    expect(screen.queryByTestId('scope-callout')).toBeNull();
    expect(window.localStorage.getItem(SCOPE_CALLOUT_KEY)).toBe('1');
  });

  it('unavailable localStorage skips the callout without breaking the page', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage disabled');
    });
    stubFetchWithMe(ME_BASE);
    await renderAndSettle();
    // strip (and the rest of the page) render fine; callout silently skipped
    await screen.findByTestId('identity-strip');
    expect(screen.queryByTestId('scope-callout')).toBeNull();
  });

  it('demo mode (no authUser) renders none of it', async () => {
    // default stub from the file-level beforeEach: every fetch fails
    await renderAndSettle();
    expect(screen.queryByTestId('identity-strip')).toBeNull();
    expect(screen.queryByTestId('view-access-scope')).toBeNull();
    expect(screen.queryByTestId('scope-callout')).toBeNull();
    expect(screen.queryByTestId('access-scope-panel')).toBeNull();
    expect(orgBadgeNodes().length).toBe(0);
  });
});
