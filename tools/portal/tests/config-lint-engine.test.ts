/**
 * config-lint/lint.js — YAML parser + LINT_RULES + lintConfig engine.
 *
 * Extracted from config-lint.jsx (PR-portal-20), previously 0%-covered —
 * including the rule check() functions where the real lint logic lives. These
 * tests drive the real rules end-to-end through lintConfig.
 */
import { describe, it, expect } from 'vitest';
import { lintConfig, LINT_RULES, parseYaml } from '../src/interactive/tools/config-lint/lint.js';

describe('parseYaml', () => {
  it('parses tenants, coerces numbers, and keeps the disable sentinel', () => {
    const t = parseYaml('db-a:\n  mysql_cpu: 98\n  _silent_mode: "disable"');
    expect(t['db-a'].mysql_cpu).toBe(98);          // numeric coercion
    expect(t['db-a']._silent_mode).toBe('disable'); // sentinel preserved
  });
});

describe('lintConfig', () => {
  it('flags an over-high cpu threshold (threshold-too-high)', () => {
    const r = lintConfig('db-a:\n  mysql_cpu: 98');
    expect(r.ok).toBe(true);
    const f = r.findings.find((x) => x.ruleId === 'threshold-too-high');
    expect(f).toBeTruthy();
    expect(f.severity).toBe('warning');
    expect(f.category).toBeTruthy(); // tagged from the rule
  });

  it('flags a warning threshold with no critical pair, and clears it when paired', () => {
    const missing = lintConfig('db-a:\n  latency_warning: 50');
    expect(missing.findings.some((f) => f.ruleId === 'missing-critical-pair')).toBe(true);

    const paired = lintConfig('db-a:\n  latency_warning: 50\n  latency_warning_critical: 80');
    expect(paired.findings.some((f) => f.ruleId === 'missing-critical-pair')).toBe(false);
  });

  it('counts only non-underscore tenants', () => {
    const r = lintConfig('_defaults:\n  foo: 1\ndb-a:\n  bar: 2');
    expect(r.tenantCount).toBe(1); // _defaults excluded
  });

  it('returns findings severity-sorted (error -> warning -> info)', () => {
    const r = lintConfig('db-a:\n  mysql_cpu: 98\n  latency_warning: 50');
    const order = { error: 0, warning: 1, info: 2 };
    for (let i = 1; i < r.findings.length; i++) {
      expect(order[r.findings[i].severity]).toBeGreaterThanOrEqual(order[r.findings[i - 1].severity]);
    }
  });

  it('reports ok:false with an empty finding set is never thrown to the UI', () => {
    // parseYaml is lenient; even odd input returns ok:true with whatever parsed.
    const r = lintConfig('');
    expect(r.ok).toBe(true);
    expect(Array.isArray(r.findings)).toBe(true);
  });
});

describe('LINT_RULES', () => {
  it('is a non-empty set of {id, severity, check} rules', () => {
    expect(Array.isArray(LINT_RULES)).toBe(true);
    expect(LINT_RULES.length).toBeGreaterThan(0);
    for (const rule of LINT_RULES) {
      expect(typeof rule.id).toBe('string');
      expect(['error', 'warning', 'info']).toContain(rule.severity);
      expect(typeof rule.check).toBe('function');
    }
  });
});
