/**
 * Unit tests for the canonical Rule Pack accessor's PACK_ORDER export —
 * rule-pack canonicalize epic, PR-1 (threshold-heatmap convergence).
 *
 * threshold-heatmap.jsx used to roll its own accessor:
 *   const __PD        = window.__PLATFORM_DATA || {};
 *   const RULE_PACKS  = __PD.rulePacks || {};   // {} offline → empty heatmap
 *   const PACK_ORDER  = __PD.packOrder || [];   // [] offline → no packs
 * Offline (no window.__PLATFORM_DATA) the tool was BROKEN (empty). PR-1
 * converges it onto _common/data/rule-packs.js, whose layered fallback
 * bakes in 16 packs. This pins the new PACK_ORDER export + the offline win.
 *
 * PACK_ORDER resolves at module-eval time (window.__PLATFORM_DATA?.packOrder
 * || Object.keys(RULE_PACK_DATA)), so each test resets the module registry
 * and dynamic-imports a fresh instance — same discipline as rule-packs.test.ts.
 *
 * NOTE: threshold-heatmap's extractMetricsFromPacks is a LOCAL, non-exported
 * function, so it can't be imported here. Per the behavior-preservation intent
 * we instead cover the canonical side it now derives from: PACK_ORDER is a
 * non-empty pack-id list offline, and the packs carry non-empty defaults, so
 * the metric-key derivation (offline) is non-empty — the key win over the old
 * empty-{} accessor.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

const MOD = '../src/interactive/tools/_common/data/rule-packs.js';

describe('canonical rule-packs — PACK_ORDER export', () => {
  beforeEach(() => {
    vi.resetModules();
    delete (window as any).__PLATFORM_DATA;
  });

  afterEach(() => {
    delete (window as any).__PLATFORM_DATA;
  });

  it('offline: PACK_ORDER equals Object.keys(RULE_PACK_DATA) — the baked-in packs', async () => {
    const { PACK_ORDER, RULE_PACK_DATA } = await import(MOD);
    expect(PACK_ORDER).toEqual(Object.keys(RULE_PACK_DATA));
  });

  it('offline: PACK_ORDER is a non-empty array of pack-id strings (16 fallback packs)', async () => {
    const { PACK_ORDER } = await import(MOD);
    expect(Array.isArray(PACK_ORDER)).toBe(true);
    for (const id of PACK_ORDER) {
      expect(typeof id).toBe('string');
      expect(id.length).toBeGreaterThan(0);
    }
    // Pin the baked-in fallback count (drift tripwire for the offline catalog).
    // Bumped 15→16 in PR-2 (the `liveness` pack was missing from the fallback);
    // rule-packs-fallback-drift.test.ts now enforces full parity vs platform-data.json.
    expect(PACK_ORDER.length).toBe(16);
  });

  it('online: PACK_ORDER passes through window.__PLATFORM_DATA.packOrder (byte-identity)', async () => {
    const STUB = {
      packOrder: ['livepack', 'otherpack'],
      rulePacks: {
        livepack: { label: 'Live Pack', category: 'database', defaults: { m: { value: 1, unit: '%', desc: 'd' } }, metrics: ['m'] },
        otherpack: { label: 'Other Pack', category: 'runtime', defaults: {}, metrics: [] },
      },
    };
    (window as any).__PLATFORM_DATA = STUB;
    const { PACK_ORDER, RULE_PACK_DATA } = await import(MOD);
    // Same reference online — no fallback, no re-derivation.
    expect(PACK_ORDER).toBe(STUB.packOrder);
    expect(RULE_PACK_DATA).toBe(STUB.rulePacks);
  });

  it('behavior-preservation (offline): metric-key derivation is non-empty — the old accessor was empty {}', async () => {
    const { PACK_ORDER, RULE_PACK_DATA } = await import(MOD);
    // Mirror threshold-heatmap's extractMetricsFromPacks([]) shape:
    // iterate PACK_ORDER, collect pack.defaults keys, drop *_critical, dedupe+sort.
    const metrics = new Set<string>();
    for (const packId of PACK_ORDER) {
      const pack = RULE_PACK_DATA[packId];
      if (pack && pack.defaults) {
        Object.keys(pack.defaults).forEach((key) => {
          if (!key.endsWith('_critical')) metrics.add(key);
        });
      }
    }
    const derived = Array.from(metrics).sort();
    // The key win: offline was [] (empty heatmap); now the baked-in packs
    // supply real metric keys.
    expect(derived.length).toBeGreaterThan(0);
  });
});
