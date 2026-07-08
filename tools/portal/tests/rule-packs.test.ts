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
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

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
