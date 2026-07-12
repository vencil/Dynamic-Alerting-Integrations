/**
 * Unit tests for the _common Rule Pack catalog accessor — portal ROI
 * refactor Wave 1 (platform-data casing fix).
 *
 * `rule-packs.js` resolves RULE_PACK_DATA at module-eval time:
 *   1. window.__PLATFORM_DATA.rulePacks — live data pre-fetched by
 *      jsx-loader.html from docs/assets/platform-data.json
 *   2. baked-in inline catalog — offline / standalone fallback
 *
 * Regression pinned here: the accessor used to read
 * `window.__platformData` (lowercase p) while jsx-loader.html assigns
 * `window.__PLATFORM_DATA` — the live path could never hit, so the
 * accessor silently served stale baked-in data even when platform-data
 * was available.
 *
 * Because the global is read at module-eval time, each test resets the
 * module registry and dynamic-imports a fresh instance.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));

const LIVE_STUB = {
  rulePacks: {
    livepack: {
      label: 'Live Pack',
      category: 'database',
      defaults: { live_metric: { value: 42, unit: '%', desc: 'live threshold' } },
      metrics: ['live_metric'],
    },
  },
};

describe('rule-packs accessor — platform-data resolution', () => {
  beforeEach(() => {
    vi.resetModules();
    delete (window as any).__PLATFORM_DATA;
  });

  afterEach(() => {
    delete (window as any).__PLATFORM_DATA;
  });

  it('serves live rulePacks when window.__PLATFORM_DATA is present (casing regression)', async () => {
    (window as any).__PLATFORM_DATA = LIVE_STUB;
    const { RULE_PACK_DATA } = await import('../src/interactive/tools/_common/data/rule-packs.js');
    // Same object reference — the live path hit, no silent fallback.
    expect(RULE_PACK_DATA).toBe(LIVE_STUB.rulePacks);
    expect(RULE_PACK_DATA.livepack.label).toBe('Live Pack');
    // Live data replaces (not merges with) the baked-in catalog.
    expect(RULE_PACK_DATA.mariadb).toBeUndefined();
  });

  it('falls back to the baked-in catalog when the global is absent', async () => {
    const { RULE_PACK_DATA } = await import('../src/interactive/tools/_common/data/rule-packs.js');
    expect(RULE_PACK_DATA.mariadb).toBeDefined();
    expect(RULE_PACK_DATA.mariadb.label).toBe('MariaDB/MySQL');
  });

  it('falls back when jsx-loader sets the global to null (fetch-failure path)', async () => {
    // jsx-loader.html assigns `window.__PLATFORM_DATA = null` when
    // platform-data.json is unavailable — optional chaining must not throw.
    (window as any).__PLATFORM_DATA = null;
    const { RULE_PACK_DATA } = await import('../src/interactive/tools/_common/data/rule-packs.js');
    expect(RULE_PACK_DATA.mariadb).toBeDefined();
  });

  it('getAllMetricKeys flattens live defaults (live path end-to-end)', async () => {
    (window as any).__PLATFORM_DATA = LIVE_STUB;
    const { getAllMetricKeys } = await import('../src/interactive/tools/_common/data/rule-packs.js');
    const keys = getAllMetricKeys([]);
    expect(keys).toEqual([
      expect.objectContaining({ key: 'live_metric', pack: 'livepack', label: 'Live Pack', value: 42 }),
    ]);
  });
});

// ---------------------------------------------------------------------------
// Saturation metricClass tagging — mirrors scaffold_tenant.py RULE_PACKS
// `metric_class: saturation` (via platform-data `metricClass`). Display-only:
// consumers show the `_critical` educational hint from this field.
// ---------------------------------------------------------------------------

// Expected saturation keys present in the INLINE FALLBACK catalog. The
// authoritative set has 22 keys (see tests/ops/test_scaffold_tenant.py
// EXPECTED_SATURATION_KEYS); the fallback carries 21 — its kubernetes entry
// predates container_cpu_throttle (existing drift, deliberately not backfilled
// here).
const EXPECTED_FALLBACK_SATURATION_KEYS = [
  'clickhouse_active_connections',
  'container_cpu',
  'container_memory',
  'db2_connections_active',
  'es_jvm_memory_used_percent',
  'jvm_memory',
  'jvm_threads',
  'kafka_consumer_lag',
  'mongodb_connections_current',
  'mysql_connections',
  'mysql_cpu',
  'nginx_connections',
  'nginx_waiting',
  'oracle_sessions_active',
  'pg_connections',
  'rabbitmq_connections',
  'rabbitmq_node_mem_percent',
  'rabbitmq_queue_messages',
  'rabbitmq_unacked_messages',
  'redis_connected_clients',
  'redis_memory_used_bytes',
];

describe('rule-packs fallback — saturation metricClass tagging', () => {
  beforeEach(() => {
    vi.resetModules();
    delete (window as any).__PLATFORM_DATA;
  });

  afterEach(() => {
    delete (window as any).__PLATFORM_DATA;
  });

  it('fallback metricClass === saturation key set matches the expected set', async () => {
    const { RULE_PACK_DATA } = await import('../src/interactive/tools/_common/data/rule-packs.js');
    const tagged = Object.values(RULE_PACK_DATA)
      .flatMap((pack: any) => Object.entries(pack.defaults ?? {})
        .filter(([, meta]: [string, any]) => meta.metricClass === 'saturation')
        .map(([key]) => key))
      .sort();
    expect(tagged).toEqual(EXPECTED_FALLBACK_SATURATION_KEYS);
  });

  it('metricClass values are only ever "saturation"', async () => {
    const { RULE_PACK_DATA } = await import('../src/interactive/tools/_common/data/rule-packs.js');
    for (const pack of Object.values(RULE_PACK_DATA) as any[]) {
      for (const meta of Object.values(pack.defaults ?? {}) as any[]) {
        if ('metricClass' in meta) expect(meta.metricClass).toBe('saturation');
      }
    }
  });

  it('fallback metricClass aligns with committed platform-data.json (fallback pack/keys only)', async () => {
    // Only compare pack/keys the fallback carries — the fallback is a
    // deliberately smaller offline snapshot (e.g. kubernetes lacks
    // container_cpu_throttle), so iterating the JSON side would flag
    // pre-existing drift this test does not own.
    const raw = readFileSync(
      resolve(__dirname, '../../../docs/assets/platform-data.json'), 'utf-8');
    const platform = JSON.parse(raw);
    const { RULE_PACK_DATA } = await import('../src/interactive/tools/_common/data/rule-packs.js');
    for (const [packId, pack] of Object.entries(RULE_PACK_DATA) as [string, any][]) {
      const jsonDefaults = platform.rulePacks?.[packId]?.defaults;
      if (!jsonDefaults) continue;
      for (const [key, meta] of Object.entries(pack.defaults ?? {}) as [string, any][]) {
        if (!(key in jsonDefaults)) continue;
        expect(jsonDefaults[key].metricClass, `${packId}.${key} metricClass drift`)
          .toBe(meta.metricClass);
      }
    }
  });
});
