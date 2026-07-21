/**
 * Unit tests for rbac-setup-wizard generators (ADR-027 / LD-6 P7d).
 *
 * `rbacGenerateYaml` emits a `groups:` list `_rbac.yaml` matching the tenant-api
 * strict RBAC parser (rbac.go RBACConfig / GroupRule / MatchBlock); `rbacValidate`
 * lints the same group definitions. These freeze the CURRENT, parser-VALID
 * behavior after the P7d drift fix (the pre-P7d output — a top-level `_rbac:`
 * map with a `description:` key, singular `permission:` and nested `filters:` —
 * was rejected wholesale by the parser).
 *
 * Assertions target real, non-trivial substrings of actual output (not
 * empty-vs-empty) to avoid the golden false-green trap. The Buffer-exact
 * whole-file clamp lives in rbac-wizard-golden.drift.test.ts; here we pin the
 * shape rules a reader would reason about.
 *
 * `rbacValidate` reads window.__t (set to English by test-setup.ts) and returns
 * `{ level, msg }` where msg is a thunk — call msg() for the string.
 */
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import {
  rbacGenerateYaml,
  rbacValidate,
} from '../src/interactive/tools/rbac-setup-wizard/utils/generators.js';

const group = (overrides: Record<string, unknown> = {}) => ({
  name: 'sre-team',
  description: 'SRE on-call',
  permission: 'read',
  tenantMode: 'all',
  tenantPrefix: '',
  specificTenants: [],
  environments: [],
  domains: [],
  claims: [],
  orgScope: '',
  ...overrides,
});

describe('rbacGenerateYaml — shape', () => {
  it('emits a top-level groups: list (NOT the pre-P7d _rbac: map)', () => {
    const yaml = rbacGenerateYaml([group()]);
    expect(yaml.startsWith('groups:\n')).toBe(true);
    expect(yaml).toContain('  - name: sre-team\n');
    // Regression guards for the exact drift this PR fixes:
    expect(yaml).not.toContain('_rbac:');
    expect(yaml).not.toContain('description:');
    expect(yaml).not.toContain('\n    permission:'); // singular scalar is gone
    expect(yaml).not.toContain('filters:'); // env/domains are flat now
  });

  it('emits permissions as a one-element list for the selected level', () => {
    expect(rbacGenerateYaml([group({ permission: 'admin' })])).toContain('    permissions: [admin]\n');
  });

  it('the free-text description is a UI-only hint and is never emitted', () => {
    const yaml = rbacGenerateYaml([group({ description: 'LEAKED-INTENT' })]);
    expect(yaml).not.toContain('LEAKED-INTENT');
  });
});

describe('rbacGenerateYaml — tenant modes', () => {
  it('tenantMode "all" emits the wildcard tenant list (quoted — bare * is a YAML alias)', () => {
    expect(rbacGenerateYaml([group({ tenantMode: 'all' })])).toContain('    tenants: ["*"]\n');
  });

  it('tenantMode "prefix" APPENDS a trailing * when the operator omitted it', () => {
    // The pre-P7d generator emitted `tenants: ["acme-"]` — an EXACT match in Go,
    // not a prefix. P7d fixes this: a prefix mode must emit a real prefix.
    const yaml = rbacGenerateYaml([group({ tenantMode: 'prefix', tenantPrefix: 'acme-' })]);
    expect(yaml).toContain('    tenants: ["acme-*"]\n');
  });

  it('tenantMode "prefix" does not double a * the operator already typed', () => {
    const yaml = rbacGenerateYaml([group({ tenantMode: 'prefix', tenantPrefix: 'acme-*' })]);
    expect(yaml).toContain('    tenants: ["acme-*"]\n');
  });

  it('tenantMode "specific" emits a quoted comma-joined tenant list', () => {
    const yaml = rbacGenerateYaml([
      group({ tenantMode: 'specific', specificTenants: ['alpha', 'beta'] }),
    ]);
    expect(yaml).toContain('    tenants: ["alpha", "beta"]\n');
  });

  it('omits the tenants key entirely when a mode yields nothing', () => {
    const yaml = rbacGenerateYaml([group({ tenantMode: 'specific', specificTenants: [] })]);
    expect(yaml).not.toContain('tenants:');
  });
});

