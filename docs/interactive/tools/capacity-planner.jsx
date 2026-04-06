---
title: "Capacity Planner"
tags: [capacity, planning, estimation]
audience: [platform-engineer]
version: v2.5.0
lang: en
related: [architecture-quiz, rule-pack-matrix, dependency-graph]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

// --- Shared platform data (from platform-data.json via jsx-loader) ---
const __PD = window.__PLATFORM_DATA || {};

// Estimated series per instance for capacity planning (heuristic: ~3x metric count)
const SERIES_ESTIMATE = {
  mariadb: 80, postgresql: 65, redis: 45, mongodb: 55, kafka: 70,
  elasticsearch: 60, kubernetes: 40, jvm: 35, nginx: 30, rabbitmq: 40,
  oracle: 60, db2: 55, clickhouse: 55, operational: 10, platform: 15,
  node: 50, etcd: 30, coredns: 20, blackbox: 15, custom: 25,
};

const RULE_PACKS = (() => {
  if (__PD.rulePacks && __PD.packOrder) {
    return __PD.packOrder.map(key => {
      const p = __PD.rulePacks[key];
      return {
        id: key,
        label: p.label,
        recording: p.recordingRules,
        alerts: p.alertRules,
        metrics: (p.metrics || []).length,
        seriesPerInstance: SERIES_ESTIMATE[key] || 30,
      };
    });
  }
  // Fallback
  return [
    { id: 'mariadb', label: 'MariaDB', recording: 11, alerts: 8, metrics: 6, seriesPerInstance: 80 },
    { id: 'postgresql', label: 'PostgreSQL', recording: 11, alerts: 9, metrics: 5, seriesPerInstance: 65 },
    { id: 'redis', label: 'Redis', recording: 11, alerts: 6, metrics: 4, seriesPerInstance: 45 },
    { id: 'mongodb', label: 'MongoDB', recording: 10, alerts: 6, metrics: 4, seriesPerInstance: 55 },
    { id: 'kafka', label: 'Kafka', recording: 13, alerts: 9, metrics: 5, seriesPerInstance: 70 },
    { id: 'elasticsearch', label: 'Elasticsearch', recording: 11, alerts: 7, metrics: 4, seriesPerInstance: 60 },
    { id: 'kubernetes', label: 'Kubernetes', recording: 7, alerts: 4, metrics: 4, seriesPerInstance: 40 },
    { id: 'jvm', label: 'JVM', recording: 9, alerts: 7, metrics: 4, seriesPerInstance: 35 },
    { id: 'nginx', label: 'Nginx', recording: 9, alerts: 6, metrics: 3, seriesPerInstance: 30 },
    { id: 'rabbitmq', label: 'RabbitMQ', recording: 12, alerts: 8, metrics: 5, seriesPerInstance: 40 },
    { id: 'oracle', label: 'Oracle', recording: 11, alerts: 7, metrics: 4, seriesPerInstance: 60 },
    { id: 'db2', label: 'DB2', recording: 12, alerts: 7, metrics: 4, seriesPerInstance: 55 },
    { id: 'clickhouse', label: 'ClickHouse', recording: 12, alerts: 7, metrics: 4, seriesPerInstance: 55 },
    { id: 'operational', label: 'Operational', recording: 0, alerts: 4, metrics: 2, seriesPerInstance: 10 },
    { id: 'platform', label: 'Platform', recording: 0, alerts: 4, metrics: 3, seriesPerInstance: 15 },
  ];
})();

function GaugeBar({ value, max, label, unit, color }) {
  const pct = Math.min((value / max) * 100, 100);
  const barColor = pct > 80 ? 'bg-red-500' : pct > 60 ? 'bg-amber-500' : color || 'bg-blue-500';
  const barStyle = { width: pct + '%' };
  return (
    <div>
      <div className="flex justify-between text-xs text-slate-600 mb-1">
        <span>{label}</span>
        <span className="font-mono">{typeof value === 'number' ? value.toLocaleString() : value}{unit}</span>
      </div>
      <div className="h-3 bg-slate-100 rounded-full overflow-hidden">
        <div className={'h-full rounded-full transition-all duration-500 ' + barColor} style={barStyle}></div>
      </div>
    </div>
  );
}

