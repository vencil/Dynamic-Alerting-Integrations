/**
 * `_common/validation/yaml-parser.js` parseDuration (#2711).
 *
 * parseDuration reads a routing timing value the way Alertmanager does
 * (prometheus/common model.ParseDuration) and returns milliseconds, or null
 * when Alertmanager refuses it. Its syntax is the generated
 * `_common/data/am-duration.json`, a copy of tenant-config.schema.json
 * definitions.duration (#2490; gen_am_duration_json.py, drift-gated).
 *
 * The shared verdict table is tests/shared/am_duration_matrix.json, written
 * from Go (pkg/config/am_duration_parity_test.go) and held against the
 * Python parser by tests/shared/test_am_duration_parity.py. This file holds
 * the portal to the same table.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseDuration } from '../src/interactive/tools/_common/validation/yaml-parser.js';
import RULE from '../src/interactive/tools/_common/data/am-duration.json';

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => JSON.parse(readFileSync(resolve(__dirname, rel), 'utf8'));
const schema = read('../../../docs/schemas/tenant-config.schema.json');
const matrix = read('../../../tests/shared/am_duration_matrix.json');

type Row = { value: string; am_valid: boolean; am_ns?: number };

describe('am-duration.json ↔ tenant-config.schema.json', () => {
  it('carries the schema pattern, minLength and description', () => {
    const def = schema.definitions.duration;
    expect(RULE.pattern).toBe(def.pattern);
    expect(RULE.minLength).toBe(def.minLength);
    expect(RULE.description).toBe(def.description);
  });

  it('every unit the schema pattern admits has a price', () => {
    // The pattern names its units as `[0-9]+<unit>`; a unit added there
    // without a price here would make parseDuration refuse a value the
    // schema accepts.
    const units = [...RULE.pattern.matchAll(/\[0-9\]\+([a-z]+)/g)].map((m) => m[1]);
    expect(units).toEqual(['y', 'w', 'd', 'h', 'm', 's', 'ms']);
    for (const u of units) expect(parseDuration(`1${u}`), u).not.toBeNull();
  });
});

describe('parseDuration — Alertmanager syntax', () => {
  it('reads single units in milliseconds', () => {
    expect(parseDuration('30s')).toBe(30_000);
    expect(parseDuration('5m')).toBe(300_000);
    expect(parseDuration('2h')).toBe(7_200_000);
    expect(parseDuration('1d')).toBe(86_400_000);
    expect(parseDuration('1w')).toBe(604_800_000);
    expect(parseDuration('1y')).toBe(31_536_000_000);
    expect(parseDuration('500ms')).toBe(500);
    expect(parseDuration('0')).toBe(0);
  });

  it('reads compound values (largest unit first)', () => {
    expect(parseDuration('1h30m')).toBe(5_400_000);
    expect(parseDuration('1h30m0s')).toBe(5_400_000);
    expect(parseDuration('1m1ms')).toBe(60_001);
  });

  it('refuses what Alertmanager refuses', () => {
    for (const bad of ['1.5h', '0.5s', '30m1h', '1h1h', '1ms1m', '1ns', '1us', '1µs',
      '', ' 1h', '1h ', '1h\n', '1H', '1', '00', '-1h', '+1h', 'abc']) {
      expect(parseDuration(bad), JSON.stringify(bad)).toBeNull();
    }
  });

  it('refuses a non-string (an unquoted YAML number has no unit)', () => {
    expect(parseDuration(30 as unknown as string)).toBeNull();
    expect(parseDuration(null as unknown as string)).toBeNull();
    expect(parseDuration(undefined as unknown as string)).toBeNull();
  });

  it('refuses int64-nanosecond overflow like model.ParseDuration', () => {
    expect(parseDuration('292y')).not.toBeNull();
    expect(parseDuration('293y')).toBeNull();
    expect(parseDuration('106751d23h47m16s854ms')).not.toBeNull();
    expect(parseDuration('106751d23h47m16s855ms')).toBeNull();
  });
});

describe('parseDuration — shared Go verdict table (am_duration_matrix.json)', () => {
  const rows: Row[] = matrix.rows;
  it('agrees with model.ParseDuration on every row', () => {
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      const got = parseDuration(row.value);
      const label = JSON.stringify(row.value);
      expect(got !== null, label).toBe(row.am_valid);
      if (row.am_valid) {
        const want = (row.am_ns as number) / 1e6;
        // am_ns above 2^53 is not exact once JSON.parse reads it.
        if (Number.isSafeInteger(row.am_ns)) expect(got, label).toBe(want);
        else expect(Math.abs((got as number) - want) / want, label).toBeLessThan(1e-12);
      }
    }
  });
});
