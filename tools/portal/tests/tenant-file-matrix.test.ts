/**
 * parseTenantYaml ↔ the exporter on whole tenant files (#3999).
 *
 * tests/shared/tenant_file_matrix.json is written by Go from the exporter's
 * own file reader (ParseTenantFile, the flat scanner's call): `served` says
 * whether the exporter serves a tenant file holding `body`. Most rows are
 * generated there — every spelling of a schedule's `default` key times every
 * shape of its fields — so a spelling nobody thought of is held here without
 * anyone adding it.
 *
 * The portal reports a file-level error (parseTenantYaml's `errors`)
 * exactly where `served` is false. A row with `portal_divergence` is an
 * accepted difference and must still differ: when the portal comes to agree,
 * the note goes.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseTenantYaml } from '../src/interactive/tools/_common/validation/yaml-parser.js';

type Row = { body: string; served: boolean; portal_divergence?: string };
const here = dirname(fileURLToPath(import.meta.url));
const matrix = JSON.parse(readFileSync(resolve(here, '../../../tests/shared/tenant_file_matrix.json'), 'utf8'));
const rows: Row[] = matrix.rows;

describe('parseTenantYaml ↔ the exporter (tenant_file_matrix.json)', () => {
  beforeEach(() => { (window as any).__t = (_zh: string, en: string) => en; });

  it('the matrix is not vacuous and holds both verdicts', () => {
    expect(rows.length).toBeGreaterThan(100);
    expect(rows.some((r) => r.served)).toBe(true);
    expect(rows.some((r) => !r.served)).toBe(true);
    for (const r of rows) expect(typeof r.served, r.body).toBe('boolean');
  });

  it('a file-level error exactly where the exporter refuses the file', () => {
    const wrong: string[] = [];
    for (const r of rows) {
      const refused = parseTenantYaml(r.body).errors.length > 0;
      const expected = r.portal_divergence ? r.served : !r.served;
      if (refused !== expected) {
        wrong.push(`${JSON.stringify(r.body)} served=${r.served} portal refuses=${refused}`
          + (r.portal_divergence ? ' (listed as a divergence: the portal now agrees, drop the note)' : ''));
      }
    }
    expect(wrong).toEqual([]);
  });

  it('a large tenant file is read in linear time', () => {
    // A key lookup per key was a scan of the map (8.5k keys, 100 KB: 8 s).
    const yaml = `${Array.from({ length: 8500 }, (_, i) => `k${i}: ${i}`).join('\n')}\n`;
    const started = Date.now();
    expect(parseTenantYaml(yaml).errors).toEqual([]);
    expect(Date.now() - started).toBeLessThan(2000);
  });

  it('`schedules: false` leaves the schedule check out, and only it (#4001)', () => {
    const refused = rows.filter((r) => !r.served && parseTenantYaml(r.body).errors.some((e: string) => /cannot decode this schedule/.test(e)));
    expect(refused.length).toBeGreaterThan(100);
    for (const r of refused) {
      const off = parseTenantYaml(r.body, { schedules: false });
      expect(off.errors, r.body).toEqual([]);
    }
    // the other file-level errors stay: a second document, a syntax error
    expect(parseTenantYaml('k: 80\n---\nk: 90\n', { schedules: false }).errors).toHaveLength(1);
    expect(parseTenantYaml('k: [\n', { schedules: false }).errors).toHaveLength(1);
    // and a body with no schedule error reads the same either way
    for (const r of rows.filter((x) => x.served).slice(0, 200)) {
      expect(parseTenantYaml(r.body, { schedules: false }), r.body).toEqual(parseTenantYaml(r.body));
    }
  });
});
