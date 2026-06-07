/**
 * playground/validation.js — playground-local YAML parser + tenant-config validator.
 *
 * Extracted from playground.jsx (PR-portal-16), previously 0%-covered. These
 * functions are intentionally NOT the _common/validation/yaml-parser.js ones —
 * the parseDuration contract differs (object+ms / s,m,h here vs seconds /
 * s,m,h,d in _common), so the suite also pins that difference.
 */
import { describe, it, expect } from 'vitest';
import {
  validateTenantConfig,
  parseYAML,
  parseDuration,
} from '../src/interactive/tools/playground/validation.js';

const VALID = `tenants:
  db-a:
    mysql_connections: "70"
    mysql_connections_critical: "95"
    mysql_cpu: "80"
    _silent_mode: "disable"
    _routing:
      receiver_type: "webhook"
      webhook_url: "https://webhook.example.com/alerts"
      group_wait: "30s"
      repeat_interval: "4h"`;

describe('parseDuration (playground-local contract)', () => {
  it('returns {ms,value,unit} for s/m/h', () => {
    expect(parseDuration('30s')).toEqual({ ms: 30000, value: 30, unit: 's' });
    expect(parseDuration('5m')).toEqual({ ms: 300000, value: 5, unit: 'm' });
    expect(parseDuration('2h')).toEqual({ ms: 7200000, value: 2, unit: 'h' });
  });

  it('does NOT support days (diverges from _common parseDuration) and rejects junk', () => {
    expect(parseDuration('1d')).toBeNull();
    expect(parseDuration('abc')).toBeNull();
    expect(parseDuration('')).toBeNull();
  });
});

describe('parseYAML', () => {
  it('parses a nested tenants structure', () => {
    const r = parseYAML('tenants:\n  a:\n    mysql_connections: "1"');
    expect(r.success).toBe(true);
    expect(r.data.tenants.a.mysql_connections).toBe('1');
  });
});

describe('validateTenantConfig', () => {
  it('accepts a well-formed tenant config', () => {
    const r = validateTenantConfig(VALID);
    expect(r.valid).toBe(true);
    expect(r.errors).toHaveLength(0);
    expect(r.summary.thresholds).toBeGreaterThan(0);
  });

  it('rejects YAML with no tenants root key', () => {
    const r = validateTenantConfig('foo: bar');
    expect(r.valid).toBe(false);
    expect(r.errors.length).toBeGreaterThan(0);
  });

  it('rejects a non-numeric threshold value', () => {
    const r = validateTenantConfig('tenants:\n  db-a:\n    mysql_connections: "abc"');
    expect(r.valid).toBe(false);
  });

  it('warns (but stays valid) on an unknown metric key', () => {
    const r = validateTenantConfig('tenants:\n  db-a:\n    not_a_real_metric: "50"');
    expect(r.warnings.length).toBeGreaterThan(0);
  });

  it('flips to invalid when a routing duration breaches its guardrail', () => {
    // group_wait guardrail is 5s..5m; "1h" is out of range.
    const bad = VALID.replace('group_wait: "30s"', 'group_wait: "1h"');
    const r = validateTenantConfig(bad);
    expect(r.valid).toBe(false);
    expect(validateTenantConfig(VALID).valid).toBe(true); // control: only the duration changed
  });
});
