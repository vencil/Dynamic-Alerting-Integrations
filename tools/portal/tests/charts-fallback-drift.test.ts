/**
 * Drift gate for the offline chart-version fallback (#2947).
 *
 * `_common/data/charts.js` resolves PLATFORM_CHARTS at module-eval time:
 *   1. window.__PLATFORM_DATA.charts — the published chart versions carried
 *      by platform-data.json (rewritten from Chart.yaml at release wrap-up)
 *   2. baked-in inline mirror — offline / standalone fallback
 *
 * The Deployment Profile Wizard passes these to `helm upgrade --install
 * --version`. A mirror that lags platform-data.json pins the customer to a
 * chart other than the one the emitted values file was written against, and
 * nothing else compares the two.
 *
 * Same discipline as images-fallback-drift.test.ts: reset the module registry
 * and dynamic-import a fresh instance per test.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const MOD = '../src/interactive/tools/_common/data/charts.js';
const WIZARD = '../src/interactive/tools/deployment-wizard/utils/generators.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const platformData = JSON.parse(
  readFileSync(resolve(__dirname, '../../../docs/assets/platform-data.json'), 'utf8'),
);
const PD_CHARTS: Record<string, string> = platformData.charts;

const TIER2 = { tier: 'tier2', environment: 'production', tenantSize: 'medium', auth: 'github', packs: ['mariadb'] };

describe('charts offline fallback — drift gate vs platform-data.json', () => {
  beforeEach(() => {
    vi.resetModules();
    delete (window as any).__PLATFORM_DATA;
  });

  afterEach(() => {
    delete (window as any).__PLATFORM_DATA;
  });

  it('platform-data.json actually carries a charts block', () => {
    // Guard the guard: dropping `charts` from the generator would otherwise
    // make every comparison below pass against undefined.
    expect(PD_CHARTS).toBeTypeOf('object');
    expect(Object.keys(PD_CHARTS).length).toBeGreaterThan(0);
  });

  it('carries the same charts at the same versions as platform-data.json', async () => {
    const { PLATFORM_CHARTS } = await import(MOD);
    expect(PLATFORM_CHARTS).toEqual(PD_CHARTS);
  });

  it('every chart the wizard emits a release for has an entry, and no entry is unused', async () => {
    const { PLATFORM_CHARTS } = await import(MOD);
    const { deployGenerateHelmReleases } = await import(WIZARD);
    const emitted = deployGenerateHelmReleases(TIER2).releases.map((r: { chart: string }) => r.chart);
    expect(Object.keys(PLATFORM_CHARTS).sort()).toEqual([...emitted].sort());
  });

  it('the wizard pins each install to this module\'s version (offline path)', async () => {
    const { PLATFORM_CHARTS } = await import(MOD);
    const { deployGenerateHelmReleases } = await import(WIZARD);
    for (const rel of deployGenerateHelmReleases(TIER2).releases) {
      expect(rel.install, rel.chart).toContain(
        `oci://ghcr.io/vencil/charts/${rel.chart} --version ${PLATFORM_CHARTS[rel.chart]} `,
      );
    }
  });

  it('lets live data override PER KEY without dropping the others', async () => {
    // da-portal ships platform-data.json inside its image; a portal whose data
    // lacks a chart the wizard emits must still pin that chart.
    (window as any).__PLATFORM_DATA = { charts: { 'tenant-api': '9.9.9' } };
    const { PLATFORM_CHARTS, chartVersion } = await import(MOD);
    const { deployGenerateHelmReleases } = await import(WIZARD);
    expect(chartVersion('tenant-api')).toBe('9.9.9');
    expect(Object.keys(PLATFORM_CHARTS).sort()).toEqual(Object.keys(PD_CHARTS).sort());
    const installs = deployGenerateHelmReleases(TIER2).releases.map((r: { install: string }) => r.install).join('\n');
    expect(installs).toContain('charts/tenant-api --version 9.9.9 ');
    expect(installs).toContain(`charts/threshold-exporter --version ${PD_CHARTS['threshold-exporter']} `);
  });

  it('refuses an unknown chart instead of emitting an unpinned or blank --version', async () => {
    const { chartVersion } = await import(MOD);
    expect(() => chartVersion('doesNotExist')).toThrow(/no exact version/);
    // An inherited property is not a chart.
    expect(() => chartVersion('constructor')).toThrow(/no exact version/);
  });

  it.each(['', '^2.9.0', '~2.9', '>=2.0.0', '2.x', 'latest', 'v2.9.0', '2.9', '2.9.0 --devel',
    '2.9.0-rc.1 --devel', '2.9.0-', '..', '2..0', '.9.0'])(
    'refuses live version %j: helm would resolve it at run time or misparse it',
    async (bad) => {
      (window as any).__PLATFORM_DATA = { charts: { 'tenant-api': bad } };
      const { chartVersion } = await import(MOD);
      const { deployGenerateHelmReleases } = await import(WIZARD);
      expect(() => chartVersion('tenant-api')).toThrow(/no exact version/);
      expect(() => deployGenerateHelmReleases(TIER2)).toThrow(/no exact version/);
    },
  );

  it('accepts a prerelease as one exact release', async () => {
    (window as any).__PLATFORM_DATA = { charts: { 'tenant-api': '2.10.0-rc.1' } };
    const { chartVersion } = await import(MOD);
    expect(chartVersion('tenant-api')).toBe('2.10.0-rc.1');
  });

  it('refuses a live version that is not a string', async () => {
    (window as any).__PLATFORM_DATA = { charts: { 'tenant-api': 2.9 } };
    const { chartVersion } = await import(MOD);
    expect(() => chartVersion('tenant-api')).toThrow(/no exact version/);
  });
});
