---
title: "Cost Estimator"
tags: [cost, estimation, resources, capacity]
audience: ["platform-engineer", "sre", "management"]
version: v2.7.0
lang: en
related: [roi-calculator, capacity-planner, migration-roi-calculator]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

// --- Shared platform data (from platform-data.json via jsx-loader) ---
const __PD = window.__PLATFORM_DATA || {};

const PACK_COUNT = (__PD.packOrder || []).length || 15;

// --- Configuration Constants ---
const SCRAPE_INTERVALS = [
  { value: 15, label: '15s' },
  { value: 30, label: '30s' },
  { value: 60, label: '60s' },
];

const RETENTION_PERIODS = [
  { value: 7, label: '7 days' },
  { value: 15, label: '15 days' },
  { value: 30, label: '30 days' },
  { value: 90, label: '90 days' },
];

const HA_REPLICAS = [1, 2, 3];

const DEPLOYMENT_MODES = [
  { id: 'configmap', label: 'ConfigMap' },
  { id: 'operator', label: 'Operator' },
];

// --- Pricing Configuration (AWS defaults) ---
const PRICING = {
  cpuPerHour: 0.05,      // $/CPU-hour
  memoryPerGBHour: 0.005, // $/GB-hour
};

const SECONDS_PER_DAY = 86400;
const HOURS_PER_MONTH = 730;

