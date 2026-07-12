/**
 * Drift gate for the offline Rule Pack fallback — rule-pack canonicalize
 * epic, PR-2.
 *
 * `_common/data/rule-packs.js` resolves RULE_PACK_DATA at module-eval time:
 *   1. window.__PLATFORM_DATA.rulePacks — live data pre-fetched by jsx-loader
 *      from docs/assets/platform-data.json (production path)
 *   2. baked-in inline catalog — offline / standalone fallback
 *
 * The baked-in catalog is a HAND-MAINTAINED SUBSET of the generated source of
 * truth docs/assets/platform-data.json (produced by `make platform-data` /
 * generate_platform_data.py). It carries only the fields the in-browser tools
 * need offline — label / category / required / defaults ({value,unit,desc}) /
 * metrics — and intentionally OMITS the derived fields (configMap /
 * recordingRules / alertRules / exporter / display / dependencies /
 * exporterFull / defaultOn).
 *
 * Nothing else guarantees the two stay in sync: someone can regenerate
 * platform-data.json (new pack, renamed metric, re-tuned threshold/desc —
 * e.g. the #944 MariaDB saturation rename) and forget the fallback. The
 * result is a silent offline/online divergence (offline tools show stale
 * packs/thresholds). PR-2 fixed a confirmed instance of exactly this: the
 * `liveness` pack was missing and 9 packs had `defaults` desc/unit/value drift.
 *
 * This gate FAILS if the carried-subset ever drifts again. It reads the
 * fallback OFFLINE (window.__PLATFORM_DATA unset → the baked-in object) and
 * the generated platform-data.json, then asserts pack-id set/order parity and
 * per-pack deep-equality of the carried fields ONLY. It deliberately does NOT
 * assert the derived fields the fallback omits.
 *
 * RULE_PACK_DATA resolves at module-eval time, so each test resets the module
 * registry and dynamic-imports a fresh instance (same discipline as
 * rule-packs.test.ts / rule-packs-canonical.test.ts).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const MOD = '../src/interactive/tools/_common/data/rule-packs.js';

// Generated source of truth. Resolved relative to this test file:
// tools/portal/tests/ -> (../../../) -> repo root -> docs/assets/platform-data.json
// (same path-resolution pattern as recipe-enums.drift.test.ts).
const __dirname = dirname(fileURLToPath(import.meta.url));
const platformData = JSON.parse(
  readFileSync(resolve(__dirname, '../../../docs/assets/platform-data.json'), 'utf8'),
);
const PD_PACKS: Record<string, any> = platformData.rulePacks;
const PD_ORDER: string[] = platformData.packOrder;

/**
 * Project a pack down to the carried subset the offline fallback mirrors.
 * `required` is normalized (missing => false) so packs that omit it in the
 * fallback still match platform-data's explicit `required: false`. `defaults`
 * missing (liveness/operational/platform in platform-data.json) normalizes to
 * {}. Derived fields are intentionally dropped and NOT compared.
 */
function carried(pack: any) {
  return {
    label: pack.label,
    category: pack.category,
    required: pack.required ?? false,
    defaults: pack.defaults ?? {},
    metrics: pack.metrics ?? [],
  };
}

describe('rule-packs offline fallback — drift gate vs platform-data.json', () => {
  beforeEach(() => {
    vi.resetModules();
    delete (window as any).__PLATFORM_DATA;
  });

  afterEach(() => {
    delete (window as any).__PLATFORM_DATA;
  });

  it('carries the same pack-id SET as platform-data.json .rulePacks', async () => {
    const { RULE_PACK_DATA } = await import(MOD);
    const fbIds = Object.keys(RULE_PACK_DATA).sort();
    const pdIds = Object.keys(PD_PACKS).sort();
    // Names the divergent ids on failure (missing / extra packs).
    expect(fbIds).toEqual(pdIds);
  });

  it('carries the packs in platform-data.json packOrder order', async () => {
    const { RULE_PACK_DATA, PACK_ORDER } = await import(MOD);
    // Object key order == packOrder, and the offline PACK_ORDER derives from it.
    expect(Object.keys(RULE_PACK_DATA)).toEqual(PD_ORDER);
    expect(PACK_ORDER).toEqual(PD_ORDER);
  });

  it('carries 16 packs offline (matches platform-data.json totals.packs)', async () => {
    const { RULE_PACK_DATA } = await import(MOD);
    expect(Object.keys(RULE_PACK_DATA)).toHaveLength(16);
    expect(platformData.totals.packs).toBe(16);
  });

  it('each pack deep-equals platform-data.json on carried fields (label/category/required/defaults/metrics)', async () => {
    const { RULE_PACK_DATA } = await import(MOD);
    for (const packId of PD_ORDER) {
      const fbPack = RULE_PACK_DATA[packId];
      // Guard: a missing pack yields a readable message instead of a throw.
      expect(fbPack, `pack "${packId}" is missing from the offline fallback`).toBeDefined();
      expect(
        carried(fbPack),
        `pack "${packId}" carried-subset drifted from platform-data.json (regenerate the fallback in rule-packs.js after \`make platform-data\`)`,
      ).toEqual(carried(PD_PACKS[packId]));
      // `toEqual` on the `defaults` object is key-ORDER-insensitive, but
      // getAllMetricKeys iterates Object.entries(pack.defaults), so key order is
      // user-visible (autocomplete / validation ordering). Assert it explicitly
      // so a regen that reorders a pack's default keys can't drift silently.
      expect(
        Object.keys(fbPack.defaults ?? {}),
        `pack "${packId}" defaults key ORDER drifted from platform-data.json (regenerate the fallback in rule-packs.js after \`make platform-data\`)`,
      ).toEqual(Object.keys(PD_PACKS[packId].defaults ?? {}));
    }
  });
});
