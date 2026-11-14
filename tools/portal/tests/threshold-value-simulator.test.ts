/**
 * The simulators' reader of a threshold value ↔ the exporter (#3865): the
 * second JavaScript half of tests/shared/threshold_value_matrix.json. The
 * first, tests/threshold-value-matrix.test.ts, holds `readThreshold` to the
 * table; this one holds every place that used to read a threshold with
 * parseFloat / Number() to the same rows:
 *
 *   - simulateAlerts, simulateWithDedup and validateConfig
 *     (_common/sim/alert-engine.js), and
 *   - annotateCounterexamples' "is this line the measured number"
 *     (_common/data/rule-packs.js).
 *
 * parseFloat stops at the first character it does not know, so `1_000` was
 * simulated as a threshold of 1, `0x1F` as 0 and `70:high` as 70 — values the
 * exporter does not read. The verdict columns are written by Go from the
 * exporter's own readers; nothing here restates the grammar.
 *
 * ⚠️ The table has no column for a `<base>_critical` key, which the exporter
 * reads by another rule: the WHOLE value is the number (resolveCriticalRows:
 * parseThresholdNumber of the trimmed value), with the same disable test.
 * `criticalKindOf` below derives that verdict from the Go-written columns:
 * for a value with no ':' the split leaves the trimmed value as `value_part`,
 * so `float_ok` IS that reading; a value with a ':' is never a number there
 * (the grammar has no ':'). resolve.go is the authority if this is doubted.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import ENUMS from '../src/interactive/tools/_common/data/recipe-enums.json';
import {
  readThresholdRow, thresholdRowDisabled, thresholdScalarText, isCriticalKey,
} from '../src/interactive/tools/_common/validation/threshold-value.js';
import {
  simulateAlerts, simulateWithDedup, validateConfig, unreadableThresholdMessage,
} from '../src/interactive/tools/_common/sim/alert-engine.js';
import { parseYaml, parseTenantYaml } from '../src/interactive/tools/_common/validation/yaml-parser.js';

type Row = {
  value: string;
  ok: boolean;
  value_part?: string;
  severity?: string;
  float_ok?: boolean;
  row_disabled: boolean;
};

// ⛔ One literal that climbs to the repo root: the shape
// tests/ops/test_ci_path_filter_coverage.py reads (see the sibling test).
const here = dirname(fileURLToPath(import.meta.url));
const matrix = JSON.parse(readFileSync(
  resolve(here, '../../../tests/shared/threshold_value_matrix.json'), 'utf8',
));
const rows: Row[] = matrix.rows;
const label = (r: Row) => JSON.stringify(r.value.length > 60 ? `${r.value.slice(0, 30)}…(${r.value.length})` : r.value);
const each = (rs: Row[]) => rs.map((r) => [label(r), r] as const);

// What the exporter does with a row's value on a threshold key.
const served = (r: Row) => r.ok && r.float_ok === true;
const kindOf = (r: Row) => {
  if (r.row_disabled) return 'disabled';
  return served(r) ? 'number' : 'unreadable';
};
// The same value on a `<base>_critical` key (see the note at the top).
const criticalKindOf = (r: Row) => {
  if (r.row_disabled) return 'disabled';
  return served(r) && !r.value.includes(':') ? 'number' : 'unreadable';
};
const byKind = (k: string) => rows.filter((r) => kindOf(r) === k);

describe('the table has every kind of row this is about', () => {
  it('served, unreadable and disabled rows, and the ones parseFloat misreads', () => {
    for (const k of ['number', 'unreadable', 'disabled']) expect(byKind(k).length, k).toBeGreaterThan(3);
    // Rows the old reader turned into a number the exporter never serves.
    const misread = byKind('unreadable').filter((r) => !Number.isNaN(parseFloat(r.value)));
    expect(misread.length).toBeGreaterThan(10);
    const values = new Set(misread.map((r) => r.value));
    for (const v of ['1_000', '0x1F', '0x10', '1e309']) expect(values.has(v), v).toBe(true);
    // And rows it read as a DIFFERENT number than the exporter serves.
    const differs = byKind('number').filter((r) => parseFloat(r.value) !== Number(r.value_part));
    expect(differs.length).toBeGreaterThan(0);
    // Rows a `_critical` key reads differently from a base key, and the empty string.
    expect(rows.filter((r) => kindOf(r) === 'number' && criticalKindOf(r) === 'unreadable').length).toBeGreaterThan(3);
    expect(rows.filter((r) => criticalKindOf(r) === 'number').length).toBeGreaterThan(3);
    expect(rows.some((r) => r.value === '')).toBe(true);
  });

  it('the disable words the portal reads are the generated copy of the table header', () => {
    expect(ENUMS.thresholdDisable).toEqual({ words: matrix.disable_words, letters: matrix.disable_letters });
  });
});

describe('readThresholdRow ↔ the exporter', () => {
  it.each(each(rows))('%s', (_l, r) => {
    const got = readThresholdRow(r.value);
    expect(got.kind).toBe(kindOf(r));
    expect(thresholdRowDisabled(r.value)).toBe(r.row_disabled);
    if (got.kind === 'number') {
      expect(got.number).toBe(Number(r.value_part));
      expect(got.severity).toBe(r.severity);
    }
    if (got.kind === 'unreadable' || got.kind === 'disabled') expect(got.raw).toBe(r.value);
  });

  it.each(each(rows))('%s on a _critical key: the whole value is the number', (_l, r) => {
    const got = readThresholdRow(r.value, 'critical');
    expect(got.kind).toBe(criticalKindOf(r));
    if (got.kind === 'number') {
      expect(got.number).toBe(Number(r.value_part));
      expect(got.severity).toBe('critical');
    }
  });

  it('which keys are _critical companions', () => {
    expect(isCriticalKey('mysql_connections_critical')).toBe(true);
    for (const k of ['mysql_connections', '_routing_x_critical', '_critical', 'critical']) expect(isCriticalKey(k), k).toBe(false);
  });

  it('a number or a boolean from a caller with no text is read as given; a map or a list is not a scalar', () => {
    expect(readThresholdRow(80)).toEqual({ kind: 'number', raw: '80', number: 80, severity: 'warning' });
    expect(readThresholdRow(0)).toMatchObject({ kind: 'number', number: 0 });
    expect(readThresholdRow(false)).toEqual({ kind: 'disabled', raw: 'false' });
    expect(readThresholdRow(true)).toEqual({ kind: 'unreadable', raw: 'true' });
    for (const v of [Infinity, -Infinity, NaN]) expect(readThresholdRow(v).kind, String(v)).toBe('unreadable');
    for (const v of [undefined, null]) expect(readThresholdRow(v), String(v)).toEqual({ kind: 'absent' });
    // The exporter refuses the empty string like any other unreadable text.
    expect(readThresholdRow('')).toEqual({ kind: 'unreadable', raw: '' });
    expect(readThresholdRow(95, 'critical')).toEqual({ kind: 'number', raw: '95', number: 95, severity: 'critical' });
    for (const v of [{ default: '80' }, ['80'], {}]) expect(readThresholdRow(v)).toEqual({ kind: 'structured' });
    expect(thresholdScalarText({})).toBeNull();
  });

  it('through the page\'s own YAML parser: the quoted texts the old reader misread', () => {
    const { config } = parseTenantYaml('a: "1_000"\nb: 1_000\nc: "0x1F"\nd: "70:high"\ne: .inf\nf: off\ng: False\nh: "70:critical"\ni: ""\n');
    const kinds = Object.fromEntries(Object.entries(config).map(([k, v]) => [k, readThresholdRow(v).kind]));
    expect(kinds).toEqual({
      a: 'unreadable', b: 'unreadable', c: 'unreadable', d: 'unreadable', e: 'unreadable',
      f: 'disabled', g: 'disabled', h: 'number', i: 'unreadable',
    });
  });

  // ── #3919: the exporter reads the scalar's TEXT (ScheduledValue.UnmarshalYAML
  // takes the node's value), so quoting changes nothing, and the page reads
  // the same text through parseTenantYaml. The verdicts below were measured
  // on the Go readers (parseThresholdValue / parseThresholdNumber /
  // thresholdRowDisabled over a yaml.v3 decode into ScheduledValue) — the
  // reading of one scalar, not what the exporter does with the whole file.
  const kindsOf = (yaml: string, tier: 'base' | 'critical' = 'base') => {
    const { config, errors } = parseTenantYaml(yaml);
    expect(errors).toEqual([]);
    return Object.fromEntries(Object.entries(config).map(([k, v]) => [k, readThresholdRow(v, tier).kind]));
  };

  it('an UNQUOTED 0x1F / 0o17 / 0b101, or a number of more than 20 digits, is unreadable, as it is quoted', () => {
    const yaml = 'hex: 0x1F\noct: 0o17\nbin: 0b101\nlong: 12345678901234567890123\n'
      + 'qhex: "0x1F"\nqoct: \'0o17\'\nqbin: "0b101"\nqlong: "12345678901234567890123"\n';
    const all = {
      hex: 'unreadable', oct: 'unreadable', bin: 'unreadable', long: 'unreadable',
      qhex: 'unreadable', qoct: 'unreadable', qbin: 'unreadable', qlong: 'unreadable',
    };
    expect(kindsOf(yaml)).toEqual(all);
    expect(kindsOf(yaml, 'critical')).toEqual(all);
    // The text is what the page shows, not the number YAML would make of it.
    const { config } = parseTenantYaml(yaml);
    expect(readThresholdRow(config.bin)).toEqual({ kind: 'unreadable', raw: '0b101' });
    expect(readThresholdRow(config.long)).toEqual({ kind: 'unreadable', raw: '12345678901234567890123' });
    // Why parseYaml's config must not be handed to the reader: the same
    // document, typed, is three numbers the exporter never serves.
    const typed = parseYaml(yaml).config;
    expect([typed.hex, typed.oct, typed.bin]).toEqual([31, 15, 5]);
  });

  it('controls — what the exporter serves is read as before, quoted or not', () => {
    const { config } = parseTenantYaml(
      'a: 1e20\nb: 1.5e20\nc: 1e-7\nd: -0\ne: 80\nf: "80"\ng: +5\nh: 017\ni: 5.\nj: .5\nk: 1E5\nl: 80:critical\nm: 80 # note\n');
    expect(Object.values(config).map((v) => readThresholdRow(v))).toMatchObject([
      { kind: 'number', number: 1e20, raw: '1e20' }, { kind: 'number', number: 1.5e20 },
      // `-0` is the text's own number (the exporter serves -0 too); it fires as 0 does.
      { kind: 'number', number: 1e-7 }, { kind: 'number', number: -0 },
      { kind: 'number', number: 80, raw: '80' }, { kind: 'number', number: 80, raw: '80' },
      { kind: 'number', number: 5 }, { kind: 'number', number: 17 }, { kind: 'number', number: 5 },
      { kind: 'number', number: 0.5 }, { kind: 'number', number: 1e5 },
      { kind: 'number', number: 80, severity: 'critical' }, { kind: 'number', number: 80, raw: '80' },
    ]);
    // The disable words, in any case and quoted or not; `True` / `yes` / `no` are not among them.
    expect(kindsOf('a: False\nb: false\nc: "false"\nd: OFF\ne: Disable\nf: True\ng: true\nh: yes\ni: no\n')).toEqual({
      a: 'disabled', b: 'disabled', c: 'disabled', d: 'disabled', e: 'disabled',
      f: 'unreadable', g: 'unreadable', h: 'unreadable', i: 'unreadable',
    });
  });

  it('the other YAML scalar shapes, each as the exporter has it', () => {
    // A YAML null writes nothing (#2518); the same letters quoted are a text.
    expect(kindsOf('a: ~\nb: null\nc: Null\nd:\ne: "~"\nf: "null"\ng: ""\nh: !!str\ni: !!str ~\n')).toEqual({
      a: 'absent', b: 'absent', c: 'absent', d: 'absent',
      e: 'unreadable', f: 'unreadable', g: 'unreadable', h: 'unreadable', i: 'unreadable',
    });
    // YAML's own floats and separators, a timestamp, a sexagesimal: texts the grammar refuses.
    expect(kindsOf('a: .inf\nb: -.inf\nc: .nan\nd: .NaN\ne: 1_000\nf: 1e999\ng: 2024-01-01\nh: 1:30\ni: 0x1F:critical\n')).toEqual({
      a: 'unreadable', b: 'unreadable', c: 'unreadable', d: 'unreadable', e: 'unreadable',
      f: 'unreadable', g: 'unreadable', h: 'unreadable', i: 'unreadable',
    });
    // An explicit tag does not change the text that is read.
    expect(kindsOf('a: !!str 5\nb: !!int 5\nc: !!float 5\nd: !!int 0x1F\ne: !!bool false\n')).toEqual({
      a: 'number', b: 'number', c: 'number', d: 'unreadable', e: 'disabled',
    });
    // Block scalars (the trailing newline is trimmed), an alias, a merge key.
    expect(kindsOf('a: |\n  80\nb: >\n  80\nc: |-\n  80:critical\n')).toEqual({ a: 'number', b: 'number', c: 'number' });
    expect(kindsOf('a: |\n  80\nc: |-\n  80:critical\n', 'critical')).toEqual({ a: 'number', c: 'unreadable' });
    expect(kindsOf('src: &x 0x1F\nk: *x\nok: &y 80\nk2: *y\n')).toMatchObject({ k: 'unreadable', k2: 'number' });
    expect(kindsOf('<<: {k: 0x1F, k2: 80}\n')).toEqual({ k: 'unreadable', k2: 'number' });
    // Only the first document is read. ⚠️ parseYaml's behaviour, kept, and
    // NOT the exporter's: it refuses a file with a second document.
    expect(readThresholdRow(parseTenantYaml('k: 80\n---\nk: 0x1F\n').config.k)).toMatchObject({ kind: 'number', number: 80 });
  });

  it('a scheduled value keeps its texts too, and is still not simulated', () => {
    const { config } = parseTenantYaml('k: {default: 0x1F, overrides: [{window: "01:00-02:00", value: 0b101}]}\n');
    expect(config.k).toEqual({ default: '0x1F', overrides: [{ window: '01:00-02:00', value: '0b101' }] });
    expect(readThresholdRow(config.k)).toEqual({ kind: 'structured' });
  });

  it('simulateAlerts and validateConfig over the page\'s parse: 0b101 is never a threshold of 5', () => {
    const { config } = parseTenantYaml('k: 0b101\nk_critical: 0o17\nok: 1e20\n');
    const reading = { current: 1e300, unit: 'x', packLabel: 'p' };
    expect(simulateAlerts(config, { k: reading, ok: reading })).toMatchObject([
      { metric: 'k', threshold: null, critical_threshold: null, firing: false, severity: 'unreadable', raw: '0b101', critical_unreadable: '0o17' },
      { metric: 'ok', threshold: 1e20, firing: true },
    ]);
    const issues = validateConfig(config, []).issues.map((i: any) => [i.field, i.level, i.msg]);
    expect(issues).toContainEqual(['k', 'error', unreadableThresholdMessage('0b101')]);
    expect(issues).toContainEqual(['k_critical', 'error', unreadableThresholdMessage('0o17', 'critical')]);
    // `1e20` is read as the number it is: the range note judges it, nothing refuses it.
    expect(issues.filter(([f]: string[]) => f === 'ok').map(([, , m]: string[]) => m))
      .toEqual(['Threshold 100000000000000000000 exceeds 100, verify if correct']);
  });

  it('a page that reads thresholds through the engine parses with parseTenantYaml, not parseYaml', () => {
    // parseYaml's numbers are the YAML parser's reading; the reader takes a
    // JavaScript number as given (a number field has no text). So the wiring
    // is what keeps `0x1F` out, and this holds it for every page outside
    // _common/ (a module in there that fed the engine would not be seen).
    const toolsDir = resolve(here, '../src/interactive/tools');
    const pages = readdirSync(toolsDir, { recursive: true, encoding: 'utf8' })
      .filter((f) => /\.[jt]sx?$/.test(f) && !f.includes('_common'))
      .map((f) => [f, readFileSync(resolve(toolsDir, f), 'utf8').replace(/^---\n[\s\S]*?\n---\n/, '')] as const);
    // Every import of `mod` in src, in any of its spellings: the names of a
    // `{ … }` list (before any `as`), or '*' for a namespace, default, bare
    // or dynamic import — which this check cannot see through, so they count
    // as importing everything.
    const importsFrom = (src: string, mod: string) => {
      const names: string[] = [];
      for (const m of src.matchAll(/import\s*([^'"();]*?)\s*(?:from\s*)?\(?\s*['"]([^'"]+)['"]/g)) {
        // with or without the extension: the bundler resolves both
        if (!m[2].replace(/\.jsx?$/, '').endsWith(mod.replace(/\.jsx?$/, ''))) continue;
        const list = /^\{([^}]*)\}$/.exec(m[1].trim());
        if (!list) { names.push('*'); continue; }
        names.push(...list[1].split(',').map((n) => n.trim().split(/\s+as\s+/)[0]).filter(Boolean));
      }
      return names;
    };
    // the helper itself, on the spellings a page could use
    expect(importsFrom("import { a as b, c } from './x/m.js';", 'x/m.js')).toEqual(['a', 'c']);
    expect(importsFrom('import { a } from "./x/m.js";', 'x/m.js')).toEqual(['a']);
    expect(importsFrom("import * as M from './x/m.js';", 'x/m.js')).toEqual(['*']);
    expect(importsFrom("const M = await import('./x/m.js');", 'x/m.js')).toEqual(['*']);
    expect(importsFrom("import { a } from './x/m';", 'x/m.js')).toEqual(['a']);
    expect(importsFrom("import { a } from './x/other.js';", 'x/m.js')).toEqual([]);

    const engine = ['simulateAlerts', 'validateConfig', 'simulateWithDedup', '*'];
    const engineUsers = pages.filter(([, src]) => importsFrom(src, 'sim/alert-engine.js').some((n) => engine.includes(n)));
    expect(engineUsers.map(([f]) => f).sort()).toEqual(['AlertPreviewTab.jsx', 'YamlValidatorTab.jsx', 'alert-simulator.jsx']);
    // Only the text-keeping parse (and the duration helper) beside the
    // engine: parseYaml, loadYamlDocument and a namespace import all hand
    // over typed values.
    for (const [f, src] of engineUsers) {
      for (const n of importsFrom(src, 'validation/yaml-parser.js')) {
        expect(['parseTenantYaml', 'parseDuration'], f).toContain(n);
      }
    }
  });
});

describe('simulateAlerts ↔ the exporter', () => {
  const reading = { current: 1e300, unit: 'x', packLabel: 'p' };

  it.each(each(rows))('%s', (_l, r) => {
    const alerts = simulateAlerts({ k: r.value }, { k: reading });
    expect(alerts).toHaveLength(1);
    const a = alerts[0];
    const kind = kindOf(r);
    if (kind === 'number') {
      expect(a.threshold).toBe(Number(r.value_part));
      expect(a.firing).toBe(true);
      // `70:critical` on the base key is a critical-severity row.
      expect(a.severity).toBe(r.severity);
      expect(a.critical_firing).toBe(r.severity === 'critical');
      return;
    }
    // Not served: never a number cut out of the text, never firing.
    expect(a.threshold).toBeNull();
    expect(a.firing).toBe(false);
    expect(a.critical_firing).toBe(false);
    expect(a.severity).toBe({ disabled: 'disabled', unreadable: 'unreadable' }[kind]);
    expect(a.raw).toBe(kind === 'unreadable' ? r.value : undefined);
  });

  it.each(each(rows))('%s as the _critical companion', (_l, r) => {
    const [a] = simulateAlerts({ k: '10', k_critical: r.value }, { k: reading });
    const kind = criticalKindOf(r);
    expect(a.threshold).toBe(10);
    expect(a.critical_threshold).toBe(kind === 'number' ? Number(r.value_part) : null);
    expect(a.critical_firing).toBe(kind === 'number');
    expect(a.severity).toBe(kind === 'number' ? 'critical' : 'warning');
    expect(a.critical_unreadable).toBe(kind === 'unreadable' ? r.value : undefined);
  });

  it('no threshold at all is not an unreadable one', () => {
    for (const config of [{}, { k: null }, { k: undefined }]) {
      expect(simulateAlerts(config, { k: reading })).toMatchObject([{ severity: 'no-threshold', threshold: null }]);
    }
  });

  it('an unreadable base value does not take the critical tier down with it', () => {
    // resolveBaseRows falls back to the platform default (not simulated) and
    // resolveCriticalRows still serves `_critical`: only a DISABLED base
    // switches the tier off.
    const [a] = simulateAlerts({ k: 'abc', k_critical: 95 }, { k: { ...reading, current: 99 } });
    expect(a).toMatchObject({
      raw: 'abc', threshold: null, critical_threshold: 95, critical_firing: true, severity: 'critical', firing: false,
    });
    const [below] = simulateAlerts({ k: 'abc', k_critical: 95 }, { k: { ...reading, current: 90 } });
    expect(below).toMatchObject({ raw: 'abc', critical_threshold: 95, critical_firing: false, severity: 'unreadable' });
    const [off] = simulateAlerts({ k: 'disable', k_critical: 95 }, { k: { ...reading, current: 99 } });
    expect(off).toMatchObject({ severity: 'disabled', critical_threshold: null, critical_firing: false });
  });
});

describe('simulateWithDedup ↔ the exporter', () => {
  const DEFS = { k: { alert: 'K', severity: 'warning' } };

  it.each(each(rows))('%s', (_l, r) => {
    const out = simulateWithDedup({ k: r.value }, { k: 1e300 }, true, DEFS);
    const kind = kindOf(r);
    expect(out.firing.map((f: any) => f.threshold)).toEqual(kind === 'number' ? [Number(r.value_part)] : []);
    expect(out.ok).toEqual([]);
    expect(out.suppressed).toEqual([]);
    expect(out.unreadable).toEqual(kind === 'unreadable' ? [{ key: 'k', def: DEFS.k, raw: r.value }] : []);
  });

  it.each(each(rows))('%s on a _critical key', (_l, r) => {
    const defs = { k_critical: { alert: 'KC', severity: 'critical' } };
    const out = simulateWithDedup({ k_critical: r.value }, { k_critical: 1e300 }, true, defs);
    const kind = criticalKindOf(r);
    expect(out.firing.map((f: any) => f.threshold)).toEqual(kind === 'number' ? [Number(r.value_part)] : []);
    expect(out.unreadable.map((u: any) => u.raw)).toEqual(kind === 'unreadable' ? [r.value] : []);
  });
});

describe('validateConfig ↔ the exporter', () => {
  type Issue = { level: string; field: string; msg: string };
  const forKey = (value: unknown): Issue[] => validateConfig({ k: value }, []).issues.filter((i: Issue) => i.field === 'k');

  it.each(each(rows))('%s', (_l, r) => {
    const issues = forKey(r.value);
    const kind = kindOf(r);
    const unreadable = issues.filter((i) => i.msg === unreadableThresholdMessage(r.value));
    expect(unreadable.map((i) => i.level)).toEqual(kind === 'unreadable' ? ['error'] : []);
    expect(issues.filter((i) => i.msg === 'Metric disabled')).toHaveLength(kind === 'disabled' ? 1 : 0);
    // The range notes judge the number the exporter serves, and only that.
    const n = Number(r.value_part);
    const ranged = issues.filter((i) => /exceeds 100|< 0, invalid/.test(i.msg));
    expect(ranged.length > 0).toBe(kind === 'number' && (n > 100 || n < 0));
    for (const i of ranged) expect(i.msg).toContain(`Threshold ${n} `);
  });

  it.each(each(rows))('%s on a _critical key', (_l, r) => {
    const issues: Issue[] = validateConfig({ k_critical: r.value }, []).issues.filter((i: Issue) => i.field === 'k_critical');
    const kind = criticalKindOf(r);
    const unreadable = issues.filter((i) => i.msg === unreadableThresholdMessage(r.value, 'critical'));
    expect(unreadable.map((i) => i.level)).toEqual(kind === 'unreadable' ? ['error'] : []);
    expect(issues.filter((i) => i.msg === 'Metric disabled')).toHaveLength(kind === 'disabled' ? 1 : 0);
    expect(issues.some((i) => /exceeds 100|< 0, invalid/.test(i.msg))).toBe(
      kind === 'number' && (Number(r.value_part) > 100 || Number(r.value_part) < 0));
  });

  it('the sentence names the value and says what the exporter does instead', () => {
    const msg = unreadableThresholdMessage('1_000');
    expect(msg).toContain('The exporter will not read this value');
    expect(msg).toContain('"1_000"');
    // A base key falls back to the platform default; it is not "no threshold".
    expect(msg).toContain('falls back to the platform default');
    expect(msg).not.toContain('critical tier');
    expect(forKey('1_000').map((i) => i.msg)).toEqual([msg]);
    // A `_critical` key has nothing to fall back to: the tier is dropped.
    const crit = unreadableThresholdMessage('95:critical', 'critical');
    expect(crit).toContain('"95:critical"');
    expect(crit).toContain('this _critical key is not served');
    expect(crit).not.toContain('platform default');
    expect(crit).not.toBe(unreadableThresholdMessage('95:critical'));
  });

  it('the range notes are about the number the exporter serves, where parseFloat reads another', () => {
    // U+0085 is trimmed by the exporter and is not whitespace to parseFloat
    // (NaN: no note at all); `-5:critical` and `1e3` are read alike.
    expect(readThresholdRow('\u0085500')).toMatchObject({ kind: 'number', number: 500 });
    expect(Number.isNaN(parseFloat('\u0085500'))).toBe(true);
    expect(forKey('\u0085500').map((i) => [i.level, i.msg])).toEqual([['warning', 'Threshold 500 exceeds 100, verify if correct']]);
    expect(forKey('\u0085-5').map((i) => i.level)).toEqual(['error']);
    expect(forKey('1e3').map((i) => i.msg)).toEqual(['Threshold 1000 exceeds 100, verify if correct']);
  });

  it('a scheduled value is not judged', () => {
    expect(forKey({ default: '1_000' })).toEqual([]);
  });
});
