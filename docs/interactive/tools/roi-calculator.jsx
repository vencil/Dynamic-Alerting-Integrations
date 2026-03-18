---
title: "ROI Calculator"
tags: [roi, cost, adoption, evaluation]
audience: [platform-engineer]
version: v2.2.0
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
    <div className="mb-4">
      <div className="flex justify-between text-sm mb-1">
        <span className="text-slate-700">{label}</span>
        <span className="font-mono text-blue-600 font-medium">{value.toLocaleString()}{unit}</span>
      </div>
      <input
        type="range" min={min} max={max} step={step} value={value}
        onChange={e => onChange(Number(e.target.value))}
        className="w-full h-2 bg-slate-200 rounded-lg appearance-none cursor-pointer accent-blue-600"
      />
      <div className="flex justify-between text-xs text-slate-400 mt-0.5">
        <span>{min}{unit}</span><span>{max}{unit}</span>
      </div>
    </div>
  );
}

function MetricCard({ title, value, unit, subtitle, color = 'blue' }) {
  const colors = {
    blue: 'bg-blue-50 border-blue-200 text-blue-700',
    green: 'bg-green-50 border-green-200 text-green-700',
    amber: 'bg-amber-50 border-amber-200 text-amber-700',
    purple: 'bg-purple-50 border-purple-200 text-purple-700',
  };
  return (
    <div className={`rounded-lg border p-4 ${colors[color] || colors.blue}`}>
      <div className="text-xs font-medium opacity-70 mb-1">{title}</div>
      <div className="text-2xl font-bold font-mono">
        {typeof value === 'number' ? value.toLocaleString(undefined, { maximumFractionDigits: 1 }) : value}
        <span className="text-sm font-normal ml-1">{unit}</span>
      </div>
      {subtitle && <div className="text-xs mt-1 opacity-60">{subtitle}</div>}
    </div>
  );
}

