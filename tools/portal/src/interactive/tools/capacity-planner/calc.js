---
title: "Capacity Planner — TSDB / memory / routing sizing"
purpose: |
  Pure sizing model for the capacity planner: given the selected rule packs and
  cluster parameters it estimates active time series, TSDB size, exporter /
  Prometheus memory, Alertmanager route + inhibit counts, and config reload time.

  Pre-PR-portal-15 this lived inline in a useMemo inside capacity-planner.jsx
  (0% coverage). Extracting it to a pure function — `packs` (the already-filtered
  pack objects) passed in rather than read from the platform-data-derived
  RULE_PACKS global — isolates the heuristics (1.5 bytes/sample, 2h head block,
  +256MB Prom base, etc.) and makes them unit-testable. Behavior is unchanged:
  the orchestrator filters RULE_PACKS by selectedPacks and passes the result.

  Public API:
    window.__computeCapacityEstimates({ packs, tenantCount, instancesPerTenant, scrapeInterval, retentionDays })
      packs: array of { recording, alerts, metrics, seriesPerInstance }

  Closure deps: none. Pure function; receives packs + scalars as args.
---

function computeCapacityEstimates({ packs, tenantCount, instancesPerTenant, scrapeInterval, retentionDays }) {
  const totalRecording = packs.reduce((s, p) => s + p.recording, 0);
  const totalAlerts = packs.reduce((s, p) => s + p.alerts, 0);
  const totalMetrics = packs.reduce((s, p) => s + p.metrics, 0);

  // Series estimation
  const seriesPerInstance = packs.reduce((s, p) => s + p.seriesPerInstance, 0);
  const totalInstances = tenantCount * instancesPerTenant;
  const totalSeries = seriesPerInstance * totalInstances;

  // TSDB size: ~1.5 bytes per sample, samples = series * (retention_seconds / scrape_interval)
  const samplesPerDay = totalSeries * (86400 / scrapeInterval);
  const totalSamples = samplesPerDay * retentionDays;
  const tsdbBytes = totalSamples * 1.5;
  const tsdbGB = tsdbBytes / (1024 ** 3);

  // Exporter memory: ~0.5 MB base + 0.1 KB per series
  const exporterMB = 50 + (totalSeries * 0.1 / 1024);

  // Prometheus memory: ~2 bytes per active sample in head block (2h)
  const headSamples = totalSeries * (7200 / scrapeInterval);
  const promMemMB = (headSamples * 2) / (1024 ** 2) + 256; // + 256 MB base

  // Alertmanager routes
  const amRoutes = tenantCount * (1 + (packs.length > 3 ? packs.length : 0)); // base + overrides
  const amInhibits = tenantCount * 2 + totalAlerts; // per-tenant + dedup

  // Reload time estimate (ms)
  const reloadMs = 50 + tenantCount * 5 + totalSeries * 0.01;

  return {
    packs: packs.length,
    totalRecording,
    totalAlerts,
    totalMetrics,
    totalInstances,
    totalSeries,
    tsdbGB: Math.max(tsdbGB, 0.01),
    exporterMB: Math.max(exporterMB, 50),
    promMemMB: Math.max(promMemMB, 256),
    amRoutes,
    amInhibits,
    reloadMs: Math.max(reloadMs, 50),
    samplesPerDay,
  };
}

// Legacy jsx-loader path: expose as window global (see PR-portal-12 / TD-030z).
window.__computeCapacityEstimates = computeCapacityEstimates;

// TRK-230e: ESM exports (esbuild dist path). Removed with jsx-loader in TRK-230z.
// <!-- jsx-loader-compat: ignore -->
export { computeCapacityEstimates };
