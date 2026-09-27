/**
 * Drift guard (#2033): every Config Template Gallery template must be a
 * tenant body the tenant schema accepts. Gallery `yaml` is a tenant fragment
 * (what goes under tenants.<id>:), so it is wrapped as
 * `tenants: { demo-tenant: … }` before validation. Before this guard, 18 of
 * 24 templates were rejected (17 flat `_routing.receiver_type`, one
 * `_metadata.compliance`) and nothing noticed.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
// js-yaml is the oracle (#2033), not a portal tool's own parser.
import { load } from 'js-yaml';
import { validateTenantDoc } from './helpers/tenant-schema';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DATA = JSON.parse(
  readFileSync(resolve(__dirname, '../../../docs/assets/template-data.json'), 'utf8'),
);

describe('template-data.json gallery templates pass tenant-config.schema.json', () => {
  it('has templates to check', () => {
    expect(DATA.templates.length).toBeGreaterThan(0);
  });

  for (const tpl of DATA.templates as Array<{ id: string; yaml: string }>) {
    it(`${tpl.id}`, () => {
      const body = load(tpl.yaml) ?? {};
      expect(validateTenantDoc({ tenants: { 'demo-tenant': body } })).toEqual([]);
    });
  }
});
