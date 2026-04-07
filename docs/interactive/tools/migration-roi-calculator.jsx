---
title: "Migration ROI Calculator"
tags: [migration, roi, promql, conversion]
audience: [platform-engineer, sre]
version: v2.6.0
lang: en
related: [roi-calculator, operator-setup-wizard, migration-simulator]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

// --- Shared platform data (from platform-data.json via jsx-loader) ---
const __PD = window.__PLATFORM_DATA || {};

const PACK_COUNT = (__PD.packOrder || []).length || 15;
const TOTAL_RULES = (() => {
  if (__PD.rulePacks && __PD.packOrder) {
    return __PD.packOrder.reduce((sum, key) => {
      const p = __PD.rulePacks[key];
      return sum + (p ? (p.recordingRules || 0) + (p.alertRules || 0) : 0);
    }, 0);
  }
  return 238; // fallback: 139 rec + 99 alert
})();

const AVERAGE_RULES_PER_PACK = PACK_COUNT > 0 ? Math.round(TOTAL_RULES / PACK_COUNT) : 16;

// ── Styles using design tokens (--da-*) from design-tokens.css ──
const styles = {
  container: {
    minHeight: '100vh',
    background: 'var(--da-color-bg)',
    padding: 'var(--da-space-8)',
  },
  maxWidth: {
    maxWidth: '1200px',
    margin: '0 auto',
  },
  header: {
    marginBottom: 'var(--da-space-6)',
  },
  title: {
    fontSize: 'var(--da-font-size-2xl)',
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-2)',
  },
  subtitle: {
    fontSize: 'var(--da-font-size-base)',
    color: 'var(--da-color-muted)',
    marginBottom: 'var(--da-space-1)',
  },
  platformData: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    marginTop: 'var(--da-space-2)',
  },
  section: {
    marginBottom: 'var(--da-space-8)',
  },
  inputGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
    gap: 'var(--da-space-6)',
    marginBottom: 'var(--da-space-8)',
  },
  inputCard: {
    backgroundColor: 'var(--da-color-surface)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-4)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  inputGroup: {
    marginBottom: 'var(--da-space-4)',
  },
  inputGroupLast: {
    marginBottom: 0,
  },
  label: {
    fontSize: 'var(--da-font-size-sm-md)',
    fontWeight: 'var(--da-font-weight-medium)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-2)',
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  value: {
    fontSize: 'var(--da-font-size-base)',
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-accent)',
  },
  slider: {
    width: '100%',
    height: '6px',
    borderRadius: '3px',
    background: 'var(--da-color-surface-border)',
    outline: 'none',
    WebkitAppearance: 'none',
    appearance: 'none',
    cursor: 'pointer',
  },
  sliderThumb: {
    width: '18px',
    height: '18px',
    borderRadius: '50%',
    background: 'var(--da-color-accent)',
    cursor: 'pointer',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  range: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    marginTop: 'var(--da-space-1)',
    display: 'flex',
    justifyContent: 'space-between',
  },
  radioGroup: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
    gap: 'var(--da-space-3)',
    marginTop: 'var(--da-space-2)',
  },
  radioLabel: {
    display: 'flex',
    alignItems: 'center',
    gap: 'var(--da-space-2)',
    padding: 'var(--da-space-2) var(--da-space-3)',
    borderRadius: 'var(--da-radius-md)',
    border: '1px solid var(--da-color-surface-border)',
    cursor: 'pointer',
    fontSize: 'var(--da-font-size-sm-md)',
    backgroundColor: 'var(--da-color-bg)',
    transition: 'all var(--da-transition-fast)',
  },
  radioLabelActive: {
    backgroundColor: 'var(--da-color-accent-soft)',
    borderColor: 'var(--da-color-accent)',
    color: 'var(--da-color-accent)',
  },
  metricsGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
    gap: 'var(--da-space-4)',
    marginBottom: 'var(--da-space-8)',
  },
  metricCard: {
    backgroundColor: 'var(--da-color-surface)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-4)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  metricLabel: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    fontWeight: 'var(--da-font-weight-medium)',
    marginBottom: 'var(--da-space-1)',
  },
  metricValue: {
    fontSize: 'var(--da-font-size-2xl)',
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-accent)',
    marginBottom: 'var(--da-space-1)',
    fontFamily: 'monospace',
  },
  metricUnit: {
    fontSize: 'var(--da-font-size-sm-md)',
    color: 'var(--da-color-muted)',
    fontWeight: 'var(--da-font-weight-normal)',
    marginLeft: 'var(--da-space-1)',
  },
  metricSubtitle: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    marginTop: 'var(--da-space-1)',
  },
  detailsPanel: {
    backgroundColor: 'var(--da-color-surface)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-4)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  detailsTitle: {
    fontSize: 'var(--da-font-size-md)',
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-3)',
  },
  detailsGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
    gap: 'var(--da-space-3)',
  },
  detailsItem: {
    padding: 'var(--da-space-3)',
    backgroundColor: 'var(--da-color-bg)',
    borderRadius: 'var(--da-radius-md)',
  },
  detailsKey: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    fontWeight: 'var(--da-font-weight-medium)',
    marginBottom: 'var(--da-space-1)',
  },
  detailsValue: {
    fontSize: 'var(--da-font-size-base)',
    fontWeight: 'var(--da-font-weight-semibold)',
    color: 'var(--da-color-fg)',
    fontFamily: 'monospace',
  },
  methodologyNote: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    borderTop: '1px solid var(--da-color-surface-border)',
    paddingTop: 'var(--da-space-4)',
    marginTop: 'var(--da-space-6)',
  },
  methodologyDetails: {
    marginTop: 'var(--da-space-2)',
    space: 'var(--da-space-1)',
  },
  methodologyLine: {
    marginBottom: 'var(--da-space-2)',
    lineHeight: 'var(--da-line-height-relaxed)',
  },
};

