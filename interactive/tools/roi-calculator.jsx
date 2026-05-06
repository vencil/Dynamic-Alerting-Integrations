---
title: "ROI Calculator"
tags: [roi, cost, adoption, evaluation]
audience: [maintainer]
version: v2.7.0
lang: en
related: [capacity-planner, health-dashboard, alert-simulator]
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

// ── Styles using design tokens (--da-*) from design-tokens.css ──
const styles = {
  container: {
    maxWidth: '80rem',
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
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-fg)',
  },
  subtitle: {
    fontSize: 'var(--da-font-size-sm)',
    color: 'var(--da-color-muted)',
    marginTop: 'var(--da-space-1)',
  },
  platformInfo: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    marginTop: 'var(--da-space-1)',
  },
  quickEstimateSection: {
    backgroundColor: 'var(--da-color-surface)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-6)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  quickEstimateTitle: {
    fontSize: 'var(--da-font-size-md)',
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-4)',
  },
  quickEstimateInputGroup: {
    display: 'grid',
    gridTemplateColumns: '1fr auto',
    gap: 'var(--da-space-3)',
    alignItems: 'flex-end',
    marginBottom: 'var(--da-space-6)',
  },
  quickEstimateResults: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: 'var(--da-space-4)',
  },
  inputLabel: {
    fontSize: 'var(--da-font-size-sm)',
    fontWeight: 'var(--da-font-weight-medium)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-1)',
    display: 'block',
  },
  input: {
    width: '100%',
    padding: 'var(--da-space-2) var(--da-space-3)',
    fontSize: 'var(--da-font-size-base)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-md)',
    backgroundColor: 'var(--da-color-surface)',
    color: 'var(--da-color-fg)',
    transition: 'border-color var(--da-transition-fast)',
  },
  inputFocus: {
    borderColor: 'var(--da-color-accent)',
    boxShadow: '0 0 0 3px var(--da-color-focus-ring)',
  },
  sliderContainer: {
    marginBottom: 'var(--da-space-4)',
  },
  sliderLabel: {
    display: 'flex',
    justifyContent: 'space-between',
    fontSize: 'var(--da-font-size-sm)',
    marginBottom: 'var(--da-space-1)',
  },
  sliderLabelKey: {
    color: 'var(--da-color-fg)',
    fontWeight: 'var(--da-font-weight-medium)',
  },
  sliderLabelValue: {
    fontFamily: 'var(--da-font-mono)',
    color: 'var(--da-color-accent)',
    fontWeight: 'var(--da-font-weight-medium)',
  },
  sliderInput: {
    width: '100%',
    height: '6px',
    borderRadius: 'var(--da-radius-full)',
    background: 'var(--da-color-surface-border)',
    outline: 'none',
    WebkitAppearance: 'none',
    appearance: 'none',
  },
  sliderRange: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    display: 'flex',
    justifyContent: 'space-between',
    marginTop: 'var(--da-space-1)',
  },
  inputSection: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr 1fr',
    gap: 'var(--da-space-6)',
  },
  inputCard: {
    backgroundColor: 'var(--da-color-surface)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-5)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  cardTitle: {
    fontSize: 'var(--da-font-size-sm)',
    fontWeight: 'var(--da-font-weight-semibold)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-4)',
  },
  resultsSection: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr 1fr 1fr',
    gap: 'var(--da-space-4)',
  },
  metricCard: {
    borderRadius: 'var(--da-radius-lg)',
    border: '1px solid',
    padding: 'var(--da-space-4)',
  },
  metricCardBlue: {
    backgroundColor: 'var(--da-color-info-soft)',
    borderColor: 'var(--da-color-accent)',
    color: 'var(--da-color-accent)',
  },
  metricCardGreen: {
    backgroundColor: 'var(--da-color-success-soft)',
    borderColor: 'var(--da-color-success)',
    color: 'var(--da-color-success)',
  },
  metricCardAmber: {
    backgroundColor: 'var(--da-color-warning-soft)',
    borderColor: 'var(--da-color-warning)',
    color: 'var(--da-color-warning)',
  },
  metricCardPurple: {
    backgroundColor: 'var(--da-color-icon-rules-bg)',
    borderColor: 'var(--da-color-icon-rules)',
    color: 'var(--da-color-icon-rules)',
  },
  metricTitle: {
    fontSize: 'var(--da-font-size-xs)',
    fontWeight: 'var(--da-font-weight-medium)',
    opacity: 0.7,
    marginBottom: 'var(--da-space-1)',
  },
  metricValue: {
    fontSize: 'var(--da-font-size-xl)',
    fontWeight: 'var(--da-font-weight-bold)',
    fontFamily: 'var(--da-font-mono)',
  },
  metricUnit: {
    fontSize: 'var(--da-font-size-sm)',
    fontWeight: 'var(--da-font-weight-normal)',
    marginLeft: 'var(--da-space-1)',
  },
  metricSubtitle: {
    fontSize: 'var(--da-font-size-xs)',
    marginTop: 'var(--da-space-1)',
    opacity: 0.6,
  },
  chartSection: {
    backgroundColor: 'var(--da-color-surface)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-5)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  chartTitle: {
    fontSize: 'var(--da-font-size-sm)',
    fontWeight: 'var(--da-font-weight-semibold)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-4)',
  },
  chartContainer: {
    display: 'flex',
    flexDirection: 'column',
    gap: 'var(--da-space-3)',
  },
  chartBar: {
    marginBottom: 'var(--da-space-2)',
  },
  chartBarLabel: {
    display: 'flex',
    justifyContent: 'space-between',
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    marginBottom: 'var(--da-space-1)',
  },
  chartBarLabelValue: {
    fontFamily: 'var(--da-font-mono)',
  },
  chartBarTrack: {
    height: '16px',
    backgroundColor: 'var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-full)',
    overflow: 'hidden',
  },
  chartBarFill: {
    height: '100%',
    borderRadius: 'var(--da-radius-full)',
    transition: 'width var(--da-transition-slow)',
  },
  chartBarBlue: {
    backgroundColor: 'var(--da-color-accent)',
  },
  chartBarAmber: {
    backgroundColor: 'var(--da-color-warning)',
  },
  chartBarPurple: {
    backgroundColor: 'var(--da-color-icon-rules)',
  },
  methodologySection: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    borderTop: '1px solid var(--da-color-surface-border)',
    paddingTop: 'var(--da-space-3)',
  },
  methodologyDetails: {
    marginTop: 'var(--da-space-2)',
    display: 'flex',
    flexDirection: 'column',
    gap: 'var(--da-space-1)',
  },
  methodologyP: {
    lineHeight: 'var(--da-line-height-relaxed)',
  },
  summaryTrigger: {
    cursor: 'pointer',
    fontWeight: 'var(--da-font-weight-medium)',
  },
  summaryTriggerHover: {
    color: 'var(--da-color-fg)',
  },
  '@media (max-width: 1024px)': {
    inputSection: {
      gridTemplateColumns: '1fr 1fr',
    },
    resultsSection: {
      gridTemplateColumns: '1fr 1fr',
    },
  },
  '@media (max-width: 640px)': {
    inputSection: {
      gridTemplateColumns: '1fr',
    },
    resultsSection: {
      gridTemplateColumns: '1fr',
    },
    quickEstimateInputGroup: {
      gridTemplateColumns: '1fr',
    },
    quickEstimateResults: {
      gridTemplateColumns: '1fr',
    },
  },
};

