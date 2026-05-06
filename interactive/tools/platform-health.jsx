---
title: "Platform Health Dashboard"
tags: [health, dashboard, monitoring, overview, operations]
audience: ["platform-engineer"]
version: v2.7.0
lang: en
related: [health-dashboard, self-service-portal, alert-simulator]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Simulated platform data (would come from Prometheus API in production) ── */
const PLATFORM_DATA = {
  exporter: {
    status: 'healthy',
    replicas: { ready: 2, total: 2 },
    uptime: '14d 6h 32m',
    lastReload: '2026-03-17T08:15:23Z',
    reloadCount: 47,
    configHash: 'a3f2c1d8e9b7',
    metricsPerTenant: { 'prod-mariadb': 8, 'prod-redis': 5, 'prod-kafka': 12, 'staging-pg': 4, 'prod-oracle': 6 },
    totalMetrics: 35,
    version: 'v2.7.0',
  },
  prometheus: {
    status: 'healthy',
    scrapeInterval: '15s',
    rulesLoaded: 238,
    rulePacksActive: 15,
    recordingRules: 139,
    alertRules: 99,
    scrapeErrors: 0,
    tsdbSizeMB: 1247,
  },
  alertmanager: {
    status: 'healthy',
    configReloaded: '2026-03-17T08:15:25Z',
    routesActive: 5,
    receiversActive: 5,
    inhibitRules: 5,
    silences: 1,
    notificationsSent24h: 12,
    notificationsFailed24h: 0,
  },
  tenants: [
    { name: 'prod-mariadb', state: 'normal', packs: ['mariadb', 'kubernetes'], metrics: 8, alertsFiring: 0, lastUpdate: '2026-03-17T07:00:00Z' },
    { name: 'prod-redis', state: 'normal', packs: ['redis', 'kubernetes'], metrics: 5, alertsFiring: 1, lastUpdate: '2026-03-17T07:30:00Z' },
    { name: 'prod-kafka', state: 'normal', packs: ['kafka', 'jvm'], metrics: 12, alertsFiring: 0, lastUpdate: '2026-03-17T06:45:00Z' },
    { name: 'staging-pg', state: 'maintenance', packs: ['postgresql', 'kubernetes'], metrics: 4, alertsFiring: 0, lastUpdate: '2026-03-17T05:00:00Z', expires: '2026-03-20T06:00:00Z' },
    { name: 'prod-oracle', state: 'normal', packs: ['oracle', 'db2', 'kubernetes'], metrics: 6, alertsFiring: 0, lastUpdate: '2026-03-17T08:00:00Z' },
  ],
};

/* ── Status indicator ── */
function StatusDot({ status }) {
  const colors = {
    healthy: 'bg-green-500',
    degraded: 'bg-yellow-500',
    down: 'bg-red-500',
    normal: 'bg-green-500',
    maintenance: 'bg-yellow-500',
    silent: 'bg-gray-400',
  };
  return (
    <span className={`inline-block w-2.5 h-2.5 rounded-full ${colors[status] || 'bg-gray-300'}`} />
  );
}

/* ── Metric card ── */
function MetricCard({ label, value, subtitle, status }) {
  return (
    <div className={`p-3 rounded-lg border ${
      status === 'warning' ? 'bg-yellow-50 border-yellow-200' :
      status === 'error' ? 'bg-red-50 border-red-200' :
      'bg-white border-gray-200'
    }`}>
      <div className="text-xs text-gray-500">{label}</div>
      <div className="text-xl font-bold text-gray-900">{value}</div>
      {subtitle && <div className="text-xs text-gray-400">{subtitle}</div>}
    </div>
  );
}