// Apply webkit appearance to sliders
const createSliderStyles = () => {
  const style = document.createElement('style');
  style.textContent = `
    input[type="range"]::-webkit-slider-thumb {
      appearance: none;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: var(--da-color-accent);
      cursor: pointer;
      box-shadow: var(--da-shadow-subtle);
    }
    input[type="range"]::-moz-range-thumb {
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: var(--da-color-accent);
      cursor: pointer;
      box-shadow: var(--da-shadow-subtle);
      border: none;
    }
  `;
  if (typeof document !== 'undefined') {
    document.head.appendChild(style);
  }
};

if (typeof document !== 'undefined') {
  createSliderStyles();
}

// ---------------------------------------------------------------------------
// Calculation models
// ---------------------------------------------------------------------------

/**
 * Platform coverage estimation.
 * Average rules per pack: 16 (238 total / 15 packs).
 * For common DB monitoring: 60-80% overlap with platform rules.
 */
function estimatePlatformCoverage({ totalRules }) {
  const platformRules = TOTAL_RULES;
  const baseOverlap = 0.70; // 70% average overlap for standard DB monitoring
  const maxCoverage = Math.min((platformRules / totalRules) * 100, 100);
  const coverage = Math.min(Math.round(baseOverlap * maxCoverage), 100);
  return coverage;
}

/**
 * Migration effort estimation.
 * Simple threshold mapping: ~5 minutes per rule.
 * Complex PromQL expressions: ~15 minutes per rule.
 * Distribution: 70% simple / 30% complex.
 */
function estimateMigrationEffort({ totalRules, recordingRules, alertRules }) {
  const simpleMinutes = 5;
  const complexMinutes = 15;
  const simpleRatio = 0.70;
  const complexRatio = 0.30;

  const avgMinutesPerRule = simpleMinutes * simpleRatio + complexMinutes * complexRatio;
  const totalMinutes = totalRules * avgMinutesPerRule;
  const hours = Math.round(totalMinutes / 60);

  return hours;
}

/**
 * Post-migration maintenance reduction.
 * From O(N×M) to O(M): N tenants × M packs → M packs.
 * Estimate monthly reduction based on tenant count.
 */
function estimateMaintenanceSavings({ currentMonthlyHours, tenants }) {
  if (tenants <= 1) return 0;

  // With N tenants, maintenance scales O(N×M)
  // With platform, it scales O(M)
  // Savings ≈ current_hours × (1 - 1/tenants)
  const savings = Math.round(currentMonthlyHours * (1 - 1/tenants));
  return Math.max(savings, 0);
}

/**
 * Break-even calculation.
 * Break-even months = migration_hours / monthly_savings.
 */