// ── Styles using design tokens (--da-*) from design-tokens.css ──
const styles = {
  container: {
    maxWidth: '1200px',
    margin: '0 auto',
    padding: 'var(--da-space-8)',
    display: 'flex',
    flexDirection: 'column',
    gap: 'var(--da-space-8)',
  },
  header: {
    marginBottom: 'var(--da-space-2)',
  },
  title: {
    fontSize: 'var(--da-font-size-2xl)',
    fontWeight: 'bold',
    color: 'var(--da-color-fg)',
    margin: 0,
    marginBottom: 'var(--da-space-2)',
  },
  subtitle: {
    fontSize: 'var(--da-font-size-sm)',
    color: 'var(--da-color-muted)',
    margin: 0,
    marginBottom: 'var(--da-space-1)',
  },
  platformInfo: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    marginTop: 'var(--da-space-1)',
  },
  inputGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))',
    gap: 'var(--da-space-6)',
    marginBottom: 'var(--da-space-8)',
  },
  inputCard: {
    backgroundColor: 'var(--da-color-bg)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-6)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  cardTitle: {
    fontSize: 'var(--da-font-size-sm)',
    fontWeight: '600',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-4)',
    margin: 0,
  },
  inputGroup: {
    marginBottom: 'var(--da-space-4)',
  },
  inputGroupLast: {
    marginBottom: 0,
  },
  label: {
    fontSize: 'var(--da-font-size-sm)',
    fontWeight: '500',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-2)',
    display: 'block',
  },
  labelWithValue: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  labelValue: {
    fontSize: 'var(--da-font-size-sm)',
    fontWeight: '600',
    color: 'var(--da-color-accent)',
    fontFamily: 'monospace',
  },
  sliderInput: {
    width: '100%',
    height: '6px',
    borderRadius: 'var(--da-radius-full)',
    background: 'var(--da-color-surface-border)',
    outline: 'none',
    WebkitAppearance: 'none',
    appearance: 'none',
    cursor: 'pointer',
  },
  sliderRange: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    display: 'flex',
    justifyContent: 'space-between',
    marginTop: 'var(--da-space-1)',
  },
  selectInput: {
    width: '100%',
    padding: 'var(--da-space-2) var(--da-space-3)',
    fontSize: 'var(--da-font-size-sm)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-md)',
    backgroundColor: 'var(--da-color-bg)',
    color: 'var(--da-color-fg)',
    cursor: 'pointer',
  },
  radioGroup: {
    display: 'flex',
    flexDirection: 'column',
    gap: 'var(--da-space-3)',
  },
  radioOption: {
    display: 'flex',
    alignItems: 'center',
    gap: 'var(--da-space-2)',
  },
  radioInput: {
    cursor: 'pointer',
    accentColor: 'var(--da-color-accent)',
  },
  radioLabel: {
    cursor: 'pointer',
    fontSize: 'var(--da-font-size-sm)',
    color: 'var(--da-color-fg)',
  },
  resultsSection: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: 'var(--da-space-6)',
    marginBottom: 'var(--da-space-8)',
  },
  resourceTable: {
    backgroundColor: 'var(--da-color-bg)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-6)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  tableTitle: {
    fontSize: 'var(--da-font-size-md)',
    fontWeight: '600',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-4)',
    margin: 0,
  },
  table: {
    width: '100%',
    borderCollapse: 'collapse',
    fontSize: 'var(--da-font-size-xs)',
  },
  tableHeader: {
    backgroundColor: 'var(--da-color-surface-border)',
    color: 'var(--da-color-fg)',
    fontWeight: '600',
    padding: 'var(--da-space-2) var(--da-space-3)',
    textAlign: 'left',
    borderBottom: '1px solid var(--da-color-surface-border)',
  },
  tableCell: {
    padding: 'var(--da-space-2) var(--da-space-3)',
    borderBottom: '1px solid var(--da-color-surface-border)',
    color: 'var(--da-color-fg)',
  },
  tableCellMonospace: {
    fontFamily: 'monospace',
    fontWeight: '500',
  },
  costCard: {
    backgroundColor: 'var(--da-color-bg)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-6)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  costCardGreen: {
    borderColor: 'var(--da-color-success)',
    backgroundColor: 'var(--da-color-success)',
    opacity: 0.05,
  },
  costValue: {
    fontSize: 'var(--da-font-size-2xl)',
    fontWeight: 'bold',
    color: 'var(--da-color-accent)',
    fontFamily: 'monospace',
    marginTop: 'var(--da-space-2)',
  },
  costLabel: {
    fontSize: 'var(--da-font-size-sm)',
    color: 'var(--da-color-muted)',
    marginBottom: 'var(--da-space-1)',
  },
  costSubtitle: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    marginTop: 'var(--da-space-2)',
  },
  comparisonSection: {
    backgroundColor: 'var(--da-color-bg)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-6)',
    boxShadow: 'var(--da-shadow-subtle)',
    gridColumn: '1 / -1',
  },
  comparisonGrid: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr auto',
    gap: 'var(--da-space-4)',
    alignItems: 'center',
  },
  comparisonCard: {
    padding: 'var(--da-space-4)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-md)',
    backgroundColor: 'var(--da-color-bg)',
  },
  comparisonLabel: {
    fontSize: 'var(--da-font-size-sm)',
    color: 'var(--da-color-muted)',
    marginBottom: 'var(--da-space-2)',
  },
  comparisonValue: {
    fontSize: 'var(--da-font-size-lg)',
    fontWeight: 'bold',
    color: 'var(--da-color-fg)',
    fontFamily: 'monospace',
  },
  deltaPositive: {
    color: 'var(--da-color-success)',
  },
  deltaNegative: {
    color: 'var(--da-color-warning)',
  },
  recommendationSection: {
    backgroundColor: 'var(--da-color-bg)',
    border: '1px solid var(--da-color-accent)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-6)',
    boxShadow: 'var(--da-shadow-subtle)',
    gridColumn: '1 / -1',
  },
  recommendationTitle: {
    fontSize: 'var(--da-font-size-md)',
    fontWeight: '600',
    color: 'var(--da-color-accent)',
    marginBottom: 'var(--da-space-3)',
    margin: 0,
  },
  recommendationText: {
    fontSize: 'var(--da-font-size-sm)',
    color: 'var(--da-color-fg)',
    lineHeight: '1.6',
    margin: 0,
  },
  methodologySection: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    borderTop: '1px solid var(--da-color-surface-border)',
    paddingTop: 'var(--da-space-4)',
    marginTop: 'var(--da-space-8)',
  },
  summaryTrigger: {
    cursor: 'pointer',
    fontWeight: '500',
    color: 'var(--da-color-accent)',
  },
  methodologyDetails: {
    marginTop: 'var(--da-space-2)',
    display: 'flex',
    flexDirection: 'column',
    gap: 'var(--da-space-2)',
  },
  methodologyP: {
    margin: 0,
    lineHeight: '1.6',
  },
  '@media (max-width: 1024px)': {
    resultsSection: {
      gridTemplateColumns: '1fr',
    },
    comparisonGrid: {
      gridTemplateColumns: '1fr',
    },
  },
  '@media (max-width: 640px)': {
    inputGrid: {
      gridTemplateColumns: '1fr',
    },
    resultsSection: {
      gridTemplateColumns: '1fr',
    },
  },
};

