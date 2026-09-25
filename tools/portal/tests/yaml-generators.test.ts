/**
 * Unit tests for tenant-manager/utils/yaml-generators.js
 * — Vitest next-batch (PR-3 of expansion).
 *
 * Both functions are pure string assembly — no React, no globals, no
 * side effects. Tests cover happy path, empty input, single tenant,
 * multi-tenant, and YAML structure invariants (apiVersion / kind /
 * metadata / data sections present).
 */
import { describe, it, expect } from 'vitest';
import {
  generateMaintenanceYaml,
  generateSilentModeYaml,
} from '../src/interactive/tools/tenant-manager/utils/yaml-generators.js';

describe('generateMaintenanceYaml', () => {
  it('emits the standard ConfigMap envelope', () => {
    const yaml = generateMaintenanceYaml(['tenant-a']);
    expect(yaml).toContain('apiVersion: v1');
    expect(yaml).toContain('kind: ConfigMap');
    expect(yaml).toContain('metadata:');
    expect(yaml).toContain('name: tenant-operational-modes');
    expect(yaml).toContain('namespace: monitoring');
    expect(yaml).toContain('data:');
  });

  it('emits one data block per tenant with _maintenance suffix', () => {
    const yaml = generateMaintenanceYaml(['db-a', 'db-b']);
    expect(yaml).toContain('  db-a_maintenance: |');
    expect(yaml).toContain('  db-b_maintenance: |');
  });

  it('each block contains mode/reason/expires', () => {
    const yaml = generateMaintenanceYaml(['db-a']);
    expect(yaml).toContain('mode: maintenance');
    expect(yaml).toContain('reason: "Scheduled maintenance"');
    expect(yaml).toMatch(/expires: "\d{4}-\d{2}-\d{2}T/);
  });

  it('handles empty tenant list (envelope only)', () => {
    const yaml = generateMaintenanceYaml([]);
    expect(yaml).toContain('data:');
    // Empty data section — no _maintenance entries.
    expect(yaml).not.toContain('_maintenance:');
  });

  it('preserves tenant order in the output', () => {
    const yaml = generateMaintenanceYaml(['z-tenant', 'a-tenant', 'm-tenant']);
    const idxZ = yaml.indexOf('z-tenant_maintenance');
    const idxA = yaml.indexOf('a-tenant_maintenance');
    const idxM = yaml.indexOf('m-tenant_maintenance');
    expect(idxZ).toBeGreaterThanOrEqual(0);
    expect(idxA).toBeGreaterThan(idxZ);
    expect(idxM).toBeGreaterThan(idxA);
  });
});

describe('generateSilentModeYaml', () => {
  const NOW = new Date('2026-09-25T10:00:00.123Z');

  it('emits a _silent_mode block per tenant, not a ConfigMap (#1988)', () => {
    const yaml = generateSilentModeYaml(['db-a', 'db-b'], NOW);
    expect(yaml).not.toContain('kind: ConfigMap');
    expect(yaml).toContain('#   db-a:');
    expect(yaml).toContain('#   db-b:');
    expect(yaml.match(/^    _silent_mode:$/gm)).toHaveLength(2);
  });

  it('uses the structured form with an RFC3339 expires 24h after now', () => {
    const yaml = generateSilentModeYaml(['db-a'], NOW);
    expect(yaml).toContain('      target: "all"');
    expect(yaml).toContain('      expires: "2026-09-26T10:00:00Z"');
    expect(yaml).toContain('      reason: "Under investigation"');
  });

  it('handles empty tenant list', () => {
    expect(generateSilentModeYaml([], NOW)).toBe('');
  });
});

describe('cross-function invariants', () => {
  it('output is a string ending without trailing newline', () => {
    // join('\n') with no trailing element → last char is content, not '\n'.
    const yaml = generateMaintenanceYaml(['t']);
    expect(yaml.endsWith('\n')).toBe(false);
    expect(typeof yaml).toBe('string');
  });
});