/* ── Component health section ── */
function ComponentHealth() {
  const components = [
    {
      name: 'threshold-exporter',
      status: PLATFORM_DATA.exporter.status,
      details: [
        { label: t('副本', 'Replicas'), value: `${PLATFORM_DATA.exporter.replicas.ready}/${PLATFORM_DATA.exporter.replicas.total}` },
        { label: t('運行時間', 'Uptime'), value: PLATFORM_DATA.exporter.uptime },
        { label: t('重載次數', 'Reloads'), value: PLATFORM_DATA.exporter.reloadCount },
        { label: 'Config Hash', value: PLATFORM_DATA.exporter.configHash },
        { label: t('版本', 'Version'), value: PLATFORM_DATA.exporter.version },
      ],
    },
    {
      name: 'Prometheus',
      status: PLATFORM_DATA.prometheus.status,
      details: [
        { label: t('規則總數', 'Total Rules'), value: PLATFORM_DATA.prometheus.rulesLoaded },
        { label: 'Recording Rules', value: PLATFORM_DATA.prometheus.recordingRules },
        { label: 'Alert Rules', value: PLATFORM_DATA.prometheus.alertRules },
        { label: 'Rule Packs', value: PLATFORM_DATA.prometheus.rulePacksActive },
        { label: 'TSDB Size', value: `${PLATFORM_DATA.prometheus.tsdbSizeMB} MB` },
      ],
    },
    {
      name: 'Alertmanager',
      status: PLATFORM_DATA.alertmanager.status,
      details: [
        { label: t('路由', 'Routes'), value: PLATFORM_DATA.alertmanager.routesActive },
        { label: 'Receivers', value: PLATFORM_DATA.alertmanager.receiversActive },
        { label: 'Inhibit Rules', value: PLATFORM_DATA.alertmanager.inhibitRules },
        { label: t('靜默中', 'Silences'), value: PLATFORM_DATA.alertmanager.silences },
        { label: t('通知 (24h)', 'Notifications (24h)'), value: `${PLATFORM_DATA.alertmanager.notificationsSent24h} sent / ${PLATFORM_DATA.alertmanager.notificationsFailed24h} failed` },
      ],
    },
  ];

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-gray-700">{t('元件健康', 'Component Health')}</h3>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {components.map(c => (
          <div key={c.name} className="p-4 bg-white rounded-lg border">
            <div className="flex items-center gap-2 mb-3">
              <StatusDot status={c.status} />
              <span className="font-medium text-sm">{c.name}</span>
              <span className={`text-xs px-1.5 py-0.5 rounded ${
                c.status === 'healthy' ? 'bg-green-100 text-green-700' :
                c.status === 'degraded' ? 'bg-yellow-100 text-yellow-700' :
                'bg-red-100 text-red-700'
              }`}>
                {c.status}
              </span>
            </div>
            <div className="space-y-1.5">
              {c.details.map((d, i) => (
                <div key={i} className="flex justify-between text-xs">
                  <span className="text-gray-500">{d.label}</span>
                  <span className="font-mono text-gray-700">{d.value}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── Tenant overview section ── */
function TenantOverview() {
  const totalFiring = PLATFORM_DATA.tenants.reduce((sum, t) => sum + t.alertsFiring, 0);
  const totalMetrics = PLATFORM_DATA.tenants.reduce((sum, t) => sum + t.metrics, 0);
  const inMaintenance = PLATFORM_DATA.tenants.filter(t => t.state === 'maintenance').length;

  const stateColors = {
    normal: 'bg-green-100 text-green-700',
    maintenance: 'bg-yellow-100 text-yellow-700',
    silent: 'bg-gray-100 text-gray-500',
  };

  const stateLabels = {
    normal: () => t('正常', 'Normal'),
    maintenance: () => t('維護中', 'Maintenance'),
    silent: () => t('靜默', 'Silent'),
  };

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-gray-700">{t('Tenant 概覽', 'Tenant Overview')}</h3>

      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <MetricCard
          label={t('Tenant 數', 'Tenants')}
          value={PLATFORM_DATA.tenants.length}
          subtitle={inMaintenance > 0 ? `${inMaintenance} ${t('維護中', 'in maintenance')}` : null}
        />
        <MetricCard
          label={t('總指標數', 'Total Metrics')}
          value={totalMetrics}
        />
        <MetricCard
          label={t('觸發中告警', 'Alerts Firing')}
          value={totalFiring}
          status={totalFiring > 0 ? 'warning' : null}
        />
        <MetricCard
          label={t('Cardinality', 'Cardinality')}
          value={`${totalMetrics} / ${PLATFORM_DATA.tenants.length * 500}`}
          subtitle={t('per-tenant 上限 500', 'per-tenant limit 500')}
        />
      </div>

      {/* Tenant table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-100">
              <th className="px-3 py-2 text-left text-xs">{t('Tenant', 'Tenant')}</th>
              <th className="px-3 py-2 text-center text-xs">{t('狀態', 'State')}</th>
              <th className="px-3 py-2 text-left text-xs">Rule Packs</th>
              <th className="px-3 py-2 text-right text-xs">{t('指標', 'Metrics')}</th>
              <th className="px-3 py-2 text-right text-xs">{t('告警', 'Alerts')}</th>
              <th className="px-3 py-2 text-right text-xs">{t('最後更新', 'Last Update')}</th>
            </tr>
          </thead>
          <tbody>
            {PLATFORM_DATA.tenants.map(tenant => (
              <tr key={tenant.name} className="border-b hover:bg-gray-50">
                <td className="px-3 py-2 font-mono text-xs font-medium">{tenant.name}</td>
                <td className="px-3 py-2 text-center">
                  <span className={`text-xs px-2 py-0.5 rounded-full ${stateColors[tenant.state]}`}>
                    {stateLabels[tenant.state]()}
                  </span>
                  {tenant.expires && (
                    <div className="text-xs text-gray-400 mt-0.5">
                      expires {tenant.expires.slice(0, 10)}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    {tenant.packs.map(p => (
                      <span key={p} className="text-xs px-1.5 py-0.5 bg-blue-50 text-blue-700 rounded">
                        {p}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="px-3 py-2 text-right font-mono text-xs">{tenant.metrics}</td>
                <td className="px-3 py-2 text-right">
                  {tenant.alertsFiring > 0 ? (
                    <span className="text-xs px-1.5 py-0.5 bg-red-100 text-red-700 rounded-full font-medium">
                      {tenant.alertsFiring} firing
                    </span>
                  ) : (
                    <span className="text-xs text-green-600">0</span>
                  )}
                </td>
                <td className="px-3 py-2 text-right text-xs text-gray-500">
                  {tenant.lastUpdate.slice(11, 16)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ── Rule Pack distribution ── */
function RulePackDistribution() {
  const packUsage = useMemo(() => {
    const usage = {};
    for (const tenant of PLATFORM_DATA.tenants) {
      for (const pack of tenant.packs) {
        usage[pack] = (usage[pack] || 0) + 1;
      }
    }
    return Object.entries(usage).sort((a, b) => b[1] - a[1]);
  }, []);

  const maxCount = Math.max(1, ...packUsage.map(([, c]) => c));

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-gray-700">{t('Rule Pack 使用分佈', 'Rule Pack Usage')}</h3>
      <div className="space-y-1.5">
        {packUsage.map(([pack, count]) => {
          const barWidth = { width: `${(count / maxCount) * 100}%` };
          return (
          <div key={pack} className="flex items-center gap-2">
            <span className="text-xs font-mono w-24 text-gray-600">{pack}</span>
            <div className="flex-1 bg-gray-100 rounded-full h-4 overflow-hidden">
              <div
                className="h-full bg-blue-500 rounded-full transition-all"
                style={barWidth}
              />
            </div>
            <span className="text-xs font-mono w-16 text-right text-gray-500">
              {count} {t('租戶', 'tenants')}
            </span>
          </div>
          );
        })}
      </div>
    </div>
  );
}

/* ── Config reload timeline ── */
function ReloadTimeline() {
  const events = [
    { time: '08:15:25', type: 'reload', desc: t('Alertmanager 自動 reload (configmap-reload sidecar)', 'Alertmanager auto-reload (configmap-reload sidecar)') },
    { time: '08:15:23', type: 'reload', desc: t('threshold-exporter 偵測 SHA-256 變更，重載配置', 'threshold-exporter detected SHA-256 change, config reloaded') },
    { time: '08:15:00', type: 'update', desc: t('ConfigMap threshold-config 更新 (kubectl apply)', 'ConfigMap threshold-config updated (kubectl apply)') },
    { time: '07:30:00', type: 'alert', desc: t('prod-redis: RedisHighMemory WARNING 觸發', 'prod-redis: RedisHighMemory WARNING fired') },
    { time: '05:00:00', type: 'state', desc: t('staging-pg: 進入 _state_maintenance 模式', 'staging-pg: entered _state_maintenance mode') },
  ];

  const typeIcons = { reload: '🔄', update: '📦', alert: '🔔', state: '🔧' };
  const typeColors = {
    reload: 'border-blue-300 bg-blue-50',
    update: 'border-green-300 bg-green-50',
    alert: 'border-yellow-300 bg-yellow-50',
    state: 'border-purple-300 bg-purple-50',
  };

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-gray-700">{t('最近事件時間軸', 'Recent Events Timeline')}</h3>
      <div className="space-y-2">
        {events.map((e, i) => (
          <div key={i} className={`flex items-start gap-3 p-2 rounded-lg border ${typeColors[e.type]}`}>
            <span className="text-sm">{typeIcons[e.type]}</span>
            <div className="flex-1 min-w-0">
              <div className="text-xs text-gray-600">{e.desc}</div>
            </div>
            <span className="text-xs font-mono text-gray-400 whitespace-nowrap">{e.time}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── Main Dashboard ── */
export default function PlatformHealth() {
  return (
    <div className="max-w-5xl mx-auto">
      <div className="mb-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">
              {t('平台健康儀表板', 'Platform Health Dashboard')}
            </h1>
            <p className="text-gray-600 mt-1">
              {t('平台元件狀態、Tenant 概覽、Rule Pack 分佈、最近事件 — 一眼掌握全局。',
                 'Component status, tenant overview, Rule Pack distribution, recent events — at a glance.')}
            </p>
          </div>
          <div className="text-right text-xs text-gray-400">
            <div>{t('模擬資料', 'Simulated Data')}</div>
            <div>{t('生產環境連接 Prometheus API', 'Production connects to Prometheus API')}</div>
          </div>
        </div>

        {/* Top-level status banner */}
        <div className="mt-4 p-3 bg-green-50 rounded-lg border border-green-200 flex items-center gap-3">
          <StatusDot status="healthy" />
          <span className="text-sm font-medium text-green-800">
            {t('平台運行正常', 'Platform Operational')}
          </span>
          <span className="text-xs text-green-600 ml-auto">
            {t('所有元件健康 · 5 Tenant · 15 Rule Pack · 238 Rules',
               'All components healthy · 5 Tenants · 15 Rule Packs · 238 Rules')}
          </span>
        </div>
      </div>

      <div className="space-y-6">
        <ComponentHealth />
        <TenantOverview />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <RulePackDistribution />
          <ReloadTimeline />
        </div>
      </div>

      {/* Footer */}
      <div className="mt-6 p-4 bg-blue-50 rounded-lg border border-blue-100">
        <h4 className="text-sm font-medium text-blue-800 mb-2">{t('提示', 'Tips')}</h4>
        <ul className="text-sm text-blue-700 space-y-1">
          <li>{t('• 此儀表板使用模擬資料。生產環境中透過 da-tools diagnose 和 batch-diagnose 取得即時資料。',
                 '• This dashboard uses simulated data. In production, use da-tools diagnose and batch-diagnose for live data.')}</li>
          <li>{t('• 配置重載由 SHA-256 hash 比對觸發 — threshold-exporter 每 15s 檢查一次。',
                 '• Config reloads are triggered by SHA-256 hash comparison — threshold-exporter checks every 15s.')}</li>
          <li>{t('• Cardinality 上限 500 per-tenant，超過會自動截斷並記錄 ERROR。',
                 '• Cardinality limit is 500 per-tenant. Exceeding triggers auto-truncation with ERROR log.')}</li>
        </ul>
      </div>
    </div>
  );
}
