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
    const t = parseYaml('db-a:\n  container_cpu: 98\n  _silent_mode: "disable"');
    expect(t['db-a'].container_cpu).toBe(98);          // numeric coercion
    expect(t['db-a']._silent_mode).toBe('disable'); // sentinel preserved
  });
});

describe('lintConfig', () => {
  it('flags an over-high cpu threshold (threshold-too-high)', () => {
    const r = lintConfig('db-a:\n  container_cpu: 98');
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
    const r = lintConfig('db-a:\n  container_cpu: 98\n  latency_warning: 50');
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

describe('repeat-interval-too-long (#2711: Alertmanager duration syntax)', () => {
  const findingsFor = (ri: string) =>
    lintConfig(`db-a:\n  _routing:\n    repeat_interval: ${ri}`)
      .findings.filter((f) => f.ruleId === 'repeat-interval-too-long');
  const TOO_LONG = 'exceeds maximum of 72h';
  const QUITE_LONG = 'is quite long';

  it('reads 1d1h as 25h (> 24h), not 1h', () => {
    const f = findingsFor('1d1h');
    expect(f).toHaveLength(1);
    expect(f[0].message).toContain(QUITE_LONG);
    expect(f[0].severity).toBe('info');
  });

  it('reads 4d as 96h (> 72h) instead of skipping it', () => {
    const f = findingsFor('4d');
    expect(f).toHaveLength(1);
    expect(f[0].message).toContain(TOO_LONG);
  });

  it('72h is in the 24h–72h band, 73h above it, 24h and 1h30m below it', () => {
    expect(findingsFor('72h').map((f) => f.message).join()).toContain(QUITE_LONG);
    expect(findingsFor('73h').map((f) => f.message).join()).toContain(TOO_LONG);
    expect(findingsFor('24h')).toEqual([]);
    expect(findingsFor('1h30m')).toEqual([]);
  });

  it('1.5h is not read as 1h; unreadable values are left to validateConfig', () => {
    for (const bad of ['1.5h', '30m1h', '100.5h']) expect(findingsFor(bad), bad).toEqual([]);
  });

  it('an unquoted number (no unit) is skipped, not a crash', () => {
    const r = lintConfig('db-a:\n  _routing:\n    repeat_interval: 30');
    expect(r.ok).toBe(true);
    expect(r.findings.filter((x) => x.ruleId === 'repeat-interval-too-long')).toEqual([]);
  });
});

describe('group-wait-too-low (#2711: Alertmanager duration syntax)', () => {
  const findingsFor = (gw: string) =>
    lintConfig(`db-a:\n  _routing:\n    group_wait: ${gw}`)
      .findings.filter((f) => f.ruleId === 'group-wait-too-low');

  it('flags any readable value under 5s, not only the literals 1s..4s', () => {
    for (const short of ['500ms', '0s', '4999ms', '1s500ms', '3000ms', '4s']) {
      const f = findingsFor(short);
      expect(f, short).toHaveLength(1);
      expect(f[0].severity).toBe('warning'); // the rule's own severity, unchanged
      expect(f[0].key).toBe('_routing.group_wait');
      expect(f[0].message).toContain('too short');
    }
  });

  it('does not flag 5s or more', () => {
    for (const ok of ['5s', '5000ms', '1m']) expect(findingsFor(ok), ok).toEqual([]);
  });

  it('an unreadable value (1.5s) is left to validateConfig', () => {
    expect(findingsFor('1.5s')).toEqual([]);
  });
});

// #2711: both timing rules judge only values parseDuration can read.
// config-lint's lenient parser keeps single quotes and trailing `# comments`,
// so an unreadable raw string may be valid YAML: it is skipped, never
// reported. Invalid durations are reported by the Self-Service Portal's
// validateConfig (js-yaml; see alert-engine.test.ts).
describe('timing rules never flag valid YAML the lenient parser keeps raw (#2711)', () => {
  const routingFindings = (body: string) =>
    lintConfig(`db-a:\n  _routing:\n${body}`).findings
      .filter((f) => f.ruleId === 'group-wait-too-low' || f.ruleId === 'repeat-interval-too-long');

  it('quoted and commented values produce no finding', () => {
    for (const body of [
      "    group_wait: '30s'",
      '    group_wait: "30s"   # comment',
      "    repeat_interval: '48h'",
      '    repeat_interval: 48h # c',
    ]) {
      expect(routingFindings(body), body).toEqual([]);
    }
  });

  it('the docs/custom-rule-governance.md example lines produce no duration finding', () => {
    // The two lines as the doc writes them, re-indented under a tenant so the
    // parser files them as _routing keys (asserted, so this is not vacuous).
    const body = [
      '    group_wait: "30s"                # 5s–5m，平台 guardrails 自動 clamp',
      '    repeat_interval: "4h"            # 1m–72h',
    ].join('\n');
    const routing = parseYaml(`db-a:\n  _routing:\n${body}`)['db-a']._routing;
    expect(Object.keys(routing)).toEqual(['group_wait', 'repeat_interval']);
    expect(routingFindings(body)).toEqual([]);
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