describe('rbacGenerateYaml — flat environments / domains', () => {
  it('emits flat environments / domains only when set (never under filters:)', () => {
    const withFilters = rbacGenerateYaml([
      group({ environments: ['production'], domains: ['payments'] }),
    ]);
    expect(withFilters).not.toContain('filters:');
    expect(withFilters).toContain('    environments: ["production"]\n');
    expect(withFilters).toContain('    domains: ["payments"]\n');
    expect(rbacGenerateYaml([group()])).not.toContain('environments:');
    expect(rbacGenerateYaml([group()])).not.toContain('domains:');
  });
});

describe('rbacGenerateYaml — claims / org-scope axis', () => {
  it('emits NO match block when no claims are configured (legacy shape preserved)', () => {
    expect(rbacGenerateYaml([group()])).not.toContain('match:');
  });

  it('emits match:{groups:[name],claims} when claims are configured — name stays the matcher', () => {
    const yaml = rbacGenerateYaml([
      group({ name: 'org-ops', claims: [{ key: 'org-code', values: ['006000J', '006001K'] }] }),
    ]);
    expect(yaml).toContain('    match:\n');
    expect(yaml).toContain('      groups: ["org-ops"]\n');
    expect(yaml).toContain('      claims:\n');
    expect(yaml).toContain('        "org-code": ["006000J", "006001K"]\n');
  });

  it('never emits a claims-only match block (a groups-less match widens the rule)', () => {
    // Even with claims present, groups:[name] is always emitted first — there is
    // no wizard state that produces a match block without a groups: line.
    const yaml = rbacGenerateYaml([
      group({ name: 'x', claims: [{ key: 'k', values: ['v'] }] }),
    ]);
    const matchIdx = yaml.indexOf('match:');
    const groupsIdx = yaml.indexOf('groups:', matchIdx);
    const claimsIdx = yaml.indexOf('claims:', matchIdx);
    expect(matchIdx).toBeGreaterThanOrEqual(0);
    expect(groupsIdx).toBeGreaterThan(matchIdx);
    expect(groupsIdx).toBeLessThan(claimsIdx);
  });

  it('drops a half-typed claim (a key with no values, or values with no key)', () => {
    const yaml = rbacGenerateYaml([
      group({ name: 'x', claims: [{ key: 'k', values: [] }, { key: '', values: ['v'] }] }),
    ]);
    expect(yaml).not.toContain('match:');
  });

  it('emits org-scope when set', () => {
    expect(rbacGenerateYaml([group({ orgScope: 'org-code' })])).toContain('    org-scope: org-code\n');
  });
});

describe('rbacGenerateYaml — YAML-safety of the name / org-scope block scalars', () => {
  // These names would otherwise be emitted BARE and either break the load
  // (trailing colon → YAML mapping) or decode to the wrong type (null family →
  // empty string → a silently dead rule). They must be quoted.
  it('quotes a name ending in a colon (bare would make YAML read a mapping)', () => {
    expect(rbacGenerateYaml([group({ name: 'platform:' })])).toContain('  - name: "platform:"\n');
  });

  it('quotes a YAML null-family name so it does not decode to an empty Name', () => {
    for (const n of ['null', 'Null', 'NULL', 'true', 'false']) {
      expect(rbacGenerateYaml([group({ name: n })])).toContain(`  - name: "${n}"\n`);
    }
  });

  it('quotes a null-family org-scope key', () => {
    expect(rbacGenerateYaml([group({ orgScope: 'null' })])).toContain('    org-scope: "null"\n');
  });

  it('still emits an ordinary identifier name bare', () => {
    expect(rbacGenerateYaml([group({ name: 'sre-team' })])).toContain('  - name: sre-team\n');
  });
});

