---
title: "Tenant Health Dashboard"
tags: [dashboard, health, demo]
audience: [tenant, platform-engineer]
version: v2.0.0-preview.2
lang: en
related: [alert-simulator, runbook-viewer, alert-timeline]
---

import React, { useState, useEffect } from 'react';

const t = window.__t || ((zh, en) => en);

const MOCK_TENANTS = {
  'db-a': {
    name: 'db-a',
    mode: 'normal',
    rulePacks: ['mariadb', 'kubernetes'],
    routing: { type: 'webhook', target: 'https://webhook.example.com/alerts' },
    thresholds: {
      mysql_connections: { threshold: 70, current: 52, unit: 'conn' },
      mysql_connections_critical: { threshold: 95, current: 52, unit: 'conn' },
      mysql_cpu: { threshold: 80, current: 43, unit: '%' },
      mysql_slow_queries: { threshold: 10, current: 2.4, unit: '/s' },
    },
    alerts: [
      { name: 'MariaDBHighConnections', state: 'ok', severity: 'warning' },
      { name: 'MariaDBHighConnectionsCritical', state: 'ok', severity: 'critical' },
      { name: 'MariaDBHighCPU', state: 'ok', severity: 'warning' },
    ],
    maintenanceExpires: null,
  },
  'db-b': {
    name: 'db-b',
    mode: 'normal',
    rulePacks: ['postgresql', 'kubernetes'],
    routing: { type: 'slack', target: '#db-b-alerts' },
    thresholds: {
      pg_connections: { threshold: 150, current: 134, unit: 'conn' },
      pg_connections_critical: { threshold: 200, current: 134, unit: 'conn' },
      pg_cache_hit_ratio: { threshold: 85, current: 91.2, unit: '%', inverted: true },
      pg_query_time: { threshold: 5000, current: 3210, unit: 'ms' },
    },
    alerts: [
      { name: 'PostgreSQLHighConnections', state: 'firing', severity: 'warning', since: '8m ago', value: '134 > 150' },
      { name: 'PostgreSQLHighConnectionsCritical', state: 'ok', severity: 'critical' },
      { name: 'PostgreSQLLowCacheHit', state: 'ok', severity: 'warning' },
    ],
    maintenanceExpires: null,
  },
  'cache': {
    name: 'cache',
    mode: 'maintenance',
    rulePacks: ['redis', 'kubernetes'],
    routing: { type: 'email', target: 'ops@example.com' },
    thresholds: {
      redis_memory: { threshold: 80, current: 88, unit: '%' },
      redis_memory_critical: { threshold: 95, current: 88, unit: '%' },
      redis_evictions: { threshold: 1000, current: 2340, unit: '/s' },
    },
    alerts: [
      { name: 'RedisHighMemory', state: 'suppressed', severity: 'warning', reason: 'Maintenance mode' },
      { name: 'RedisHighEvictions', state: 'suppressed', severity: 'warning', reason: 'Maintenance mode' },
    ],
    maintenanceExpires: '2026-03-20T06:00:00Z',
  },
};

function GaugeBar({ label, current, threshold, unit, inverted }) {
  const pct = inverted ? (current / 100) * 100 : Math.min((current / threshold) * 100, 100);
  const danger = inverted ? current < threshold : current > threshold * 0.9;
  const warn = inverted ? current < threshold * 1.1 : current > threshold * 0.75;
  const color = danger ? 'bg-red-500' : warn ? 'bg-amber-400' : 'bg-green-500';

  return (
    <div className="mb-3">
      <div className="flex justify-between text-xs mb-1">
        <span className="text-slate-600 font-medium">{label}</span>
        <span className="font-mono text-slate-500">{current}{unit} / {threshold}{unit}</span>
      </div>
      <div className="h-2.5 bg-slate-100 rounded-full overflow-hidden">
        <div className={`h-full ${color} rounded-full transition-all`} style={{ width: Math.min(pct, 100) + '%' }} />
      </div>
    </div>
  );
}

function Countdown({ expiresISO }) {
  const [remaining, setRemaining] = useState('');
  useEffect(() => {
    function update() {
      const diff = new Date(expiresISO) - new Date();
      if (diff <= 0) { setRemaining('Expired'); return; }
      const d = Math.floor(diff / 86400000);
      const h = Math.floor((diff % 86400000) / 3600000);
      const m = Math.floor((diff % 3600000) / 60000);
      setRemaining(`${d}d ${h}h ${m}m`);
    }
    update();
    const id = setInterval(update, 60000);
    return () => clearInterval(id);
  }, [expiresISO]);
  return <span className="font-mono text-sm font-semibold text-amber-700">{remaining}</span>;
}