function BarChart({ data }) {
  const maxVal = Math.max(...data.map(d => d.value), 1);
  return (
    <div className="space-y-3">
      {data.map((d, i) => {
        const barStyle = { width: `${Math.max((d.value / maxVal) * 100, 2)}%` };
        return (
        <div key={i}>
          <div className="flex justify-between text-xs text-slate-600 mb-1">
            <span>{d.label}</span>
            <span className="font-mono">${d.value.toLocaleString(undefined, { maximumFractionDigits: 0 })}</span>
          </div>
          <div className="h-4 bg-slate-100 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-500 ${d.color || 'bg-blue-500'}`}
              style={barStyle}
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
  // Input parameters
  const [tenants, setTenants] = useState(20);
  const [packs, setPacks] = useState(PACK_COUNT);
  const [changeMinutes, setChangeMinutes] = useState(30);
  const [changesPerMonth, setChangesPerMonth] = useState(4);
  const [oncallStaff, setOncallStaff] = useState(3);
  const [hourlyRate, setHourlyRate] = useState(75);
  const [stormsPerMonth, setStormsPerMonth] = useState(8);
  const [avgAlertsPerStorm, setAvgAlertsPerStorm] = useState(15);
  const [manualOnboardMinutes, setManualOnboardMinutes] = useState(120);

  // Calculations
  const results = useMemo(() => {
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

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-8">
      {/* Header */}
      <div>
        <h2 className="text-2xl font-bold text-slate-800">
          {t('採用效益試算器', 'ROI Calculator')}
        </h2>
        <p className="text-sm text-slate-500 mt-1">
          {t(
            '調整下方參數，即時計算 Dynamic Alerting 平台的採用效益。',
            'Adjust parameters below to calculate adoption benefits in real-time.'
          )}
        </p>
        <div className="text-xs text-slate-400 mt-1">
          {t(
            `平台數據：${PACK_COUNT} 個 Rule Pack、${TOTAL_RULES} 條規則`,
            `Platform data: ${PACK_COUNT} Rule Packs, ${TOTAL_RULES} rules`
          )}
        </div>
      </div>

      {/* Input Section */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="bg-white rounded-xl border border-slate-200 p-5">
          <h3 className="text-sm font-semibold text-slate-700 mb-4">
            {t('組織規模', 'Organization Scale')}
          </h3>
          <Slider label={t('租戶數量', 'Tenant Count')} value={tenants} onChange={setTenants} min={1} max={500} />
          <Slider label={t('啟用 Rule Pack 數', 'Active Rule Packs')} value={packs} onChange={setPacks} min={1} max={20} />
          <Slider label={t('On-call 人員數', 'On-call Staff')} value={oncallStaff} onChange={setOncallStaff} min={1} max={20} />
          <Slider label={t('平均時薪 (USD)', 'Hourly Rate (USD)')} value={hourlyRate} onChange={setHourlyRate} min={20} max={200} unit="$" />
        </div>

        <div className="bg-white rounded-xl border border-slate-200 p-5">
          <h3 className="text-sm font-semibold text-slate-700 mb-4">
            {t('配置變更', 'Config Changes')}
          </h3>
          <Slider label={t('每次變更耗時 (分)', 'Minutes per Change')} value={changeMinutes} onChange={setChangeMinutes} min={5} max={120} unit={t('分', 'min')} />
          <Slider label={t('每月變更次數', 'Changes per Month')} value={changesPerMonth} onChange={setChangesPerMonth} min={1} max={30} />
          <Slider label={t('手動 Onboard 耗時 (分)', 'Manual Onboard (min)')} value={manualOnboardMinutes} onChange={setManualOnboardMinutes} min={15} max={480} unit={t('分', 'min')} />
        </div>

        <div className="bg-white rounded-xl border border-slate-200 p-5">
          <h3 className="text-sm font-semibold text-slate-700 mb-4">
            {t('告警風暴', 'Alert Storms')}
          </h3>
          <Slider label={t('每月風暴次數', 'Storms per Month')} value={stormsPerMonth} onChange={setStormsPerMonth} min={0} max={30} />
          <Slider label={t('每次平均告警數', 'Avg Alerts per Storm')} value={avgAlertsPerStorm} onChange={setAvgAlertsPerStorm} min={1} max={100} />
        </div>
      </div>

      {/* Results Section */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <MetricCard
          title={t('年度節省', 'Annual Savings')}
          value={results.annual.total}
          unit="USD"
          subtitle={t('三項合計', 'Combined total')}
          color="green"
        />
        <MetricCard
          title={t('規則維護節省', 'Rule Maintenance Saved')}
          value={results.rule.savedHours}
          unit={t('小時/月', 'hrs/mo')}
          subtitle={`${results.rule.reduction.toFixed(0)}% ${t('降幅', 'reduction')}`}
          color="blue"
        />
        <MetricCard
          title={t('告警降幅', 'Alert Reduction')}
          value={results.storm.reducedAlerts}
          unit={t('則/月', '/mo')}
          subtitle={`${results.storm.reductionPct.toFixed(0)}% ${t('壓制率', 'suppressed')}`}
          color="amber"
        />
        <MetricCard
          title={t('Onboard 加速', 'Onboard Speedup')}
          value={results.ttm.reduction}
          unit="%"
          subtitle={`${results.ttm.totalSavedHours.toFixed(1)} ${t('小時節省', 'hrs saved')}`}
          color="purple"
        />
      </div>

      {/* Annual Breakdown Chart */}
      <div className="bg-white rounded-xl border border-slate-200 p-5">
        <h3 className="text-sm font-semibold text-slate-700 mb-4">
          {t('年度節省明細', 'Annual Savings Breakdown')}
        </h3>
        <BarChart data={[
          {
            label: t('規則維護 O(N×M) → O(M)', 'Rule Maintenance O(N×M) → O(M)'),
            value: results.annual.ruleAnnual,
            color: 'bg-blue-500',
          },
          {
            label: t('告警風暴壓制（On-call 效率）', 'Alert Storm Suppression (On-call Efficiency)'),
            value: results.annual.alertAnnual,
            color: 'bg-amber-500',
          },
          {
            label: t('Onboard 自動化 (scaffold + migrate)', 'Onboard Automation (scaffold + migrate)'),
            value: results.annual.ttmAnnual,
            color: 'bg-purple-500',
          },
        ]} />
      </div>

      {/* Methodology Note */}
      <div className="text-xs text-slate-400 border-t border-slate-100 pt-3">
        <details>
          <summary className="cursor-pointer hover:text-slate-600">
            {t('計算方法說明', 'Methodology')}
          </summary>
          <div className="mt-2 space-y-1">
            <p>{t(
              '規則維護：傳統 O(N×M) 模型（N 租戶 × M Pack × 每次耗時 × 月次數）vs 平台 O(M) 模型（M Pack × 耗時 × 次數）。',
              'Rule Maintenance: Traditional O(N×M) model (N tenants × M packs × time × monthly changes) vs Platform O(M) model (M packs × time × changes).'
            )}</p>
            <p>{t(
              '告警壓制：Severity Dedup ~40% + Maintenance Mode ~25% + Silent Mode ~15%，含遞減效果修正。',
              'Alert Suppression: Severity Dedup ~40% + Maintenance Mode ~25% + Silent Mode ~15%, with diminishing returns adjustment.'
            )}</p>
            <p>{t(
              'Onboard：手動 YAML 撰寫 + 驗證 vs da-tools scaffold + validate-config + generate-routes（~5 分鐘）。',
              'Onboard: Manual YAML authoring + validation vs da-tools scaffold + validate-config + generate-routes (~5 min).'
            )}</p>
            <p>{t(
              '年度 TCO：規則維護 ×12 月 + Onboard 一次性 + 告警壓制 × On-call 人數 × 20% 效率回收 ×12 月。',
              'Annual TCO: Rule maintenance ×12mo + Onboard one-time + Alert reduction × on-call staff × 20% efficiency recovery ×12mo.'
            )}</p>
          </div>
        </details>
      </div>
    </div>
  );
}
