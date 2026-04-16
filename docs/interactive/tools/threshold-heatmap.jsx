---
title: "Threshold Heatmap"
tags: [threshold, visualization, heatmap, metrics, distribution]
audience: [platform-engineer, domain-expert, sre]
version: v2.6.0
lang: en
related: [rule-pack-matrix, capacity-planner, multi-tenant-comparison]
---

/**
 * Design Token Migration (ADR-015 / DEC-A)
 * ==========================================
 * This component uses design tokens via arbitrary Tailwind values (--da-color-*)
 * instead of hardcoded Tailwind color palette classes. This ensures consistency
 * with the design system and enables theme-aware styling.
 * Token mapping: neutrals (slate → --da-color-surface/fg/border),
 * severity colors (red/orange/yellow/green → --da-color-error/warning/success/info),
 * accent/interactive (blue → --da-color-accent), card backgrounds (white → --da-color-card-bg).
 */

import React, { useState, useMemo, useCallback } from 'react';

const t = window.__t || ((zh, en) => en);

const __PD = window.__PLATFORM_DATA || {};
const RULE_PACKS = __PD.rulePacks || {};
const PACK_ORDER = __PD.packOrder || [];

// Color scale + non-color symbol encoding (WCAG 1.4.1 — don't rely on color alone).
// Returns { colorClass, symbol, tier } so each cell carries dual encoding:
// Unicode symbol is readable by screen readers and colorblind users (~8% male).
// Ranges match legend in UI (0-33 / 33-66 / 66-85 / >P95).
function getCellSeverity(value, min, max, percentile95) {
  if (value === null || value === undefined) {
    return { colorClass: 'bg-[color:var(--da-color-surface)]', symbol: '', tier: 'none' };
  }

  // Flag outliers first (>P95)
  if (value > percentile95) {
    return {
      colorClass: 'bg-[color:var(--da-color-error)] text-[color:var(--da-color-card-bg)] font-bold',
      symbol: '\u274C', // ❌
      tier: 'outlier',
    };
  }

  const ratio = max === min ? 0 : (value - min) / (max - min);

  if (ratio < 0.33) {
    return { colorClass: 'bg-[color:var(--da-color-success-soft)] text-[color:var(--da-color-success)]', symbol: '\u2713', tier: 'low' }; // ✓
  }
  if (ratio < 0.66) {
    // Note: Yellow (medium) maps to warning-soft since there's no separate caution token
    return { colorClass: 'bg-[color:var(--da-color-warning-soft)] text-[color:var(--da-color-warning)]', symbol: '\u26A0', tier: 'medium' }; // ⚠
  }
  if (ratio < 0.85) {
    return { colorClass: 'bg-[color:var(--da-color-warning-soft)] text-[color:var(--da-color-warning)]', symbol: '\u26A0\u26A0', tier: 'high' }; // ⚠⚠
  }
  return { colorClass: 'bg-[color:var(--da-color-warning-soft)] text-[color:var(--da-color-warning)]', symbol: '\u26A0\u26A0', tier: 'high' };
}

// Back-compat wrapper (preserves existing call-sites).
function getColorClass(value, min, max, percentile95) {
  return getCellSeverity(value, min, max, percentile95).colorClass;
}

// Human-readable tier label for aria-label / screen-reader announcements.
function tierLabel(tier) {
  switch (tier) {
    case 'low': return t('低', 'Low');
    case 'medium': return t('中', 'Medium');
    case 'high': return t('高', 'High');
    case 'outlier': return t('異常值', 'Outlier');
    default: return '';
  }
}

/**
 * Calculate min, max, mean, percentile95 from an array of values.
 */
function calculateStats(values) {
  const nums = values.filter(v => v !== null && v !== undefined && !isNaN(v)).map(Number);
  if (nums.length === 0) return { min: 0, max: 100, mean: 50, p95: 95 };

  const sorted = nums.sort((a, b) => a - b);
  const min = sorted[0];
  const max = sorted[sorted.length - 1];
  const mean = nums.reduce((a, b) => a + b, 0) / nums.length;
  const p95Index = Math.floor(sorted.length * 0.95);
  const p95 = sorted[p95Index];

  return { min, max, mean, p95 };
}

