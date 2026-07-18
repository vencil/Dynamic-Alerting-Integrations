/**
 * Characterization tests for rbac-setup-wizard generators.
 *
 * `rbacGenerateYaml` / `rbacValidate` emit a deployable `_rbac:` YAML
 * block and lint the same group definitions. They are engine-extracted
 * (utils/generators.js) but had ZERO unit coverage before this PR — the
 * output is security-relevant (RBAC scope), so these tests freeze the
 * CURRENT behavior as a safety net for the ROI refactor cycle. Assertions
 * target real, non-trivial substrings of actual output (not empty-vs-empty)
 * to avoid the golden false-green trap.
 *
 * `rbacValidate` reads window.__t (set to English by test-setup.ts) and
 * returns `{ level, msg }` where msg is a thunk — call msg() for the string.
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
  permission: 'editor',
  tenantMode: 'all',
  tenantPrefix: '',
  specificTenants: [],
  environments: [],
  domains: [],
  ...overrides,
});

describe('rbacGenerateYaml', () => {
  it('emits an _rbac: block with the group name, description and permission', () => {
    const yaml = rbacGenerateYaml([group()]);
    expect(yaml.startsWith('_rbac:\n')).toBe(true);
    expect(yaml).toContain('  sre-team:\n');
    expect(yaml).toContain('    description: "SRE on-call"\n');
    expect(yaml).toContain('    permission: editor\n');
  });

  it('tenantMode "all" emits the wildcard tenant list', () => {
    expect(rbacGenerateYaml([group({ tenantMode: 'all' })])).toContain('    tenants: ["*"]\n');
  });

  it('tenantMode "prefix" emits the single-prefix tenant list', () => {
    const yaml = rbacGenerateYaml([group({ tenantMode: 'prefix', tenantPrefix: 'acme-' })]);
    expect(yaml).toContain('    tenants: ["acme-"]\n');
  });

  it('tenantMode "specific" emits a quoted comma-joined tenant list', () => {
    const yaml = rbacGenerateYaml([
      group({ tenantMode: 'specific', specificTenants: ['alpha', 'beta'] }),
    ]);
    expect(yaml).toContain('    tenants: ["alpha", "beta"]\n');
  });

  it('emits a filters block only when environments or domains are set', () => {
    const withFilters = rbacGenerateYaml([
      group({ environments: ['production'], domains: ['payments'] }),
    ]);
    expect(withFilters).toContain('    filters:\n');
    expect(withFilters).toContain('      environments: ["production"]\n');
    expect(withFilters).toContain('      domains: ["payments"]\n');

    // no filters set → no filters block at all
    expect(rbacGenerateYaml([group()])).not.toContain('filters:');
  });

  it('skips groups with no name (they contribute nothing to the YAML)', () => {
    const yaml = rbacGenerateYaml([
      group({ name: '', description: 'DROPPED-desc' }),
      group({ name: 'kept', description: 'kept-desc' }),
    ]);
    expect(yaml).toContain('  kept:\n');
    expect(yaml).toContain('    description: "kept-desc"\n');
    expect(yaml).not.toContain('DROPPED-desc');
  });

  it('property: output always starts with the _rbac: header for any group set', () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            name: fc.string(),
            description: fc.string(),
            permission: fc.constantFrom('viewer', 'editor', 'admin'),
            tenantMode: fc.constant('all'),
            tenantPrefix: fc.constant(''),
            specificTenants: fc.constant([]),
            environments: fc.constant([]),
            domains: fc.constant([]),
          }),
          { maxLength: 5 },
        ),
        (groups) => rbacGenerateYaml(groups).startsWith('_rbac:\n'),
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

  it('flags "specific" mode with no selected tenants as an error', () => {
    const out = msgs([group({ tenantMode: 'specific', specificTenants: [] })]);
    expect(out).toContainEqual({ level: 'error', msg: 'No specific tenants selected' });
  });

  it('a well-formed non-admin group produces no warnings', () => {
    expect(msgs([group({ permission: 'editor', tenantMode: 'all' })])).toEqual([]);
  });
});
