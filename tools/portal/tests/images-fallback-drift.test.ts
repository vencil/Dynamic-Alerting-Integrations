/**
 * Drift gate for the offline container-image fallback (#1245).
 *
 * `_common/data/images.js` resolves PLATFORM_IMAGES at module-eval time:
 *   1. window.__PLATFORM_DATA.images — live data pre-fetched by jsx-loader
 *      from docs/assets/platform-data.json (production path)
 *   2. baked-in inline mirror — offline / standalone fallback
 *
 * WHY THIS GATE EXISTS. The Deployment Profile Wizard emits these refs into a
 * values.yaml the customer copies out and applies. They lived as literals in
 * generators.js with no source of truth behind them and rotted to a v2.7.0-era
 * snapshot that survived every release: prom/prometheus v2.52.0 against a
 * shipped v3.13.1 (an entire MAJOR — and 2.x predates the Prometheus 3.0
 * left-open lookback that this repo's rule-pack fixtures are calibrated
 * against, so a customer following the wizard would run alerts whose timing we
 * never validated), alertmanager v0.27.0 vs v0.33.1, jimmidyson/configmap-reload
 * long after #1251 replaced that component wholesale, and
 * `oauth2-proxy/oauth2-proxy:v7.6.0` — a ref whose `quay.io/` registry prefix
 * is missing, so it resolves to a Docker Hub repository that does not exist and
 * the emitted YAML ImagePullBackOffs.
 *
 * Nothing caught any of it: check_image_refs_resolve.py (the deploy-ref SSOT
 * and the thing the nightly CVE scan is keyed on) only globs each chart's
 * values.yaml plus the k8s manifest tree, so a JavaScript generator sat
 * outside every detection surface in the repo. This gate is that surface.
 * (Note for future editors: do NOT write those two globs literally here — the
 * chart one contains a star-slash sequence that terminates this block comment
 * early and esbuild then fails to parse the whole file.)
 *
 * The generator side is separately fail-loud: build_wizard_images() in
 * generate_platform_data.py raises if a mapped third-party repo disappears
 * from the SSOT, so a future replacement-in-place (the configmap-reload case)
 * cannot silently drop a key and leave the wizard emitting a dead ref.
 *
 * PLATFORM_IMAGES resolves at module-eval time, so each test resets the module
 * registry and dynamic-imports a fresh instance (same discipline as
 * rule-packs-fallback-drift.test.ts).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const MOD = '../src/interactive/tools/_common/data/images.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const platformData = JSON.parse(
  readFileSync(resolve(__dirname, '../../../docs/assets/platform-data.json'), 'utf8'),
);
const PD_IMAGES: Record<string, string> = platformData.images;

describe('images offline fallback — drift gate vs platform-data.json', () => {
  beforeEach(() => {
    vi.resetModules();
    delete (window as any).__PLATFORM_DATA;
  });

  afterEach(() => {
    delete (window as any).__PLATFORM_DATA;
  });

  it('platform-data.json actually carries an images block', () => {
    // Guard the guard: without this, dropping `images` from the generator
    // would make every comparison below vacuously pass against undefined.
    expect(PD_IMAGES).toBeTypeOf('object');
    expect(Object.keys(PD_IMAGES).length).toBeGreaterThan(0);
  });

  it('carries the same key SET as platform-data.json .images', async () => {
    const { PLATFORM_IMAGES } = await import(MOD);
    expect(Object.keys(PLATFORM_IMAGES).sort()).toEqual(Object.keys(PD_IMAGES).sort());
  });

  it('carries byte-identical refs for every key', async () => {
    const { PLATFORM_IMAGES } = await import(MOD);
    // Whole-object compare so the failure output names every drifted ref at
    // once rather than stopping at the first.
    expect(PLATFORM_IMAGES).toEqual(PD_IMAGES);
  });

  it('emits a TAG on every ref and never a digest', async () => {
    const { PLATFORM_IMAGES } = await import(MOD);
    for (const [key, ref] of Object.entries(PLATFORM_IMAGES as Record<string, string>)) {
      // A digest here would freeze the customer's starting values.yaml at the
      // instant we generated it AND silently defeat their own `--set
      // image.tag=` bump (digest wins) — the footgun helm/*/values.yaml warns
      // about. Our supply-chain digest pinning (#902 L2) belongs on what WE
      // deploy, not on what we hand out.
      expect(ref, `${key} must not carry a digest`).not.toContain('@sha256:');
      expect(ref, `${key} must carry an explicit tag`).toMatch(/:[^/:]+$/);
    }
  });

  it('prefers window.__PLATFORM_DATA.images over the inline mirror', async () => {
    // Proves the live path is actually wired — otherwise the mirror would be
    // the only thing anyone ever sees and this whole gate would be theatre.
    (window as any).__PLATFORM_DATA = { images: { prometheus: 'live/only:v9.9.9' } };
    const { PLATFORM_IMAGES } = await import(MOD);
    expect(PLATFORM_IMAGES).toEqual({ prometheus: 'live/only:v9.9.9' });
  });

  it('splits repository/tag on the LAST colon (registry ports survive)', async () => {
    (window as any).__PLATFORM_DATA = {
      images: { local: 'registry.example.com:5000/foo/bar:v1.2.3' },
    };
    const { imageRepository, imageTag } = await import(MOD);
    expect(imageRepository('local')).toBe('registry.example.com:5000/foo/bar');
    expect(imageTag('local')).toBe('v1.2.3');
  });
});
