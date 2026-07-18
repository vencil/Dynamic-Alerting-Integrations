/**
 * Render + fetch-stub tests for access-report-dryrun.jsx (LD-6 P7c).
 *
 * The component sources its tenant dropdown from useTenantData (which fetches
 * /api/v1/tenants/search + /api/v1/groups) and its diff from a self-contained
 * POST to the dry-run endpoint. We stub global fetch keyed on URL so the
 * dropdown reaches API mode (dataSource==='api' → enabled) and the POST
 * returns a per-test discriminated-union outcome.
 *
 * Covered outcomes (spec §3 degradation matrix): 200 full diff, 200 EMPTY
 * diff (must show the R1 scope band + grant-set guard, ⛔ never "no change"),
 * 403 locked card, 404 version card, 400 CANDIDATE_INVALID inline verbatim,
 * plus the demo-fallback gate (dropdown disabled + reason when the tenant API
 * is unreachable — ⛔ never pre-judged, only the demo dataSource degrades it).
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import AccessReportDryRun from '../src/interactive/tools/access-report-dryrun.jsx';

const CAVEATS = [
  "candidate evaluated under THIS deployment's --identity-claim-headers declaration",
  'org labeling taken from the LIVE _tenant_orgs.yaml (candidate tenant-org input not supported)',
  'a renamed rule appears as removed+added (grants pair by rule name)',
  "presence-implies-membership applies: a grant entry's existence is itself weakly identifying",
];

// A minimal VALID 200 diff envelope (empty diff, matching empty grant sets) for
// tests that exercise the copy button / snapshot / locks rather than the diff
// rendering itself.
const OK_DIFF = {
  schema_version: 1,
  generated_at: '2026-07-18T00:00:00Z',
  candidate_sha256: 'abc123',
  baseline: { verdict: 'grants_found', mode: 'rules', grants: [] },
  candidate: { verdict: 'grants_found', mode: 'rules', grants: [] },
  diff: { alignment: 'exact', changed: [], added: [], removed: [] },
  caveats: CAVEATS,
};

function makeResp(status: number, body: any, opts: { json?: boolean } = {}) {
  const json = opts.json !== false;
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: () => (json ? Promise.resolve(body) : Promise.reject(new Error('no json body'))),
  } as any;
}

// dryRunResponder(url, opts) → a fetch-resolved Response for the POST.
function stubFetch(dryRunResponder: (u: string, o: any) => Promise<any>) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: any, opts: any) => {
      const u = String(url);
      if (u.includes('/api/v1/tenants/search')) {
        return Promise.resolve(
          makeResp(200, {
            items: [{ id: 'acme', environment: 'production', tier: 'tier-1', domain: 'finance' }],
            total_matched: 1,
          }),
        );
      }
      if (u.includes('/api/v1/groups')) {
        return Promise.resolve(makeResp(200, []));
      }
      if (u.includes('/access-report/dry-run')) {
        return dryRunResponder(u, opts);
      }
      return Promise.reject(new Error('unexpected url ' + u));
    }),
  );
}

// Render, wait for API mode (dropdown enabled + tenant present), select the
// tenant, and click Run — the shared preamble for every POST-outcome test.
async function renderSelectRun(dryRunResponder: (u: string, o: any) => Promise<any>) {
  stubFetch(dryRunResponder);
  render(<AccessReportDryRun />);
  const select = (await screen.findByLabelText('Select tenant')) as HTMLSelectElement;
  await waitFor(() => expect(select.disabled).toBe(false));
  await waitFor(() => expect(select.querySelector('option[value="acme"]')).not.toBeNull());
  fireEvent.change(select, { target: { value: 'acme' } });
  const runBtn = screen.getByRole('button', { name: 'Run dry-run' });
  await waitFor(() => expect(runBtn).not.toBeDisabled());
  fireEvent.click(runBtn);
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('AccessReportDryRun — dropdown gate', () => {
  it('demo fallback (tenant API unreachable) → dropdown DISABLED with a reason (never hidden)', async () => {
    // Every fetch rejects → useTenantData lands on demo → dataSource!=='api'.
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('offline test'))));
    render(<AccessReportDryRun />);
    const select = (await screen.findByLabelText('Select tenant')) as HTMLSelectElement;
    // The dropdown is present but disabled — ⛔ not removed from the DOM.
    await waitFor(() => expect(select.disabled).toBe(true));
    // findByText retries past the transient "Loading tenant list…" state.
    expect(await screen.findByText(/Not connected to the tenant API/i)).toBeInTheDocument();
    // Run stays disabled: no tenant can be selected in demo mode.
    expect(screen.getByRole('button', { name: 'Run dry-run' })).toBeDisabled();
  });
});

describe('AccessReportDryRun — POST outcomes', () => {
  it('200 full diff renders the verdict band, R1 scope band, and a changed-rule card', async () => {
    await renderSelectRun(() =>
      Promise.resolve(
        makeResp(200, {
          schema_version: 1,
          generated_at: '2026-07-18T00:00:00Z',
          candidate_sha256: 'deadbeef',
          baseline: {
            verdict: 'grants_found',
            mode: 'rules',
            grants: [
              { index: 0, rule: 'db-team', platform_wide: false, permissions: ['read'], effective: { read: true, write: false, admin: false } },
            ],
          },
          candidate: {
            verdict: 'grants_found',
            mode: 'rules',
            grants: [
              { index: 0, rule: 'db-team', platform_wide: true, permissions: ['read', 'admin'], effective: { read: true, write: false, admin: true } },
            ],
          },
          diff: {
            alignment: 'exact',
            changed: [
              { rule: 'db-team', live_index: 0, candidate_index: 0, outcome_shadow: { from: 'not_required', to: 'conditional_on_caller_org' } },
            ],
            added: [],
            removed: [],
          },
          caveats: CAVEATS,
        }),
      ),
    );

    // R8 verdict/mode band + R1 mandatory scope caveat band both present.
    expect(await screen.findByText('Candidate verdict / mode')).toBeInTheDocument();
    expect(screen.getByText(/Read first:/)).toBeInTheDocument();
    // The changed-rule card renders the rule name (card-per-rule, not a table).
    expect(screen.getByText('Changed rules')).toBeInTheDocument();
    expect(screen.getByText('db-team')).toBeInTheDocument();
    // candidate_sha256 is pinned.
    expect(screen.getByText(/deadbeef/)).toBeInTheDocument();
  });

  it('200 EMPTY diff shows the R1 band + grant-set guard, ⛔ NEVER a "no change" success', async () => {
    await renderSelectRun(() =>
      Promise.resolve(
        makeResp(200, {
          schema_version: 1,
          generated_at: '2026-07-18T00:00:00Z',
          candidate_sha256: 'cafef00d',
          baseline: {
            verdict: 'grants_found',
            mode: 'rules',
            // grant set DIFFERS from candidate (permissions widened) even though
            // the server's three-axis diff is empty — the R1/R9 blind spot.
            grants: [
              { index: 0, rule: 'db-team', platform_wide: false, permissions: ['read'], effective: { read: true, write: false, admin: false } },
            ],
          },
          candidate: {
            verdict: 'grants_found',
            mode: 'rules',
            grants: [
              { index: 0, rule: 'db-team', platform_wide: false, permissions: ['read', 'admin'], effective: { read: true, write: false, admin: true } },
            ],
          },
          diff: { alignment: 'exact', changed: [], added: [], removed: [] },
          caveats: CAVEATS,
        }),
      ),
    );

    // The empty-diff card exists, but it is a MEASUREMENT, not a "no change".
    expect(await screen.findByText('No change on the three org-gate axes')).toBeInTheDocument();
    // R9 guard fires loudly because the grant sets differ.
    expect(screen.getByText(/grant sets are NOT identical/)).toBeInTheDocument();
    // R1 scope band is still mandatory here.
    expect(screen.getByText(/Read first:/)).toBeInTheDocument();
    // ⛔ never a green "identical / no change" success claim in this case.
    expect(screen.queryByText(/identical in the full view/)).toBeNull();
  });

  it('403 → locked card (role=status) echoing the server error verbatim, no retry', async () => {
    const serverErr = 'platform admin (non-org-scoped) permission required for access reports';
    await renderSelectRun(() =>
      Promise.resolve(makeResp(403, { error: serverErr, code: 'FORBIDDEN' })),
    );
    const card = await screen.findByText('Locked — insufficient permission');
    expect(card).toBeInTheDocument();
    // verbatim server error is echoed.
    expect(screen.getByText(serverErr)).toBeInTheDocument();
    // the locked block is a role=status region (a11y contract).
    expect(screen.getByText(/requires platform-admin/).closest('[role="status"]')).not.toBeNull();
  });

  it('404 → version-unsupported card (⛔ body is never parsed)', async () => {
    await renderSelectRun(() => Promise.resolve(makeResp(404, null, { json: false })));
    expect(await screen.findByText('tenant-api version does not support this')).toBeInTheDocument();
  });

  it('400 CANDIDATE_INVALID → inline verbatim parse detail', async () => {
    const detail = 'candidate _rbac.yaml rejected: rule "db-*" has an empty claim value list';
    await renderSelectRun(() =>
      Promise.resolve(makeResp(400, { code: 'CANDIDATE_INVALID', error: detail })),
    );
    expect(await screen.findByText('Candidate _rbac.yaml rejected')).toBeInTheDocument();
    // the server's (possibly long, English) detail is shown verbatim.
    expect(screen.getByText(detail)).toBeInTheDocument();
  });

  it('200 with a non-array diff bucket → malformed card, ⛔ never a renderer crash (CR-1)', async () => {
    await renderSelectRun(() =>
      Promise.resolve(
        makeResp(200, {
          ...OK_DIFF,
          // A trusted first-party server never emits this (Go make([]T,0)); a
          // tampered/malformed 200 with a string diff bucket would crash .map on
          // render — the wrapper must degrade it to "malformed", not "ok".
          diff: { alignment: 'exact', changed: 'x', added: [], removed: [] },
        }),
      ),
    );
    expect(await screen.findByText('Unexpected response shape')).toBeInTheDocument();
  });

  it('copy redacted re-fetches the SNAPSHOT candidate, not the edited textarea (CR-3)', async () => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn(() => Promise.resolve()) } });
    const bodies: string[] = [];
    await renderSelectRun((_u, o) => {
      bodies.push(o && o.body ? String(o.body) : '');
      return Promise.resolve(makeResp(200, OK_DIFF));
    });
    // Run submitted the initial (empty) yamlText. Now edit the textarea AFTER Run.
    const ta = screen.getByLabelText('Candidate _rbac.yaml content') as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: 'groups:\n  - name: EDITED_AFTER_RUN' } });
    const copyBtn = await screen.findByRole('button', { name: /Copy shareable/ });
    fireEvent.click(copyBtn);
    await waitFor(() => expect(bodies.length).toBe(2)); // [0]=Run(full), [1]=copy(redacted)
    // The copy POST must carry the SNAPSHOT candidate (what Run sent), NOT the
    // edited textarea — otherwise the copied redacted view would lie about which
    // candidate produced the on-screen diff.
    expect(bodies[1]).not.toContain('EDITED_AFTER_RUN');
    expect(JSON.parse(bodies[1]).candidate.rbac_yaml).toBe(
      JSON.parse(bodies[0]).candidate.rbac_yaml,
    );
  });

  it('copy hitting 403 flips the shared forbidden lock and disables copy (CR-2)', async () => {
    await renderSelectRun((u) => {
      if (u.includes('view=redacted')) {
        return Promise.resolve(makeResp(403, { code: 'FORBIDDEN', error: 'nope' }));
      }
      return Promise.resolve(makeResp(200, OK_DIFF));
    });
    const copyBtn = await screen.findByRole('button', { name: /Copy shareable/ });
    expect(copyBtn).not.toBeDisabled();
    fireEvent.click(copyBtn);
    // the redacted 403 sets the shared forbidden lock → copy becomes disabled
    // (same lock that disables Run), so a locked tool cannot be hammered.
    await waitFor(() => expect(copyBtn).toBeDisabled());
  });
});
