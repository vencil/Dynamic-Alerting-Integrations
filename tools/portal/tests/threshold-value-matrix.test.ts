/**
 * The RecipeBuilder's reader of a threshold value ↔ the exporter (#3457):
 * the JavaScript half of tests/shared/threshold_value_matrix.json.
 *
 * The matrix's verdict columns are written by Go (pkg/config
 * threshold_value_parity_test.go) from the exporter's own readers, and hold
 * the Go, Python and JSON-schema readers to the same rows. Here every row is
 * read by `readThreshold`, and what the form calls a valid threshold is what
 * the exporter serves — so a value the form lets through is not one the
 * tenant-api PUT then answers 422 for.
 *
 * The form used to read the number with `Number()`, which takes `0x1F`,
 * `0o17`, `0b101` and `Infinity`, and trims with JavaScript's own whitespace
 * set. Those are rows of the matrix.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import ENUMS from '../src/interactive/tools/_common/data/recipe-enums.json';
import {
  readThreshold,
  parseThresholdValue,
  composeThreshold,
  isFieldValid,
  allRequiredValid,
} from '../src/interactive/tools/recipe-builder/engine.js';

type Row = {
  value: string;
  ok: boolean;
  value_part?: string;
  severity?: string;
  grammar_ok?: boolean;
  float_ok?: boolean;
  custom_alert_disabled: boolean;
  row_disabled: boolean;
};

// ⛔ Each path is one literal that climbs to the repo root: that is the shape
// tests/ops/test_ci_path_filter_coverage.py reads to hold ci.yml's `portal`
// path filter to the files this test opens. A repo-root constant joined with
// 'tests/…' is a shape it does not see.
const here = dirname(fileURLToPath(import.meta.url));
const matrix = JSON.parse(readFileSync(
  resolve(here, '../../../tests/shared/threshold_value_matrix.json'), 'utf8',
));
const contract = JSON.parse(readFileSync(
  resolve(here, '../../../components/threshold-exporter/app/pkg/config/recipe_contract.json'), 'utf8',
));
const schema = JSON.parse(readFileSync(
  resolve(here, '../../../docs/schemas/tenant-config.schema.json'), 'utf8',
));
const rows: Row[] = matrix.rows;
const label = (r: Row) => JSON.stringify(r.value.length > 60 ? `${r.value.slice(0, 30)}…(${r.value.length})` : r.value);

describe('threshold value matrix ↔ RecipeBuilder', () => {
  it('the table is the one Go writes, with both verdicts and the rows this is about', () => {
    expect(rows.length).toBeGreaterThan(100);
    expect(new Set(rows.map((r) => r.ok))).toEqual(new Set([true, false]));
    const byValue = new Map(rows.map((r) => [r.value, r]));
    for (const v of ['0x1F', '0o17', '0b101', '0x10', 'Infinity', '1e309', ':critical', '', '1_000', '0x1p4',
      '123456789012345678901', '\ufeff70', '70\u0085']) {
      expect(byValue.has(v), v).toBe(true);
    }
    expect(byValue.get('70\u0085')!.float_ok).toBe(true);
    expect(byValue.get('\ufeff70')!.float_ok).toBe(false);
  });

  it('the form reads the generated copy of the contract pattern and of the trim set', () => {
    expect(ENUMS.patterns.thresholdNumber).toBe(contract.patterns.thresholdNumber);
    expect(ENUMS.thresholdTrim).toEqual(matrix.trim_code_points);
    expect(ENUMS.severity).toEqual(matrix.severities);
  });

  it.each(rows.map((r) => [label(r), r] as const))('readThreshold(%s) is the exporter\'s reading', (_l, r) => {
    const got = readThreshold(r.value);
    expect(got.ok).toBe(r.ok);
    if (!r.ok) return;
    expect(got.valuePart).toBe(r.value_part);
    expect(got.severity).toBe(r.severity);
    expect(got.grammarOk).toBe(r.grammar_ok);
    expect(got.number !== null).toBe(r.float_ok);
    if (got.number !== null) {
      // A text of the grammar is plain decimal: Number() of the value half.
      expect(got.number).toBe(Number(r.value_part));
      expect(Number.isFinite(got.number)).toBe(true);
    }
  });

  it.each(rows.map((r) => [label(r), r] as const))('the form calls %s valid exactly when the exporter serves it', (_l, r) => {
    const served = r.ok && r.float_ok === true;
    expect(isFieldValid('threshold', r.value)).toBe(served);
    expect(!Number.isNaN(parseThresholdValue(r.value))).toBe(served);
    const form = { name: 'a', metric: 'm', window: '5m', op: '>', threshold: r.value, severity: 'critical' };
    // An empty field is "not filled in" before it is "invalid"; both are not ready.
    expect(allRequiredValid('threshold', form)).toBe(served);
  });

  it.each(rows.filter((r) => r.ok && r.float_ok).map((r) => [label(r), r] as const))(
    'what the form emits for %s is read back as the same number, with the dropdown\'s severity', (_l, r) => {
      for (const severity of ENUMS.severity) {
        const emitted = composeThreshold({ threshold: r.value, severity });
        const back = readThreshold(emitted);
        expect(back.ok).toBe(true);
        expect(back.severity).toBe(severity);
        expect(back.number).toBe(readThreshold(r.value).number);
      }
    },
  );

  it.each(rows.filter((r) => !(r.ok && r.float_ok)).map((r) => [label(r), r] as const))(
    'what the form would emit for the refused %s is refused too', (_l, r) => {
      const back = readThreshold(composeThreshold({ threshold: r.value, severity: 'critical' }));
      expect(back.ok && back.number !== null).toBe(false);
    },
  );
});

describe('the patterns compile in every ECMAScript mode', () => {
  // `v` (unicodeSets) is the mode an HTML `pattern=` attribute compiles in;
  // a character class such as `[-+]` is a syntax error there.
  const sources: Array<[string, string]> = [
    ['patterns.thresholdNumber', contract.patterns.thresholdNumber],
    ['thresholdScalar.pattern', schema.definitions.thresholdScalar.pattern],
  ];
  const samples = rows.flatMap((r) => [r.value, r.value_part ?? '']);
  it.each(sources)('%s reads alike with no flag, `u` and `v`', (_name, source) => {
    const plain = samples.map((s) => new RegExp(source).test(s));
    expect(plain.includes(true) && plain.includes(false)).toBe(true);
    for (const flag of ['u', 'v']) {
      expect(samples.map((s) => new RegExp(source, flag).test(s)), `flag ${flag}`).toEqual(plain);
    }
  });
});

describe('the pattern the form reads is the grammar, not Number()', () => {
  const re = new RegExp(ENUMS.patterns.thresholdNumber);
  it('refuses what Number() reads and the exporter does not', () => {
    for (const v of ['0x1F', '0o17', '0b101', '0X10', 'Infinity', '-Infinity', '', ' 5', '5 ', '5\n', '1_000']) {
      expect(re.test(v), v).toBe(false);
    }
    for (const v of ['0x1F', '0o17', '0b101', 'Infinity', '', ' 5']) {
      expect(Number.isNaN(Number(v)), `Number(${JSON.stringify(v)}) reads it`).toBe(false);
    }
  });
  it('takes a mantissa of 20 digits and not of 21, wherever the point is', () => {
    const digits = '12345678901234567890';
    expect(re.test(digits)).toBe(true);
    expect(re.test(`${digits}1`)).toBe(false);
    for (let i = 0; i <= 20; i += 1) {
      expect(re.test(`${digits.slice(0, i)}.${digits.slice(i)}`), `point at ${i}`).toBe(true);
      expect(re.test(`${digits.slice(0, i)}.${digits.slice(i)}1`), `21 digits, point at ${i}`).toBe(false);
    }
    expect(re.test(`${digits}e-${'9'.repeat(30)}`)).toBe(true);
  });
});
