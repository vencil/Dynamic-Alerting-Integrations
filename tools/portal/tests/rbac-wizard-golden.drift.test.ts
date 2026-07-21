/**
 * Wizard → parser drift tripwire, JS end (ADR-027 / LD-6 P7d).
 *
 * The RBAC Setup Wizard's `rbacGenerateYaml` emits an `_rbac.yaml` that the
 * tenant-api strict parser must accept — the pre-P7d output drifted so far it
 * was rejected at line 1. This test clamps the generator's output to the
 * committed fixtures under
 *   components/tenant-api/internal/rbac/testdata/wizard/*.yaml
 * with BUFFER-EXACT equality: any change to the generator that isn't mirrored
 * into a fixture reddens here.
 *
 * The Go end (components/tenant-api/internal/rbac/wizard_drift_test.go)
 * `//go:embed`s those same fixtures and proves they load through
 * ParseCandidateConfig with liveness assertions. Two ends, one snapshot:
 *   - this file binds  generator output  ==  fixture bytes
 *   - the Go file binds fixture bytes     parse-load + semantically live
 * Because the fixtures live under components/tenant-api/**, regenerating one
 * flips the CI `go_changed` path filter so the Go leg runs on a portal-only PR.
 *
 * We deliberately do NOT re-validate the YAML against docs/schemas/rbac.schema.json
 * here: the Go leg already parses the identical bytes through the real engine
 * the schema only mirrors (and asserts the permission enum via permCovers), so a
 * JS-side schema check would add a js-yaml + ajv dependency for zero marginal
 * coverage. The byte-clamp + Go parse close the loop.
 *
 * The INPUTS below are the canonical fixture inputs; they MUST stay identical to
 * the ones the fixtures were generated from. If you change an input, regenerate
 * the fixture (see the Go file's header) — do not hand-edit the .yaml.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { rbacGenerateYaml } from '../src/interactive/tools/rbac-setup-wizard/utils/generators.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_DIR = resolve(__dirname, '../../../components/tenant-api/internal/rbac/testdata/wizard');

// Default group shape mirrors the wizard's addGroup() initializer.
const g = (o: Record<string, unknown> = {}) => ({
  name: 'g', description: '', permission: 'read', tenantMode: 'all',
  specificTenants: [], tenantPrefix: '', environments: [], domains: [],
  claims: [], orgScope: '', ...o,
});

// Canonical fixture INPUTS — keep identical to the Go corpus + expected count.
const FIXTURES: Record<string, ReturnType<typeof g>[]> = {
  'f1-legacy-full': [g({ name: 'platform-admins', permission: 'admin', tenantMode: 'all',
    environments: ['production', 'staging'], domains: ['finance'] })],
  'f2-match-orgscope-prefix': [g({ name: 'org-ops', permission: 'write', tenantMode: 'prefix', tenantPrefix: 'acme-',
    claims: [{ key: 'org-code', values: ['006000J', '006001K'] }], orgScope: 'org-code' })],
  'f3-match-groups-only-specific': [g({ name: 'sre-oncall', permission: 'read', tenantMode: 'specific',
    specificTenants: ['alpha', 'beta'], claims: [{ key: 'region', values: ['emea'] }] })],
  'f4-multi-group': [
    g({ name: 'platform-admins', permission: 'admin', tenantMode: 'all', domains: ['finance'] }),
    g({ name: 'org-ops', permission: 'write', tenantMode: 'prefix', tenantPrefix: 'acme-',
      claims: [{ key: 'org-code', values: ['006000J'] }], orgScope: 'org-code' }),
    g({ name: 'sre-oncall', permission: 'read', tenantMode: 'specific', specificTenants: ['alpha'] }),
  ],
  'f5-zero-groups': [],
};

describe('rbac wizard → parser drift tripwire (byte-clamp)', () => {
  // Mirrors wizardExpectedFixtureCount in the Go file — losing or adding a
  // fixture without wiring both ends reddens.
  it('covers exactly the expected fixture set', () => {
    expect(Object.keys(FIXTURES).sort()).toEqual([
      'f1-legacy-full',
      'f2-match-orgscope-prefix',
      'f3-match-groups-only-specific',
      'f4-multi-group',
      'f5-zero-groups',
    ]);
  });

  for (const [name, groups] of Object.entries(FIXTURES)) {
    it(`${name}: generator output is byte-identical to the committed fixture`, () => {
      const expected = readFileSync(resolve(FIXTURE_DIR, `${name}.yaml`));
      const actual = Buffer.from(rbacGenerateYaml(groups), 'utf8');
      // Buffer-exact (not toContain / not parsed-deep-equal): indentation,
      // quoting and trailing whitespace are all real regressions for output an
      // operator copy-pastes.
      expect(actual.equals(expected)).toBe(true);
    });
  }
});
