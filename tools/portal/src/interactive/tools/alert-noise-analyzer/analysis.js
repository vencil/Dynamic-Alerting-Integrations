---
title: "Alert Noise Analyzer — analysis engine"
purpose: |
  Pure alert-history analysis: analyzeAlerts derives counts by
  severity/tenant/name, MTTR over resolved alerts, flapping detection
  (>=3 fires within a sliding 1h window), the top-5 noisiest alerts, and a
  dedup-opportunity rate. Plus the small time helpers it builds on.

  Pre-PR-portal-22 these were inline in alert-noise-analyzer.jsx (367 LOC)
  with 0% coverage. The analysis carries no i18n, so this module has no
  window.__t dependency. parseTime falls back to Date.now() only when a
  timestamp is absent (callers pass startsAt/endsAt).

  Public API:
    window.__analyzeAlerts(alerts)        -> { totalAlerts, bySeverity, byTenant, topNoisy, mttr, flapping, dedupRate, resolvedCount } | null
    window.__formatDuration(minutes)      -> e.g. "45s" / "12m" / "1.5h"
    window.__parseTime(ts)  window.__durationMinutes(startMs, endMs)

  Closure deps: none (analysis is i18n-free).
---

// ── Utility ───────────────────────────────────────────────────────

function parseTime(ts) {
  return ts ? new Date(ts).getTime() : Date.now();
}

function durationMinutes(startMs, endMs) {
  return Math.max(0, (endMs - startMs) / 60000);
}

function formatDuration(minutes) {
  if (minutes < 1) return `${Math.round(minutes * 60)}s`;
  if (minutes < 60) return `${Math.round(minutes)}m`;
  return `${(minutes / 60).toFixed(1)}h`;
}

// ── Analysis Functions ─────────────────────────────────────────────

function analyzeAlerts(alerts) {
  if (!alerts.length) return null;

  // Basic counts
  const totalAlerts = alerts.length;
  const bySeverity = {};
  const byTenant = {};
  const byName = {};

  alerts.forEach(a => {
    bySeverity[a.severity] = (bySeverity[a.severity] || 0) + 1;
    byTenant[a.tenant] = (byTenant[a.tenant] || 0) + 1;
    byName[a.alertname] = (byName[a.alertname] || 0) + 1;
  });

  // MTTR: Mean Time To Resolve
  const resolved = alerts.filter(a => a.endsAt && a.status === 'resolved');
  const durations = resolved.map(a => durationMinutes(parseTime(a.startsAt), parseTime(a.endsAt)));
  const mttr = durations.length ? durations.reduce((s, d) => s + d, 0) / durations.length : 0;

  // Flapping detection: alerts that fire ≥3 times in 1h
  const flapping = [];
  const nameGroups = {};
  alerts.forEach(a => {
    const key = `${a.tenant}/${a.alertname}`;
    if (!nameGroups[key]) nameGroups[key] = [];
    nameGroups[key].push(parseTime(a.startsAt));
  });
  Object.entries(nameGroups).forEach(([key, times]) => {
    times.sort((a, b) => a - b);
    // Sliding 1-hour window
    for (let i = 0; i < times.length; i++) {
      const windowEnd = times[i] + 3600000;
      const count = times.filter(t => t >= times[i] && t <= windowEnd).length;
      if (count >= 3) { flapping.push({ key, count }); break; }
    }
  });

  // Top noisy alerts
  const topNoisy = Object.entries(byName)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5)
    .map(([name, count]) => ({ name, count, pct: (count / totalAlerts * 100).toFixed(1) }));

  // Dedup effectiveness: count alerts that overlap in time with same name+tenant
  let dedupCandidates = 0;
  Object.values(nameGroups).forEach(times => {
    if (times.length > 1) dedupCandidates += times.length - 1;
  });
  const dedupRate = totalAlerts > 0 ? ((dedupCandidates / totalAlerts) * 100).toFixed(1) : 0;

  return {
    totalAlerts,
    bySeverity,
    byTenant,
    topNoisy,
    mttr,
    flapping,
    dedupRate,
    resolvedCount: resolved.length,
  };
}

// Legacy jsx-loader path: expose as window globals (see PR-portal-12 / TD-030z).
window.__parseTime = parseTime;
window.__durationMinutes = durationMinutes;
window.__formatDuration = formatDuration;
window.__analyzeAlerts = analyzeAlerts;

// TRK-230e: ESM exports (esbuild dist path). Removed with jsx-loader in TRK-230z.
// <!-- jsx-loader-compat: ignore -->
export { parseTime, durationMinutes, formatDuration, analyzeAlerts };
