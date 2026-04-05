---
title: "Threshold Calculator"
tags: [calculator, thresholds, yaml]
audience: [domain-expert, tenant]
version: v2.4.0
lang: en
related: [playground, alert-simulator, schema-explorer]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

const METRIC_PROFILES = {
  mysql_connections: {
    label: 'MySQL Connections',
    unit: 'connections',
    desc: 'Current number of active database connections',
    typical: { min: 10, max: 500, p50: 50, p90: 120, p95: 180, p99: 250 },
  },
  mysql_cpu: {
    label: 'MySQL CPU',
    unit: '%',
    desc: 'CPU usage percentage of the database process',
    typical: { min: 2, max: 100, p50: 25, p90: 60, p95: 75, p99: 90 },
  },
  pg_connections: {
    label: 'PostgreSQL Connections',
    unit: 'connections',
    desc: 'Active PostgreSQL connections',
    typical: { min: 5, max: 300, p50: 30, p90: 80, p95: 120, p99: 200 },
  },
  pg_cache_hit_ratio: {
    label: 'PG Cache Hit Ratio',
    unit: '%',
    desc: 'Buffer cache hit percentage (higher is better, threshold is minimum)',
    typical: { min: 70, max: 100, p50: 95, p90: 98, p95: 99, p99: 99.5 },
    inverted: true,
  },
  redis_memory: {
    label: 'Redis Memory',
    unit: '%',
    desc: 'Memory usage as percentage of maxmemory',
    typical: { min: 10, max: 100, p50: 40, p90: 70, p95: 80, p99: 92 },
  },
  redis_evictions: {
    label: 'Redis Evictions',
    unit: 'evictions/s',
    desc: 'Key eviction rate per second',
    typical: { min: 0, max: 5000, p50: 50, p90: 500, p95: 1000, p99: 3000 },
  },
  kafka_lag: {
    label: 'Kafka Consumer Lag',
    unit: 'messages',
    desc: 'Max consumer group lag in messages',
    typical: { min: 0, max: 1000000, p50: 1000, p90: 50000, p95: 100000, p99: 500000 },
  },
};

const PERCENTILES = ['p50', 'p90', 'p95', 'p99'];

function suggestThreshold(profile, percentile, customValues) {
  const cv = customValues || {};
  const base = cv[percentile] !== undefined ? cv[percentile] : profile.typical[percentile];
  // Warning = selected percentile + 15% headroom, Critical = +40% headroom
  const warning = profile.inverted ? Math.max(0, Math.round(base * 0.85)) : Math.round(base * 1.15);
  const critical = profile.inverted ? Math.max(0, Math.round(base * 0.7)) : Math.round(base * 1.4);
  return { warning, critical };
}

function generateYAML(selections) {
  const lines = ['tenants:', '  my-app:'];
  selections.forEach(s => {
    lines.push(`    ${s.metric}: "${s.warning}"`);
    lines.push(`    ${s.metric}_critical: "${s.critical}"`);
  });
  return lines.join('\n');
}