// ---------------------------------------------------------------------------
// Calculation Models
// ---------------------------------------------------------------------------

/**
 * Calculate threshold-exporter resource usage.
 * Memory: base 50MB + 2MB per (tenant × packs)
 * CPU: base 0.1 cores + 0.01 per tenant
 */
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

// ---------------------------------------------------------------------------
// UI Components
// ---------------------------------------------------------------------------

function Slider({ label, value, onChange, min, max, step = 1, unit = '' }) {
  return (
    <div style={styles.inputGroup}>
      <label style={{...styles.label, ...styles.labelWithValue}}>
        <span>{label}</span>
        <span style={styles.labelValue}>{value}{unit}</span>
      </label>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        style={styles.sliderInput}
        role="slider"
        aria-label={label}
        aria-valuemin={min}
        aria-valuemax={max}
        aria-valuenow={value}
      />
      <div style={styles.sliderRange}>
        <span>{min}{unit}</span>
        <span>{max}{unit}</span>
      </div>
    </div>
  );
}

function Select({ label, value, onChange, options, required = false }) {
  return (
    <div style={styles.inputGroup}>
      <label style={styles.label}>{label}</label>
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        style={styles.selectInput}
        aria-label={label}
        required={required}
      >
        {options.map(opt => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function RadioGroup({ label, value, onChange, options }) {
  return (
    <div style={styles.inputGroup}>
      <label style={styles.label}>{label}</label>
      <div style={styles.radioGroup} role="radiogroup" aria-label={label}>
        {options.map(opt => (
          <div key={opt.id} style={styles.radioOption}>
            <input
              type="radio"
              id={opt.id}
              name={label}
              value={opt.id}
              checked={value === opt.id}
              onChange={e => onChange(e.target.value)}
              style={styles.radioInput}
            />
            <label htmlFor={opt.id} style={styles.radioLabel}>{opt.label}</label>
          </div>
        ))}
      </div>
    </div>
  );
}

function ResourceTable({ data, replicas, mode }) {
  const { components, summary } = data;

  return (
    <div style={styles.resourceTable}>
      <h3 style={styles.tableTitle}>
        {t('資源概觀', 'Resource Summary')} ({replicas}x HA)
      </h3>
      <table style={styles.table}>
        <thead>
          <tr>
            <th style={styles.tableHeader}>{t('組件', 'Component')}</th>
            <th style={styles.tableHeader}>{t('CPU', 'CPU')}</th>
            <th style={styles.tableHeader}>{t('記憶體', 'Memory')}</th>
            <th style={styles.tableHeader}>{t('儲存空間', 'Storage')}</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td style={styles.tableCell}>threshold-exporter (×{replicas})</td>
            <td style={{...styles.tableCell, ...styles.tableCellMonospace}}>
              {components.exporter.cpuCores.toFixed(2)} cores
            </td>
            <td style={{...styles.tableCell, ...styles.tableCellMonospace}}>
              {(components.exporter.memoryMB / 1024).toFixed(2)} GB
            </td>
            <td style={styles.tableCell}>—</td>
          </tr>
          <tr>
            <td style={styles.tableCell}>Prometheus (×{replicas})</td>
            <td style={{...styles.tableCell, ...styles.tableCellMonospace}}>
              0.25 cores
            </td>
            <td style={{...styles.tableCell, ...styles.tableCellMonospace}}>
              {(components.prometheus.memoryMB / 1024).toFixed(2)} GB
            </td>
            <td style={{...styles.tableCell, ...styles.tableCellMonospace}}>
              {components.prometheus.storageGB.toFixed(2)} GB
            </td>
          </tr>
          <tr>
            <td style={styles.tableCell}>Alertmanager (×{replicas})</td>
            <td style={{...styles.tableCell, ...styles.tableCellMonospace}}>
              {components.alertmanager.cpuCores.toFixed(2)} cores
            </td>
            <td style={{...styles.tableCell, ...styles.tableCellMonospace}}>
              {(components.alertmanager.memoryMB / 1024).toFixed(2)} GB
            </td>
            <td style={styles.tableCell}>—</td>
          </tr>
          {components.operator.cpuCores > 0 && (
            <tr>
              <td style={styles.tableCell}>Prometheus Operator</td>
              <td style={{...styles.tableCell, ...styles.tableCellMonospace}}>
                {components.operator.cpuCores.toFixed(2)} cores
              </td>
              <td style={{...styles.tableCell, ...styles.tableCellMonospace}}>
                {(components.operator.memoryMB / 1024).toFixed(2)} GB
              </td>
              <td style={styles.tableCell}>—</td>
            </tr>
          )}
          <tr style={{ backgroundColor: 'var(--da-color-surface-border)' }}>
            <td style={{...styles.tableCell, fontWeight: 'bold'}}>Total</td>
            <td style={{...styles.tableCell, ...styles.tableCellMonospace, fontWeight: 'bold'}}>
              {summary.totalCpuCores.toFixed(2)} cores
            </td>
            <td style={{...styles.tableCell, ...styles.tableCellMonospace, fontWeight: 'bold'}}>
              {(summary.totalMemoryMB / 1024).toFixed(2)} GB
            </td>
            <td style={{...styles.tableCell, ...styles.tableCellMonospace, fontWeight: 'bold'}}>
              {summary.storageGB.toFixed(2)} GB
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function CostCard({ label, cost, subtitle }) {
  return (
    <div style={{...styles.costCard, ...styles.costCardGreen}}>
      <div style={styles.costLabel}>{label}</div>
      <div style={styles.costValue}>${cost.toFixed(2)}</div>
      {subtitle && <div style={styles.costSubtitle}>{subtitle}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export default function CostEstimator() {
  const [tenants, setTenants] = useState(10);
  const [packsPerTenant, setPacksPerTenant] = useState(5);
  const [scrapeInterval, setScrapeInterval] = useState(15);
  const [retentionDays, setRetentionDays] = useState(15);
  const [replicas, setReplicas] = useState(2);
  const [mode, setMode] = useState('configmap');

  // Memoized calculations
  const results = useMemo(() => {
    return calcTotalResources(tenants, packsPerTenant, scrapeInterval, retentionDays, replicas, mode);
  }, [tenants, packsPerTenant, scrapeInterval, retentionDays, replicas, mode]);

  // Compare with alternate mode
  const alternateMode = mode === 'configmap' ? 'operator' : 'configmap';
  const alternateResults = useMemo(() => {
    return calcTotalResources(tenants, packsPerTenant, scrapeInterval, retentionDays, replicas, alternateMode);
  }, [tenants, packsPerTenant, scrapeInterval, retentionDays, replicas, alternateMode]);

  const costDelta = alternateResults.costs.totalMonthlyCost - results.costs.totalMonthlyCost;
  const costDeltaPercent = results.costs.totalMonthlyCost > 0
    ? (costDelta / results.costs.totalMonthlyCost) * 100
    : 0;

  // Recommendation
  const recommendationText = `For ${tenants} tenant${tenants !== 1 ? 's' : ''} with ${packsPerTenant} pack${packsPerTenant !== 1 ? 's' : ''} each, we recommend ${replicas}x HA replica${replicas !== 1 ? 's' : ''} with ${retentionDays}-day retention (${scrapeInterval}s scrape). Estimated monthly cost: $${results.costs.totalMonthlyCost.toFixed(2)}. Using ${mode === 'operator' ? 'Operator' : 'ConfigMap'} mode.`;

  return (
    <div style={styles.container}>
      {/* Header */}
      <div style={styles.header}>
        <h2 style={styles.title}>
          {t('成本估算機', 'Cost Estimator')}
        </h2>
        <p style={styles.subtitle}>
          {t(
            '根據部署配置估算基礎設施成本',
            'Estimate infrastructure costs based on your deployment configuration.'
          )}
        </p>
        <div style={styles.platformInfo}>
          {t(
            `平台數據：${PACK_COUNT} 個 Rule Pack`,
            `Platform data: ${PACK_COUNT} Rule Packs`
          )}
        </div>
      </div>

      {/* Input Section */}
      <div style={styles.inputGrid}>
        <div style={styles.inputCard}>
          <h3 style={styles.cardTitle}>
            {t('規模配置', 'Scale Configuration')}
          </h3>
          <Slider
            label={t('租戶數量', 'Number of Tenants')}
            value={tenants}
            onChange={setTenants}
            min={1}
            max={500}
          />
          <Slider
            label={t('每個租戶的 Rule Pack 數', 'Rule Packs per Tenant')}
            value={packsPerTenant}
            onChange={setPacksPerTenant}
            min={1}
            max={15}
          />
        </div>

        <div style={styles.inputCard}>
          <h3 style={styles.cardTitle}>
            {t('Prometheus 設定', 'Prometheus Configuration')}
          </h3>
          <Select
            label={t('抓取間隔', 'Scrape Interval')}
            value={scrapeInterval}
            onChange={setScrapeInterval}
            options={SCRAPE_INTERVALS}
          />
          <Select
            label={t('保留期間', 'Retention Period')}
            value={retentionDays}
            onChange={setRetentionDays}
            options={RETENTION_PERIODS}
          />
        </div>

        <div style={styles.inputCard}>
          <h3 style={styles.cardTitle}>
            {t('部署配置', 'Deployment Configuration')}
          </h3>
          <RadioGroup
            label={t('HA 副本數', 'HA Replicas')}
            value={replicas}
            onChange={setReplicas}
            options={HA_REPLICAS.map(r => ({ id: r, label: `${r}x replica${r !== 1 ? 's' : ''}` }))}
          />
          <RadioGroup
            label={t('部署模式', 'Deployment Mode')}
            value={mode}
            onChange={setMode}
            options={DEPLOYMENT_MODES}
          />
        </div>
      </div>

      {/* Results Section */}
      <div style={styles.resultsSection}>
        <ResourceTable data={results} replicas={replicas} mode={mode} />
        <div>
          <CostCard
            label={t('月度費用估算', 'Estimated Monthly Cost')}
            cost={results.costs.totalMonthlyCost}
            subtitle={`CPU: $${results.costs.cpuCost.toFixed(2)} | Memory: $${results.costs.memoryCost.toFixed(2)}`}
          />
        </div>
      </div>

      {/* Cost Comparison */}
      <div style={styles.comparisonSection}>
        <h3 style={styles.tableTitle}>
          {t('部署模式比較', 'Deployment Mode Comparison')}
        </h3>
        <div style={styles.comparisonGrid}>
          <div style={styles.comparisonCard}>
            <div style={styles.comparisonLabel}>
              {mode === 'configmap' ? 'Current: ConfigMap' : 'ConfigMap'}
            </div>
            <div style={styles.comparisonValue}>
              ${mode === 'configmap' ? results.costs.totalMonthlyCost.toFixed(2) : alternateResults.costs.totalMonthlyCost.toFixed(2)}
            </div>
          </div>
          <div style={styles.comparisonCard}>
            <div style={styles.comparisonLabel}>
              {mode === 'operator' ? 'Current: Operator' : 'Operator'}
            </div>
            <div style={styles.comparisonValue}>
              ${mode === 'operator' ? results.costs.totalMonthlyCost.toFixed(2) : alternateResults.costs.totalMonthlyCost.toFixed(2)}
            </div>
          </div>
          <div style={styles.comparisonCard}>
            <div style={styles.comparisonLabel}>
              {t('差異', 'Difference')}
            </div>
            <div style={{
              ...styles.comparisonValue,
              ...(costDelta > 0 ? styles.deltaNegative : styles.deltaPositive)
            }}>
              {costDelta > 0 ? '+' : ''}{costDelta.toFixed(2)} ({costDeltaPercent.toFixed(1)}%)
            </div>
          </div>
        </div>
      </div>

      {/* Recommendation */}
      <div style={styles.recommendationSection} role="region" aria-live="polite" aria-label="Recommendation">
        <h3 style={styles.recommendationTitle}>
          {t('建議', 'Recommendation')}
        </h3>
        <p style={styles.recommendationText}>{recommendationText}</p>
      </div>

      {/* Methodology */}
      <div style={styles.methodologySection}>
        <details>
          <summary style={styles.summaryTrigger}>
            {t('計算方法與假設', 'Calculation Methodology')}
          </summary>
          <div style={styles.methodologyDetails}>
            <p style={styles.methodologyP}>
              <strong>{t('threshold-exporter', 'threshold-exporter')}:</strong> {t(
                '基數 50MB + 每個(租戶 × Pack) 2MB 記憶體；基數 0.1 cores + 每個租戶 0.01 cores CPU。',
                'Base 50MB + 2MB per (tenant × pack) memory; Base 0.1 cores + 0.01 per tenant CPU.'
              )}
            </p>
            <p style={styles.methodologyP}>
              <strong>{t('Prometheus TSDB', 'Prometheus TSDB')}:</strong> {t(
                '時間序列 = 租戶 × Pack × 12 平均指標 × 3 嚴重度。每個樣本 1.5 位元組，樣本數 = (保留期間 × 86400) / 抓取間隔。儲存 = 序列 × 位元組 × 樣本。',
                'Time series = tenants × packs × 12 metrics × 3 severities. 1.5 bytes per sample, samples = (retention_days × 86400) / scrape_interval. Storage = series × bytes × samples.'
              )}
            </p>
            <p style={styles.methodologyP}>
              <strong>{t('Alertmanager', 'Alertmanager')}:</strong> {t(
                '基數 64MB + 每 100 個租戶 1MB；0.05 cores 固定。',
                'Base 64MB + 1MB per 100 tenants; 0.05 cores fixed.'
              )}
            </p>
            <p style={styles.methodologyP}>
              <strong>{t('Operator 開銷', 'Operator Overhead')}:</strong> {t(
                '128MB + 0.1 cores（共享，非按副本計算）。',
                '128MB + 0.1 cores (shared, not per replica).'
              )}
            </p>
            <p style={styles.methodologyP}>
              <strong>{t('定價', 'Pricing')}:</strong> {t(
                `AWS 預設：$${PRICING.cpuPerHour}/CPU·小時、$${PRICING.memoryPerGBHour}/GB·小時，730 小時/月。`,
                `AWS defaults: $${PRICING.cpuPerHour}/CPU·hour, $${PRICING.memoryPerGBHour}/GB·hour, 730 hours/month.`
              )}
            </p>
          </div>
        </details>
      </div>
    </div>
  );
}
