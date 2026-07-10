---
title: "Platform Health — Demo Fixture"
purpose: |
  Simulated platform snapshot for the Platform Health dashboard. In
  production the dashboard would fetch this from the Prometheus API;
  here it is a static, self-contained demo payload (docs-site / GitHub
  Pages fallback — no backend required).

  Extracted from platform-health.jsx in the da-portal ROI refactor
  (Wave 5b). Pure data — no React, no side effects. Behavior contract:
  byte-identical to the inline `PLATFORM_DATA` const that lived in the
  orchestrator before decomposition.

  NAMING: exported as `PLATFORM_HEALTH_DATA` (not `PLATFORM_DATA`) so it
  registers as `window.__PLATFORM_HEALTH_DATA` and does NOT collide with
  the platform-injected `window.__PLATFORM_DATA` global (real rule-pack
  data consumed by threshold-heatmap / rule-pack tools). This demo is
  unrelated to that contract.

  Tenant names (prod-mariadb, prod-redis, …) are demo fixture ids per
  dev-rule #2's fixture-convention allowance — not hardcoded production
  tenants.
---

const PLATFORM_HEALTH_DATA = {
  exporter: {
    status: 'healthy',
    replicas: { ready: 2, total: 2 },
    uptime: '14d 6h 32m',
    lastReload: '2026-03-17T08:15:23Z',
    reloadCount: 47,
    configHash: 'a3f2c1d8e9b7',
    metricsPerTenant: { 'prod-mariadb': 8, 'prod-redis': 5, 'prod-kafka': 12, 'staging-pg': 4, 'prod-oracle': 6 },
    totalMetrics: 35,
    version: 'v2.7.0',
  },
  prometheus: {
    status: 'healthy',
    scrapeInterval: '15s',
    rulesLoaded: 238,
    rulePacksActive: 15,
    recordingRules: 139,
    alertRules: 99,
    scrapeErrors: 0,
    tsdbSizeMB: 1247,
  },
  alertmanager: {
    status: 'healthy',
    configReloaded: '2026-03-17T08:15:25Z',
    routesActive: 5,
    receiversActive: 5,
    inhibitRules: 5,
    silences: 1,
    notificationsSent24h: 12,
    notificationsFailed24h: 0,
  },
  tenants: [
    { name: 'prod-mariadb', state: 'normal', packs: ['mariadb', 'kubernetes'], metrics: 8, alertsFiring: 0, lastUpdate: '2026-03-17T07:00:00Z' },
    { name: 'prod-redis', state: 'normal', packs: ['redis', 'kubernetes'], metrics: 5, alertsFiring: 1, lastUpdate: '2026-03-17T07:30:00Z' },
    { name: 'prod-kafka', state: 'normal', packs: ['kafka', 'jvm'], metrics: 12, alertsFiring: 0, lastUpdate: '2026-03-17T06:45:00Z' },
    { name: 'staging-pg', state: 'maintenance', packs: ['postgresql', 'kubernetes'], metrics: 4, alertsFiring: 0, lastUpdate: '2026-03-17T05:00:00Z', expires: '2026-03-20T06:00:00Z' },
    { name: 'prod-oracle', state: 'normal', packs: ['oracle', 'db2', 'kubernetes'], metrics: 6, alertsFiring: 0, lastUpdate: '2026-03-17T08:00:00Z' },
  ],
};

// Vestigial window-global registration (the retired jsx-loader read path).
// No live code reads it — the components import it via ESM. Pruned in
// TRK-230z along with the ESM export's compat marker below. The name is
// deliberately NOT `__PLATFORM_DATA`: that global is the platform-injected
// live rule-pack payload eight other tools read.
window.__PLATFORM_HEALTH_DATA = PLATFORM_HEALTH_DATA;

// ESM export for the esbuild dist-bundle path + vitest.
// <!-- jsx-loader-compat: ignore -->
export { PLATFORM_HEALTH_DATA };
