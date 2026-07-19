/**
 * Characterization tests for operator-setup-wizard generators.
 *
 * Five pure functions (validateTenantName / generateOperatorCommand /
 * generateMigrationCommand / generateAlertmanagerConfigPreview /
 * getReceiverConfig) had ZERO unit coverage before this PR. They emit
 * deployable CLI commands and an AlertmanagerConfig CRD, so these tests
 * freeze the CURRENT behavior as a safety net for the ROI refactor cycle.
 * Assertions target real, non-trivial substrings of actual output to avoid
 * the golden false-green trap.
 */
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import {
  validateTenantName,
  generateOperatorCommand,
  generateMigrationCommand,
  generateAlertmanagerConfigPreview,
  getReceiverConfig,
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
});

describe('generateOperatorCommand', () => {
  it('builds the da-tools operator-generate command with all flags', () => {
    const out = generateOperatorCommand(config());
    expect(out.startsWith('da-tools operator-generate')).toBe(true);
    expect(out).toContain('--crd-version=v1beta1');
    expect(out).toContain('--namespace=monitoring');
    expect(out).toContain('--rule-mode=operator');
    expect(out).toContain('--receiver-type=slack');
    expect(out).toContain('--receiver-secret=slack-secret');
    expect(out).toContain('--tenants=alpha,beta');
  });

  it('applies defaults when crdVersion/namespace/ruleMode are absent', () => {
    const out = generateOperatorCommand(config({ crdVersion: undefined, namespace: undefined, ruleMode: undefined }));
    expect(out).toContain('--crd-version=v1beta1');
    expect(out).toContain('--namespace=monitoring');
    expect(out).toContain('--rule-mode=operator');
  });

  it('omits --tenants when no tenants are selected, and adds --operator-version when set', () => {
    const noTenants = generateOperatorCommand(config({ selectedTenants: [] }));
    expect(noTenants).not.toContain('--tenants=');

    const versioned = generateOperatorCommand(config({ operatorVersion: '0.9.0' }));
    expect(versioned).toContain('--operator-version=0.9.0');
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
    expect(out).toContain('--namespace=monitoring');
    expect(out).toContain('--tenants=alpha,beta');
    expect(out).toContain('--dry-run');
  });
});

describe('getReceiverConfig', () => {
  it('returns a secret-templated fragment for each known receiver type', () => {
    expect(getReceiverConfig('slack', 'my-secret')).toContain('webhook_url');
    expect(getReceiverConfig('slack', 'my-secret')).toContain('"my-secret"');
    expect(getReceiverConfig('pagerduty', 'pd')).toContain('service_key');
    expect(getReceiverConfig('email', 'em')).toContain('authUsername');
    expect(getReceiverConfig('opsgenie', 'og')).toContain('api_key');
  });

  it('falls back to a placeholder webhook URL for unknown receiver types', () => {
    expect(getReceiverConfig('carrier-pigeon', 's')).toBe('url: "https://example.com/webhook"');
  });
});

describe('generateAlertmanagerConfigPreview', () => {
  it('renders an AlertmanagerConfig CRD for the selected tenant', () => {
    const out = generateAlertmanagerConfigPreview(config(), 0);
    expect(out).toContain('kind: AlertmanagerConfig');
    expect(out).toContain('name: alpha-alertmanager-config');
    expect(out).toContain("receiver: 'slack'");
    expect(out).toContain('slackConfigs:');
    // the receiver fragment is inlined
    expect(out).toContain('webhook_url');
  });

  it('honors the tenant index', () => {
    expect(generateAlertmanagerConfigPreview(config(), 1)).toContain('name: beta-alertmanager-config');
  });

  it('returns an empty string when the tenant index is out of range', () => {
    expect(generateAlertmanagerConfigPreview(config({ selectedTenants: [] }), 0)).toBe('');
  });

  it('property: every rendered CRD names the AlertmanagerConfig kind', () => {
    fc.assert(
      fc.property(
        fc.array(fc.stringMatching(/^[a-z][a-z0-9-]{0,20}$/), { minLength: 1, maxLength: 4 }),
        (tenants) => {
          const out = generateAlertmanagerConfigPreview(config({ selectedTenants: tenants }), 0);
          return out.includes('kind: AlertmanagerConfig') && out.includes(`name: ${tenants[0]}-alertmanager-config`);
        },
      ),
    );
  });
});