export default function ThresholdCalculator() {
  const [selectedMetric, setSelectedMetric] = useState('mysql_connections');
  const [selectedPercentile, setSelectedPercentile] = useState('p90');
  const [customValues, setCustomValues] = useState({});
  const [basket, setBasket] = useState([]);

  const profile = METRIC_PROFILES[selectedMetric];
  const thresholds = useMemo(
    () => suggestThreshold(profile, selectedPercentile, customValues),
    [selectedMetric, selectedPercentile, customValues]
  );

  const updateCustomValue = (pct, val) => {
    setCustomValues(prev => ({ ...prev, [pct]: val === '' ? undefined : Number(val) }));
  };

  const addToBasket = () => {
    const existing = basket.findIndex(b => b.metric === selectedMetric);
    const entry = { metric: selectedMetric, label: profile.label, warning: thresholds.warning, critical: thresholds.critical };
    if (existing >= 0) {
      setBasket(prev => prev.map((b, i) => i === existing ? entry : b));
    } else {
      setBasket(prev => [...prev, entry]);
    }
  };

  const removeFromBasket = (metric) => {
    setBasket(prev => prev.filter(b => b.metric !== metric));
  };

  const yaml = useMemo(() => basket.length > 0 ? generateYAML(basket) : '', [basket]);

  const copyYAML = () => {
    navigator.clipboard.writeText(yaml);
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-5xl mx-auto">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('閾值計算器', 'Threshold Calculator')}</h1>
          <p className="text-slate-600">{t('根據工作負載統計數據計算建議閾值', 'Calculate recommended thresholds based on workload statistics')}</p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
          <div className="lg:col-span-2 space-y-6">
            {/* Metric Selection */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('選擇指標', 'Select Metric')}</h3>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                {Object.entries(METRIC_PROFILES).map(([key, m]) => (
                  <button
                    key={key}
                    onClick={() => { setSelectedMetric(key); setCustomValues({}); }}
                    className={`text-left px-3 py-2 rounded-lg text-sm transition-colors ${
                      selectedMetric === key
                        ? 'bg-blue-600 text-white font-medium'
                        : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
                    }`}
                  >
                    {m.label}
                  </button>
                ))}
              </div>
              <p className="text-sm text-slate-500 mt-3">{profile.desc}</p>
            </div>

            {/* Percentile Sliders */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h3 className="text-sm font-semibold text-slate-900 mb-4">
                {t('百分位數值（可自訂）', 'Percentile Values (customizable)')}
              </h3>
              <div className="space-y-4">
                {PERCENTILES.map(pct => {
                  const val = customValues[pct] !== undefined ? customValues[pct] : profile.typical[pct];
                  return (
                    <div key={pct} className="flex items-center gap-4">
                      <span className={`w-10 text-sm font-mono font-semibold ${
                        pct === selectedPercentile ? 'text-blue-600' : 'text-slate-500'
                      }`}>{pct}</span>
                      <input
                        type="range"
                        min={profile.typical.min}
                        max={profile.typical.max}
                        value={val}
                        onChange={(e) => updateCustomValue(pct, e.target.value)}
                        className="flex-1 h-2 rounded-lg appearance-none cursor-pointer bg-slate-200"
                      />
                      <input
                        type="number"
                        value={val}
                        onChange={(e) => updateCustomValue(pct, e.target.value)}
                        className="w-24 text-right text-sm border border-slate-300 rounded-lg px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-400"
                      />
                      <span className="text-xs text-slate-400 w-20">{profile.unit}</span>
                    </div>
                  );
                })}
              </div>

              <div className="mt-4">
                <h4 className="text-sm font-semibold text-slate-700 mb-2">{t('目標百分位', 'Target Percentile')}</h4>
                <div className="flex gap-2">
                  {PERCENTILES.map(pct => (
                    <button
                      key={pct}
                      onClick={() => setSelectedPercentile(pct)}
                      className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                        selectedPercentile === pct
                          ? 'bg-blue-600 text-white'
                          : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                      }`}
                    >
                      {pct}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-slate-500 mt-2">
                  {profile.inverted
                    ? t('反向指標：閾值為最小值，低於此值觸發告警', 'Inverted metric: threshold is minimum, alerts fire below this value')
                    : t('閾值基於所選百分位 + 安全餘量（warning +15%, critical +40%）', 'Thresholds based on selected percentile + safety margin (warning +15%, critical +40%)')
                  }
                </p>
              </div>
            </div>

            {/* Calculated Thresholds */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h3 className="text-sm font-semibold text-slate-900 mb-4">{t('計算結果', 'Calculated Thresholds')}</h3>
              <div className="grid grid-cols-2 gap-4">
                <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 text-center">
                  <div className="text-3xl font-bold text-amber-700">{thresholds.warning}</div>
                  <div className="text-xs text-amber-600 mt-1 font-medium">WARNING</div>
                  <div className="text-xs text-slate-500">{profile.unit}</div>
                </div>
                <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-center">
                  <div className="text-3xl font-bold text-red-700">{thresholds.critical}</div>
                  <div className="text-xs text-red-600 mt-1 font-medium">CRITICAL</div>
                  <div className="text-xs text-slate-500">{profile.unit}</div>
                </div>
              </div>
              <button
                onClick={addToBasket}
                className="mt-4 w-full px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors"
              >
                {basket.some(b => b.metric === selectedMetric)
                  ? t('更新到配置', 'Update in Config')
                  : t('加入配置', 'Add to Config')}
              </button>
            </div>
          </div>

          {/* Right: YAML Output */}
          <div className="lg:col-span-1">
            <div className="sticky top-8 space-y-4">
              <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
                <h3 className="text-lg font-semibold text-slate-900 mb-3">{t('配置預覽', 'Config Preview')}</h3>
                {basket.length === 0 ? (
                  <p className="text-sm text-slate-400 italic">{t('尚未加入任何指標', 'No metrics added yet. Calculate thresholds and click "Add to Config".')}</p>
                ) : (
                  <>
                    <div className="space-y-2 mb-4">
                      {basket.map(b => (
                        <div key={b.metric} className="flex items-center justify-between text-sm">
                          <span className="text-slate-700">{b.label}</span>
                          <div className="flex items-center gap-2">
                            <span className="text-amber-600 font-mono text-xs">{b.warning}</span>
                            <span className="text-slate-300">/</span>
                            <span className="text-red-600 font-mono text-xs">{b.critical}</span>
                            <button
                              onClick={() => removeFromBasket(b.metric)}
                              className="text-slate-400 hover:text-red-500 text-xs ml-1"
                            >✕</button>
                          </div>
                        </div>
                      ))}
                    </div>
                    <pre className="bg-slate-900 text-slate-100 p-4 rounded-lg text-xs overflow-x-auto font-mono max-h-48 overflow-y-auto">
                      {yaml}
                    </pre>
                    <div className="flex gap-2 mt-3">
                      <button
                        onClick={copyYAML}
                        className="flex-1 px-3 py-2 bg-slate-700 text-white rounded-lg text-xs font-medium hover:bg-slate-600"
                      >
                        {t('複製 YAML', 'Copy YAML')}
                      </button>
                      <a
                        href={`../assets/jsx-loader.html?component=../playground.jsx#yaml=${btoa(unescape(encodeURIComponent(yaml)))}`}
                        className="flex-1 px-3 py-2 bg-green-600 text-white rounded-lg text-xs font-medium hover:bg-green-700 text-center"
                      >
                        {t('在 Playground 驗證', 'Validate in Playground')}
                      </a>
                    </div>
                  </>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