// ---------------------------------------------------------------------------
// Calculation models
// ---------------------------------------------------------------------------

/**
 * O(N×M) → O(M) rule maintenance model.
 * Without platform: each tenant needs individual rule config = N tenants × M packs × T minutes.
 * With platform: configure once per pack = M packs × T minutes (tenant inherits via profiles).
 */
function calcRuleMaintenance({ tenants, packs, changeMinutes, changesPerMonth }) {
  const withoutHours = (tenants * packs * changeMinutes * changesPerMonth) / 60;
  const withHours = (packs * changeMinutes * changesPerMonth) / 60;
  const savedHours = Math.max(withoutHours - withHours, 0);
  const reduction = withoutHours > 0 ? (savedHours / withoutHours) * 100 : 0;
  return { withoutHours, withHours, savedHours, reduction };
}

/**
 * Alert storm reduction via auto-suppression + maintenance mode.
 * Empirical model: severity dedup removes ~40% noise, maintenance mode removes ~25%,
 * silent mode removes ~15%. Combined effect with overlap adjustment.
 */
function calcAlertStormReduction({ stormsPerMonth, avgAlertsPerStorm }) {
  const dedupRate = 0.40;
  const maintenanceRate = 0.25;
  const silentRate = 0.15;
  // Combined reduction with diminishing returns
  const combined = 1 - (1 - dedupRate) * (1 - maintenanceRate) * (1 - silentRate);
  const totalAlertsMonth = stormsPerMonth * avgAlertsPerStorm;
  const reducedAlerts = Math.round(totalAlertsMonth * combined);
  return { totalAlertsMonth, reducedAlerts, reductionPct: combined * 100 };
}

