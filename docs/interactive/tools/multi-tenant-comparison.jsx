---
title: "Multi-Tenant Comparison"
tags: [tenant, comparison, threshold, outlier, analysis]
audience: ["platform", "domain-expert"]
version: v2.1.0
lang: en
related: [capacity-planner, roi-calculator, alert-noise-analyzer]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

// ── Sample Data ───────────────────────────────────────────────────
// Simulates multiple tenant YAML configs with threshold overrides.
const SAMPLE_TENANTS = [
  {
    name: "db-a",
    profile: "high-load",
    thresholds: {
      mysql_connections: 70, mysql_cpu: 75, container_cpu: 80,
      container_memory: 85, oracle_sessions_active: 250,
      oracle_tablespace_used_pct: 90, db2_connections_active: 200,
    },
    silentMode: "none", maintenance: false,
  },
  {
    name: "db-b",
    profile: "standard",
    thresholds: {
      mysql_connections: 80, mysql_cpu: 80, container_cpu: 80,
      container_memory: 85, oracle_sessions_active: 200,
      oracle_tablespace_used_pct: 85, db2_connections_active: 200,
    },
    silentMode: "none", maintenance: false,
  },
  {
    name: "db-c",
    profile: "high-load",
    thresholds: {
      mysql_connections: 65, mysql_cpu: 70, container_cpu: 90,
      container_memory: 95, oracle_sessions_active: 300,
      oracle_tablespace_used_pct: 88, db2_connections_active: 180,
    },
    silentMode: "warning", maintenance: false,
  },
  {
    name: "db-d",
    profile: "standard",
    thresholds: {
      mysql_connections: 80, mysql_cpu: 80, container_cpu: 80,
      container_memory: 85, oracle_sessions_active: 200,
      oracle_tablespace_used_pct: 85, db2_connections_active: 200,
    },
    silentMode: "none", maintenance: true,
  },
  {
    name: "db-e",
    profile: "relaxed",
    thresholds: {
      mysql_connections: 95, mysql_cpu: 95, container_cpu: 95,
      container_memory: 98, oracle_sessions_active: 500,
      oracle_tablespace_used_pct: 95, db2_connections_active: 400,
    },
    silentMode: "none", maintenance: false,
  },
];

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

// ── Components ────────────────────────────────────────────────────

function MetricCard({ label, value, sub }) {
  return (
    <div style={{ background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 8, padding: '12px 16px', textAlign: 'center', minWidth: 100 }}>
      <div style={{ fontSize: 12, color: '#64748b', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 700, color: '#1e293b' }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

function BarChart({ data, maxVal, label }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 12, color: '#64748b', marginBottom: 4 }}>{label}</div>
      {data.map((item, i) => {
        const pct = maxVal > 0 ? (item.value / maxVal) * 100 : 0;
        const isOutlier = item.outlier;
        return (
          <div key={i} style={{ display: 'flex', alignItems: 'center', marginBottom: 3 }}>
            <span style={{ width: 50, fontSize: 11, color: '#475569', textAlign: 'right', marginRight: 8 }}>{item.label}</span>
            <div style={{ flex: 1, background: '#f1f5f9', borderRadius: 4, height: 20, position: 'relative' }}>
              <div style={{
                width: `${Math.min(pct, 100)}%`, height: '100%', borderRadius: 4,
                background: isOutlier ? '#ef4444' : '#3b82f6',
                transition: 'width 0.3s',
              }} />
            </div>
            <span style={{ width: 45, fontSize: 11, color: isOutlier ? '#ef4444' : '#475569', textAlign: 'right', marginLeft: 8, fontWeight: isOutlier ? 700 : 400 }}>{item.value}</span>
          </div>
        );
      })}
    </div>
  );
}

function HeatmapRow({ metric, tenants, stats }) {
  const range = stats.max - stats.min || 1;
  return (
    <tr>
      <td style={{ padding: '6px 12px', fontSize: 13, fontWeight: 500, borderBottom: '1px solid #e2e8f0' }}>{metric}</td>
      {tenants.map((tenant, i) => {
        const val = tenant.thresholds[metric];
        const norm = (val - stats.min) / range;
        const hue = 120 - norm * 120; // green(low) → red(high)
        const bg = `hsl(${hue}, 60%, 90%)`;
        const isDefault = val === DEFAULTS[metric];
        return (
          <td key={i} style={{
            padding: '6px 12px', textAlign: 'center', fontSize: 13,
            background: bg, borderBottom: '1px solid #e2e8f0',
            fontWeight: isDefault ? 400 : 700,
            color: isDefault ? '#64748b' : '#1e293b',
          }}>
            {val}{isDefault && <span style={{ fontSize: 10, color: '#94a3b8' }}> (d)</span>}
          </td>
        );
      })}
      <td style={{ padding: '6px 12px', textAlign: 'center', fontSize: 12, color: '#64748b', borderBottom: '1px solid #e2e8f0' }}>
        {stats.mean} ± {stats.stddev}
      </td>
    </tr>
  );
}

