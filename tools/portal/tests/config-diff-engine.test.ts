/**
 * Unit tests for config-diff/diff.js — portal ROI wave 2.
 *
 * config-diff previously carried a hand-rolled YAML mini-parser
 * (`extractTenants`) that lacked the prototype-pollution guard and size
 * guard the shared `_common/validation/yaml-parser.js` already has. The
 * engine now delegates each tenant's body to that shared parseYaml and
 * keeps only a thin tenant-splitting layer. These tests pin:
 *
 *   - multi-tenant split into per-tenant configs (behaviour parity with
 *     the old extractTenants: same tenants, same keys)
 *   - _routing block flattened + diffed BY VALUE (not object identity,
 *     which would report a phantom change on every render)
 *   - prototype-pollution guard at BOTH the tenant-name level (guarded by
 *     the wrapper) and the key level (inherited from parseYaml)
 *   - size guard rejects an oversized document
 */
import { describe, it, expect } from 'vitest';
import { extractTenants, computeDiff } from '../src/interactive/tools/config-diff/diff.js';

const EXAMPLE_OLD = [
  'tenants:',
  '  db-a:',
  '    mysql_connections: "80"',
  '    mysql_cpu: "75"',
  '    _routing:',
  '      receiver_type: "slack"',
  '      webhook_url: "https://hooks.slack.com/services/xxx/old"',
  '      group_wait: "30s"',
  '  db-b:',
  '    pg_connections: "100"',
  '    pg_cache_hit_ratio: "90"',
].join('\n');

const EXAMPLE_NEW = [
  'tenants:',
  '  db-a:',
  '    mysql_connections: "120"',
  '    mysql_connections_critical: "200"',
  '    mysql_cpu: "75"',
  '    _routing:',
  '      receiver_type: "webhook"',
  '      webhook_url: "https://hooks.example.com/alerts"',
  '      group_wait: "30s"',
  '  db-b:',
  '    pg_connections: "150"',
  '    pg_cache_hit_ratio: "85"',
  '  cache:',
  '    redis_memory: "80"',
  '    redis_memory_critical: "95"',
].join('\n');

describe('extractTenants — multi-tenant split', () => {
  it('splits a tenants: block into per-tenant configs with the right keys', () => {
    const { tenants, errors } = extractTenants(EXAMPLE_NEW);
    expect(errors).toEqual([]);
    expect(Object.keys(tenants).sort()).toEqual(['cache', 'db-a', 'db-b']);
    // Scalar values are quote-stripped by the shared parser.
    expect(tenants['db-a'].mysql_connections).toBe('120');
    expect(tenants['db-a'].mysql_connections_critical).toBe('200');
    expect(tenants['db-b'].pg_connections).toBe('150');
    expect(tenants['cache'].redis_memory).toBe('80');
  });

  it('flattens the _routing block to a comparable multi-line string', () => {
    const { tenants } = extractTenants(EXAMPLE_NEW);
    const routing = tenants['db-a']._routing;
    expect(typeof routing).toBe('string');
    expect(routing).toContain('receiver_type: webhook');
    expect(routing).toContain('webhook_url: https://hooks.example.com/alerts');
  });

  it('returns empty tenants + no error for empty input (no crash)', () => {
    expect(extractTenants('')).toEqual({ tenants: {}, errors: [] });
  });
});

describe('computeDiff — change detection parity', () => {
  it('detects added/removed tenants and added/changed keys', () => {
    const { changes, errors } = computeDiff(EXAMPLE_OLD, EXAMPLE_NEW);
    expect(errors).toEqual([]);
    const types = changes.map((c) => c.type);
    // cache tenant is new; db-a.mysql_connections_critical is a new key;
    // db-a.mysql_connections + db-a._routing + db-b.* changed.
    expect(types).toContain('tenant-added');
    expect(types).toContain('key-added');
    expect(types).toContain('key-changed');

    const cacheAdded = changes.find((c) => c.type === 'tenant-added' && c.tenant === 'cache');
    expect(cacheAdded).toBeTruthy();

    const connChanged = changes.find(
      (c) => c.type === 'key-changed' && c.tenant === 'db-a' && c.key === 'mysql_connections',
    );
    expect(connChanged).toMatchObject({ oldVal: '80', newVal: '120' });
  });

  it('reports _routing as changed when a sub-value changes', () => {
    const { changes } = computeDiff(EXAMPLE_OLD, EXAMPLE_NEW);
    const routingChanged = changes.find(
      (c) => c.type === 'key-changed' && c.tenant === 'db-a' && c.key === '_routing',
    );
    expect(routingChanged).toBeTruthy();
  });

  it('does NOT report a phantom _routing change when both sides are identical', () => {
    // Regression guard: parseYaml returns _routing as an OBJECT; comparing
    // objects by reference (===) would always be unequal. The engine
    // flattens to a string so identical routing blocks compare equal.
    const { changes } = computeDiff(EXAMPLE_NEW, EXAMPLE_NEW);
    expect(changes).toEqual([]);
  });
});

