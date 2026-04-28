---
title: "Alert Simulator"
tags: [simulation, alerts, dedup]
audience: ["domain-expert", tenant]
version: v2.7.0
lang: en
related: [alert-timeline, runbook-viewer, config-lint]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

const ALERT_DEFS = {
  mysql_connections:          { alert: 'MariaDBHighConnections',          severity: 'warning',  label: 'MySQL Connections' },
  mysql_connections_critical: { alert: 'MariaDBHighConnectionsCritical',  severity: 'critical', label: 'MySQL Connections (Critical)' },
  mysql_cpu:                  { alert: 'MariaDBHighCPU',                  severity: 'warning',  label: 'MySQL CPU %' },
  pg_connections:             { alert: 'PostgreSQLHighConnections',       severity: 'warning',  label: 'PG Connections' },
  pg_connections_critical:    { alert: 'PostgreSQLHighConnectionsCritical', severity: 'critical', label: 'PG Connections (Critical)' },
  pg_cache_hit_ratio:         { alert: 'PostgreSQLLowCacheHit',          severity: 'warning',  label: 'PG Cache Hit %', inverted: true },
  redis_memory:               { alert: 'RedisHighMemory',                severity: 'warning',  label: 'Redis Memory %' },
  redis_memory_critical:      { alert: 'RedisHighMemoryCritical',        severity: 'critical', label: 'Redis Memory % (Critical)' },
  redis_evictions:            { alert: 'RedisHighEvictions',             severity: 'warning',  label: 'Redis Evictions/s' },
  kafka_lag:                  { alert: 'KafkaHighConsumerLag',           severity: 'warning',  label: 'Kafka Consumer Lag' },
  kafka_lag_critical:         { alert: 'KafkaHighConsumerLagCritical',   severity: 'critical', label: 'Kafka Lag (Critical)' },
};

const DEFAULT_CONFIG = {
  mysql_connections: '100',
  mysql_connections_critical: '200',
  mysql_cpu: '80',
  redis_memory: '80',
  redis_memory_critical: '95',
};

const DEFAULT_METRICS = {
  mysql_connections: 120,
  mysql_cpu: 45,
  redis_memory: 72,
};

function simulate(config, metrics, dedupEnabled) {
  const firing = [];
  const suppressed = [];
  const ok = [];

  Object.entries(config).forEach(([key, thresholdStr]) => {
    const def = ALERT_DEFS[key];
    if (!def) return;
    const threshold = parseFloat(thresholdStr);
    if (isNaN(threshold)) return;
    const current = metrics[key];
    if (current === undefined || current === '') return;
    const val = parseFloat(current);

    const wouldFire = def.inverted ? val < threshold : val > threshold;
    if (wouldFire) {
      firing.push({ key, def, threshold, current: val });
    } else {
      ok.push({ key, def, threshold, current: val });
    }
  });

  // Severity dedup: if critical fires, suppress matching warning
  if (dedupEnabled) {
    const criticalFiring = new Set(firing.filter(f => f.def.severity === 'critical').map(f => f.key.replace('_critical', '')));
    firing.forEach(f => {
      if (f.def.severity === 'warning' && criticalFiring.has(f.key)) {
        suppressed.push({ ...f, reason: 'Suppressed by severity dedup (critical alert active)' });
      }
    });
    const suppressedKeys = new Set(suppressed.map(s => s.key));
    return {
      firing: firing.filter(f => !suppressedKeys.has(f.key)),
      suppressed,
      ok,
    };
  }

  return { firing, suppressed: [], ok };
}