// ── Main Component ────────────────────────────────────────────────

function MultiTenantComparison() {
  const [tenants] = useState(SAMPLE_TENANTS);
  const [selectedMetric, setSelectedMetric] = useState('mysql_connections');
  const [outlierThreshold, setOutlierThreshold] = useState(1.5);

  const metrics = useMemo(() => Object.keys(DEFAULTS), []);

  const stats = useMemo(
    () => Object.fromEntries(metrics.map(m => [m, computeStats(tenants, m)])),
    [tenants, metrics]
  );

  const outliers = useMemo(
    () => detectOutliers(tenants, selectedMetric, outlierThreshold),
    [tenants, selectedMetric, outlierThreshold]
  );

  const commonSettings = useMemo(() => findCommonSettings(tenants), [tenants]);
  const divergent = useMemo(() => findDivergent(tenants), [tenants]);

  const outlierTenantNames = new Set(outliers.map(o => o.tenant));

  const barData = tenants.map(tenant => ({
    label: tenant.name,
    value: tenant.thresholds[selectedMetric] || 0,
    outlier: outlierTenantNames.has(tenant.name),
  }));

  const maxBarVal = Math.max(...barData.map(d => d.value), DEFAULTS[selectedMetric] || 0);

  // Summary
  const totalTenants = tenants.length;
  const customCount = tenants.filter(t => {
    return Object.entries(t.thresholds).some(([k, v]) => v !== DEFAULTS[k]);
  }).length;
  const maintenanceCount = tenants.filter(t => t.maintenance).length;
  const silentCount = tenants.filter(t => t.silentMode !== 'none').length;

  return (
    <div style={{ fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif', maxWidth: 960, margin: '0 auto', padding: 24 }}>
      <h2 style={{ fontSize: 22, fontWeight: 700, color: '#1e293b', marginBottom: 4 }}>
        {t('多租戶閾值比較', 'Multi-Tenant Threshold Comparison')}
      </h2>
      <p style={{ fontSize: 14, color: '#64748b', marginBottom: 24 }}>
        {t(
          '橫向比較所有租戶的閾值設定，識別異常值和共同配置。',
          'Compare threshold settings across all tenants, identify outliers and common configurations.'
        )}
      </p>

      {/* Summary Cards */}
      <div style={{ display: 'flex', gap: 16, marginBottom: 24, flexWrap: 'wrap' }}>
        <MetricCard label={t('租戶數', 'Tenants')} value={totalTenants} />
        <MetricCard label={t('自定義', 'Custom')} value={customCount} sub={`/ ${totalTenants}`} />
        <MetricCard label={t('維護中', 'Maintenance')} value={maintenanceCount} />
        <MetricCard label={t('靜默模式', 'Silent Mode')} value={silentCount} />
        <MetricCard label={t('指標數', 'Metrics')} value={metrics.length} />
        <MetricCard label={t('共同設定', 'Common')} value={commonSettings.length} sub={`/ ${metrics.length}`} />
      </div>

      {/* Heatmap Table */}
      <div style={{ border: '1px solid #e2e8f0', borderRadius: 8, overflow: 'auto', marginBottom: 24 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ background: '#f8fafc' }}>
              <th style={{ padding: '8px 12px', textAlign: 'left', fontSize: 12, color: '#64748b', borderBottom: '2px solid #e2e8f0' }}>
                {t('指標', 'Metric')}
              </th>
              {tenants.map((tenant, i) => (
                <th key={i} style={{ padding: '8px 12px', textAlign: 'center', fontSize: 12, color: '#64748b', borderBottom: '2px solid #e2e8f0' }}>
                  {tenant.name}
                  {tenant.maintenance && <span title="Maintenance" style={{ color: '#f59e0b' }}> ⚠</span>}
                  {tenant.silentMode !== 'none' && <span title={`Silent: ${tenant.silentMode}`} style={{ color: '#8b5cf6' }}> 🔇</span>}
                </th>
              ))}
              <th style={{ padding: '8px 12px', textAlign: 'center', fontSize: 12, color: '#64748b', borderBottom: '2px solid #e2e8f0' }}>
                {t('平均 ± 標準差', 'Mean ± StdDev')}
              </th>
            </tr>
          </thead>
          <tbody>
            {metrics.map(m => stats[m] && (
              <HeatmapRow key={m} metric={m} tenants={tenants} stats={stats[m]} />
            ))}
          </tbody>
        </table>
      </div>

      {/* Metric Drill-down */}
      <div style={{ display: 'flex', gap: 24, marginBottom: 24, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 300 }}>
          <h3 style={{ fontSize: 16, fontWeight: 600, color: '#1e293b', marginBottom: 12 }}>
            {t('指標鑽探', 'Metric Drill-down')}
          </h3>
          <div style={{ display: 'flex', gap: 12, marginBottom: 12, alignItems: 'center' }}>
            <select
              value={selectedMetric}
              onChange={e => setSelectedMetric(e.target.value)}
              style={{ padding: '6px 10px', borderRadius: 6, border: '1px solid #cbd5e1', fontSize: 13 }}
            >
              {metrics.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
            <label style={{ fontSize: 12, color: '#64748b' }}>
              {t('離群閾值', 'Outlier σ')}:
              <input
                type="range" min="1" max="3" step="0.1"
                value={outlierThreshold}
                onChange={e => setOutlierThreshold(parseFloat(e.target.value))}
                style={{ width: 80, marginLeft: 6 }}
              />
              <span style={{ marginLeft: 4 }}>{outlierThreshold}σ</span>
            </label>
          </div>
          <BarChart
            data={barData}
            maxVal={maxBarVal * 1.1}
            label={`${selectedMetric} (${t('預設', 'default')}: ${DEFAULTS[selectedMetric]})`}
          />
          {outliers.length > 0 && (
            <div style={{ marginTop: 8, padding: '8px 12px', background: '#fef2f2', borderRadius: 6, border: '1px solid #fecaca' }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: '#dc2626' }}>
                {t('離群值', 'Outliers')}:
              </span>
              {outliers.map((o, i) => (
                <span key={i} style={{ fontSize: 12, color: '#dc2626', marginLeft: 8 }}>
                  {o.tenant} = {o.value} (z={o.zscore})
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Divergence Ranking */}
        <div style={{ flex: 1, minWidth: 300 }}>
          <h3 style={{ fontSize: 16, fontWeight: 600, color: '#1e293b', marginBottom: 12 }}>
            {t('差異排行', 'Divergence Ranking')}
          </h3>
          <div style={{ border: '1px solid #e2e8f0', borderRadius: 8, overflow: 'hidden' }}>
            {divergent.map((item, i) => (
              <div key={i} style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 12px', borderBottom: i < divergent.length - 1 ? '1px solid #f1f5f9' : 'none', background: i === 0 ? '#fef2f2' : 'white' }}>
                <span style={{ fontSize: 13, fontWeight: i === 0 ? 600 : 400 }}>{item.metric}</span>
                <span style={{ fontSize: 12, color: '#64748b' }}>
                  σ={item.stats.stddev} | {item.stats.min}–{item.stats.max}
                </span>
              </div>
            ))}
            {commonSettings.length > 0 && (
              <div style={{ padding: '8px 12px', background: '#f0fdf4', borderTop: '1px solid #e2e8f0' }}>
                <span style={{ fontSize: 12, color: '#16a34a' }}>
                  {t('一致', 'Common')}: {commonSettings.join(', ')}
                </span>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Recommendations */}
      <div style={{ border: '1px solid #e2e8f0', borderRadius: 8, padding: 16, background: '#f8fafc' }}>
        <h3 style={{ fontSize: 16, fontWeight: 600, color: '#1e293b', marginBottom: 8 }}>
          {t('建議', 'Recommendations')}
        </h3>
        <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13, color: '#475569', lineHeight: 1.8 }}>
          {divergent.length > 0 && divergent[0].stats.stddev > 10 && (
            <li>{t(
              `${divergent[0].metric} 差異最大（σ=${divergent[0].stats.stddev}），建議統一或確認是否為預期設定。`,
              `${divergent[0].metric} has the highest divergence (σ=${divergent[0].stats.stddev}). Consider standardizing or confirming intentional variation.`
            )}</li>
          )}
          {maintenanceCount > 0 && (
            <li>{t(
              `${maintenanceCount} 個租戶處於維護模式，確認是否需要恢復正常運營。`,
              `${maintenanceCount} tenant(s) in maintenance mode. Verify if they should return to normal operations.`
            )}</li>
          )}
          {silentCount > 0 && (
            <li>{t(
              `${silentCount} 個租戶啟用靜默模式，可能遺漏告警。`,
              `${silentCount} tenant(s) have silent mode enabled. Alerts may be suppressed.`
            )}</li>
          )}
          {commonSettings.length > metrics.length * 0.5 && (
            <li>{t(
              `${commonSettings.length}/${metrics.length} 個指標在所有租戶中一致，考慮將這些設為 Profile 預設值。`,
              `${commonSettings.length}/${metrics.length} metrics are identical across all tenants. Consider setting these as Profile defaults.`
            )}</li>
          )}
          {outliers.length > 0 && (
            <li>{t(
              `發現 ${outliers.length} 個離群值，請確認是否為有意設定或需要調整。`,
              `Found ${outliers.length} outlier(s). Verify if these are intentional or need adjustment.`
            )}</li>
          )}
        </ul>
      </div>
    </div>
  );
}

export default MultiTenantComparison;
