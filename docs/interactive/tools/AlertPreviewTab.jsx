---
title: "Alert Preview Tab"
tags: [self-service, alerts, internal]
audience: ["platform-engineer", "tenant"]
version: v2.3.0
lang: en
---

import React, { useState, useMemo, useCallback, useEffect } from 'react';

const t = window.__t || ((zh, en) => en);
const {
  RULE_PACK_DATA, generateSampleYaml, parseYaml, simulateAlerts,
  RulePackSelector,
} = window.__portalShared;

function AlertPreviewTab() {
  const [selectedPacks, setSelectedPacks] = useState(['mariadb', 'kubernetes']);
  const [yaml, setYaml] = useState('');
  const [metrics, setMetrics] = useState({});
  const [alerts, setAlerts] = useState(null);
  const [recData, setRecData] = useState(null);

  // Build metric sliders from selected packs
  const packMetrics = useMemo(() => {
    const result = {};
    for (const packId of selectedPacks) {
      const pack = RULE_PACK_DATA[packId];
      if (!pack || !pack.defaults) continue;
      for (const [key, meta] of Object.entries(pack.defaults)) {
        result[key] = {
          current: meta.value * 0.9,
          threshold: meta.value,
          unit: meta.unit,
          packLabel: pack.label,
          packId,
        };
      }
    }
    return result;
  }, [selectedPacks]);

  // Initialize YAML and metrics when packs change
  useEffect(() => {
    setYaml(generateSampleYaml(selectedPacks, false));
    const initMetrics = {};
    for (const [key, meta] of Object.entries(packMetrics)) {
      initMetrics[key] = {
        current: Math.round(meta.threshold * 0.9),
        unit: meta.unit,
        packLabel: meta.packLabel,
      };
    }
    setMetrics(initMetrics);
    setAlerts(null);
  }, [selectedPacks]);

  // Load recommendation data on mount
  useEffect(() => {
    fetch('../../assets/recommendation-data.json')
      .then(res => res.json())
      .then(data => setRecData(data))
      .catch(err => console.error('Failed to load recommendation data:', err));
  }, []);

  const simulate = useCallback(() => {
    const { config } = parseYaml(yaml);
    const result = simulateAlerts(config, metrics);
    setAlerts(result);
  }, [yaml, metrics]);

  const updateMetric = (key, value) => {
    setMetrics(prev => ({
      ...prev,
      [key]: { ...prev[key], current: parseFloat(value) || 0 },
    }));
  };

  // Group alerts by pack
  const groupedAlerts = useMemo(() => {
    if (!alerts) return {};
    const groups = {};
    for (const a of alerts) {
      const label = a.packLabel || t('其他', 'Other');
      if (!groups[label]) groups[label] = [];
      groups[label].push(a);
    }
    return groups;
  }, [alerts]);

  // Determine slider max based on unit
  const getSliderMax = (key, meta) => {
    if (meta.unit === '%') return 100;
    const threshold = packMetrics[key]?.threshold || 100;
    return Math.max(threshold * 2, 200);
  };

  return (
    <div>
      <h3 className="text-lg font-semibold mb-3">
        {t('多指標告警預覽', 'Multi-Metric Alert Preview')}
      </h3>
      <p className="text-sm text-gray-600 mb-4">
        {t('選擇 Rule Pack，同時預覽所有相關 metric 的告警觸發狀態。調整 slider 觀察聯動效果。',
           'Select Rule Packs and preview all related metric alert states simultaneously. Adjust sliders to observe interactions.')}
      </p>

      {/* Rule Pack Selector */}
      <div className="mb-4 p-3 bg-gray-50 rounded-lg border">
        <div className="text-sm font-medium text-gray-700 mb-2">
          {t('選擇 Rule Pack', 'Select Rule Packs')}
        </div>
        <RulePackSelector selected={selectedPacks} onChange={setSelectedPacks} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* YAML config */}
        <div>
          <h4 className="text-sm font-medium text-gray-700 mb-2">
            {t('Tenant 配置', 'Tenant Config')}
          </h4>
          <textarea
            value={yaml}
            onChange={(e) => setYaml(e.target.value)}
            className="w-full h-48 font-mono text-xs p-2 border rounded-lg bg-gray-50"
          />
        </div>

        {/* Metric sliders grouped by pack */}
        <div>
          <h4 className="text-sm font-medium text-gray-700 mb-2">
            {t('模擬指標值', 'Simulated Metrics')}
            <span className="ml-2 text-gray-400 text-xs font-normal">
              {t(`共 ${Object.keys(metrics).length} 個指標`, `${Object.keys(metrics).length} metrics`)}
            </span>
          </h4>
          <div className="space-y-3 max-h-64 overflow-y-auto pr-1">
            {selectedPacks.map(packId => {
              const pack = RULE_PACK_DATA[packId];
              if (!pack || !pack.defaults) return null;
              const packKeys = Object.keys(pack.defaults).filter(k => metrics[k]);
              if (packKeys.length === 0) return null;
              return (
                <div key={packId}>
                  <div className="text-xs font-medium text-gray-500 mb-1">{pack.label}</div>
                  {packKeys.map(key => {
                    const val = metrics[key];
                    if (!val) return null;
                    const sliderMax = getSliderMax(key, val);
                    return (
                      <div key={key} className="flex items-center gap-2 mb-1">
                        <label className="text-xs font-mono w-44 text-gray-600 truncate" title={key}>{key}</label>
                        <input
                          type="range"
                          min="0" max={sliderMax}
                          value={val.current}
                          onChange={(e) => updateMetric(key, e.target.value)}
                          className="flex-1"
                        />
                        <span className="text-xs font-mono w-20 text-right">
                          {val.current}{val.unit === '%' ? '%' : ` ${val.unit}`}
                        </span>
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="mt-3 flex gap-2">
        <button
          onClick={simulate}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
        >
          {t('模擬所有告警', 'Simulate All Alerts')}
        </button>
        {recData?.recommendations && (
          <button
            onClick={() => {
              const { config } = parseYaml(yaml);
              if (!config || !config.rules) {
                alert(t('無法解析 YAML 配置', 'Unable to parse YAML config'));
                return;
              }
              let updated = yaml;
              for (const [metric, rec] of Object.entries(recData.recommendations)) {
                const thresholdPattern = new RegExp(`(${metric}:\\s*)([\\d.]+)`, 'g');
                const criticalPattern = new RegExp(`(${metric}_critical:\\s*)([\\d.]+)`, 'g');
                updated = updated.replace(thresholdPattern, `$1${rec.recommended}`);
                if (recData.recommendations[`${metric}_critical`]) {
                  updated = updated.replace(criticalPattern, `$1${recData.recommendations[`${metric}_critical`].recommended}`);
                }
              }
              setYaml(updated);
            }}
            className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors"
          >
            {t('套用推薦值', 'Apply Recommended Values')}
          </button>
        )}
      </div>

      {alerts && (
        <div className="mt-4">
          {/* Summary bar */}
          <div className="flex gap-3 mb-3 text-sm">
            <span className="px-2 py-1 bg-red-100 text-red-800 rounded">
              {t('觸發中', 'Firing')}: {alerts.filter(a => a.firing).length}
            </span>
            <span className="px-2 py-1 bg-green-100 text-green-800 rounded">
              OK: {alerts.filter(a => a.severity === 'ok').length}
            </span>
            <span className="px-2 py-1 bg-gray-100 text-gray-600 rounded">
              {t('無閾值/已禁用', 'No threshold/Disabled')}: {alerts.filter(a => !a.threshold).length}
            </span>
          </div>

          {/* Grouped alert results */}
          {Object.entries(groupedAlerts).map(([group, groupAlerts]) => (
            <div key={group} className="mb-4">
              <div className="text-sm font-medium text-gray-700 mb-1 flex items-center gap-2">
                <span>{group}</span>
                <span className="text-xs text-gray-400">({groupAlerts.length} {t('指標', 'metrics')})</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-100">
                      <th className="px-3 py-1.5 text-left text-xs">{t('指標', 'Metric')}</th>
                      <th className="px-3 py-1.5 text-right text-xs">{t('目前值', 'Current')}</th>
                      <th className="px-3 py-1.5 text-center text-xs">{t('閾值', 'Threshold')}</th>
                      <th className="px-3 py-1.5 text-center text-xs">{t('推薦值', 'Recommended')}</th>
                      <th className="px-3 py-1.5 text-left text-xs">{t('狀態列', 'Bar')}</th>
                      <th className="px-3 py-1.5 text-center text-xs">{t('狀態', 'Status')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {groupAlerts.map((a, i) => {
                      const barWidthStyle = { width: `${Math.min(100, (a.current / (a.threshold * 1.5)) * 100)}%` };
                      const thresholdMarkerStyle = { left: `${Math.min(100, (a.threshold / (a.threshold * 1.5)) * 100)}%` };
                      const criticalMarkerStyle = { left: `${Math.min(100, (a.critical_threshold / (a.threshold * 1.5)) * 100)}%` };
                      return (
                      <tr key={i} className={
                        a.critical_firing ? 'bg-red-50' :
                        a.firing ? 'bg-yellow-50' : 'bg-white'
                      }>
                        <td className="px-3 py-1.5 font-mono text-xs">{a.metric}</td>
                        <td className="px-3 py-1.5 text-right text-xs">
                          {a.current != null ? a.current : '-'}
                        </td>
                        <td className="px-3 py-1.5 text-center text-xs">
                          {a.threshold != null ? (
                            <span>
                              {a.threshold}
                              {a.critical_threshold ? ` / ${a.critical_threshold}` : ''}
                            </span>
                          ) : '-'}
                        </td>
                        <td className="px-3 py-1.5 text-center text-xs">
                          {recData?.recommendations?.[a.metric] ? (
                            <div className="flex items-center gap-1 justify-center">
                              <span
                                title={`P50: ${recData.recommendations[a.metric].p50}, P95: ${recData.recommendations[a.metric].p95}, P99: ${recData.recommendations[a.metric].p99}`}
                              >
                                {recData.recommendations[a.metric].recommended}
                              </span>
                              <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                                recData.recommendations[a.metric].confidence === 'high' ? 'bg-green-100 text-green-800' :
                                recData.recommendations[a.metric].confidence === 'medium' ? 'bg-yellow-100 text-yellow-800' :
                                'bg-red-100 text-red-800'
                              }`}>
                                {recData.recommendations[a.metric].confidence}
                              </span>
                              {a.threshold != null && Math.abs(a.threshold - recData.recommendations[a.metric].recommended) / recData.recommendations[a.metric].recommended > 0.3 && (
                                <span title={t('閾值與推薦值差異 > 30%', 'Threshold differs >30% from recommended')}>⚠️</span>
                              )}
                            </div>
                          ) : '-'}
                        </td>
                        <td className="px-3 py-1.5">
                          {a.threshold != null && (
                            <div className="relative h-3 bg-gray-200 rounded-full overflow-hidden w-32">
                              <div
                                className={`absolute top-0 left-0 h-full rounded-full transition-all ${
                                  a.critical_firing ? 'bg-red-500' :
                                  a.firing ? 'bg-yellow-500' : 'bg-green-500'
                                }`}
                                style={barWidthStyle}
                              />
                              {/* Threshold marker */}
                              <div
                                className="absolute top-0 h-full w-0.5 bg-gray-600"
                                style={thresholdMarkerStyle}
                                title={`threshold: ${a.threshold}`}
                              />
                              {a.critical_threshold && (
                                <div
                                  className="absolute top-0 h-full w-0.5 bg-red-700"
                                  style={criticalMarkerStyle}
                                  title={`critical: ${a.critical_threshold}`}
                                />
                              )}
                              {recData?.recommendations?.[a.metric]?.recommended != null && (
                                <div
                                  className="absolute top-0 h-full w-1 bg-green-600 opacity-70"
                                  style={{ left: `${Math.min(100, (recData.recommendations[a.metric].recommended / (a.threshold * 1.5)) * 100)}%` }}
                                  title={`${t('推薦值', 'Recommended')}: ${recData.recommendations[a.metric].recommended}`}
                                />
                              )}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-1.5 text-center">
                          {a.severity === 'critical' ? (
                            <span className="px-2 py-0.5 bg-red-100 text-red-800 rounded-full text-xs font-medium">CRITICAL</span>
                          ) : a.severity === 'warning' ? (
                            <span className="px-2 py-0.5 bg-yellow-100 text-yellow-800 rounded-full text-xs font-medium">FIRING</span>
                          ) : a.severity === 'disabled' ? (
                            <span className="px-2 py-0.5 bg-gray-100 text-gray-500 rounded-full text-xs font-medium">DISABLED</span>
                          ) : a.severity === 'no-threshold' ? (
                            <span className="px-2 py-0.5 bg-gray-100 text-gray-400 rounded-full text-xs font-medium">—</span>
                          ) : (
                            <span className="px-2 py-0.5 bg-green-100 text-green-800 rounded-full text-xs font-medium">OK</span>
                          )}
                        </td>
                      </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ))}

          {/* Inhibit rule explanation */}
          {alerts.some(a => a.critical_firing) && (
            <div className="mt-3 p-3 rounded-lg bg-purple-50 border border-purple-200 text-xs text-purple-800">
              <span className="font-medium">{t('Severity Dedup 生效', 'Severity Dedup Active')}:</span>{' '}
              {t('有 CRITICAL 等級觸發 — Alertmanager inhibit rule 將自動抑制對應的 WARNING 告警，通知管道只收到一次最高嚴重度。',
                 'CRITICAL severity firing — Alertmanager inhibit rules will suppress corresponding WARNING alerts. Notification channel receives only the highest severity.')}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* Register for dependency loading */
window.__AlertPreviewTab = AlertPreviewTab;
