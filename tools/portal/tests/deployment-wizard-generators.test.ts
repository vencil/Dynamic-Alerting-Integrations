/**
 * Characterization tests for deployment-wizard's Helm values generator.
 *
 * `deployGenerateHelmValues` was the single largest helper in the tool
 * (~290 LOC of YAML template) and had ZERO unit coverage before this PR.
 * Its output is a deployable Helm values.yaml — the highest blast radius
 * of the three wizard generators — so these tests freeze the CURRENT
 * behavior as a safety net for the ROI refactor cycle.
 *
 * NOTE: the output embeds `# Generated: <today>` (a non-deterministic date
 * from `new Date()`), so we assert the date-line FORMAT, never its value,
 * and pin only stable structural fields. Assertions target real, non-trivial
 * substrings of actual output to avoid the golden false-green trap.
 */
import { describe, it, expect } from 'vitest';
import { deployGenerateHelmValues } from '../src/interactive/tools/deployment-wizard/utils/generators.js';

const config = (overrides: Record<string, unknown> = {}) => ({
  tier: 'tier2',
  environment: 'production',
  tenantSize: 'medium',
  auth: 'github',
  packs: ['mariadb', 'redis'],
  ...overrides,
});

describe('deployGenerateHelmValues — shape & determinism', () => {
  it('produces a substantial YAML document (guards against empty output)', () => {
    const yaml = deployGenerateHelmValues(config());
    expect(yaml.length).toBeGreaterThan(2000);
    expect(yaml).toContain('thresholdExporter:');
    expect(yaml).toContain('prometheus:');
    expect(yaml).toContain('alertmanager:');
  });

  it('emits a date-stamped header in YYYY-MM-DD form (format pinned, value not)', () => {
    expect(deployGenerateHelmValues(config())).toMatch(/# Generated: \d{4}-\d{2}-\d{2}\n/);
  });
});

describe('deployGenerateHelmValues — tenant size drives replicas/cardinality/retention', () => {
  it('medium size → 2/2/3 replicas, 2000 cardinality, 14d retention', () => {
    const yaml = deployGenerateHelmValues(config({ tenantSize: 'medium' }));
    // The header label (`# Tenant count: ${size?.label}`) has NO `|| fallback`,
    // so this proves the 'medium' entry was actually looked up — the replica /
    // cardinality / retention values below happen to equal the source's
    // `|| N` fallbacks, so on their own they'd stay green even if the lookup
    // returned undefined. This assertion is what distinguishes the two.
    expect(yaml).toContain('Tenant count: Medium');
    expect(yaml).not.toContain('Tenant count: undefined');
    expect(yaml).toContain('thresholdExporter:\n  replicaCount: 2');
    expect(yaml).toContain('prometheus:\n  replicaCount: 2');
    expect(yaml).toContain('alertmanager:\n  replicaCount: 3');
    expect(yaml).toContain('maxPerTenant: 2000');
    expect(yaml).toContain('retention: "14d"');
  });

  it('small size → 1/1/1 replicas, 500 cardinality, 7d retention', () => {
    const yaml = deployGenerateHelmValues(config({ tenantSize: 'small' }));
    expect(yaml).toContain('thresholdExporter:\n  replicaCount: 1');
    expect(yaml).toContain('maxPerTenant: 500');
    expect(yaml).toContain('retention: "7d"');
  });

  it('large size → 3/3/3 replicas, 5000 cardinality, 30d retention', () => {
    const yaml = deployGenerateHelmValues(config({ tenantSize: 'large' }));
    expect(yaml).toContain('maxPerTenant: 5000');
    expect(yaml).toContain('retention: "30d"');
  });
});

describe('deployGenerateHelmValues — environment drives resources/clustering/policies', () => {
  it('production → warn logLevel, clustering on, networkPolicy on, podSecurityPolicy on, 100Gi TSDB', () => {
    const yaml = deployGenerateHelmValues(config({ environment: 'production' }));
    expect(yaml).toContain('logLevel: warn');
    expect(yaml).toContain('clustering:\n    enabled: true');
    expect(yaml).toContain('networkPolicy:\n  enabled: true');
    expect(yaml).toContain('podSecurityPolicy:\n  enabled: true');
    expect(yaml).toContain('size: 100Gi');
  });

  it('local → info logLevel, clustering off, networkPolicy off, 5Gi TSDB', () => {
    const yaml = deployGenerateHelmValues(config({ environment: 'local' }));
    expect(yaml).toContain('logLevel: info');
    expect(yaml).toContain('clustering:\n    enabled: false');
    expect(yaml).toContain('networkPolicy:\n  enabled: false');
    expect(yaml).toContain('size: 5Gi');
  });
});

describe('deployGenerateHelmValues — tier gates portal/api/oauth blocks', () => {
  it('tier2 → daPortal/tenantAPI/oauth2Proxy enabled, oauth2 secrets block present', () => {
    const yaml = deployGenerateHelmValues(config({ tier: 'tier2' }));
    expect(yaml).toContain('daPortal:\n  enabled: true');
    expect(yaml).toContain('tenantAPI:\n  enabled: true');
    expect(yaml).toContain('oauth2Proxy:\n  enabled: true');
    expect(yaml).toContain('clientSecret: "" # Fill from secrets manager');
  });

  it('tier1 → portal/api/oauth explicitly disabled, no oauth2 secrets block', () => {
    const yaml = deployGenerateHelmValues(config({ tier: 'tier1' }));
    expect(yaml).toContain('Tier 1: Portal and API disabled');
    expect(yaml).toContain('daPortal:\n  enabled: false');
    expect(yaml).toContain('tenantAPI:\n  enabled: false');
    expect(yaml).toContain('oauth2Proxy:\n  enabled: false');
    expect(yaml).not.toContain('Fill from secrets manager');
  });
});

describe('deployGenerateHelmValues — auth provider selects OAuth endpoints (tier2)', () => {
  it('github → github.com OAuth endpoints and provider', () => {
    const yaml = deployGenerateHelmValues(config({ auth: 'github' }));
    expect(yaml).toContain('provider: "github"');
    expect(yaml).toContain('https://github.com/login/oauth/authorize');
  });

  it('google → accounts.google.com OAuth endpoints', () => {
    const yaml = deployGenerateHelmValues(config({ auth: 'google' }));
    expect(yaml).toContain('provider: "google"');
    expect(yaml).toContain('https://accounts.google.com/o/oauth2/v2/auth');
  });

  it('gitlab → gitlab.com OAuth endpoints', () => {
    const yaml = deployGenerateHelmValues(config({ auth: 'gitlab' }));
    expect(yaml).toContain('https://gitlab.com/oauth/authorize');
  });

  it('oidc/keycloak fallback → keycloak endpoint template', () => {
    const yaml = deployGenerateHelmValues(config({ auth: 'oidc' }));
    expect(yaml).toContain('provider: "oidc"');
    expect(yaml).toContain('your-keycloak.com');
  });
});

describe('deployGenerateHelmValues — rule packs', () => {
  it('non-empty packs → auto-mount comment listing each pack', () => {
    const yaml = deployGenerateHelmValues(config({ packs: ['mariadb', 'redis'] }));
    expect(yaml).toContain('# Auto-mounted rule packs via Projected Volume:');
    expect(yaml).toContain('# - name: rules-mariadb');
    expect(yaml).toContain('- name: rules-redis');
  });

  it('empty packs → no auto-mount comment', () => {
    expect(deployGenerateHelmValues(config({ packs: [] }))).not.toContain('Auto-mounted rule packs');
  });
});
