/**
 * alert-builder/builder.js — PrometheusRule YAML builder + validators.
 *
 * Extracted from alert-builder.jsx (PR-portal-19), previously 0%-covered.
 * buildYaml is pure string assembly; tests assert the composed rule fields and
 * the default-fallback behavior, plus the identifier/threshold validators.
 */
import { describe, it, expect } from 'vitest';
import {
  isValidAlertName,
  isValidGroupName,
  isValidLabelKey,
  isValidThreshold,
  buildYaml,
  canAdvance,
} from '../src/interactive/tools/alert-builder/builder.js';

describe('buildYaml', () => {
  it('composes a full PrometheusRule from the wizard config', () => {
    const yaml = buildYaml({
      alertName: 'HighCPU',
      groupName: 'cpu-alerts',
      summary: 'CPU high',
      expression: 'rate(cpu[5m])',
      op: '>',
      threshold: '0.8',
      forDuration: '10m',
      severity: 'critical',
      description: 'CPU over 80%',
      labels: { team: 'sre' },
    });
    expect(yaml).toContain('- name: cpu-alerts');
    expect(yaml).toContain('- alert: HighCPU');
    expect(yaml).toContain('expr: rate(cpu[5m]) > 0.8');
    expect(yaml).toContain('for: 10m');
    expect(yaml).toContain('severity: critical');
    expect(yaml).toContain('team: sre');
    expect(yaml).toContain('summary: "CPU high"');     // JSON.stringify-quoted
    expect(yaml).toContain('description: "CPU over 80%"');
  });

  it('falls back to sensible defaults and omits description when absent', () => {
    const yaml = buildYaml({});
    expect(yaml).toContain('- name: my-alerts');
    expect(yaml).toContain('- alert: MyAlert');
    expect(yaml).toContain('expr: rate(metric[5m]) > 0');
    expect(yaml).toContain('for: 5m');
    expect(yaml).toContain('severity: warning');
    expect(yaml).toContain('summary: "MyAlert fired"');
    expect(yaml).not.toContain('description:');
  });
});

describe('validators', () => {
  it('isValidAlertName rejects dashes; isValidGroupName allows them', () => {
    expect(isValidAlertName('HighCPU')).toBe(true);
    expect(isValidAlertName('with-dash')).toBe(false);
    expect(isValidGroupName('my-alerts')).toBe(true); // group names allow '-'
    expect(isValidGroupName('1bad')).toBe(false);
  });

  it('isValidLabelKey requires a lowercase-led snake key', () => {
    expect(isValidLabelKey('severity')).toBe(true);
    expect(isValidLabelKey('Severity')).toBe(false);
  });

  it('isValidThreshold accepts numeric strings only', () => {
    expect(isValidThreshold('0.8')).toBe(true);
    expect(isValidThreshold('80')).toBe(true);
    expect(isValidThreshold('abc')).toBe(false);
    expect(isValidThreshold('')).toBe(false);
    expect(isValidThreshold('   ')).toBe(false);
  });
});

describe('canAdvance', () => {
  it('step 0 needs a valid alert name, group name and a summary', () => {
    expect(canAdvance(0, { alertName: 'A', groupName: 'g', summary: 'x' })).toBe(true);
    expect(canAdvance(0, { alertName: '1bad', groupName: 'g', summary: 'x' })).toBe(false);
    expect(canAdvance(0, { alertName: 'A', groupName: 'g', summary: '  ' })).toBe(false);
  });

  it('step 1 needs expression, op, numeric threshold and a duration', () => {
    expect(canAdvance(1, { expression: 'x', op: '>', threshold: '5', forDuration: '5m' })).toBe(true);
    expect(canAdvance(1, { expression: 'x', op: '>', threshold: 'abc', forDuration: '5m' })).toBe(false);
  });

  it('step 2 needs a severity; final step always advances', () => {
    expect(canAdvance(2, { severity: 'warning' })).toBe(true);
    expect(canAdvance(2, { severity: '' })).toBe(false);
    expect(canAdvance(3, {})).toBe(true);
  });
});