describe('rbacGenerateYaml — structural invariants', () => {
  it('skips groups with no name (they contribute nothing to the YAML)', () => {
    const yaml = rbacGenerateYaml([
      group({ name: '', description: 'DROPPED' }),
      group({ name: 'kept' }),
    ]);
    expect(yaml).toContain('  - name: kept\n');
    expect(yaml).not.toContain('DROPPED');
  });

  it('property: emits exactly one rule block per NAMED group', () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            name: fc.constantFrom('', 'team-a', 'team-b', 'sre', 'ops'),
            description: fc.string(),
            permission: fc.constantFrom('read', 'write', 'admin'),
            tenantMode: fc.constant('all'),
            tenantPrefix: fc.constant(''),
            specificTenants: fc.constant([] as string[]),
            environments: fc.constant([] as string[]),
            domains: fc.constant([] as string[]),
            claims: fc.constant([] as unknown[]),
            orgScope: fc.constant(''),
          }),
          { maxLength: 6 },
        ),
        (groups) => {
          const yaml = rbacGenerateYaml(groups);
          const namedCount = groups.filter((x) => x.name).length;
          const emitted = (yaml.match(/\n {2}- name: /g) || []).length;
          return yaml.startsWith('groups:\n') && emitted === namedCount;
        },
      ),
    );
  });
});

describe('rbacValidate', () => {
  const msgs = (groups: unknown[]) => rbacValidate(groups).map((w: any) => ({ level: w.level, msg: w.msg() }));

  it('flags a missing group name as an error', () => {
    const out = msgs([group({ name: '' })]);
    expect(out).toContainEqual({ level: 'error', msg: 'Group name cannot be empty' });
  });

  it('flags a missing permission as an error', () => {
    const out = msgs([group({ permission: '' })]);
    expect(out).toContainEqual({ level: 'error', msg: 'Permission not set' });
  });

  it('warns when an admin group can access all tenants (over-broad scope)', () => {
    const out = msgs([group({ tenantMode: 'all', permission: 'admin' })]);
    expect(out.some((w) => w.level === 'warning' && /very broad/.test(w.msg))).toBe(true);
  });

  it('flags a group that yields no tenants (specific mode, empty list) as an error', () => {
    const out = msgs([group({ tenantMode: 'specific', specificTenants: [] })]);
    expect(out.some((w) => w.level === 'error' && /grants no tenants/.test(w.msg))).toBe(true);
  });

  it('flags a claim key with disallowed characters as an error', () => {
    const out = msgs([group({ claims: [{ key: 'bad key!', values: ['v'] }] })]);
    expect(out.some((w) => w.level === 'error' && /Claim key .* disallowed/.test(w.msg))).toBe(true);
  });

  it('warns that claim / org-scope keys must be declared or the file fails to load', () => {
    const out = msgs([group({ orgScope: 'org-code' })]);
    expect(out.some((w) => w.level === 'warning' && /--identity-claim-headers/.test(w.msg))).toBe(true);
  });

  it('flags a whitespace-only group name as an error', () => {
    const out = msgs([group({ name: '   ' })]);
    expect(out.some((w) => w.level === 'error' && /only whitespace/.test(w.msg))).toBe(true);
  });

  it('warns that a claim row with a key but no values will be dropped', () => {
    const out = msgs([group({ claims: [{ key: 'org-code', values: [] }] })]);
    expect(out.some((w) => w.level === 'warning' && /no values and will be dropped/.test(w.msg))).toBe(true);
  });

  it('warns that a claim row with values but no key will be dropped', () => {
    const out = msgs([group({ claims: [{ key: '', values: ['acme'] }] })]);
    expect(out.some((w) => w.level === 'warning' && /no key and will be dropped/.test(w.msg))).toBe(true);
  });

  it('a well-formed non-admin group produces no warnings', () => {
    expect(msgs([group({ permission: 'read', tenantMode: 'all' })])).toEqual([]);
  });
});
