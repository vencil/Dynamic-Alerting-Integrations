/**
 * Unit tests for tenant-manager/utils/yaml-generators.js
 * — Vitest next-batch (PR-3 of expansion).
 *
 * Both generators are pure string assembly — no React, no globals, no side
 * effects. Each emits per-tenant fragments headed by `# <id>`, to be pasted
 * under tenants.<id>: in that tenant's existing file (#1988 silent, #2033
 * maintenance). Pasted fragments are checked against the tenant schema.
 */
import { describe, it, expect } from 'vitest';
import {
  generateMaintenanceYaml,
  generateSilentModeYaml,
} from '../src/interactive/tools/tenant-manager/utils/yaml-generators.js';
// js-yaml is the oracle (#2033), not a portal tool's own parser.
import { load } from 'js-yaml';
import { validateTenantDoc } from './helpers/tenant-schema';

const NOW = new Date('2026-09-25T10:00:00.123Z');

// Paste each `# <id>` block under tenants.<id>: of its own tenant.
function pasteEach(out: string, ids: string[]) {
  const blocks = out.split('\n\n');
  expect(blocks.map(b => b.split('\n')[0])).toEqual(ids.map(id => `# ${id}`));
  return blocks.map((b, i) => {
    const body = b.split('\n').filter(l => !l.startsWith('#')).join('\n');
    return load(`tenants:\n  ${ids[i]}:\n${body}`) as any;
  });
}

describe.each([
  ['generateMaintenanceYaml', generateMaintenanceYaml, '_state_maintenance',
    { expires: '2026-09-26T10:00:00Z', reason: 'Scheduled maintenance' }],
  ['generateSilentModeYaml', generateSilentModeYaml, '_silent_mode',
    { target: 'all', expires: '2026-09-26T10:00:00Z', reason: 'Under investigation' }],
] as const)('%s', (_name, gen, key, want) => {
  it('emits one block per tenant, headed by a single `# <id>` line', () => {
    const blocks = gen(['db-a', 'db-b'], NOW).split('\n\n');
    expect(blocks.map(b => b.split('\n')[0])).toEqual(['# db-a', '# db-b']);
    for (const b of blocks) expect(b.split('\n').filter(l => l.startsWith('#'))).toHaveLength(1);
  });

  it(`each block pasted under tenants.<id>: parses to that tenant's ${key} and passes the schema`, () => {
    const ids = ['db-a', 'db-b'];
    pasteEach(gen(ids, NOW), ids).forEach((doc, i) => {
      expect(doc).toEqual({ tenants: { [ids[i]]: { [key]: want } } });
      expect(validateTenantDoc(doc)).toEqual([]);
    });
  });

  it('expires is after the injected now (never a fixed date)', () => {
    for (const now of [NOW, new Date('2031-01-01T00:00:00Z')]) {
      const [doc] = pasteEach(gen(['t'], now), ['t']);
      expect(Date.parse(doc.tenants.t[key].expires)).toBeGreaterThan(now.getTime());
    }
  });

  it('is not a ConfigMap and not a standalone file', () => {
    const out = gen(['db-a'], NOW);
    expect(out).not.toMatch(/apiVersion|kind:|ConfigMap|^tenants:/m);
  });

  it('preserves tenant order', () => {
    const heads = gen(['z', 'a', 'm'], NOW).split('\n\n').map(b => b.split('\n')[0]);
    expect(heads).toEqual(['# z', '# a', '# m']);
  });

  it('handles empty tenant list', () => {
    expect(gen([], NOW)).toBe('');
  });
});
