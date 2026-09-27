/**
 * schema-explorer ↔ tenant-config.schema.json (#2033).
 *
 * 1. Drift guard: every key path the explorer lists must exist in
 *    definitions.tenantConfig. A `_`-prefixed key must be a declared
 *    property — tenantConfig's additionalProperties accepts any string as a
 *    threshold, so `_domain_policy: finance` would otherwise "pass" while the
 *    exporter reads nothing from it.
 * 2. Insert: each insertable leaf produces a whole document rooted at
 *    `tenants:` with the leaf nested under its parents, and that document is
 *    accepted by the schema — except the listed leaves whose schema requires
 *    a sibling, which must be rejected for exactly that reason.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
// js-yaml is the oracle (#2033), not a portal tool's own parser.
import { load } from 'js-yaml';
import SchemaExplorer, { SCHEMA, TENANT_PLACEHOLDER, buildInsertYaml } from '../src/interactive/tools/schema-explorer.jsx';
import { TENANT_SCHEMA, validateTenantDoc } from './helpers/tenant-schema';

type Node = { key: string; example?: string; children?: Node[] };

const defs = TENANT_SCHEMA.definitions;
const deref = (s: any): any => (s && s.$ref ? deref(defs[s.$ref.replace('#/definitions/', '')]) : s);

// Sub-schemas a key can land in (all oneOf/anyOf branches are candidates).
function step(schema: any, key: string, top: boolean): any[] {
  const s = deref(schema);
  const branches = s.oneOf || s.anyOf;
  if (branches) return branches.flatMap((b: any) => step(b, key, top));
  if (key === '[]') return s.items ? [s.items] : [];
  if (s.properties && key in s.properties) return [s.properties[key]];
  // Only thresholds (non-reserved keys listed under <tenant_name>) may fall
  // through to additionalProperties; any other root is a reserved key and
  // must be declared.
  if (top && !key.startsWith('_') && s.additionalProperties) return [s.additionalProperties];
  return [];
}
function resolves(path: string[]): boolean {
  const threshold = path[0] === TENANT_PLACEHOLDER;
  const segs = path
    .filter(k => k !== TENANT_PLACEHOLDER)
    .flatMap(k => (k.startsWith('[].') ? ['[]', k.slice(3)] : [k]));
  let cur: any[] = [defs.tenantConfig];
  segs.forEach((seg, i) => { cur = cur.flatMap(s => step(s, seg, threshold && i === 0)); });
  return cur.length > 0;
}

function walk(nodes: Node[], parent: string[] = []): Array<{ path: string[]; node: Node }> {
  return nodes.flatMap(n => {
    const path = [...parent, n.key];
    return [{ path, node: n }, ...walk(n.children || [], path)];
  });
}
const ALL = walk(SCHEMA as Node[]);
const LEAVES = ALL.filter(e => !e.node.children?.length && e.node.example !== undefined);
const id = (p: string[]) => p.join('.');

describe('every explorer key path exists in tenantConfig', () => {
  for (const { path } of ALL) {
    it(id(path), () => { expect(resolves(path)).toBe(true); });
  }

  it('resolver positive controls: flat receiver_type, platform-file keys, _metadata.team are rejected', () => {
    for (const p of [['_routing', 'receiver_type'], ['_routing', 'webhook_url'], ['_routing_enforced', 'noc_webhook_url'],
      ['_metadata', 'team'], ['_routing_defaults', 'receiver_type'], ['_defaults'], ['routing_profiles'],
      ['_domain_policy'], ['_instance_mapping']]) {
      expect(resolves(p)).toBe(false);
    }
    expect(resolves([TENANT_PLACEHOLDER, 'mysql_connections'])).toBe(true);
  });
});

// Leaves whose schema requires a sibling the insert cannot know; each must be
// rejected with a `required` error naming that sibling (so the list cannot go
// stale silently).
const NOW = new Date('2026-09-25T10:00:00Z');
// Siblings the insert adds under these roots (receiver / relative expires).
const SIBLING_OF: Record<string, string[]> = { _routing: ['receiver'], _state_maintenance: ['expires'] };

const NEEDS_SIBLING: Record<string, string> = {
  '_silent_mode.reason': 'target',
  '_state_maintenance.recurring.[].cron': 'duration',
  '_state_maintenance.recurring.[].duration': 'cron',
};

describe('insert produces tenants.<id>.<parent>.<child> accepted by the schema', () => {
  it('has insertable leaves, nested ones among them', () => {
    expect(LEAVES.length).toBeGreaterThan(10);
    expect(LEAVES.some(l => insertDepth(l.path) >= 2)).toBe(true);
  });

  for (const { path, node } of LEAVES) {
    it(id(path), () => {
      const doc: any = load(buildInsertYaml(path, node.example!, NOW));
      expect(Object.keys(doc)).toEqual(['tenants']);
      expect(Object.keys(doc.tenants)).toEqual(['db-a']);
      const keys = path.filter(k => k !== TENANT_PLACEHOLDER);
      let cur = doc.tenants['db-a'];
      keys.forEach((k, i) => {
        if (k.startsWith('[].')) {
          expect(Array.isArray(cur)).toBe(true);
          cur = cur[0][k.slice(3)];
        } else {
          // Directly under a root key the insert also carries that root's
          // safety sibling (see INSERT_SIBLINGS pinned below).
          const extra = i === 1 ? (SIBLING_OF[keys[0]] || []) : [];
          expect(Object.keys(cur).sort()).toEqual([...extra, k].sort());
          cur = cur[k];
        }
      });
      expect(cur).toEqual(load(node.example!));

      const errors = validateTenantDoc(doc);
      const sibling = NEEDS_SIBLING[id(path)];
      if (sibling) {
        expect(errors.some(e => e.keyword === 'required' && e.params.missingProperty === sibling)).toBe(true);
      } else {
        expect(errors).toEqual([]);
      }
    });
  }

  it('no timestamp is offered as an insert (a fixed date is past or decades away)', () => {
    for (const { node } of LEAVES) expect(node.example).not.toMatch(/\d{4}-\d{2}-\d{2}T/);
  });
});

describe('inserts under _routing / _state_maintenance are safe on their own', () => {
  const under = (root: string) => LEAVES.filter(l => l.path[0] === root && l.path.length > 1);

  it('every _routing leaf insert carries a webhook receiver (the exporter skips a _routing without one)', () => {
    expect(under('_routing').length).toBeGreaterThanOrEqual(4);
    for (const { path, node } of under('_routing')) {
      const r = (load(buildInsertYaml(path, node.example!, NOW)) as any).tenants['db-a']._routing;
      expect(r.receiver).toEqual({ type: 'webhook', url: 'https://webhook.example.com/alerts' });
    }
  });

  it('every _state_maintenance leaf insert carries an expires after the injected now', () => {
    expect(under('_state_maintenance').length).toBeGreaterThanOrEqual(3);
    for (const now of [NOW, new Date('2031-01-01T00:00:00Z')]) {
      for (const { path, node } of under('_state_maintenance')) {
        const m = (load(buildInsertYaml(path, node.example!, now)) as any).tenants['db-a']._state_maintenance;
        expect(Date.parse(m.expires)).toBeGreaterThan(now.getTime());
      }
    }
  });
});

function insertDepth(path: string[]) {
  return path.filter(k => k !== TENANT_PLACEHOLDER).length;
}

describe('the Insert button sends the nested document to the playground', () => {
  it('_routing.group_wait opens the playground with tenants.db-a._routing.group_wait', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(<SchemaExplorer />);
    const code = screen.getAllByText('group_wait', { selector: 'code' })[0];
    const row = code.closest('div.flex-1') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: /Insert to Playground/ }));
    expect(open).toHaveBeenCalledTimes(1);
    const url = String(open.mock.calls[0][0]);
    const yaml = decodeURIComponent(escape(atob(url.split('#yaml=')[1])));
    expect(load(yaml)).toEqual({ tenants: { 'db-a': { _routing: {
      receiver: { type: 'webhook', url: 'https://webhook.example.com/alerts' },
      group_wait: '30s',
    } } } });
    open.mockRestore();
  });
});
