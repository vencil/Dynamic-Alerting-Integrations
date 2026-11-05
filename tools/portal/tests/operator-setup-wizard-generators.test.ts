/**
 * Characterization tests for operator-setup-wizard generators.
 *
 * Four pure functions (validateTenantName / generateOperatorCommand /
 * generateMigrationCommand / generateRoutingCommands) emit deployable CLI
 * commands. Assertions target real, non-trivial substrings of actual output
 * to avoid the golden false-green trap.
 *
 * #3572: routing is no AlertmanagerConfig CRD — operator-generate refuses
 * `--components alertmanager` and the receiver / secret / api-version flags
 * (rc 2), so the wizard emits none of them and shows the generate-routes +
 * Secret + configSecret commands instead of an AlertmanagerConfig preview.
 */
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import {
  validateTenantName,
  generateOperatorCommand,
  generateMigrationCommand,
  generateRoutingCommands,
  ROUTING_SECRET,
} from '../src/interactive/tools/operator-setup-wizard/utils/generators.js';

const config = (overrides: Record<string, unknown> = {}) => ({
  crdVersion: 'v1beta1',
  namespace: 'monitoring',
  ruleMode: 'operator',
  receiverType: 'slack',
  receiverSecret: 'slack-secret',
  selectedTenants: ['alpha', 'beta'],
  operatorVersion: '',
  ...overrides,
});

describe('validateTenantName', () => {
  it('accepts RFC-1123 compliant names', () => {
    expect(validateTenantName('alpha')).toBe(true);
    expect(validateTenantName('team-a1')).toBe(true);
    expect(validateTenantName('a')).toBe(true);
  });

  it('rejects names with uppercase, leading/trailing hyphen, or invalid chars', () => {
    expect(validateTenantName('Alpha')).toBe(false);
    expect(validateTenantName('-alpha')).toBe(false);
    expect(validateTenantName('alpha-')).toBe(false);
    expect(validateTenantName('a_b')).toBe(false);
    expect(validateTenantName('')).toBe(false);
  });

  // ADR-035: the rule is read from the generated tenant-id.json, which carries
  // the 63-character limit the old local regex lacked.
  it('enforces the 63-character limit of the shared tenant-id rule', () => {
    expect(validateTenantName('a'.repeat(63))).toBe(true);
    expect(validateTenantName('a'.repeat(64))).toBe(false);
    expect(validateTenantName('abc\n')).toBe(false);
  });
});

// The flags that only fed AlertmanagerConfig, and the ones operator-generate
// never had: a command carrying any of them exits 2.
const REFUSED_OR_UNKNOWN = [
  '--components', '--config-dir', '--api-version', '--crd-version',
  '--receiver-template', '--receiver-type', '--receiver-secret',
  '--secret-name', '--secret-key', '--tenants', '--rule-mode', '--operator-version',
];

describe('generateOperatorCommand', () => {
  it('builds an operator-generate command from flags the tool has', () => {
    const out = generateOperatorCommand(config());
    expect(out.startsWith('da-tools operator-generate')).toBe(true);
    expect(out).toContain('--rule-packs-dir rule-packs');
    expect(out).toContain('--namespace=monitoring');
    expect(out).toContain('--output-dir operator-crds/');
  });

  it('carries no routing, receiver or unknown flag whatever the config holds', () => {
    const out = generateOperatorCommand(config({ operatorVersion: '0.9.0', crdVersion: 'v1alpha1' }));
    for (const flag of REFUSED_OR_UNKNOWN) expect(out).not.toContain(flag);
    expect(out).not.toContain('alertmanager');
  });

  it('applies the namespace default and continues lines with a backslash-newline', () => {
    const out = generateOperatorCommand(config({ namespace: undefined }));
    expect(out).toContain('--namespace=monitoring');
    expect(out).toContain(' \\\n  --namespace=monitoring');
  });
});

describe('generateMigrationCommand', () => {
  it('returns null unless ruleMode is dual-stack', () => {
    expect(generateMigrationCommand(config({ ruleMode: 'operator' }))).toBeNull();
    expect(generateMigrationCommand(config({ ruleMode: 'configmap' }))).toBeNull();
  });

  it('emits a migrate-to-operator --dry-run command in dual-stack mode', () => {
    const out = generateMigrationCommand(config({ ruleMode: 'dual-stack' }));
    expect(out).toContain('da-tools migrate-to-operator');
    expect(out).toContain('--source-dir configmaps/');
    expect(out).toContain('--namespace=monitoring');
    expect(out).toContain('--dry-run');
    for (const flag of REFUSED_OR_UNKNOWN) expect(out).not.toContain(flag);
  });
});

describe('generateRoutingCommands', () => {
  it('renders alertmanager.yaml with generate-routes and hands it over as the configSecret', () => {
    const out = generateRoutingCommands(config({ namespace: 'obs' }));
    expect(out).toContain('da-tools generate-routes --config-dir conf.d --output-alertmanager');
    expect(out).toContain('-o alertmanager.yaml');
    expect(out).toContain(`kubectl create secret generic ${ROUTING_SECRET} -n obs`);
    expect(out).toContain('--from-file=alertmanager.yaml');
    expect(out).toContain(`configSecret to ${ROUTING_SECRET}`);
    expect(out).toContain('alertmanager.alertmanagerSpec.configSecret');
  });

  it('never renders an AlertmanagerConfig', () => {
    expect(generateRoutingCommands(config())).not.toContain('kind: AlertmanagerConfig');
  });

  it('property: the Secret lands in the chosen namespace', () => {
    fc.assert(
      fc.property(fc.stringMatching(/^[a-z][a-z0-9-]{0,20}$/), (ns) =>
        generateRoutingCommands(config({ namespace: ns })).includes(`-n ${ns} `)),
    );
  });
});
