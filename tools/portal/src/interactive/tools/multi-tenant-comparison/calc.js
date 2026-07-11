---
title: "Multi-Tenant Comparison — cross-tenant threshold statistics"
purpose: |
  Pure data + functions extracted from multi-tenant-comparison.jsx (portal ROI
  extraction, mirroring threshold-calculator wave 6) so the comparison
  statistics — the analytical core of the tool — can be exercised without
  React. Pre-extraction these were inline at the top of the .jsx with 0% unit
  coverage: the outlier / divergence / common-setting math had no tests.

  Behavior is a VERBATIM, byte-identical move, not a re-derivation: the
  orchestrator now imports these back and keeps only React state + render.
  Any change to the numbers here is a behavior change and must be intentional.

  Public API:
    DEFAULTS                          per-metric default threshold values
    computeStats(tenants, metric)     -> { min, max, mean, median, stddev, count, defaultVal } | null
      population variance (/N), median = sorted[floor(len/2)] (upper-middle on
      even length), mean & stddev rounded to 1 decimal (min/max/median NOT),
      null values filtered before stats, empty -> null.
    detectOutliers(tenants, metric, threshold = 1.5)
      -> [{ tenant, value, zscore }]  z-score fence |val - mean| > threshold*stddev;
         uses the ROUNDED mean/stddev; stddev === 0 -> []; zscore rounded to 2 dp.
    findCommonSettings(tenants)       -> [metric] where every tenant shares the value
    findDivergent(tenants)            -> [{ metric, stats }] filtered stddev>0, sorted stddev DESC

  Closure deps: none. Pure functions; receive tenants / metric as args.
---

const DEFAULTS = {
  mysql_connections: 80, mysql_cpu: 80, container_cpu: 80,
  container_memory: 85, oracle_sessions_active: 200,
  oracle_tablespace_used_pct: 85, db2_connections_active: 200,
};

// ── Analysis Functions ────────────────────────────────────────────

function computeStats(tenants, metric) {
  const values = tenants.map(t => t.thresholds[metric]).filter(v => v != null);
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const sum = values.reduce((s, v) => s + v, 0);
  const mean = sum / values.length;
  const variance = values.reduce((s, v) => s + (v - mean) ** 2, 0) / values.length;
  const stddev = Math.sqrt(variance);
  return {
    min: sorted[0],
    max: sorted[sorted.length - 1],
    mean: Math.round(mean * 10) / 10,
    median: sorted[Math.floor(sorted.length / 2)],
    stddev: Math.round(stddev * 10) / 10,
    count: values.length,
    defaultVal: DEFAULTS[metric] || 0,
  };
}

function detectOutliers(tenants, metric, threshold = 1.5) {
  const stats = computeStats(tenants, metric);
  if (!stats || stats.stddev === 0) return [];
  return tenants.filter(t => {
    const val = t.thresholds[metric];
    return val != null && Math.abs(val - stats.mean) > threshold * stats.stddev;
  }).map(t => ({ tenant: t.name, value: t.thresholds[metric], zscore: Math.round(((t.thresholds[metric] - stats.mean) / stats.stddev) * 100) / 100 }));
}

function findCommonSettings(tenants) {
  const metrics = Object.keys(DEFAULTS);
  return metrics.filter(m => {
    const values = tenants.map(t => t.thresholds[m]);
    return values.every(v => v === values[0]);
  });
}

function findDivergent(tenants) {
  const metrics = Object.keys(DEFAULTS);
  return metrics
    .map(m => ({ metric: m, stats: computeStats(tenants, m) }))
    .filter(item => item.stats && item.stats.stddev > 0)
    .sort((a, b) => b.stats.stddev - a.stats.stddev);
}

export { computeStats, detectOutliers, findCommonSettings, findDivergent, DEFAULTS };