export default function HealthDashboard() {
  const [selectedTenant, setSelectedTenant] = useState('db-a');
  const tenant = MOCK_TENANTS[selectedTenant];

  const firingCount = tenant.alerts.filter(a => a.state === 'firing').length;
  const suppressedCount = tenant.alerts.filter(a => a.state === 'suppressed').length;
  const okCount = tenant.alerts.filter(a => a.state === 'ok').length;

  const modeColors = { normal: 'bg-green-100 text-green-800', maintenance: 'bg-amber-100 text-amber-800', silent: 'bg-purple-100 text-purple-800' };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-5xl mx-auto">
        <div className="mb-6">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('租戶健康儀表板', 'Tenant Health Dashboard')}</h1>
          <p className="text-slate-600">{t('模擬即時健康視圖（展示用途）', 'Simulated real-time health view (demo purposes)')}</p>
        </div>

        {/* Tenant Selector */}
        <div className="flex gap-2 mb-6">
          {Object.keys(MOCK_TENANTS).map(key => (
            <button
              key={key}
              onClick={() => setSelectedTenant(key)}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                selectedTenant === key ? 'bg-blue-600 text-white' : 'bg-white text-slate-700 border border-slate-200 hover:border-blue-300'
              }`}
            >
              {key}
              {MOCK_TENANTS[key].alerts.some(a => a.state === 'firing') && (
                <span className="ml-1.5 inline-block w-2 h-2 bg-red-500 rounded-full"></span>
              )}
            </button>
          ))}
        </div>

        {/* Status Bar */}
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-3 mb-6">
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <div className="text-xs text-slate-500 mb-1">{t('模式', 'Mode')}</div>
            <span className={`text-sm font-semibold px-2 py-0.5 rounded ${modeColors[tenant.mode]}`}>{tenant.mode}</span>
          </div>
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <div className="text-xs text-slate-500 mb-1">Rule Packs</div>
            <div className="text-sm font-semibold text-slate-900">{tenant.rulePacks.join(', ')}</div>
          </div>
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <div className="text-xs text-slate-500 mb-1">{t('路由', 'Routing')}</div>
            <div className="text-sm font-semibold text-slate-900">{tenant.routing.type}</div>
            <div className="text-xs text-slate-400 truncate">{tenant.routing.target}</div>
          </div>
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <div className="text-xs text-slate-500 mb-1">{t('告警', 'Alerts')}</div>
            <div className="flex gap-2">
              <span className="text-red-600 font-bold">{firingCount}</span>
              <span className="text-slate-300">/</span>
              <span className="text-purple-600 font-bold">{suppressedCount}</span>
              <span className="text-slate-300">/</span>
              <span className="text-green-600 font-bold">{okCount}</span>
            </div>
          </div>
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <div className="text-xs text-slate-500 mb-1">{t('維護到期', 'Maintenance')}</div>
            {tenant.maintenanceExpires ? (
              <Countdown expiresISO={tenant.maintenanceExpires} />
            ) : (
              <span className="text-sm text-slate-400">—</span>
            )}
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Threshold Gauges */}
          <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
            <h3 className="text-sm font-semibold text-slate-900 mb-4">{t('閾值儀表', 'Threshold Gauges')}</h3>
            {Object.entries(tenant.thresholds).map(([key, m]) => (
              <GaugeBar key={key} label={key} current={m.current} threshold={m.threshold} unit={m.unit} inverted={m.inverted} />
            ))}
          </div>

          {/* Alert Status */}
          <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
            <h3 className="text-sm font-semibold text-slate-900 mb-4">{t('告警狀態', 'Alert Status')}</h3>
            <div className="space-y-2">
              {tenant.alerts.map(a => {
                const stateStyles = {
                  ok: 'border-green-200 bg-green-50',
                  firing: 'border-red-200 bg-red-50',
                  suppressed: 'border-purple-200 bg-purple-50',
                };
                const stateIcons = { ok: '🟢', firing: '🔴', suppressed: '🟣' };
                return (
                  <div key={a.name} className={`p-3 rounded-lg border ${stateStyles[a.state]}`}>
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span>{stateIcons[a.state]}</span>
                        <span className="font-mono text-sm font-medium text-slate-900">{a.name}</span>
                      </div>
                      <span className={`text-xs font-semibold px-1.5 py-0.5 rounded ${
                        a.severity === 'critical' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'
                      }`}>{a.severity}</span>
                    </div>
                    {a.state === 'firing' && (
                      <div className="text-xs text-red-700 mt-1">{t('已觸發', 'Firing')} {a.since} — {a.value}</div>
                    )}
                    {a.state === 'suppressed' && (
                      <div className="text-xs text-purple-700 mt-1">{a.reason}</div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