/**
 * Time-to-market improvement: scaffold + migration automation.
 * Without: manual YAML authoring + validation + route config.
 * With: `da-tools scaffold` + `validate-config` + `generate-routes`.
 */
function calcTimeToMarket({ tenants, manualOnboardMinutes }) {
  const automatedMinutes = 5; // scaffold + validate + generate-routes
  const perTenantSaved = Math.max(manualOnboardMinutes - automatedMinutes, 0);
  const totalSavedHours = (tenants * perTenantSaved) / 60;
  const reduction = manualOnboardMinutes > 0
    ? (perTenantSaved / manualOnboardMinutes) * 100 : 0;
  return { manualHours: (tenants * manualOnboardMinutes) / 60, automatedHours: (tenants * automatedMinutes) / 60, totalSavedHours, reduction };
}

/**
 * Annual TCO savings combining all three dimensions.
 */
function calcAnnualSavings({ ruleSavedHours, ttmSavedHours, hourlyRate, alertReduction, oncallStaff }) {
  const ruleAnnual = ruleSavedHours * 12 * hourlyRate;
  const ttmAnnual = ttmSavedHours * hourlyRate; // one-time per new tenant, amortized
  // Alert reduction → less on-call fatigue → estimated 20% productivity recovery
  const alertAnnual = alertReduction * oncallStaff * hourlyRate * 0.20 * 12;
  return { ruleAnnual, ttmAnnual, alertAnnual, total: ruleAnnual + ttmAnnual + alertAnnual };
}

// ---------------------------------------------------------------------------
// UI Components
// ---------------------------------------------------------------------------

function Slider({ label, value, onChange, min, max, step = 1, unit = '' }) {
  return (
    <div style={styles.sliderContainer}>
      <div style={styles.sliderLabel}>
        <span style={styles.sliderLabelKey}>{label}</span>
        <span style={styles.sliderLabelValue}>{value.toLocaleString()}{unit}</span>
      </div>
      <input
        type="range" min={min} max={max} step={step} value={value}
        onChange={e => onChange(Number(e.target.value))}
        style={{...styles.sliderInput}}
      />
      <div style={styles.sliderRange}>
        <span>{min}{unit}</span><span>{max}{unit}</span>
      </div>
    </div>
  );
}

function MetricCard({ title, value, unit, subtitle, color = 'blue' }) {
  const colorMap = {
    blue: styles.metricCardBlue,
    green: styles.metricCardGreen,
    amber: styles.metricCardAmber,
    purple: styles.metricCardPurple,
  };
  return (
    <div style={{...styles.metricCard, ...colorMap[color]}}>
      <div style={styles.metricTitle}>{title}</div>
      <div style={styles.metricValue}>
        {typeof value === 'number' ? value.toLocaleString(undefined, { maximumFractionDigits: 1 }) : value}
        <span style={styles.metricUnit}>{unit}</span>
      </div>
      {subtitle && <div style={styles.metricSubtitle}>{subtitle}</div>}
    </div>
  );
}

