/**
 * Drift guard (#2033): every Config Template Gallery template must be a
 * tenant body the tenant schema accepts. Gallery `yaml` is a tenant fragment
 * (what goes under tenants.<id>:), so it is wrapped as
 * `tenants: { demo-tenant: … }` before validation. Before this guard, 18 of
 * 24 templates were rejected (17 flat `_routing.receiver_type`, one
 * `_metadata.compliance`) and nothing noticed.
 *
 * Templates are checked the way the gallery serves them: after its load-time
 * placeholder resolution (`{{expires:+24h}}` → now + 24h) with a fixed now.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
// js-yaml is the oracle (#2033), not a portal tool's own parser.
import { load } from 'js-yaml';
import { validateTenantDoc, undeclaredReservedKeys } from './helpers/tenant-schema';
import { resolveTemplateData } from '../src/interactive/tools/template-gallery/placeholders.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const RAW = JSON.parse(
  readFileSync(resolve(__dirname, '../../../docs/assets/template-data.json'), 'utf8'),
);
const NOW = new Date('2026-09-25T10:00:00Z');
const DATA = resolveTemplateData(RAW, NOW);

describe('template-data.json gallery templates pass tenant-config.schema.json', () => {
  it('has templates to check', () => {
    expect(DATA.templates.length).toBeGreaterThan(0);
  });

  for (const tpl of DATA.templates as Array<{ id: string; yaml: string }>) {
    it(`${tpl.id}`, () => {
      const body = load(tpl.yaml) ?? {};
      expect(validateTenantDoc({ tenants: { 'demo-tenant': body } })).toEqual([]);
      expect(undeclaredReservedKeys(body as any)).toEqual([]);
    });
  }

  it('checker positive control: an undeclared _ key is caught even though the schema accepts it', () => {
    const body = { _domain_policy: 'finance' };
    expect(validateTenantDoc({ tenants: { t: body } })).toEqual([]);
    expect(undeclaredReservedKeys(body)).toEqual(['_domain_policy']);
  });
});

describe('gallery expiry times are relative to load time (#2033)', () => {
  it('no placeholder survives resolution', () => {
    expect(JSON.stringify(DATA)).not.toContain('{{');
  });

  // The window check below runs against a FIXED now, so a hard-coded date
  // inside that window would pass until real time overtakes it. This one
  // needs no clock: every expires in the shipped JSON must be a placeholder.
  it('every expires in the raw JSON is a {{expires:…}} placeholder', () => {
    const raw: Array<[string, unknown]> = [];
    const walk = (id: string, v: any) => {
      if (!v || typeof v !== 'object') return;
      for (const [k, x] of Object.entries(v)) {
        if (k === 'expires') raw.push([id, x]);
        walk(id, x);
      }
    };
    for (const tpl of RAW.templates) walk(tpl.id, load(tpl.yaml));
    expect(raw.length).toBeGreaterThanOrEqual(2);
    for (const [id, e] of raw) {
      expect({ id, e, placeholder: /^\{\{expires:[^}]+\}\}$/.test(String(e)) })
        .toEqual({ id, e, placeholder: true });
    }
  });

  it('every expires is after now and within 30 days', () => {
    const found: Array<[string, string]> = [];
    const walk = (id: string, v: any) => {
      if (!v || typeof v !== 'object') return;
      for (const [k, x] of Object.entries(v)) {
        if (k === 'expires') found.push([id, String(x)]);
        walk(id, x);
      }
    };
    for (const tpl of DATA.templates) walk(tpl.id, load(tpl.yaml));
    expect(found.length).toBeGreaterThanOrEqual(2);
    for (const [id, e] of found) {
      const t = Date.parse(e);
      expect({ id, e, after: t > NOW.getTime() }).toEqual({ id, e, after: true });
      expect({ id, e, within30d: t < NOW.getTime() + 30 * 86400e3 }).toEqual({ id, e, within30d: true });
    }
  });
});