describe('security guards inherited/added by the engine', () => {
  it('drops a __proto__ TENANT id without polluting Object.prototype', () => {
    const malicious = [
      'tenants:',
      '  __proto__:',
      '    polluted: "yes"',
      '  db-a:',
      '    mysql_cpu: "75"',
    ].join('\n');
    const { tenants } = extractTenants(malicious);
    // Real guard: a `tenants['__proto__'] = {}` assignment would SWAP the
    // object's prototype (hasOwnProperty would still report false, so that
    // check proves nothing). Assert the prototype is untouched instead.
    expect(Object.getPrototypeOf(tenants)).toBe(Object.prototype);
    expect(Object.keys(tenants)).toEqual(['db-a']);
    // Object.prototype must not have been polluted.
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
  });

  it('drops a __proto__ KEY inside a tenant (inherited from parseYaml)', () => {
    const malicious = [
      'tenants:',
      '  db-a:',
      '    __proto__: "bad"',
      '    mysql_cpu: "75"',
    ].join('\n');
    const { tenants } = extractTenants(malicious);
    expect(Object.prototype.hasOwnProperty.call(tenants['db-a'], '__proto__')).toBe(false);
    expect(tenants['db-a'].mysql_cpu).toBe('75');
    expect(({} as Record<string, unknown>).bad).toBeUndefined();
  });

  it('rejects an oversized document via the size guard', () => {
    // Build a valid multi-tenant doc that exceeds the 100 KB default.
    const body = '  db-x:\n    k: "1"\n';
    const huge = 'tenants:\n' + body.repeat(10000); // ~180 KB
    const { tenants, errors } = extractTenants(huge);
    expect(errors.length).toBeGreaterThan(0);
    expect(errors[0]).toMatch(/size limit|大小限制/);
    // Guard returns before any parsing — tenants stays empty.
    expect(tenants).toEqual({});
  });
});

// Modeled on components/threshold-exporter/config/conf.d/db-b.yaml — the real
// list-valued _custom_alerts (ADR-024) that parseYaml flattens to {}. db-b is
// the established portal-demo tenant id (not a hardcoded-tenant violation).
const CUSTOM_BASE = [
  'tenants:',
  '  db-b:',
  '    mysql_connections: "100"',
  '    _custom_alerts:',
  '      - recipe: threshold',
  '        name: mariadb_conns_high',
  '        op: ">"',
  '        threshold: "150:warning"',
  '        mode: page',
].join('\n');
const CUSTOM_CHANGED = CUSTOM_BASE.replace('150:warning', '200:critical');

const ROUTING_BASE = [
  'tenants:',
  '  db-b:',
  '    _routing:',
  '      receiver:',
  '        type: "webhook"',
  '        url: "https://webhook.db-b.example.com/alerts"',
  '      group_wait: "30s"',
].join('\n');
const ROUTING_CHANGED = ROUTING_BASE.replace('/alerts"', '/alerts-v2"');

describe('nested / list keys parseYaml does not model (HIGH-1 regression)', () => {
  it('surfaces a _custom_alerts list body as a non-empty comparable string', () => {
    // Pre-fix this was '' (parseYaml returns {} for a non-_routing nested key,
    // flattenValue({}) === ''), which is exactly what hid the change below.
    const { tenants } = extractTenants(CUSTOM_BASE);
    expect(tenants['db-b']._custom_alerts).not.toBe('');
    expect(tenants['db-b']._custom_alerts).toContain('150:warning');
  });

  it('detects a change INSIDE a _custom_alerts list block (fails pre-fix)', () => {
    const { changes } = computeDiff(CUSTOM_BASE, CUSTOM_CHANGED);
    const f = changes.find(
      (c) => c.type === 'key-changed' && c.tenant === 'db-b' && c.key === '_custom_alerts',
    );
    expect(f).toBeTruthy();
  });

  it('does not report a phantom _custom_alerts change when identical', () => {
    // The raw-text fallback must be deterministic, or it would re-introduce
    // the phantom-change class of bug on the other side.
    expect(computeDiff(CUSTOM_BASE, CUSTOM_BASE).changes).toEqual([]);
  });

  it('still detects a deep _routing.receiver.url change (fallback must not break _routing)', () => {
    const { changes } = computeDiff(ROUTING_BASE, ROUTING_CHANGED);
    const f = changes.find(
      (c) => c.type === 'key-changed' && c.tenant === 'db-b' && c.key === '_routing',
    );
    expect(f).toBeTruthy();
  });
});
