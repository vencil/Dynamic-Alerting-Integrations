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
 * exporter serves nothing for. The verdict columns are written by Go from the
 * exporter's own readers; nothing here restates the grammar.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import ENUMS from '../src/interactive/tools/_common/data/recipe-enums.json';
import {
  readThresholdRow, thresholdRowDisabled, thresholdScalarText,
} from '../src/interactive/tools/_common/validation/threshold-value.js';
import {
  simulateAlerts, simulateWithDedup, validateConfig, unreadableThresholdMessage,
} from '../src/interactive/tools/_common/sim/alert-engine.js';
import { parseYaml } from '../src/interactive/tools/_common/validation/yaml-parser.js';

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
  if (r.value === '') return 'absent';
  if (r.row_disabled) return 'disabled';
  return served(r) ? 'number' : 'unreadable';
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

  it('a parsed YAML scalar is read by its text; a map or a list is not a scalar', () => {
    expect(readThresholdRow(80)).toEqual({ kind: 'number', raw: '80', number: 80, severity: 'warning' });
    expect(readThresholdRow(0)).toMatchObject({ kind: 'number', number: 0 });
    expect(readThresholdRow(false)).toEqual({ kind: 'disabled', raw: 'false' });
    expect(readThresholdRow(true)).toEqual({ kind: 'unreadable', raw: 'true' });
    for (const v of [Infinity, -Infinity, NaN]) expect(readThresholdRow(v).kind, String(v)).toBe('unreadable');
    for (const v of [undefined, null, '']) expect(readThresholdRow(v), String(v)).toEqual({ kind: 'absent' });
    for (const v of [{ default: '80' }, ['80'], {}]) expect(readThresholdRow(v)).toEqual({ kind: 'structured' });
    expect(thresholdScalarText({})).toBeNull();
  });

  it('through the page\'s own YAML parser: the quoted texts the old reader misread', () => {
    const { config } = parseYaml('a: "1_000"\nb: 1_000\nc: "0x1F"\nd: "70:high"\ne: .inf\nf: off\ng: False\nh: "70:critical"\n');
    const kinds = Object.fromEntries(Object.entries(config).map(([k, v]) => [k, readThresholdRow(v).kind]));
    expect(kinds).toEqual({
      a: 'unreadable', b: 'unreadable', c: 'unreadable', d: 'unreadable', e: 'unreadable',
      f: 'disabled', g: 'disabled', h: 'number',
    });
  });

  it('known limit: an UNQUOTED 0x1F is already the number 31 when the parser hands it over', () => {
    // The exporter reads the scalar's text and refuses `0x1F`; js-yaml's core
    // schema has resolved it. Pinned so a parser change that keeps the text
    // shows up here, and so nobody reads the docstring's limit as closed.
    const { config } = parseYaml('k: 0x1F\n');
    expect(config.k).toBe(31);
    expect(readThresholdRow(config.k)).toMatchObject({ kind: 'number', number: 31 });
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
    expect(a.severity).toBe({ absent: 'no-threshold', disabled: 'disabled', unreadable: 'unreadable' }[kind]);
    if (kind === 'unreadable') expect(a.raw).toBe(r.value);
  });

  it.each(each(rows))('%s as the _critical companion', (_l, r) => {
    const [a] = simulateAlerts({ k: '10', k_critical: r.value }, { k: reading });
    const kind = kindOf(r);
    expect(a.threshold).toBe(10);
    expect(a.critical_threshold).toBe(kind === 'number' ? Number(r.value_part) : null);
    expect(a.critical_firing).toBe(kind === 'number');
    expect(a.severity).toBe(kind === 'number' ? 'critical' : 'warning');
    expect(a.critical_unreadable).toBe(kind === 'unreadable' ? r.value : undefined);
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

  it('the sentence names the value and says the exporter will not read it', () => {
    const msg = unreadableThresholdMessage('1_000');
    expect(msg).toContain('The exporter will not read this value');
    expect(msg).toContain('"1_000"');
    expect(forKey('1_000').map((i) => i.msg)).toEqual([msg]);
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