/**
 * Extract all metrics from selected Rule Packs.
 */
function extractMetricsFromPacks(selectedPacks) {
  const metrics = new Set();
  const packIds = selectedPacks.length > 0 ? selectedPacks : PACK_ORDER;

  for (const packId of packIds) {
    const pack = RULE_PACKS[packId];
    if (pack && pack.defaults) {
      Object.keys(pack.defaults).forEach(key => {
        if (!key.endsWith('_critical')) {
          metrics.add(key);
        }
      });
    }
  }

  return Array.from(metrics).sort();
}

// Generate sample tenant data
function generateSampleTenantData() {
  const tenants = ['db-a', 'db-b', 'db-c', 'db-d', 'db-e'];
  const metrics = extractMetricsFromPacks([]);
  const data = {};

  for (const tenant of tenants) {
    data[tenant] = {};
    for (const metric of metrics) {
      const packId = Object.entries(RULE_PACKS).find(([_, pack]) =>
        pack.defaults && pack.defaults[metric]
      )?.[0];

      if (packId) {
        const defaultVal = RULE_PACKS[packId].defaults[metric]?.value;
        // Add some variance per tenant
        const variance = (Math.random() - 0.5) * 0.3;
        data[tenant][metric] = Math.max(
          defaultVal * (1 + variance),
          defaultVal * 0.5
        );
      }
    }
  }

  return { tenants, data };
}