function estimateBreakEven({ migrationHours, monthlySavingsHours }) {
  if (monthlySavingsHours <= 0) return Infinity;
  return Math.round(migrationHours / monthlySavingsHours * 10) / 10;
}

/**
 * Annual savings in USD.
 */
function estimateAnnualSavings({ monthlySavingsHours, hourlyRate }) {
  const annualHours = monthlySavingsHours * 12;
  return Math.round(annualHours * hourlyRate);
}

// ---------------------------------------------------------------------------
// UI Components
// ---------------------------------------------------------------------------

function Slider({ label, value, onChange, min, max, step = 1, unit = '' }) {
  return (
    <div style={styles.inputGroup}>
      <div style={styles.label}>
        <span>{label}</span>
        <span style={styles.value}>
          {value.toLocaleString()}<span style={styles.metricUnit}>{unit}</span>
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        style={{
          ...styles.slider,
          WebkitAppearance: 'slider-horizontal',
        }}
      />
      <div style={styles.range}>
        <span>{min}{unit}</span>
        <span>{max}{unit}</span>
      </div>
    </div>
  );
}

function RadioGroup({ label, value, onChange, options }) {
  return (
    <div style={styles.inputGroup}>
      <div style={styles.label}>{label}</div>
      <div style={styles.radioGroup}>
        {options.map(opt => (
          <label
            key={opt.value}
            style={{
              ...styles.radioLabel,
              ...(value === opt.value ? styles.radioLabelActive : {}),
            }}
          >
            <input
              type="radio"
              name={label}
              value={opt.value}
              checked={value === opt.value}
              onChange={e => onChange(e.target.value)}
              style={{ cursor: 'pointer' }}
            />
            {opt.label}
          </label>
        ))}
      </div>
    </div>
  );
}

function MetricCard({ label, value, unit, subtitle }) {
  return (
    <div style={styles.metricCard}>
      <div style={styles.metricLabel}>{label}</div>
      <div>
        <span style={styles.metricValue}>{typeof value === 'number' ? value.toLocaleString() : value}</span>
        <span style={styles.metricUnit}>{unit}</span>
      </div>
      {subtitle && <div style={styles.metricSubtitle}>{subtitle}</div>}
    </div>
  );
}