export default function CapacityPlanner() {
  const [tenantCount, setTenantCount] = useState(5);
  const [instancesPerTenant, setInstancesPerTenant] = useState(3);
  const [scrapeInterval, setScrapeInterval] = useState(15);
  const [retentionDays, setRetentionDays] = useState(15);
  // Initialize from flow state if available (e.g., packs selected in Rule Pack Selector step)
  const flowPacks = (window.__FLOW_STATE && window.__FLOW_STATE.selectedPacks)
    ? new Set(window.__FLOW_STATE.selectedPacks) : null;
  const [selectedPacks, setSelectedPacks] = useState(flowPacks || new Set(['mariadb', 'kubernetes', 'node']));

  const togglePack = (id) => {
    setSelectedPacks(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const estimates = useMemo(() => {
    const packs = RULE_PACKS.filter(rp => selectedPacks.has(rp.id));
    const totalRecording = packs.reduce((s, p) => s + p.recording, 0);
    const totalAlerts = packs.reduce((s, p) => s + p.alerts, 0);
    const totalMetrics = packs.reduce((s, p) => s + p.metrics, 0);

    // Series estimation
    const seriesPerInstance = packs.reduce((s, p) => s + p.seriesPerInstance, 0);
    const totalInstances = tenantCount * instancesPerTenant;
    const totalSeries = seriesPerInstance * totalInstances;

    // TSDB size: ~1.5 bytes per sample, samples = series * (retention_seconds / scrape_interval)
    const samplesPerDay = totalSeries * (86400 / scrapeInterval);
    const totalSamples = samplesPerDay * retentionDays;
    const tsdbBytes = totalSamples * 1.5;
    const tsdbGB = tsdbBytes / (1024 ** 3);

    // Exporter memory: ~0.5 MB base + 0.1 KB per series
    const exporterMB = 50 + (totalSeries * 0.1 / 1024);

    // Prometheus memory: ~2 bytes per active sample in head block (2h)
    const headSamples = totalSeries * (7200 / scrapeInterval);
    const promMemMB = (headSamples * 2) / (1024 ** 2) + 256; // + 256 MB base

    // Alertmanager routes
    const amRoutes = tenantCount * (1 + (packs.length > 3 ? packs.length : 0)); // base + overrides
    const amInhibits = tenantCount * 2 + totalAlerts; // per-tenant + dedup

    // Reload time estimate (ms)
    const reloadMs = 50 + tenantCount * 5 + totalSeries * 0.01;

    return {
      packs: packs.length,
      totalRecording,
      totalAlerts,
      totalMetrics,
      totalInstances,
      totalSeries,
      tsdbGB: Math.max(tsdbGB, 0.01),
      exporterMB: Math.max(exporterMB, 50),
      promMemMB: Math.max(promMemMB, 256),
      amRoutes,
      amInhibits,
      reloadMs: Math.max(reloadMs, 50),
      samplesPerDay,
    };
  }, [tenantCount, instancesPerTenant, scrapeInterval, retentionDays, selectedPacks]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-5xl mx-auto">
        <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('容量規劃器', 'Capacity Planner')}</h1>
        <p className="text-slate-600 mb-6">{t('輸入叢集參數，預估 TSDB 大小、記憶體和 Alertmanager 路由數量', 'Input cluster parameters to estimate TSDB size, memory, and Alertmanager route counts')}</p>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Input panel */}
          <div className="lg:col-span-1 space-y-6">
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h2 className="text-sm font-semibold text-slate-800 mb-4">{t('叢集參數', 'Cluster Parameters')}</h2>

              <label className="block mb-4">
                <span className="text-xs text-slate-600">{t('Tenant 數量', 'Number of Tenants')}</span>
                <input type="range" min={1} max={50} value={tenantCount} onChange={(e) => setTenantCount(parseInt(e.target.value))}
                  className="w-full mt-1" />
                <div className="text-right text-sm font-mono font-bold text-blue-600">{tenantCount}</div>
              </label>

              <label className="block mb-4">
                <span className="text-xs text-slate-600">{t('每 Tenant 實例數', 'Instances per Tenant')}</span>
                <input type="range" min={1} max={20} value={instancesPerTenant} onChange={(e) => setInstancesPerTenant(parseInt(e.target.value))}
                  className="w-full mt-1" />
                <div className="text-right text-sm font-mono font-bold text-blue-600">{instancesPerTenant}</div>
              </label>

              <label className="block mb-4">
                <span className="text-xs text-slate-600">{t('Scrape Interval（秒）', 'Scrape Interval (seconds)')}</span>
                <select value={scrapeInterval} onChange={(e) => setScrapeInterval(parseInt(e.target.value))}
                  className="w-full mt-1 px-3 py-2 border border-slate-200 rounded-lg text-sm bg-white">
                  <option value={10}>10s</option>
                  <option value={15}>15s ({t('預設', 'default')})</option>
                  <option value={30}>30s</option>
                  <option value={60}>60s</option>
                </select>
              </label>

              <label className="block">
                <span className="text-xs text-slate-600">{t('Retention（天）', 'Retention (days)')}</span>
                <input type="range" min={1} max={90} value={retentionDays} onChange={(e) => setRetentionDays(parseInt(e.target.value))}
                  className="w-full mt-1" />
                <div className="text-right text-sm font-mono font-bold text-blue-600">{retentionDays}d</div>
              </label>
            </div>

            {/* Rule Pack selection */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h2 className="text-sm font-semibold text-slate-800 mb-3">{t('啟用的 Rule Pack', 'Enabled Rule Packs')}</h2>
              <div className="space-y-1">
                {RULE_PACKS.map(rp => (
                  <label key={rp.id} className="flex items-center gap-2 py-1 cursor-pointer">
                    <input type="checkbox" checked={selectedPacks.has(rp.id)} onChange={() => togglePack(rp.id)}
                      className="w-3.5 h-3.5 rounded border-slate-300 text-blue-600" />
                    <span className="text-sm text-slate-700">{rp.label}</span>
                    <span className="text-xs text-slate-400 ml-auto">{rp.alerts}a/{rp.recording}r</span>
                  </label>
                ))}
              </div>
              <div className="mt-3 text-xs text-slate-500">
                {t(`已選 ${selectedPacks.size} 個 Rule Pack`, `${selectedPacks.size} Rule Packs selected`)}
              </div>
            </div>
          </div>

          {/* Results panel */}
          <div className="lg:col-span-2 space-y-6">
            {/* Key metrics */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              {[
                { label: t('時間序列', 'Time Series'), value: estimates.totalSeries.toLocaleString(), color: 'text-blue-600' },
                { label: t('TSDB 大小', 'TSDB Size'), value: `${estimates.tsdbGB.toFixed(1)} GB`, color: 'text-purple-600' },
                { label: t('Exporter 記憶體', 'Exporter Memory'), value: `${Math.round(estimates.exporterMB)} MB`, color: 'text-green-600' },
                { label: t('Prom 記憶體', 'Prom Memory'), value: `${Math.round(estimates.promMemMB)} MB`, color: 'text-amber-600' },
              ].map((m, i) => (
                <div key={i} className="bg-white rounded-xl border border-slate-200 p-4 text-center">
                  <div className={`text-xl font-bold ${m.color}`}>{m.value}</div>
                  <div className="text-xs text-slate-500 mt-1">{m.label}</div>
                </div>
              ))}
            </div>

            {/* Detailed gauges */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 space-y-5">
              <h2 className="text-sm font-semibold text-slate-800">{t('資源預估明細', 'Resource Estimation Details')}</h2>

              <GaugeBar label={t('活躍時間序列', 'Active Time Series')} value={estimates.totalSeries} max={500 * tenantCount} unit="" color="bg-blue-500" />
              <GaugeBar label={t('TSDB 磁碟（GB）', 'TSDB Disk (GB)')} value={parseFloat(estimates.tsdbGB.toFixed(1))} max={Math.max(estimates.tsdbGB * 2, 10)} unit=" GB" color="bg-purple-500" />
              <GaugeBar label={t('Exporter 記憶體（MB）', 'Exporter Memory (MB)')} value={Math.round(estimates.exporterMB)} max={512} unit=" MB" color="bg-green-500" />
              <GaugeBar label={t('Prometheus 記憶體（MB）', 'Prometheus Memory (MB)')} value={Math.round(estimates.promMemMB)} max={4096} unit=" MB" color="bg-amber-500" />
              <GaugeBar label={t('Alertmanager Routes', 'Alertmanager Routes')} value={estimates.amRoutes} max={500} unit="" color="bg-pink-500" />
              <GaugeBar label={t('Alertmanager Inhibit Rules', 'Alertmanager Inhibit Rules')} value={estimates.amInhibits} max={1000} unit="" color="bg-indigo-500" />
              <GaugeBar label={t('Config Reload 預估（ms）', 'Config Reload Estimate (ms)')} value={Math.round(estimates.reloadMs)} max={5000} unit=" ms" color="bg-teal-500" />
            </div>

            {/* Summary table */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h2 className="text-sm font-semibold text-slate-800 mb-3">{t('計算摘要', 'Calculation Summary')}</h2>
              <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm">
                {[
                  [t('Tenants', 'Tenants'), tenantCount],
                  [t('總實例數', 'Total Instances'), estimates.totalInstances],
                  [t('Rule Pack 數', 'Rule Packs'), estimates.packs],
                  [t('Recording Rules', 'Recording Rules'), estimates.totalRecording],
                  [t('Alert Rules', 'Alert Rules'), estimates.totalAlerts],
                  [t('每日樣本數', 'Samples/day'), estimates.samplesPerDay.toLocaleString()],
                ].map(([label, val], i) => (
                  <div key={i} className="flex justify-between py-1 border-b border-slate-50">
                    <span className="text-slate-600">{label}</span>
                    <span className="font-mono font-bold text-slate-900">{val}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Recommendations */}
            <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 text-sm text-blue-800">
              <h3 className="font-semibold mb-2">{t('建議', 'Recommendations')}</h3>
              <ul className="space-y-1 text-xs">
                {estimates.totalSeries > 100000 && <li>⚠️ {t('時間序列超過 100K，考慮啟用 cardinality guard', 'Series > 100K — consider enabling cardinality guard')}</li>}
                {estimates.tsdbGB > 50 && <li>⚠️ {t('TSDB > 50 GB，考慮縮短 retention 或使用遠端儲存', 'TSDB > 50 GB — consider shorter retention or remote storage')}</li>}
                {estimates.promMemMB > 2048 && <li>⚠️ {t('Prometheus 記憶體 > 2 GB，考慮 sharding 或 federation', 'Prometheus memory > 2 GB — consider sharding or federation')}</li>}
                {estimates.reloadMs > 2000 && <li>⚠️ {t('Reload 時間 > 2s，考慮 incremental reload', 'Reload time > 2s — consider incremental reload')}</li>}
                {estimates.totalSeries <= 100000 && estimates.tsdbGB <= 50 && estimates.promMemMB <= 2048 && (
                  <li>✅ {t('所有指標在健康範圍內', 'All metrics within healthy range')}</li>
                )}
              </ul>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