export default function ThresholdHeatmap() {
  const [selectedPacks, setSelectedPacks] = useState([]);
  const [selectedTenants, setSelectedTenants] = useState(['db-a', 'db-b', 'db-c']);
  const [detailCell, setDetailCell] = useState(null);
  const [csvExported, setCsvExported] = useState(false);
  const [lang, setLang] = useState('en');

  // Sample data
  const { tenants: allTenants, data: tenantData } = useMemo(() => generateSampleTenantData(), []);
  const metrics = useMemo(() => extractMetricsFromPacks(selectedPacks), [selectedPacks]);
  const tenants = selectedTenants.length > 0 ? selectedTenants : allTenants;

  // Calculate statistics for color scaling
  const allValues = useMemo(() => {
    const vals = [];
    for (const tenant of tenants) {
      for (const metric of metrics) {
        const val = tenantData[tenant]?.[metric];
        if (val) vals.push(val);
      }
    }
    return vals;
  }, [tenantData, tenants, metrics]);

  const stats = useMemo(() => calculateStats(allValues), [allValues]);

  const togglePack = (packId) => {
    setSelectedPacks(prev =>
      prev.includes(packId)
        ? prev.filter(p => p !== packId)
        : [...prev, packId]
    );
  };

  const toggleTenant = (tenantId) => {
    setSelectedTenants(prev =>
      prev.includes(tenantId)
        ? prev.filter(t => t !== tenantId)
        : [...prev, tenantId]
    );
  };

  const exportCsv = () => {
    const rows = [];
    rows.push(['Tenant', ...metrics].join(','));

    for (const tenant of tenants) {
      const row = [tenant];
      for (const metric of metrics) {
        const val = tenantData[tenant]?.[metric];
        row.push(val !== undefined ? val.toFixed(2) : 'N/A');
      }
      rows.push(row.join(','));
    }

    const csv = rows.join('\n');
    const element = document.createElement('a');
    element.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv);
    element.download = `threshold-heatmap-${new Date().toISOString().split('T')[0]}.csv`;
    document.body.appendChild(element);
    element.click();
    document.body.removeChild(element);

    setCsvExported(true);
    setTimeout(() => setCsvExported(false), 2000);
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-[color:var(--da-color-accent-soft)] to-[color:var(--da-color-info-soft)] p-8">
      <div className="max-w-full mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-4xl font-bold text-[color:var(--da-color-fg)] mb-2">
            {t('閾值熱力圖', 'Threshold Heatmap')}
          </h1>
          <p className="text-[color:var(--da-color-muted)]">
            {t('可視化租戶間的閾值分佈，識別異常值', 'Visualize threshold distribution across tenants, identify outliers')}
          </p>
        </div>

        <div className="grid grid-cols-1 xl:grid-cols-4 gap-6">
          {/* Left Sidebar: Controls */}
          <div className="xl:col-span-1 space-y-4">
            {/* Language */}
            <div className="bg-[color:var(--da-color-card-bg)] rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] p-4">
              <h3 className="text-xs font-semibold text-[color:var(--da-color-fg)] uppercase tracking-wide mb-2">
                {t('語言', 'Language')}
              </h3>
              <div className="flex gap-1">
                <button
                  onClick={() => setLang('en')}
                  className={`flex-1 px-2 py-1.5 text-xs font-medium rounded transition-colors ${
                    lang === 'en'
                      ? 'bg-[color:var(--da-color-accent-soft)] text-[color:var(--da-color-accent)]'
                      : 'bg-[color:var(--da-color-surface)] text-[color:var(--da-color-muted)] hover:bg-[color:var(--da-color-surface-border)]'
                  }`}
                >
                  EN
                </button>
                <button
                  onClick={() => setLang('zh')}
                  className={`flex-1 px-2 py-1.5 text-xs font-medium rounded transition-colors ${
                    lang === 'zh'
                      ? 'bg-[color:var(--da-color-accent-soft)] text-[color:var(--da-color-accent)]'
                      : 'bg-[color:var(--da-color-surface)] text-[color:var(--da-color-muted)] hover:bg-[color:var(--da-color-surface-border)]'
                  }`}
                >
                  中文
                </button>
              </div>
            </div>

            {/* Rule Pack Filter */}
            <div className="bg-[color:var(--da-color-card-bg)] rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] p-4">
              <h3 className="text-xs font-semibold text-[color:var(--da-color-fg)] uppercase tracking-wide mb-2">
                {t('Rule Pack 篩選', 'Rule Pack Filter')}
              </h3>
              <div className="space-y-1.5">
                {PACK_ORDER.map(packId => {
                  const pack = RULE_PACKS[packId];
                  if (!pack) return null;
                  return (
                    <label key={packId} className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={selectedPacks.includes(packId)}
                        onChange={() => togglePack(packId)}
                        className="w-4 h-4"
                      />
                      <span className="text-xs text-[color:var(--da-color-fg)]">{pack.label}</span>
                    </label>
                  );
                })}
              </div>
              {selectedPacks.length === 0 && (
                <div className="mt-2 text-xs text-[color:var(--da-color-muted)]">
                  {t('（全部顯示）', '(showing all)')}
                </div>
              )}
            </div>

            {/* Tenant Filter */}
            <div className="bg-[color:var(--da-color-card-bg)] rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] p-4">
              <h3 className="text-xs font-semibold text-[color:var(--da-color-fg)] uppercase tracking-wide mb-2">
                {t('租戶篩選', 'Tenant Filter')}
              </h3>
              <div className="space-y-1.5">
                {allTenants.map(tenantId => (
                  <label key={tenantId} className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={selectedTenants.includes(tenantId)}
                      onChange={() => toggleTenant(tenantId)}
                      className="w-4 h-4"
                    />
                    <span className="text-xs text-[color:var(--da-color-fg)] font-mono">{tenantId}</span>
                  </label>
                ))}
              </div>
            </div>

            {/* Statistics */}
            <div className="bg-[color:var(--da-color-card-bg)] rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] p-4" role="region" aria-live="polite" aria-label={t('統計摘要', 'Statistics summary')}>
              <h3 className="text-xs font-semibold text-[color:var(--da-color-fg)] uppercase tracking-wide mb-3">
                {t('統計', 'Statistics')}
              </h3>
              <div className="space-y-2 text-xs">
                <div className="flex justify-between">
                  <span className="text-[color:var(--da-color-muted)]">{t('最小值', 'Min')}:</span>
                  <span className="font-mono font-semibold text-[color:var(--da-color-fg)]">{stats.min.toFixed(1)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-[color:var(--da-color-muted)]">{t('平均值', 'Mean')}:</span>
                  <span className="font-mono font-semibold text-[color:var(--da-color-fg)]">{stats.mean.toFixed(1)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-[color:var(--da-color-muted)]">{t('最大值', 'Max')}:</span>
                  <span className="font-mono font-semibold text-[color:var(--da-color-fg)]">{stats.max.toFixed(1)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-[color:var(--da-color-muted)]">{t('P95 異常值', 'P95 (outlier)')}:</span>
                  <span className="font-mono font-semibold text-[color:var(--da-color-error)]">{stats.p95.toFixed(1)}</span>
                </div>
              </div>
            </div>

            {/* Export */}
            <button
              onClick={exportCsv}
              className={`w-full px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                csvExported
                  ? 'bg-[color:var(--da-color-success-soft)] text-[color:var(--da-color-success)]'
                  : 'bg-[color:var(--da-color-accent)] text-[color:var(--da-color-card-bg)] hover:bg-[color:var(--da-color-accent-hover)]'
              }`}
            >
              {csvExported ? '✓ ' + t('已下載', 'Downloaded') : t('下載 CSV', 'Download CSV')}
            </button>
          </div>

          {/* Main: Heatmap */}
          <div className="xl:col-span-3 bg-[color:var(--da-color-card-bg)] rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] p-6 overflow-auto" role="region" aria-live="polite" aria-label={t('閾值熱力圖', 'Threshold heatmap grid')}>
            <div className="inline-block min-w-full">
              {/* Heatmap Table */}
              <table className="border-collapse" role="table" aria-label={t('閾值分佈表格', 'Threshold distribution table')}>
                <thead>
                  <tr>
                    <th className="border border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] px-3 py-2 text-xs font-semibold text-[color:var(--da-color-fg)] sticky left-0 z-10 text-left min-w-20">
                      {t('租戶', 'Tenant')}
                    </th>
                    {metrics.map(metric => (
                      <th
                        key={metric}
                        className="border border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] px-2 py-2 text-xs font-mono text-[color:var(--da-color-fg)] text-center min-w-16"
                        title={metric}
                      >
                        <div className="truncate">{metric}</div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {tenants.map(tenant => (
                    <tr key={tenant}>
                      <td className="border border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] px-3 py-2 text-xs font-mono font-semibold text-[color:var(--da-color-fg)] sticky left-0 z-10">
                        {tenant}
                      </td>
                      {metrics.map(metric => {
                        const value = tenantData[tenant]?.[metric];
                        const { colorClass, symbol, tier } = getCellSeverity(
                          value, stats.min, stats.max, stats.p95
                        );
                        const valueText = value ? value.toFixed(0) : '—';
                        const tierText = tierLabel(tier);
                        const ariaLabel = value
                          ? `${tenant} ${metric}: ${value.toFixed(2)}${tierText ? ', ' + tierText : ''}`
                          : `${tenant} ${metric}: ${t('無資料', 'no data')}`;

                        return (
                          <td
                            key={`${tenant}-${metric}`}
                            className={`border border-[color:var(--da-color-surface-border)] px-2 py-2 text-xs font-mono text-center cursor-pointer transition-opacity hover:opacity-75 ${colorClass}`}
                            onClick={() => setDetailCell({ tenant, metric, value })}
                            title={value ? `${metric} = ${value.toFixed(2)} (${tierText})` : 'No data'}
                            aria-label={ariaLabel}
                            role="gridcell"
                          >
                            {symbol && (
                              <span aria-hidden="true" className="mr-1 opacity-90">{symbol}</span>
                            )}
                            {valueText}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Legend: symbol + color dual encoding (WCAG 1.4.1 — not color alone). */}
            <div className="mt-6 flex flex-wrap gap-4 items-center text-xs" role="list" aria-label={t('圖例', 'Legend')}>
              <span className="font-semibold text-[color:var(--da-color-fg)]">{t('圖例', 'Legend')}:</span>
              <div className="flex flex-wrap gap-3">
                <div className="flex items-center gap-1" role="listitem">
                  <div className="w-4 h-4 bg-[color:var(--da-color-success-soft)] border border-[color:var(--da-color-surface-border)] flex items-center justify-center text-[color:var(--da-color-success)] font-bold" aria-hidden="true">✓</div>
                  <span className="text-[color:var(--da-color-muted)]">{t('低 (0–33%)', 'Low (0–33%)')}</span>
                </div>
                <div className="flex items-center gap-1" role="listitem">
                  <div className="w-4 h-4 bg-[color:var(--da-color-warning-soft)] border border-[color:var(--da-color-surface-border)] flex items-center justify-center text-[color:var(--da-color-warning)] font-bold" aria-hidden="true">⚠</div>
                  <span className="text-[color:var(--da-color-muted)]">{t('中 (33–66%)', 'Medium (33–66%)')}</span>
                </div>
                <div className="flex items-center gap-1" role="listitem">
                  <div className="w-4 h-4 bg-[color:var(--da-color-warning-soft)] border border-[color:var(--da-color-surface-border)] flex items-center justify-center text-[color:var(--da-color-warning)] font-bold text-[10px]" aria-hidden="true">⚠⚠</div>
                  <span className="text-[color:var(--da-color-muted)]">{t('高 (66–85%)', 'High (66–85%)')}</span>
                </div>
                <div className="flex items-center gap-1" role="listitem">
                  <div className="w-4 h-4 bg-[color:var(--da-color-error)] border border-[color:var(--da-color-surface-border)] flex items-center justify-center text-[color:var(--da-color-card-bg)] font-bold" aria-hidden="true">❌</div>
                  <span className="text-[color:var(--da-color-muted)] font-semibold">{t('異常值 (> P95)', 'Outlier (> P95)')}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Detail Panel */}
        {detailCell && (
          <div className="fixed inset-0 bg-black/50 flex items-center justify-center p-4 z-50">
            <div className="bg-[color:var(--da-color-card-bg)] rounded-xl shadow-2xl max-w-md w-full p-6 relative" role="dialog" aria-live="polite" aria-atomic="true" aria-label={t('閾值詳情', 'Threshold details')}>
              <button
                onClick={() => setDetailCell(null)}
                className="absolute top-4 right-4 text-[color:var(--da-color-muted)] hover:text-[color:var(--da-color-fg)]"
              >
                ✕
              </button>

              <h3 className="text-lg font-bold text-[color:var(--da-color-fg)] mb-4">
                {t('閾值詳情', 'Threshold Details')}
              </h3>

              <div className="space-y-3">
                <div>
                  <span className="text-xs font-semibold text-[color:var(--da-color-muted)] uppercase tracking-wide">
                    {t('租戶', 'Tenant')}
                  </span>
                  <div className="text-lg font-mono font-bold text-[color:var(--da-color-fg)]">{detailCell.tenant}</div>
                </div>

                <div>
                  <span className="text-xs font-semibold text-[color:var(--da-color-muted)] uppercase tracking-wide">
                    {t('指標', 'Metric')}
                  </span>
                  <div className="text-lg font-mono font-bold text-[color:var(--da-color-fg)]">{detailCell.metric}</div>
                </div>

                <div>
                  <span className="text-xs font-semibold text-[color:var(--da-color-muted)] uppercase tracking-wide">
                    {t('當前閾值', 'Current Threshold')}
                  </span>
                  <div className="text-2xl font-mono font-bold text-[color:var(--da-color-accent)]">
                    {detailCell.value ? detailCell.value.toFixed(2) : 'N/A'}
                  </div>
                </div>

                <div className="bg-[color:var(--da-color-surface)] rounded-lg p-3 border border-[color:var(--da-color-surface-border)]">
                  <span className="text-xs font-semibold text-[color:var(--da-color-muted)] uppercase tracking-wide">
                    {t('統計比較', 'Statistical Comparison')}
                  </span>
                  <div className="mt-2 space-y-1 text-xs text-[color:var(--da-color-fg)] font-mono">
                    <div>
                      {t('距平均值', 'vs Mean')}: {detailCell.value
                        ? ((detailCell.value - stats.mean) / stats.mean * 100).toFixed(1)
                        : 'N/A'}%
                    </div>
                    <div>
                      {t('距最大值', 'vs Max')}: {detailCell.value
                        ? ((stats.max - detailCell.value) / stats.max * 100).toFixed(1)
                        : 'N/A'}%
                    </div>
                  </div>
                </div>
              </div>

              <button
                onClick={() => setDetailCell(null)}
                className="w-full mt-6 px-4 py-2 bg-[color:var(--da-color-accent)] text-[color:var(--da-color-card-bg)] text-sm font-medium rounded-lg hover:bg-[color:var(--da-color-accent-hover)] transition-colors"
              >
                {t('關閉', 'Close')}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
