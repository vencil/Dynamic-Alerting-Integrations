/**
 * config-lint/lint.js — YAML parser + LINT_RULES + lintConfig engine.
 *
 * Extracted from config-lint.jsx (PR-portal-20), previously 0%-covered —
 * including the rule check() functions where the real lint logic lives. These
 * tests drive the real rules end-to-end through lintConfig.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { lintConfig, LINT_RULES, parseYaml } from '../src/interactive/tools/config-lint/lint.js';
import { parseTenantYaml } from '../src/interactive/tools/_common/validation/yaml-parser.js';

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

describe('threshold values are read as the exporter reads them (#3964)', () => {
  // Measured against da-guard served-values built from main e0e7ee33b
  // (`defaults: {mysql_connection_usage_percent: 80}`, tenant t1 writing the
  // value): the first group is refused (`refused: unparsed` / `non_finite`,
  // the default 80 served), the second is served as the number shown.
  const KEY = 'mysql_connection_usage_percent';
  const lint = (value: string) =>
    lintConfig(`t1:\n  ${KEY}: ${value}\n  _routing:\n    receiver: x\n`).findings.map((f: any) => f.ruleId);

  it.each(['0x64', '"0x64"', '0o17', '0b101', '1_000', 'Infinity'])(
    '%s is not read: reported as threshold-not-read, never judged as a number', (value) => {
      expect(lint(value)).toEqual(['threshold-not-read']);
    });

  it.each([['96', 96], ['"96"', 96], ["'96'", 96], ['96   # note', 96], ['1e3', 1000], ['96:critical', 96]])(
    '%s is served as %d: threshold-too-high judges it', (value, served) => {
      const r = lintConfig(`t1:\n  ${KEY}: ${value}\n  _routing:\n    receiver: x\n`);
      expect(r.findings.map((f: any) => f.ruleId)).toEqual(['threshold-too-high']);
      expect(r.findings[0].message).toContain(`${KEY} = ${served} `);
    });

  it('a readable value at or under the limit, a disable word and a null say nothing', () => {
    for (const value of ['95', '"95"', 'DISABLE', 'disable', '~', '']) expect(lint(value), value).toEqual([]);
  });

  it('the not-read finding names the value as written, in the wording of the other pages', () => {
    const f = lintConfig(`t1:\n  ${KEY}: 0x64\n`).findings.find((x: any) => x.ruleId === 'threshold-not-read');
    expect(f).toMatchObject({ tenant: 't1', key: KEY, severity: 'warning' });
    expect(f.message).toContain('"0x64"');
    expect(f.message).toContain('will not read this value');
  });

  it('a _critical key is read whole: `95:critical` is not read there, `0x64` is not compared', () => {
    const notRead = lintConfig('t1:\n  x_warning: 50\n  x_warning_critical: 95:critical\n').findings;
    expect(notRead.filter((f: any) => f.ruleId === 'threshold-not-read').map((f: any) => f.key)).toEqual(['x_warning_critical']);
    expect(notRead.find((f: any) => f.message.includes('_critical key'))).toBeTruthy();

    const hex = lintConfig('t1:\n  x_warning: 0x64\n  x_warning_critical: 90\n').findings.map((f: any) => f.ruleId);
    expect(hex).toContain('threshold-not-read');
    expect(hex).not.toContain('critical-lower-than-warning');
  });

  it('critical-lower-than-warning compares the values the exporter serves', () => {
    const ids = (yaml: string) => lintConfig(yaml).findings.map((f: any) => f.ruleId);
    expect(ids("t1:\n  x_warning: '90'\n  x_warning_critical: \"80\"\n")).toContain('critical-lower-than-warning');
    expect(ids('t1:\n  x_warning: 90   # note\n  x_warning_critical: 80\n')).toContain('critical-lower-than-warning');
    expect(ids('t1:\n  x_warning: 80\n  x_warning_critical: 90\n')).not.toContain('critical-lower-than-warning');
    // The warning key is a base key: `90:warning` is served as 90 there
    // (da-guard served-values: 90 beside a _critical of 80); read whole, as a
    // _critical key is, it would not be read at all.
    expect(ids('t1:\n  x_warning: 90:warning\n  x_warning_critical: 80\n')).toContain('critical-lower-than-warning');
  });

  it('threshold-too-low reads the text: `"5"` is 5, `0x5` is not read', () => {
    const ids = (v: string) => lintConfig(`t1:\n  x_warning: ${v}\n  x_warning_critical: 50\n`).findings.map((f: any) => f.ruleId);
    expect(ids('"5"')).toContain('threshold-too-low');
    expect(ids('0x5')).not.toContain('threshold-too-low');
    expect(ids('0x5')).toContain('threshold-not-read');
  });

  it('a schedule is not judged; a mapping without `default` and a list are not read', () => {
    const findings = lintConfig(`t1:\n  a_usage: {default: 99}\n  b_usage: {x: 1}\n  c_usage: [99]\n`).findings
      .filter((f: any) => f.ruleId !== 'no-routing');
    expect(findings.map((f: any) => [f.ruleId, f.key])).toEqual([
      ['threshold-not-read', 'b_usage'], ['threshold-not-read', 'c_usage'],
    ]);
  });

  it('`_` keys, `_` blocks and routing_profiles are not thresholds', () => {
    const yaml = '_defaults:\n  x_usage: 0x64\nrouting_profiles:\n  p:\n    group_wait: 30s\n'
      + 't1:\n  _routing:\n    group_wait: 30s\n  _silent_mode: "0x1"\n';
    const ids = lintConfig(yaml).findings.map((f: any) => f.ruleId);
    expect(ids).not.toContain('threshold-not-read');
    expect(ids).not.toContain('threshold-too-high');
  });

  it('YAML the exporter cannot read: one finding, and no threshold judged from the lenient parse', () => {
    const r = lintConfig(`t1:\n  ${KEY}: 99\n  _routing:\n    receiver: x\n  bad: [\n`);
    expect(r.ok).toBe(true);
    const ids = r.findings.map((f: any) => f.ruleId);
    expect(ids).toEqual(['threshold-not-read']);
    expect(r.findings[0].message).toMatch(/^Threshold values not checked: /);

    const multi = lintConfig(`t1:\n  ${KEY}: 99\n  _routing:\n    receiver: x\n---\nt2:\n  ${KEY}: 99\n  _routing:\n    receiver: x\n`).findings;
    expect(multi.map((f: any) => f.ruleId)).toEqual(['threshold-not-read']);
    expect(multi[0].message).toContain('more than one document');
  });

  it('the rule boundaries: >95 and <10 are strict, critical equal to warning is reported', () => {
    const ids = (yaml: string) => lintConfig(yaml).findings.map((f: any) => f.ruleId);
    expect(ids('t1:\n  x_warning: 10\n  x_warning_critical: 50\n')).not.toContain('threshold-too-low');
    expect(ids('t1:\n  x_warning: 80\n  x_warning_critical: "80"\n')).toContain('critical-lower-than-warning');
  });

  it('threshold-too-high looks only at _percent / _usage / cpu keys; too-low never at a _critical key', () => {
    const ids = (yaml: string) => lintConfig(yaml).findings.map((f: any) => [f.ruleId, f.key]);
    expect(ids('t1:\n  x_latency: 99\n')).not.toContainEqual(['threshold-too-high', 'x_latency']);
    expect(ids('t1:\n  x_warning: 3\n  x_warning_critical: 5\n').filter(([r]: string[]) => r === 'threshold-too-low'))
      .toEqual([['threshold-too-low', 'x_warning']]);
  });

  it('a top-level null or list is not a tenant of thresholds', () => {
    const empty = lintConfig('t0:\nt1: [0x64, 99]\nt2:\n  x_usage: 99\n');
    expect(empty.ok).toBe(true);
    expect(empty.findings.filter((f: any) => f.ruleId.startsWith('threshold-')).map((f: any) => [f.ruleId, f.tenant]))
      .toEqual([['threshold-too-high', 't2']]);
  });

  it('a tenant that merges (`<<: *base`) is judged on its own keys; the `<<` entry is not a threshold', () => {
    const yaml = '_base: &b\n  x_usage: 99\nt1:\n  <<: *b\n  y_usage: 0x64\n  _routing:\n    receiver: x\n';
    expect(lintConfig(yaml).findings.map((f: any) => [f.ruleId, f.tenant, f.key])).toEqual([
      ['threshold-not-read', 't1', 'y_usage'], ['threshold-not-checked', '-', '<<'],
    ]);
    // Any merge key, once: at the tenant (block, flow, a list item), and
    // inside a threshold value, where js-yaml has expanded it (the exporter
    // refuses `{<<: {default: 99}}`: no `default` of its own).
    const merged = (y: string) => lintConfig(y).findings.filter((f: any) => f.ruleId === 'threshold-not-checked').map((f: any) => f.key);
    for (const y of [
      't1:\n  <<: {x_usage: 99}\n', 't1: {<<: {x_usage: 99}}\n', 't1:\n  x_usage: {<<: {default: 99}}\n',
      '_s: &s\n  default: 99\nt1:\n  x_usage:\n    <<: *s\n', '_s: &s {a: 1}\nt1:\n  x_usage: [1]\n  y:\n    - <<: *s\n',
    ]) expect(merged(y), y).toEqual(['<<']);
    expect(merged('t1:\n  x_usage: 50\n  y: "a<<b"\n')).toEqual([]);
    expect(merged('t1:\n  x_usage: 50\n  y: "a<<: b"\n')).toEqual([]);
  });

  it('a conf.d file pasted whole (`tenants:`) is not read as a tenant named "tenants"', () => {
    const ids = lintConfig('tenants:\n  t1:\n    x_usage: 0x64\n    _routing:\n      receiver: x\n').findings
      .map((f: any) => f.ruleId);
    expect(ids.filter((r: string) => r.startsWith('threshold-'))).toEqual([]);
  });

  it('_defaults is not judged, and says so once (threshold-not-checked)', () => {
    const r = lintConfig('_defaults:\n  x_usage: 99\n  y_warning: 200\n  y_warning_critical: 150\n  _silent_mode: x\n');
    expect(r.findings.map((f: any) => [f.ruleId, f.tenant, f.severity])).toEqual([['threshold-not-checked', '_defaults', 'info']]);
    // only _defaults: any other `_` block is no tenant (a platform block, an anchor block)
    for (const y of ['_shared:\n  x_usage: 99\n', '_routing_defaults:\n  receiver: x\n  group_wait: 30s\n',
      '_routing_enforced:\n  enabled: true\n']) {
      expect(lintConfig(y).findings.filter((f: any) => f.ruleId === 'threshold-not-checked'), y).toEqual([]);
    }
    // nothing to say for a _defaults with no threshold key, or none at all
    expect(lintConfig('_defaults:\n  _silent_mode: x\n').findings).toEqual([]);
    expect(lintConfig('_defaults: [1]\n').findings).toEqual([]);
    const empty = lintConfig('_defaults:\n');
    expect(empty.ok).toBe(true);
    expect(empty.findings).toEqual([]);
    expect(lintConfig('t1:\n  x_usage: 50\n  _routing:\n    receiver: x\n').findings).toEqual([]);
  });

  it('a warning key written `N:critical` is not compared as a warning', () => {
    const ids = lintConfig('t1:\n  x_warning: 90:critical\n  x_warning_critical: 80\n').findings.map((f: any) => f.ruleId);
    expect(ids).not.toContain('critical-lower-than-warning');
    expect(lintConfig('t1:\n  x_warning: 5:critical\n').findings.map((f: any) => f.ruleId)).not.toContain('threshold-too-low');
    expect(lintConfig('t1:\n  x_warning: 5:warning\n').findings.map((f: any) => f.ruleId)).toContain('threshold-too-low');
    // a key with `_critical` inside is left out of too-low, as before
    expect(lintConfig('t1:\n  x_critical_warning: 5\n').findings.map((f: any) => f.ruleId)).not.toContain('threshold-too-low');
  });
});

describe('Config Lint ↔ the exporter (threshold_value_matrix.json)', () => {
  // Every row of the table Go writes from the exporter's own readers, on a
  // base key and a `_critical` key, as the page reads it. The value goes in as
  // a double-quoted YAML scalar (a JSON string is one), so the text the page
  // reads is the row's `value` exactly. A page that read a threshold its own
  // way (#3964: Number() on the lenient parse) fails here row by row.
  type Row = { value: string; ok: boolean; value_part?: string; float_ok?: boolean; row_disabled: boolean };
  const here = dirname(fileURLToPath(import.meta.url));
  const rows: Row[] = JSON.parse(readFileSync(resolve(here, '../../../tests/shared/threshold_value_matrix.json'), 'utf8')).rows;
  const served = (r: Row) => r.ok && r.float_ok === true;
  const findings = (yaml: string) => lintConfig(yaml).findings.filter((f: any) => f.ruleId.startsWith('threshold-'))
    .map((f: any) => [f.ruleId, f.key]);

  it('the table has served, unreadable and disabled rows', () => {
    expect(rows.filter((r) => !r.row_disabled && served(r)).length).toBeGreaterThan(3);
    expect(rows.filter((r) => !r.row_disabled && !served(r)).length).toBeGreaterThan(3);
    expect(rows.filter((r) => r.row_disabled).length).toBeGreaterThan(3);
  });

  it('a base key: not read where the exporter refuses, too high only on what it serves', () => {
    const wrong: string[] = [];
    for (const r of rows) {
      const got = findings(`t1:\n  x_usage: ${JSON.stringify(r.value)}\n  _routing:\n    receiver: x\n`);
      let want: string[][] = [];
      if (r.row_disabled) want = [];
      else if (!served(r)) want = [['threshold-not-read', 'x_usage']];
      else if (Number(r.value_part) > 95) want = [['threshold-too-high', 'x_usage']];
      if (JSON.stringify(got) !== JSON.stringify(want)) wrong.push(`${JSON.stringify(r.value)}: ${JSON.stringify(got)}`);
    }
    expect(wrong).toEqual([]);
  });

  it('the same rows written as plain scalars, where the YAML text reading gives the row back', () => {
    // A page that took YAML's typed reading (`0x64` → 100) passes the quoted
    // rows above; it fails these.
    const plain = rows.filter((r) => r.value !== '' && !/[\s#'"\\]/.test(r.value)
      && parseTenantYaml(`k: ${r.value}\n`).config.k === r.value);
    expect(plain.length).toBeGreaterThan(50);
    const wrong: string[] = [];
    for (const r of plain) {
      const got = findings(`t1:\n  x_usage: ${r.value}\n  _routing:\n    receiver: x\n`);
      let want: string[][] = [];
      if (r.row_disabled) want = [];
      else if (!served(r)) want = [['threshold-not-read', 'x_usage']];
      else if (Number(r.value_part) > 95) want = [['threshold-too-high', 'x_usage']];
      if (JSON.stringify(got) !== JSON.stringify(want)) wrong.push(`${r.value}: ${JSON.stringify(got)}`);
    }
    expect(wrong).toEqual([]);
  });

  it('a `_critical` key is read whole: not read unless the exporter serves the whole value', () => {
    const wrong: string[] = [];
    for (const r of rows) {
      const got = findings(`t1:\n  y_critical: ${JSON.stringify(r.value)}\n  _routing:\n    receiver: x\n`);
      const notRead = !r.row_disabled && !(served(r) && !r.value.includes(':'));
      const want = notRead ? [['threshold-not-read', 'y_critical']] : [];
      if (JSON.stringify(got) !== JSON.stringify(want)) wrong.push(`${JSON.stringify(r.value)}: ${JSON.stringify(got)}`);
    }
    expect(wrong).toEqual([]);
  });
});

describe('a conf.d tenant file pasted whole (#3998)', () => {
  const FILE = 'tenants:\n  t1:\n    mysql_connection_usage_percent: 0x64\n    _routing:\n      receiver: x\n';

  it('says so once, and nothing is reported on a tenant named "tenants"', () => {
    const r = lintConfig(FILE);
    expect(r.ok).toBe(true);
    expect(r.findings.map((f: any) => [f.ruleId, f.tenant, f.severity])).toEqual([['confd-tenants-wrapper', 'tenants', 'warning']]);
    expect(r.findings[0].message).toContain('paste what is under tenants:');
  });

  it('flow style and a file without _routing are the same file', () => {
    for (const y of ['tenants: {t1: {x_usage: 99}}\n', 'tenants:\n  t1:\n    x_warning: 5\n']) {
      expect(lintConfig(y).findings.map((f: any) => f.ruleId), y).toEqual(['confd-tenants-wrapper']);
    }
  });

  it('findings on real tenants beside it stay', () => {
    const ids = lintConfig(`${FILE}t2:\n  x_usage: 99\n`).findings.map((f: any) => [f.ruleId, f.tenant]);
    expect(ids).toContainEqual(['confd-tenants-wrapper', 'tenants']);
    expect(ids).toContainEqual(['threshold-too-high', 't2']);
    expect(ids).toContainEqual(['no-routing', 't2']);
  });

  it('a tenant of the page\'s own named `tenants` stays a tenant', () => {
    const ids = lintConfig('tenants:\n  x_usage: 99\n').findings.map((f: any) => [f.ruleId, f.tenant]);
    expect(ids).toEqual([['threshold-too-high', 'tenants'], ['no-routing', 'tenants']]);
    expect(lintConfig('tenants:\n  t1:\n    x_usage: 50\n  x_usage: 99\n').findings.map((f: any) => f.ruleId))
      .not.toContain('confd-tenants-wrapper');
  });

  it('a conf.d file with an empty tenant is still one', () => {
    expect(lintConfig('tenants:\n  t1:\n  t2:\n    x_usage: 50\n').findings.map((f: any) => f.ruleId)).toEqual(['confd-tenants-wrapper']);
    expect(lintConfig('tenants: {}\n').findings.map((f: any) => f.ruleId)).toEqual(['confd-tenants-wrapper']);
  });

  it('a page tenant named `tenants` with schedules or `_` keys stays a tenant', () => {
    const ids = (y: string) => lintConfig(y).findings.map((f: any) => [f.ruleId, f.tenant]);
    expect(ids('tenants:\n  x_usage:\n    default: "0x64"\n')).toEqual([['threshold-not-read', 'tenants'], ['no-routing', 'tenants']]);
    expect(ids('tenants:\n  _routing:\n    receiver: x\n')).toEqual([]);
  });

  it('a conf.d file the YAML reader refuses is not recognised: its error is reported instead', () => {
    for (const y of ['tenants:\n  t1:\n    x_usage: "70"\n---\n', 'tenants:\n  t1:\n    a: 1\n  t1:\n    a: 2\n']) {
      const ids = lintConfig(y).findings.map((f: any) => [f.ruleId, f.tenant]);
      expect(ids.map(([r]: string[]) => r), y).not.toContain('confd-tenants-wrapper');
      expect(ids, y).toContainEqual(['threshold-not-read', '-']);
    }
  });

  it('a page tenant named `tenants` that only merges stays a tenant', () => {
    const y = '_base: &base\n  x_usage: 99\n  _routing:\n    receiver: x\ntenants:\n  <<: *base\n';
    const ids = lintConfig(y).findings.map((f: any) => [f.ruleId, f.tenant]);
    expect(ids.map(([r]: string[]) => r)).not.toContain('confd-tenants-wrapper');
    expect(ids).toContainEqual(['threshold-not-checked', '-']);
    expect(lintConfig(y).tenantCount).toBe(1);
  });

  it('file-level findings give way to it too: a merge key in a pasted conf.d file', () => {
    const y = '_b: &b {x_usage: 99}\ntenants:\n  t1:\n    <<: *b\n';
    expect(lintConfig(y).findings.map((f: any) => [f.ruleId, f.tenant])).toEqual([['confd-tenants-wrapper', 'tenants']]);
  });

  it('unreadable YAML: nothing is taken for a conf.d file, and the error is said', () => {
    const ids = (y: string) => lintConfig(y).findings.map((f: any) => [f.ruleId, f.tenant]);
    for (const y of ['tenants: 5\n---\n', 'tenants: [a, b]\nt2:\n  x_usage: 99\nt2:\n  x_usage: 98\n',
      'tenants:\n  - a\n  - b\n---\n', 'tenants:\n  foo\n---\n', 'tenants: |\n  t1:\n    a: 1\n---\n',
      'tenants:\n  x_usage:\n    "default": "0x64"\n---\n']) {
      expect(ids(y).map(([r]: string[]) => r), y).not.toContain('confd-tenants-wrapper');
      expect(ids(y), y).toContainEqual(['threshold-not-read', '-']);
    }
  });

  it('routing_profiles and `_` blocks beside it are no tenants: the wrapper still speaks for the file', () => {
    // (no-routing on `routing_profiles` is the lenient rule's own, as on main)
    const y = 'routing_profiles:\n  p:\n    group_wait: 30s\n_b: &b {x_usage: 1}\ntenants:\n  t1:\n    <<: *b\n';
    const ids = lintConfig(y).findings.map((f: any) => [f.ruleId, f.tenant]);
    expect(ids.filter(([, t]: string[]) => t === '-' || t === 'tenants')).toEqual([['confd-tenants-wrapper', 'tenants']]);
  });

  it('beside real tenants, the file-level findings stay', () => {
    const y = '_b: &b {x_usage: 99}\ntenants:\n  t1:\n    <<: *b\nt2:\n  x_usage: 50\n  _routing:\n    receiver: x\n';
    const ids = lintConfig(y).findings.map((f: any) => [f.ruleId, f.tenant]);
    expect(ids).toContainEqual(['confd-tenants-wrapper', 'tenants']);
    expect(ids).toContainEqual(['threshold-not-checked', '-']);
  });

  it('no conf.d file and no tenant: a YAML error is still said', () => {
    expect(lintConfig('_x: [\n').findings.map((f: any) => [f.ruleId, f.tenant])).toEqual([['threshold-not-read', '-']]);
  });

  it('a quoted `tenants:` key is the same file', () => {
    for (const y of ['"tenants":\n  t1:\n    x_usage: 99\n', "'tenants':\n  t1:\n    x_usage: 50\n    _routing:\n      receiver: x\n",
      '"tenant\\u0073":\n  t1:\n    x_usage: 99\n', '"tenant\\x73":\n  t1:\n    x_usage: 99\n']) {
      const r = lintConfig(y);
      expect(r.findings.map((f: any) => [f.ruleId, f.tenant]), y).toEqual([['confd-tenants-wrapper', 'tenants']]);
      expect(r.tenantCount, y).toBe(0);
    }
  });

  it('`tenants` is not counted as a tenant', () => {
    expect(lintConfig(`${FILE}t2:\n  x_usage: 50\n`).tenantCount).toBe(1);
    expect(lintConfig('tenants:\n  x_usage: 99\n').tenantCount).toBe(1);
  });

  it('not a conf.d file: `tenants` that holds no tenant mappings', () => {
    expect(lintConfig('tenants: 5\n').findings.map((f: any) => f.ruleId)).not.toContain('confd-tenants-wrapper');
    // a list of tenant-like mappings is still no `tenants:` section
    expect(lintConfig('tenants: [{a: 1}, ~]\n').findings.map((f: any) => f.ruleId)).not.toContain('confd-tenants-wrapper');
    expect(lintConfig('tenants: [a]\n').findings.map((f: any) => f.ruleId)).not.toContain('confd-tenants-wrapper');
  });
});

describe('this page does not check schedules: its top level is tenants (#4001)', () => {
  const R = '  _routing:\n    receiver: x\n';
  const ids = (yaml: string) => lintConfig(yaml).findings.map((f: any) => [f.ruleId, f.tenant, f.key]);

  it('a tenant key named `default` is not taken for a schedule (the exporter serves these files)', () => {
    // no file-level schedule error: `default` is an ordinary key, and a list
    // is no threshold the exporter reads
    expect(ids(`t1:\n  default: [1]\n${R}`)).toEqual([['threshold-not-read', 't1', 'default']]);
    // and the thresholds beside it are judged
    expect(ids(`t1:\n  mysql_connection_usage_percent: 99\n  default: 80\n  overrides: 5\n${R}`))
      .toEqual([['threshold-too-high', 't1', 'mysql_connection_usage_percent']]);
  });

  it('a conf.d file whose tenant is named `default` is recognised', () => {
    expect(ids('tenants:\n  default:\n    x_usage: "70"\n    _routing:\n      receiver: x\n'))
      .toEqual([['confd-tenants-wrapper', 'tenants', '-']]);
  });

  it('a threshold written as a schedule the exporter refuses is not checked here (as before #3974)', () => {
    for (const v of ['{default: [1]}', '{default: 80, overrides: 5}']) {
      expect(ids(`t1:\n  mysql_connection_usage_percent: ${v}\n${R}`), v).toEqual([]);
    }
  });

  it('no row of tenant_file_matrix.json, as a tenant of this page, yields a schedule error', () => {
    // Each body is one tenant's keys; here it sits under `t1:`. Whatever the
    // exporter makes of it, this page says nothing about schedules.
    const matrix = JSON.parse(readFileSync(resolve(dirname(fileURLToPath(import.meta.url)),
      '../../../tests/shared/tenant_file_matrix.json'), 'utf8'));
    expect(matrix.rows.length).toBeGreaterThan(1000);
    const wrong: string[] = [];
    for (const { body } of matrix.rows) {
      const yaml = `t1:\n${body.replace(/\n$/, '').split('\n').map((l: string) => `  ${l}`).join('\n')}\n`;
      const said = lintConfig(yaml).findings.filter((f: any) => /cannot decode this schedule/.test(f.message));
      if (said.length) wrong.push(JSON.stringify(body));
    }
    expect(wrong).toEqual([]);
  });
});