function BarChart({ data }) {
  const maxVal = Math.max(...data.map(d => d.value), 1);
  const colorMap = {
    blue: styles.chartBarBlue,
    amber: styles.chartBarAmber,
    purple: styles.chartBarPurple,
  };
  return (
    <div style={styles.chartContainer}>
      {data.map((d, i) => {
        const barWidth = Math.max((d.value / maxVal) * 100, 2);
        const fillColor = colorMap[d.color] || colorMap.blue;
        return (
          <div key={i} style={styles.chartBar}>
            <div style={styles.chartBarLabel}>
              <span>{d.label}</span>
              <span style={styles.chartBarLabelValue}>${d.value.toLocaleString(undefined, { maximumFractionDigits: 0 })}</span>
            </div>
            <div style={styles.chartBarTrack}>
              <div
                style={{...styles.chartBarFill, ...fillColor, width: `${barWidth}%`}}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export default function ROICalculator() {
  // Quick Estimate mode state
  const [quickTenants, setQuickTenants] = useState(20);
  const [showDetails, setShowDetails] = useState(false);

  // Full calculator input parameters
  const [tenants, setTenants] = useState(20);
  const [packs, setPacks] = useState(PACK_COUNT);
  const [changeMinutes, setChangeMinutes] = useState(30);
  const [changesPerMonth, setChangesPerMonth] = useState(4);
  const [oncallStaff, setOncallStaff] = useState(3);
  const [hourlyRate, setHourlyRate] = useState(75);
  const [stormsPerMonth, setStormsPerMonth] = useState(8);
  const [avgAlertsPerStorm, setAvgAlertsPerStorm] = useState(15);
  const [manualOnboardMinutes, setManualOnboardMinutes] = useState(120);

  // Quick estimate calculations (with default assumptions)
  const quickResults = useMemo(() => {
    const rule = calcRuleMaintenance({
      tenants: quickTenants,
      packs: PACK_COUNT,
      changeMinutes: 30,
      changesPerMonth: 4
    });
    const storm = calcAlertStormReduction({ stormsPerMonth: 8, avgAlertsPerStorm: 15 });
    const ttm = calcTimeToMarket({ tenants: quickTenants, manualOnboardMinutes: 120 });
    const annual = calcAnnualSavings({
      ruleSavedHours: rule.savedHours,
      ttmSavedHours: ttm.totalSavedHours,
      hourlyRate: 75,
      alertReduction: storm.reducedAlerts,
      oncallStaff: 3,
    });
    return { annual, rule, ttm };
  }, [quickTenants]);

  // Full calculator calculations
  const fullResults = useMemo(() => {
    const rule = calcRuleMaintenance({ tenants, packs, changeMinutes, changesPerMonth });
    const storm = calcAlertStormReduction({ stormsPerMonth, avgAlertsPerStorm });
    const ttm = calcTimeToMarket({ tenants, manualOnboardMinutes });
    const annual = calcAnnualSavings({
      ruleSavedHours: rule.savedHours,
      ttmSavedHours: ttm.totalSavedHours,
      hourlyRate,
      alertReduction: storm.reducedAlerts,
      oncallStaff,
    });
    return { rule, storm, ttm, annual };
  }, [tenants, packs, changeMinutes, changesPerMonth, oncallStaff, hourlyRate,
      stormsPerMonth, avgAlertsPerStorm, manualOnboardMinutes]);

  const handleQuickEstimateChange = (val) => {
    const numVal = Number(val);
    setQuickTenants(numVal);
    setTenants(numVal);
  };

  return (
    <div style={styles.container}>
      {/* Header */}
      <div style={styles.header}>
        <h2 style={styles.title}>
          {t('採用效益試算器', 'ROI Calculator')}
        </h2>
        <p style={styles.subtitle}>
          {t(
            '調整下方參數，即時計算 Dynamic Alerting 平台的採用效益。',
            'Adjust parameters below to calculate adoption benefits in real-time.'
          )}
        </p>
        <div style={styles.platformInfo}>
          {t(
            `平台數據：${PACK_COUNT} 個 Rule Pack、${TOTAL_RULES} 條規則`,
            `Platform data: ${PACK_COUNT} Rule Packs, ${TOTAL_RULES} rules`
          )}
        </div>
      </div>

      {/* Quick Estimate Mode */}
      <div style={styles.quickEstimateSection}>
        <h3 style={styles.quickEstimateTitle}>
          {t('快速估算', 'Quick Estimate')}
        </h3>
        <div style={styles.quickEstimateInputGroup}>
          <div>
            <label style={styles.inputLabel}>
              {t('您管理多少個租戶？', 'How many tenants do you manage?')}
            </label>
            <input
              type="number"
              min="1"
              max="500"
              value={quickTenants}
              onChange={e => handleQuickEstimateChange(e.target.value)}
              style={styles.input}
            />
          </div>
        </div>
        <div style={styles.quickEstimateResults}>
          <MetricCard
            title={t('年度節省', 'Annual Savings')}
            value={quickResults.annual.total}
            unit="USD"
            color="green"
          />
          <MetricCard
            title={t('月度時間節省', 'Monthly Time Saved')}
            value={(quickResults.rule.savedHours / 12).toFixed(1)}
            unit={t('小時', 'hrs')}
            color="blue"
          />
        </div>
      </div>

      {/* Expand button for details */}
      <div style={{ paddingBottom: 'var(--da-space-2)' }}>
        <button
          onClick={() => setShowDetails(!showDetails)}
          style={{
            background: 'transparent',
            border: 'none',
            color: 'var(--da-color-accent)',
            cursor: 'pointer',
            fontSize: 'var(--da-font-size-sm)',
            fontWeight: 'var(--da-font-weight-medium)',
            padding: 0,
          }}
        >
          {showDetails ? '▼' : '▶'} {t('顯示詳細計算器', 'Show detailed calculator')}
        </button>
      </div>

      {showDetails && (
        <>
          {/* Input Section */}
          <div style={styles.inputSection}>
            <div style={styles.inputCard}>
              <h3 style={styles.cardTitle}>
                {t('組織規模', 'Organization Scale')}
              </h3>
              <Slider label={t('租戶數量', 'Tenant Count')} value={tenants} onChange={setTenants} min={1} max={500} />
              <Slider label={t('啟用 Rule Pack 數', 'Active Rule Packs')} value={packs} onChange={setPacks} min={1} max={20} />
              <Slider label={t('On-call 人員數', 'On-call Staff')} value={oncallStaff} onChange={setOncallStaff} min={1} max={20} />
              <Slider label={t('平均時薪 (USD)', 'Hourly Rate (USD)')} value={hourlyRate} onChange={setHourlyRate} min={20} max={200} unit="$" />
            </div>

            <div style={styles.inputCard}>
              <h3 style={styles.cardTitle}>
                {t('配置變更', 'Config Changes')}
              </h3>
              <Slider label={t('每次變更耗時 (分)', 'Minutes per Change')} value={changeMinutes} onChange={setChangeMinutes} min={5} max={120} unit={t('分', 'min')} />
              <Slider label={t('每月變更次數', 'Changes per Month')} value={changesPerMonth} onChange={setChangesPerMonth} min={1} max={30} />
              <Slider label={t('手動 Onboard 耗時 (分)', 'Manual Onboard (min)')} value={manualOnboardMinutes} onChange={setManualOnboardMinutes} min={15} max={480} unit={t('分', 'min')} />
            </div>

            <div style={styles.inputCard}>
              <h3 style={styles.cardTitle}>
                {t('告警風暴', 'Alert Storms')}
              </h3>
              <Slider label={t('每月風暴次數', 'Storms per Month')} value={stormsPerMonth} onChange={setStormsPerMonth} min={0} max={30} />
              <Slider label={t('每次平均告警數', 'Avg Alerts per Storm')} value={avgAlertsPerStorm} onChange={setAvgAlertsPerStorm} min={1} max={100} />
            </div>
          </div>

          {/* Results Section */}
          <div style={styles.resultsSection}>
            <MetricCard
              title={t('年度節省', 'Annual Savings')}
              value={fullResults.annual.total}
              unit="USD"
              subtitle={t('三項合計', 'Combined total')}
              color="green"
            />
            <MetricCard
              title={t('規則維護節省', 'Rule Maintenance Saved')}
              value={fullResults.rule.savedHours}
              unit={t('小時/月', 'hrs/mo')}
              subtitle={`${fullResults.rule.reduction.toFixed(0)}% ${t('降幅', 'reduction')}`}
              color="blue"
            />
            <MetricCard
              title={t('告警降幅', 'Alert Reduction')}
              value={fullResults.storm.reducedAlerts}
              unit={t('則/月', '/mo')}
              subtitle={`${fullResults.storm.reductionPct.toFixed(0)}% ${t('壓制率', 'suppressed')}`}
              color="amber"
            />
            <MetricCard
              title={t('Onboard 加速', 'Onboard Speedup')}
              value={fullResults.ttm.reduction}
              unit="%"
              subtitle={`${fullResults.ttm.totalSavedHours.toFixed(1)} ${t('小時節省', 'hrs saved')}`}
              color="purple"
            />
          </div>

          {/* Annual Breakdown Chart */}
          <div style={styles.chartSection}>
            <h3 style={styles.chartTitle}>
              {t('年度節省明細', 'Annual Savings Breakdown')}
            </h3>
            <BarChart data={[
              {
                label: t('規則維護 O(N×M) → O(M)', 'Rule Maintenance O(N×M) → O(M)'),
                value: fullResults.annual.ruleAnnual,
                color: 'blue',
              },
              {
                label: t('告警風暴壓制（On-call 效率）', 'Alert Storm Suppression (On-call Efficiency)'),
                value: fullResults.annual.alertAnnual,
                color: 'amber',
              },
              {
                label: t('Onboard 自動化 (scaffold + migrate)', 'Onboard Automation (scaffold + migrate)'),
                value: fullResults.annual.ttmAnnual,
                color: 'purple',
              },
            ]} />
          </div>

          {/* Methodology Note */}
          <div style={styles.methodologySection}>
            <details>
              <summary style={styles.summaryTrigger}>
                {t('計算方法說明', 'Methodology')}
              </summary>
              <div style={styles.methodologyDetails}>
                <p style={styles.methodologyP}>{t(
                  '規則維護：傳統 O(N×M) 模型（N 租戶 × M Pack × 每次耗時 × 月次數）vs 平台 O(M) 模型（M Pack × 耗時 × 次數）。',
                  'Rule Maintenance: Traditional O(N×M) model (N tenants × M packs × time × monthly changes) vs Platform O(M) model (M packs × time × changes).'
                )}</p>
                <p style={styles.methodologyP}>{t(
                  '告警壓制：Severity Dedup ~40% + Maintenance Mode ~25% + Silent Mode ~15%，含遞減效果修正。',
                  'Alert Suppression: Severity Dedup ~40% + Maintenance Mode ~25% + Silent Mode ~15%, with diminishing returns adjustment.'
                )}</p>
                <p style={styles.methodologyP}>{t(
                  'Onboard：手動 YAML 撰寫 + 驗證 vs da-tools scaffold + validate-config + generate-routes（~5 分鐘）。',
                  'Onboard: Manual YAML authoring + validation vs da-tools scaffold + validate-config + generate-routes (~5 min).'
                )}</p>
                <p style={styles.methodologyP}>{t(
                  '年度 TCO：規則維護 ×12 月 + Onboard 一次性 + 告警壓制 × On-call 人數 × 20% 效率回收 ×12 月。',
                  'Annual TCO: Rule maintenance ×12mo + Onboard one-time + Alert reduction × on-call staff × 20% efficiency recovery ×12mo.'
                )}</p>
              </div>
            </details>
          </div>
        </>
      )}
    </div>
  );
}
