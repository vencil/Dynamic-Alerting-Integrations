---
title: "Cost Estimator — resource & cost calculators"
purpose: |
  Pure functions that turn a sizing config (tenants, packs/tenant, scrape
  interval, retention, HA replicas, deployment mode) into per-component
  resource estimates and a monthly cost projection.

  Pre-PR-portal-12 these were inline at the top of cost-estimator.jsx.
  Splitting them out drops ~110 LOC from the component, isolates the
  arithmetic so it can be unit-tested directly (it was 0%-covered while
  buried in the JSX), and keeps PRICING as the single source of truth for
  both the cost math and the "AWS defaults" footnote in the UI.

  Public API:
    window.__calcExporterResources(tenants, packsPerTenant, replicas)
    window.__calcPrometheusResources(tenants, packsPerTenant, scrapeInterval, retentionDays)
    window.__calcAlertmanagerResources(tenants, replicas)
    window.__calcOperatorResources()
    window.__calcTotalResources(tenants, packsPerTenant, scrapeInterval, retentionDays, replicas, mode)
    window.__PRICING                                 AWS default unit prices

  Closure deps: none. Pure functions; receive config as args.
---

// --- Pricing Configuration (AWS defaults) ---
const PRICING = {
  cpuPerHour: 0.05,      // $/CPU-hour
  memoryPerGBHour: 0.005, // $/GB-hour
};

const SECONDS_PER_DAY = 86400;
const HOURS_PER_MONTH = 730;

function calcExporterResources(tenants, packsPerTenant, replicas) {
  const memoryPerReplica = 50 + (tenants * packsPerTenant * 2);
  const cpuPerReplica = 0.1 + (tenants * 0.01);
  return {
    memoryMB: memoryPerReplica * replicas,
    cpuCores: cpuPerReplica * replicas,
    memoryPerReplica,
    cpuPerReplica,
  };
}

/**
 * Calculate Prometheus TSDB storage and memory.
 * Time series: tenants × packs × 12 metrics avg × 3 severities
 * Bytes per sample: 1.5 bytes
 * Samples per series: (retention_days × 86400) / scrape_interval
 * Storage = series × bytes × samples
 * Memory: ~2× active series × 2KB chunk overhead
 */
function calcPrometheusResources(tenants, packsPerTenant, scrapeInterval, retentionDays) {
  const metricsPerPack = 12;
  const severities = 3;
  const timeSeries = tenants * packsPerTenant * metricsPerPack * severities;

  const bytesPerSample = 1.5;
  const samplesPerSeries = (retentionDays * SECONDS_PER_DAY) / scrapeInterval;
  const storageGB = (timeSeries * bytesPerSample * samplesPerSeries) / (1024 ** 3);

  // Memory: active series (approximately 10% of total) × 2KB
  const activeSeriesMemoryMB = (timeSeries * 0.1 * 2) / 1024;

  return {
    timeSeries,
    storageGB: Math.max(storageGB, 0.1),
    memoryMB: activeSeriesMemoryMB,
  };
}

/**
 * Calculate Alertmanager resources.
 * Memory: 64MB base + 1MB per 100 tenants
 * CPU: 0.05 cores (fixed)
 */
function calcAlertmanagerResources(tenants, replicas) {
  const memoryPerReplica = 64 + (tenants / 100);
  const cpuPerReplica = 0.05;
  return {
    memoryMB: memoryPerReplica * replicas,
    cpuCores: cpuPerReplica * replicas,
    memoryPerReplica,
    cpuPerReplica,
  };
}

/**
 * Calculate Operator overhead (if applicable).
 * Memory: 128MB, CPU: 0.1 cores (shared, not per replica)
 */
function calcOperatorResources() {
  return {
    memoryMB: 128,
    cpuCores: 0.1,
  };
}

/**
 * Aggregate total resources and calculate costs.
 */
function calcTotalResources(tenants, packsPerTenant, scrapeInterval, retentionDays, replicas, mode) {
  const exporter = calcExporterResources(tenants, packsPerTenant, replicas);
  const prometheus = calcPrometheusResources(tenants, packsPerTenant, scrapeInterval, retentionDays);
  const alertmanager = calcAlertmanagerResources(tenants, replicas);
  const operator = mode === 'operator' ? calcOperatorResources() : { memoryMB: 0, cpuCores: 0 };

  const totalMemoryMB = exporter.memoryMB + prometheus.memoryMB + alertmanager.memoryMB + operator.memoryMB;
  const totalCpuCores = exporter.cpuCores + 0.25 + alertmanager.cpuCores + operator.cpuCores;

  // Monthly cost (730 hours/month)
  const cpuCost = totalCpuCores * HOURS_PER_MONTH * PRICING.cpuPerHour;
  const memoryCost = (totalMemoryMB / 1024) * HOURS_PER_MONTH * PRICING.memoryPerGBHour;
  const totalMonthlyCost = cpuCost + memoryCost;

  return {
    components: {
      exporter,
      prometheus,
      alertmanager,
      operator,
    },
    summary: {
      totalMemoryMB,
      totalCpuCores,
      storageGB: prometheus.storageGB,
    },
    costs: {
      cpuCost,
      memoryCost,
      totalMonthlyCost,
    },
  };
}

// Vestigial window-global registration (the retired jsx-loader read path).
// No live code reads these now — cost-estimator.jsx imports via ESM. Pruned
// in TRK-230z along with the ESM exports' compat marker below.
window.__PRICING = PRICING;
window.__calcExporterResources = calcExporterResources;
window.__calcPrometheusResources = calcPrometheusResources;
window.__calcAlertmanagerResources = calcAlertmanagerResources;
window.__calcOperatorResources = calcOperatorResources;
window.__calcTotalResources = calcTotalResources;

// TRK-230e: ESM exports (esbuild dist path). Removed with jsx-loader in TRK-230z.
// <!-- jsx-loader-compat: ignore -->
export {
  PRICING,
  calcExporterResources,
  calcPrometheusResources,
  calcAlertmanagerResources,
  calcOperatorResources,
  calcTotalResources,
};
