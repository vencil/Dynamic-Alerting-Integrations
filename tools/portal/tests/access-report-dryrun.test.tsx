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

// Like renderSelectRun, but flips the opt-in org-value toggle ON before Run —
// the shared preamble for the ?include=org_values disclosure tests.
async function renderToggleRun(dryRunResponder: (u: string, o: any) => Promise<any>) {
  stubFetch(dryRunResponder);
  render(<AccessReportDryRun />);
  const select = (await screen.findByLabelText('Select tenant')) as HTMLSelectElement;
  await waitFor(() => expect(select.disabled).toBe(false));
  await waitFor(() => expect(select.querySelector('option[value="acme"]')).not.toBeNull());
  fireEvent.change(select, { target: { value: 'acme' } });
  fireEvent.click(screen.getByRole('checkbox', { name: /Reveal org values/ }));
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

describe('AccessReportDryRun — org-values disclosure (?include=org_values)', () => {
  // A 200 whose candidate carries ONE added grant on a REQUIRED org gate, so the
  // added-grant enrichment renders OrgGateValues when the toggle is on.
  const ORG_RESP = (orgGate: any, tenantOrgs?: string[]) => ({
    schema_version: 1,
    generated_at: '2026-07-18T00:00:00Z',
    candidate_sha256: 'org123',
    baseline: {
      verdict: 'grants_found',
      mode: 'rules',
      grants: [],
      tenant: tenantOrgs
        ? { id: 'acme', org_status: 'labeled', orgs: tenantOrgs }
        : { id: 'acme', org_status: 'labeled' },
    },
    candidate: {
      verdict: 'grants_found',
      mode: 'rules',
      grants: [
        { index: 0, rule: 'g', permissions: ['read'], effective: { read: true }, org_gate: orgGate },
      ],
    },
    diff: { alignment: 'exact', changed: [], added: [{ rule: 'g', candidate_index: 0 }], removed: [], unchanged: [] },
    caveats: CAVEATS,
  });

  it('toggle ON → Run URL carries ?include=org_values and renders server passing values', async () => {
    const urls: string[] = [];
    await renderToggleRun((u) => {
      urls.push(u);
      return Promise.resolve(makeResp(200, ORG_RESP({ required: true, passing_org_values: ['sre', 'dba'] })));
    });
    // Opt-in disclosure lands in the URL (server-evaluated; ⛔ NOT client-derived).
    expect(urls.some((u) => /[?&]include=org_values/.test(u))).toBe(true);
    expect(await screen.findByText(/caller org values that pass this gate/i)).toBeInTheDocument();
    expect(screen.getByText('sre, dba')).toBeInTheDocument();
  });

  it('unsatisfiable gate → renders the ⚠ "no org value can pass" warning (highest-value signal)', async () => {
    await renderToggleRun(() =>
      Promise.resolve(makeResp(200, ORG_RESP({ required: true, unsatisfiable: true, passing_org_values: [] }))));
    expect(
      await screen.findByText(/No org value can pass this gate \(unsatisfiable\)/i),
    ).toBeInTheDocument();
  });

  it('toggle OFF (default) → URL omits include and org values are not shown', async () => {
    const urls: string[] = [];
    await renderSelectRun((u) => {
      urls.push(u);
      return Promise.resolve(makeResp(200, ORG_RESP({ required: true, passing_org_values: ['sre'] })));
    });
    await screen.findByText(/org123/); // candidate_sha256 → the diff has rendered
    expect(urls.every((u) => !/include=org_values/.test(u))).toBe(true);
    expect(screen.queryByText(/caller org values that pass this gate/i)).toBeNull();
  });

  it('redacted / no org values present → renders the fallback, never crashes on .join', async () => {
    // gate.required but passing_org_values ABSENT (redacted view / not included) —
    // must degrade to the "(no org values)" fallback, ⛔ not throw / show undefined.
    await renderToggleRun(() => Promise.resolve(makeResp(200, ORG_RESP({ required: true }))));
    expect(await screen.findByText(/no org values provided by server/i)).toBeInTheDocument();
  });

  it('toggling the checkbox AFTER Run does NOT change the shown org values (snapshot, ⛔ no UI tearing)', async () => {
    // Run with the toggle ON → org values render. The result carries the
    // Run-time orgValuesRequested snapshot; the render MUST read that snapshot,
    // ⛔ not the live checkbox. If it read live state, flipping the checkbox
    // after Run (no re-fetch) would make the on-screen org values appear/vanish
    // — a UI tear that lies about which request produced the shown diff.
    let calls = 0;
    await renderToggleRun(() => {
      calls += 1;
      return Promise.resolve(makeResp(200, ORG_RESP({ required: true, passing_org_values: ['sre', 'dba'] })));
    });
    expect(await screen.findByText('sre, dba')).toBeInTheDocument();
    // Flip the toggle OFF WITHOUT clicking Run again.
    const checkbox = screen.getByRole('checkbox', { name: /Reveal org values/ }) as HTMLInputElement;
    fireEvent.click(checkbox);
    await waitFor(() => expect(checkbox.checked).toBe(false));
    // ⛔ No new request fired, and the shown result still reflects the ON snapshot.
    expect(calls).toBe(1);
    expect(screen.getByText('sre, dba')).toBeInTheDocument();
    expect(screen.getByText(/caller org values that pass this gate/i)).toBeInTheDocument();
  });
});

describe('AccessReportDryRun — coarse alignment (same-source iron law)', () => {
  // When two rules share a name the server cannot align them 1:1: it downgrades
  // to alignment='coarse' and dumps BOTH sides into added[]+removed[] (changed
  // stays empty). The UI MUST render them as two independent lists + a warning
  // and MUST NEVER re-pair same-name added/removed into a 'changed' card —
  // client-side pairing would violate the same-source iron law (server holds the
  // truth) and could invert a permission-widen into a narrow, misleading an
  // auditor. This guards the constraint the code already honors (findByIndex is
  // index-only, single-directional) so a future refactor cannot regress it.
  const COARSE_RESP = {
    schema_version: 1,
    generated_at: '2026-07-18T00:00:00Z',
    candidate_sha256: 'coarse1',
    baseline: {
      verdict: 'grants_found',
      mode: 'rules',
      grants: [
        { index: 0, rule: 'db-ops', permissions: ['read'], effective: { read: true, write: false, admin: false } },
      ],
    },
    candidate: {
      verdict: 'grants_found',
      mode: 'rules',
      grants: [
        { index: 1, rule: 'db-ops', permissions: ['read', 'admin'], effective: { read: true, write: false, admin: true } },
      ],
    },
    // Same rule name on both sides → server left them UNaligned (coarse).
    diff: {
      alignment: 'coarse',
      changed: [],
      added: [{ rule: 'db-ops', candidate_index: 1 }],
      removed: [{ rule: 'db-ops', live_index: 0 }],
    },
    caveats: CAVEATS,
  };

  it('coarse → warning banner + separate Added/Removed, ⛔ NEVER a re-paired "Changed" card', async () => {
    await renderSelectRun(() => Promise.resolve(makeResp(200, COARSE_RESP)));

    // R3 coarse banner warns without pointing at a side.
    expect(await screen.findByText(/could not align exactly/i)).toBeInTheDocument();
    // Both sides render as their OWN list — never merged.
    expect(screen.getByText('Added grants (candidate only)')).toBeInTheDocument();
    expect(screen.getByText('Removed grants (live only)')).toBeInTheDocument();
    expect(screen.getByText('ADDED')).toBeInTheDocument();
    expect(screen.getByText('REMOVED')).toBeInTheDocument();
    // The same-name rule appears TWICE — once per side, independently (⛔ the UI
    // did not collapse the ambiguous pair into a single entry).
    expect(screen.getAllByText('db-ops')).toHaveLength(2);
    // ⛔ same-source iron law: with server changed=[], the client must NOT invent
    // a 'changed' card by re-pairing the same-name add/remove.
    expect(screen.queryByText('Changed rules')).toBeNull();
    // added/removed are non-empty → this is NOT the empty-diff "no change" path.
    expect(screen.queryByText('No change on the three org-gate axes')).toBeNull();
  });
});
