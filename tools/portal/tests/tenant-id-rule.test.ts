import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import RULE from '../src/interactive/tools/_common/data/tenant-id.json';

/**
 * ADR-035: the portal's tenant-id.json is generated from
 * docs/schemas/tenant-config.schema.json#/definitions/tenantId by
 * scripts/tools/dx/gen_tenant_id_json.py. This pins that the copy carries the
 * schema's rule and that ECMAScript's RegExp gives the same verdicts as Go
 * (pkg/tenantid) and Python (tests/shared/test_tenant_id_rule.py) on the shared
 * case table.
 */
const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => JSON.parse(readFileSync(resolve(__dirname, rel), 'utf8'));
const schema = read('../../../docs/schemas/tenant-config.schema.json');
const cases = read(
  '../../../components/threshold-exporter/app/pkg/tenantid/testdata/tenant_id_cases.json',
);

describe('tenant-id.json ↔ tenant-config.schema.json', () => {
  it('carries the schema pattern and description', () => {
    expect(RULE.pattern).toBe(schema.definitions.tenantId.pattern);
    expect(RULE.description).toBe(schema.definitions.tenantId.description);
  });
});

describe('tenant-id rule verdicts (shared case table)', () => {
  const re = new RegExp(RULE.pattern);
  it('accepts every valid case', () => {
    expect(cases.valid.length).toBeGreaterThan(0);
    for (const id of cases.valid) expect(re.test(id), JSON.stringify(id)).toBe(true);
  });
  it('rejects every invalid case', () => {
    expect(cases.invalid.length).toBeGreaterThan(0);
    for (const id of cases.invalid) expect(re.test(id), JSON.stringify(id)).toBe(false);
  });
});