function DetailsPanel({ title, items }) {
  return (
    <div style={styles.detailsPanel}>
      <div style={styles.detailsTitle}>{title}</div>
      <div style={styles.detailsGrid}>
        {items.map((item, idx) => (
          <div key={idx} style={styles.detailsItem}>
            <div style={styles.detailsKey}>{item.key}</div>
            <div style={styles.detailsValue}>{item.value}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export default function MigrationROICalculator() {
  // Input parameters
  const [promqlLines, setPromqlLines] = useState(500);
  const [recordingRules, setRecordingRules] = useState(100);
  const [alertRules, setAlertRules] = useState(80);
  const [tenants, setTenants] = useState(20);
  const [monthlyMaintenanceHours, setMonthlyMaintenanceHours] = useState(40);
  const [deploymentMode, setDeploymentMode] = useState('configmap');
  const [hourlyRate, setHourlyRate] = useState(75);

  // Calculations
  const results = useMemo(() => {
    const totalRules = recordingRules + alertRules;
    const coverage = estimatePlatformCoverage({ totalRules: promqlLines });
    const migrationHours = estimateMigrationEffort({ totalRules: promqlLines, recordingRules, alertRules });
    const monthlySavings = estimateMaintenanceSavings({ currentMonthlyHours: monthlyMaintenanceHours, tenants });
    const breakEven = estimateBreakEven({ migrationHours, monthlySavingsHours: monthlySavings });
    const annualSavings = estimateAnnualSavings({ monthlySavingsHours: monthlySavings, hourlyRate });

    return {
      coverage,
      migrationHours,
      monthlySavings,
      breakEven,
      annualSavings,
      totalRules,
    };
  }, [promqlLines, recordingRules, alertRules, tenants, monthlyMaintenanceHours, hourlyRate]);

  return (
    <div style={styles.container}>
      <div style={styles.maxWidth}>
        {/* Header */}
        <div style={styles.header}>
          <h2 style={styles.title}>
            {t('遷移效益試算器', 'Migration ROI Calculator')}
          </h2>
          <p style={styles.subtitle}>
            {t(
              '評估 PromQL 規則轉換至 Dynamic Alerting 平台的效益、成本與回本期。',
              'Evaluate the benefits, effort, and break-even timeline for migrating PromQL rules to the Dynamic Alerting platform.'
            )}
          </p>
          <div style={styles.platformData}>
            {t(
              `平台數據：${PACK_COUNT} 個 Rule Pack、${TOTAL_RULES} 條規則（平均每 pack ${AVERAGE_RULES_PER_PACK} 條）`,
              `Platform data: ${PACK_COUNT} Rule Packs, ${TOTAL_RULES} rules (~${AVERAGE_RULES_PER_PACK} rules per pack)`
            )}
          </div>
        </div>

        {/* Input Section */}
        <div style={styles.section}>
          <div style={styles.inputGrid}>
            {/* Current State */}
            <div style={styles.inputCard}>
              <h3 style={{ ...styles.label, marginBottom: 'var(--da-space-4)', color: 'var(--da-color-fg)' }}>
                {t('當前規則狀態', 'Current State')}
              </h3>
              <Slider
                label={t('PromQL 規則行數', 'PromQL Line Count')}
                value={promqlLines}
                onChange={setPromqlLines}
                min={50}
                max={10000}
                step={50}
              />
              <Slider
                label={t('Recording Rules 數量', 'Recording Rules')}
                value={recordingRules}
                onChange={setRecordingRules}
                min={10}
                max={2000}
                step={10}
              />
              <Slider
                label={t('Alert Rules 數量', 'Alert Rules')}
                value={alertRules}
                onChange={setAlertRules}
                min={10}
                max={2000}
                step={10}
                style={styles.inputGroupLast}
              />
            </div>

            {/* Tenant & Maintenance */}
            <div style={styles.inputCard}>
              <h3 style={{ ...styles.label, marginBottom: 'var(--da-space-4)', color: 'var(--da-color-fg)' }}>
                {t('租戶與維護', 'Tenants & Maintenance')}
              </h3>
              <Slider
                label={t('目前租戶數量', 'Current Tenant Count')}
                value={tenants}
                onChange={setTenants}
                min={1}
                max={500}
                step={5}
              />
              <Slider
                label={t('每月維護時數', 'Monthly Maintenance (hrs)')}
                value={monthlyMaintenanceHours}
                onChange={setMonthlyMaintenanceHours}
                min={1}
                max={200}
                step={5}
                unit={t('小時', 'hrs')}
                style={styles.inputGroupLast}
              />
            </div>

            {/* Deployment & Rate */}
            <div style={styles.inputCard}>
              <h3 style={{ ...styles.label, marginBottom: 'var(--da-space-4)', color: 'var(--da-color-fg)' }}>
                {t('配置與成本', 'Deployment & Cost')}
              </h3>
              <RadioGroup
                label={t('部署模式', 'Deployment Mode')}
                value={deploymentMode}
                onChange={setDeploymentMode}
                options={[
                  { value: 'configmap', label: t('ConfigMap 路徑', 'ConfigMap Path') },
                  { value: 'operator', label: t('Operator 路徑', 'Operator Path') },
                ]}
              />
              <Slider
                label={t('時薪 (USD)', 'Hourly Rate (USD)')}
                value={hourlyRate}
                onChange={setHourlyRate}
                min={20}
                max={200}
                step={5}
                unit="$"
                style={styles.inputGroupLast}
              />
            </div>
          </div>
        </div>

        {/* Results Section */}
        <div style={styles.section}>
          <div style={styles.metricsGrid}>
            <MetricCard
              label={t('預估平台涵蓋率', 'Platform Coverage')}
              value={results.coverage}
              unit="%"
              subtitle={t('可被現有 Rule Pack 覆蓋的規則', 'Rules covered by existing packs')}
            />
            <MetricCard
              label={t('遷移工作量', 'Migration Effort')}
              value={results.migrationHours}
              unit={t('小時', 'hours')}
              subtitle={t('包含驗證與測試', 'Includes validation & testing')}
            />
            <MetricCard
              label={t('月度節省時數', 'Monthly Savings')}
              value={results.monthlySavings}
              unit={t('小時/月', 'hrs/mo')}
              subtitle={t('維護自動化後減少', 'From O(N×M) to O(M)')}
            />
            <MetricCard
              label={t('回本期', 'Break-even')}
              value={results.breakEven === Infinity ? t('永不', '∞') : results.breakEven}
              unit={t('個月', 'months')}
              subtitle={t('實現正 ROI 需時', 'To positive ROI')}
            />
            <MetricCard
              label={t('年度節省', 'Annual Savings')}
              value={`$${results.annualSavings.toLocaleString()}`}
              unit=""
              subtitle={t('維護成本年度減少額', 'Maintenance cost reduction')}
            />
          </div>
        </div>

        {/* Details Panel */}
        <div style={styles.section}>
          <DetailsPanel
            title={t('詳細計算結果', 'Detailed Results')}
            items={[
              { key: t('規則總數', 'Total Rules'), value: results.totalRules },
              { key: t('涵蓋的規則數', 'Covered Rules'), value: Math.round(results.totalRules * results.coverage / 100) },
              { key: t('需補充的規則', 'Rules to Add'), value: Math.round(results.totalRules * (100 - results.coverage) / 100) },
              { key: t('遷移成本 (小時)', 'Migration Cost'), value: results.migrationHours },
              { key: t('月度維護節省', 'Monthly Savings'), value: `${results.monthlySavings} hrs` },
              { key: t('年度維護成本減少', 'Annual Cost Reduction'), value: `$${results.annualSavings.toLocaleString()}` },
              { key: t('回本期 (月)', 'Break-even (months)'), value: results.breakEven === Infinity ? t('永不', '∞') : results.breakEven },
              { key: t('部署模式', 'Deployment Mode'), value: deploymentMode === 'configmap' ? 'ConfigMap' : 'Operator' },
            ]}
          />
        </div>

        {/* Methodology Note */}
        <div style={styles.methodologyNote}>
          <details>
            <summary style={{ cursor: 'pointer', fontWeight: 'var(--da-font-weight-semibold)', marginBottom: 'var(--da-space-2)' }}>
              {t('計算方法說明', 'Calculation Methodology')}
            </summary>
            <div style={styles.methodologyDetails}>
              <p style={styles.methodologyLine}>
                <strong>{t('平台涵蓋率：', 'Platform Coverage: ')}</strong>
                {t(
                  '現有 Rule Pack 包含 238 條規則（15 個 pack），平均每 pack 16 條。針對常見 DB 監控場景，預估與用戶規則的重疊率 60~80%。',
                  'Existing Rule Packs contain 238 rules (15 packs), ~16 rules per pack. For standard DB monitoring scenarios, we estimate 60-80% overlap with your rules.'
                )}
              </p>
              <p style={styles.methodologyLine}>
                <strong>{t('遷移工作量：', 'Migration Effort: ')}</strong>
                {t(
                  '簡單閾值對應 (~5分鐘/規則) 佔 70%，複雜 PromQL 表達式 (~15分鐘/規則) 佔 30%。',
                  'Simple threshold mapping (~5 min/rule) accounts for 70%, complex PromQL expressions (~15 min/rule) for 30%.'
                )}
              </p>
              <p style={styles.methodologyLine}>
                <strong>{t('月度節省：', 'Monthly Savings: ')}</strong>
                {t(
                  '傳統模式下規則維護為 O(N×M)（N 租戶數 × M pack 數），平台模式為 O(M)。節省量 ≈ 當前小時數 × (1 - 1/租戶數)。',
                  'Traditional rule maintenance is O(N×M) (N tenants × M packs), platform model is O(M). Savings ≈ current hours × (1 - 1/tenants).'
                )}
              </p>
              <p style={styles.methodologyLine}>
                <strong>{t('回本期：', 'Break-even: ')}</strong>
                {t(
                  '遷移工作量 (小時) ÷ 月度維護節省 (小時) = 回本期 (月)。',
                  'Migration effort (hours) ÷ monthly savings (hours) = break-even (months).'
                )}
              </p>
              <p style={styles.methodologyLine}>
                <strong>{t('年度節省：', 'Annual Savings: ')}</strong>
                {t(
                  '月度節省時數 × 12 月 × 時薪。',
                  'Monthly savings (hours) × 12 months × hourly rate.'
                )}
              </p>
            </div>
          </details>
        </div>
      </div>
    </div>
  );
}