export default function AlertSimulator() {
  const [config, setConfig] = useState(DEFAULT_CONFIG);
  const [metrics, setMetrics] = useState(DEFAULT_METRICS);
  const [dedupEnabled, setDedupEnabled] = useState(true);
  const [routingType, setRoutingType] = useState('slack');

  const result = useMemo(() => simulate(config, metrics, dedupEnabled), [config, metrics, dedupEnabled]);

  const updateConfig = (key, val) => setConfig(prev => ({ ...prev, [key]: val }));
  const removeConfig = (key) => setConfig(prev => { const n = { ...prev }; delete n[key]; return n; });
  const updateMetric = (key, val) => setMetrics(prev => ({ ...prev, [key]: val }));

  const addMetric = (key) => {
    if (!config[key]) updateConfig(key, '100');
    if (metrics[key] === undefined) updateMetric(key, 0);
  };

  const availableKeys = Object.keys(ALERT_DEFS).filter(k => !config[k]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-6xl mx-auto">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('告警模擬器', 'Alert Simulator')}</h1>
          <p className="text-slate-600">{t('輸入配置和指標值，查看哪些告警會觸發', 'Input config + metric values to see which alerts would fire, get suppressed, or stay OK')}</p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Left: Config + Metrics */}
          <div className="space-y-4">
            {/* Thresholds */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h3 className="text-sm font-semibold text-slate-900 mb-4">{t('閾值配置', 'Threshold Config')}</h3>
              <div className="space-y-3">
                {Object.entries(config).map(([key, val]) => {
                  const def = ALERT_DEFS[key];
                  return (
                    <div key={key} className="flex items-center gap-2">
                      <span className="text-xs font-mono text-slate-600 w-44 truncate" title={key}>{key}</span>
                      <input
                        type="number"
                        value={val}
                        onChange={(e) => updateConfig(key, e.target.value)}
                        className="w-24 px-2 py-1 text-sm border border-slate-300 rounded focus:outline-none focus:ring-2 focus:ring-blue-400"
                      />
                      <span className={`text-xs px-1.5 py-0.5 rounded ${def?.severity === 'critical' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'}`}>
                        {def?.severity || '?'}
                      </span>
                      <button onClick={() => removeConfig(key)} className="text-slate-400 hover:text-red-500 text-xs">✕</button>
                    </div>
                  );
                })}
              </div>
              {availableKeys.length > 0 && (
                <div className="mt-3">
                  <select
                    onChange={(e) => { if (e.target.value) { addMetric(e.target.value); e.target.value = ''; } }}
                    className="text-xs px-2 py-1 border border-slate-300 rounded bg-white text-slate-600"
                    defaultValue=""
                  >
                    <option value="">+ {t('加入指標', 'Add metric...')}</option>
                    {availableKeys.map(k => <option key={k} value={k}>{k} ({ALERT_DEFS[k].label})</option>)}
                  </select>
                </div>
              )}
            </div>

            {/* Simulated Metric Values */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h3 className="text-sm font-semibold text-slate-900 mb-4">{t('模擬指標值', 'Simulated Metric Values')}</h3>
              <p className="text-xs text-slate-500 mb-3">{t('這些代表你的服務當前的實際值', 'These represent the current actual values from your services')}</p>
              <div className="space-y-3">
                {Object.keys(config).map(key => {
                  const def = ALERT_DEFS[key];
                  const val = metrics[key] !== undefined ? metrics[key] : '';
                  return (
                    <div key={key} className="flex items-center gap-2">
                      <span className="text-xs text-slate-600 w-44 truncate">{def?.label || key}</span>
                      <input
                        type="number"
                        value={val}
                        onChange={(e) => updateMetric(key, e.target.value)}
                        placeholder="current value"
                        className="w-24 px-2 py-1 text-sm border border-slate-300 rounded focus:outline-none focus:ring-2 focus:ring-blue-400"
                      />
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Options */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <div className="flex items-center gap-4">
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={dedupEnabled} onChange={(e) => setDedupEnabled(e.target.checked)} className="w-4 h-4 rounded" />
                  <span className="text-slate-700">{t('嚴重度去重', 'Severity Dedup')}</span>
                </label>
                <select value={routingType} onChange={(e) => setRoutingType(e.target.value)} className="text-xs px-2 py-1 border border-slate-300 rounded bg-white">
                  <option value="slack">Slack</option>
                  <option value="webhook">Webhook</option>
                  <option value="email">Email</option>
                  <option value="teams">Teams</option>
                  <option value="pagerduty">PagerDuty</option>
                </select>
              </div>
            </div>
          </div>

          {/* Right: Results */}
          <div className="space-y-4">
            {/* Summary */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('模擬結果', 'Simulation Results')}</h3>
              <div className="grid grid-cols-3 gap-3 text-center">
                <div className="bg-red-50 border border-red-200 rounded-lg p-3">
                  <div className="text-2xl font-bold text-red-600">{result.firing.length}</div>
                  <div className="text-xs text-red-700">{t('觸發中', 'Firing')}</div>
                </div>
                <div className="bg-purple-50 border border-purple-200 rounded-lg p-3">
                  <div className="text-2xl font-bold text-purple-600">{result.suppressed.length}</div>
                  <div className="text-xs text-purple-700">{t('已抑制', 'Suppressed')}</div>
                </div>
                <div className="bg-green-50 border border-green-200 rounded-lg p-3">
                  <div className="text-2xl font-bold text-green-600">{result.ok.length}</div>
                  <div className="text-xs text-green-700">OK</div>
                </div>
              </div>
            </div>

            {/* Firing Alerts */}
            {result.firing.length > 0 && (
              <div className="bg-white rounded-xl shadow-sm border border-red-200 p-6">
                <h4 className="text-sm font-semibold text-red-700 mb-3">🔴 {t('觸發中的告警', 'Firing Alerts')}</h4>
                <div className="space-y-2">
                  {result.firing.map(f => (
                    <div key={f.key} className="bg-red-50 rounded-lg p-3 text-sm">
                      <div className="flex items-center justify-between mb-1">
                        <span className="font-mono font-semibold text-red-900">{f.def.alert}</span>
                        <span className={`text-xs px-1.5 py-0.5 rounded ${f.def.severity === 'critical' ? 'bg-red-200 text-red-800' : 'bg-amber-200 text-amber-800'}`}>{f.def.severity}</span>
                      </div>
                      <div className="text-xs text-red-700">
                        {t('當前值', 'Current')}: <strong>{f.current}</strong> {f.def.inverted ? '<' : '>'} {t('閾值', 'threshold')}: <strong>{f.threshold}</strong>
                      </div>
                      <div className="text-xs text-slate-500 mt-1">
                        → {t('通知發送到', 'Notifies via')} <strong>{routingType}</strong>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Suppressed */}
            {result.suppressed.length > 0 && (
              <div className="bg-white rounded-xl shadow-sm border border-purple-200 p-6">
                <h4 className="text-sm font-semibold text-purple-700 mb-3">🟣 {t('被抑制的告警', 'Suppressed Alerts')}</h4>
                <div className="space-y-2">
                  {result.suppressed.map(s => (
                    <div key={s.key} className="bg-purple-50 rounded-lg p-3 text-sm">
                      <div className="font-mono font-semibold text-purple-900 mb-1">{s.def.alert}</div>
                      <div className="text-xs text-purple-700">{s.reason}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* OK */}
            {result.ok.length > 0 && (
              <div className="bg-white rounded-xl shadow-sm border border-green-200 p-6">
                <h4 className="text-sm font-semibold text-green-700 mb-3">🟢 OK</h4>
                <div className="space-y-1">
                  {result.ok.map(o => (
                    <div key={o.key} className="flex items-center justify-between text-xs text-slate-600 py-1">
                      <span className="font-mono">{o.def.alert}</span>
                      <span>{o.current} {o.def.inverted ? '≥' : '≤'} {o.threshold}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* No metrics input */}
            {result.firing.length === 0 && result.ok.length === 0 && (
              <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 text-center">
                <div className="text-3xl mb-3">📊</div>
                <p className="text-sm text-slate-500">{t('輸入模擬指標值以查看結果', 'Enter simulated metric values to see results')}</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
