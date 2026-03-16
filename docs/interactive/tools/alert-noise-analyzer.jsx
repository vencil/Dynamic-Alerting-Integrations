---
title: "Alert Noise Analyzer"
tags: [alerts, noise, MTTA, MTTR, analysis]
audience: ["platform", "domain-expert"]
version: v2.1.0
lang: en
related: [alert-simulator, alert-timeline, health-dashboard]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

// ── Sample Data ───────────────────────────────────────────────────
const SAMPLE_ALERTS = [
  { alertname: "MariaDBHighConnections", tenant: "db-a", severity: "warning",  startsAt: "2025-01-15T10:00:00Z", endsAt: "2025-01-15T10:05:00Z", status: "resolved" },
  { alertname: "MariaDBHighConnections", tenant: "db-a", severity: "warning",  startsAt: "2025-01-15T10:10:00Z", endsAt: "2025-01-15T10:12:00Z", status: "resolved" },
  { alertname: "MariaDBHighConnections", tenant: "db-a", severity: "warning",  startsAt: "2025-01-15T10:20:00Z", endsAt: "2025-01-15T10:22:00Z", status: "resolved" },
  { alertname: "MariaDBHighConnections", tenant: "db-a", severity: "warning",  startsAt: "2025-01-15T10:35:00Z", endsAt: "2025-01-15T10:40:00Z", status: "resolved" },
  { alertname: "MariaDBHighCPU",         tenant: "db-a", severity: "warning",  startsAt: "2025-01-15T10:01:00Z", endsAt: "2025-01-15T10:30:00Z", status: "resolved" },
  { alertname: "PostgreSQLHighConnections", tenant: "db-b", severity: "warning", startsAt: "2025-01-15T11:00:00Z", endsAt: "2025-01-15T11:45:00Z", status: "resolved" },
  { alertname: "RedisHighMemory",        tenant: "db-a", severity: "warning",  startsAt: "2025-01-15T12:00:00Z", endsAt: "2025-01-15T14:00:00Z", status: "resolved" },
  { alertname: "MariaDBHighConnectionsCritical", tenant: "db-a", severity: "critical", startsAt: "2025-01-15T10:03:00Z", endsAt: "2025-01-15T10:08:00Z", status: "resolved" },
];

// ── Utility ───────────────────────────────────────────────────────

function parseTime(ts) {
  return ts ? new Date(ts).getTime() : Date.now();
}

function durationMinutes(startMs, endMs) {
  return Math.max(0, (endMs - startMs) / 60000);
}

function formatDuration(minutes) {
  if (minutes < 1) return `${Math.round(minutes * 60)}s`;
  if (minutes < 60) return `${Math.round(minutes)}m`;
  return `${(minutes / 60).toFixed(1)}h`;
}

// ── Analysis Functions ─────────────────────────────────────────────

function analyzeAlerts(alerts) {
  if (!alerts.length) return null;

  // Basic counts
  const totalAlerts = alerts.length;
  const bySeverity = {};
  const byTenant = {};
  const byName = {};

  alerts.forEach(a => {
    bySeverity[a.severity] = (bySeverity[a.severity] || 0) + 1;
    byTenant[a.tenant] = (byTenant[a.tenant] || 0) + 1;
    byName[a.alertname] = (byName[a.alertname] || 0) + 1;
  });

  // MTTR: Mean Time To Resolve
  const resolved = alerts.filter(a => a.endsAt && a.status === 'resolved');
  const durations = resolved.map(a => durationMinutes(parseTime(a.startsAt), parseTime(a.endsAt)));
  const mttr = durations.length ? durations.reduce((s, d) => s + d, 0) / durations.length : 0;

  // Flapping detection: alerts that fire ≥3 times in 1h
  const flapping = [];
  const nameGroups = {};
  alerts.forEach(a => {
    const key = `${a.tenant}/${a.alertname}`;
    if (!nameGroups[key]) nameGroups[key] = [];
    nameGroups[key].push(parseTime(a.startsAt));
  });
  Object.entries(nameGroups).forEach(([key, times]) => {
    times.sort((a, b) => a - b);
    // Sliding 1-hour window
    for (let i = 0; i < times.length; i++) {
      const windowEnd = times[i] + 3600000;
      const count = times.filter(t => t >= times[i] && t <= windowEnd).length;
      if (count >= 3) { flapping.push({ key, count }); break; }
    }
  });

  // Top noisy alerts
  const topNoisy = Object.entries(byName)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5)
    .map(([name, count]) => ({ name, count, pct: (count / totalAlerts * 100).toFixed(1) }));

  // Dedup effectiveness: count alerts that overlap in time with same name+tenant
  let dedupCandidates = 0;
  Object.values(nameGroups).forEach(times => {
    if (times.length > 1) dedupCandidates += times.length - 1;
  });
  const dedupRate = totalAlerts > 0 ? ((dedupCandidates / totalAlerts) * 100).toFixed(1) : 0;

  return {
    totalAlerts,
    bySeverity,
    byTenant,
    topNoisy,
    mttr,
    flapping,
    dedupRate,
    resolvedCount: resolved.length,
  };
}

// ── Severity Badge ─────────────────────────────────────────────────

function SeverityBadge({ severity }) {
  const colors = {
    critical: 'bg-red-100 text-red-800 border-red-300',
    warning: 'bg-yellow-100 text-yellow-800 border-yellow-300',
    info: 'bg-blue-100 text-blue-800 border-blue-300',
  };
  return (
    <span className={`inline-block px-2 py-0.5 text-xs font-medium rounded border ${colors[severity] || 'bg-gray-100 text-gray-600 border-gray-300'}`}>
      {severity}
    </span>
  );
}

// ── Stat Card ───────────────────────────────────────────────────────

function StatCard({ label, value, sub, color = 'blue' }) {
  const borderColor = {
    blue: 'border-blue-400', red: 'border-red-400',
    yellow: 'border-yellow-400', green: 'border-green-400',
  }[color] || 'border-gray-400';
  return (
    <div className={`bg-white rounded-lg shadow p-4 border-l-4 ${borderColor}`}>
      <div className="text-sm text-gray-500">{label}</div>
      <div className="text-2xl font-bold mt-1">{value}</div>
      {sub && <div className="text-xs text-gray-400 mt-1">{sub}</div>}
    </div>
  );
}

// ── Bar Chart ───────────────────────────────────────────────────────

function HorizontalBar({ items, maxVal }) {
  return (
    <div className="space-y-2">
      {items.map(({ label, value, pct }) => (
        <div key={label} className="flex items-center gap-2">
          <div className="w-40 text-sm text-gray-700 truncate" title={label}>{label}</div>
          <div className="flex-1 bg-gray-100 rounded-full h-5 relative">
            <div
              className="bg-blue-500 h-5 rounded-full transition-all"
              style={{ width: `${Math.max(2, (value / maxVal) * 100)}%` }}
            />
            <span className="absolute right-2 top-0 text-xs text-gray-600 leading-5">
              {value} ({pct}%)
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Main Component ──────────────────────────────────────────────────

export default function AlertNoiseAnalyzer() {
  const [jsonInput, setJsonInput] = useState('');
  const [alerts, setAlerts] = useState(SAMPLE_ALERTS);
  const [error, setError] = useState('');

  const handleLoad = () => {
    try {
      const parsed = JSON.parse(jsonInput);
      const arr = Array.isArray(parsed) ? parsed : (parsed.data || parsed.alerts || []);
      if (!arr.length) { setError(t('空的告警資料', 'Empty alert data')); return; }
      setAlerts(arr);
      setError('');
    } catch (e) {
      setError(t('JSON 解析失敗: ', 'JSON parse error: ') + e.message);
    }
  };

  const handleLoadSample = () => {
    setAlerts(SAMPLE_ALERTS);
    setJsonInput('');
    setError('');
  };

  const analysis = useMemo(() => analyzeAlerts(alerts), [alerts]);

  return (
    <div className="max-w-4xl mx-auto p-4 space-y-6">
      <div className="text-center">
        <h2 className="text-2xl font-bold text-gray-800">
          {t('告警噪音分析器', 'Alert Noise Analyzer')}
        </h2>
        <p className="text-gray-500 mt-1">
          {t('分析告警歷史，找出噪音來源與改善機會',
             'Analyze alert history to find noise sources and improvement opportunities')}
        </p>
      </div>

      {/* Input Section */}
      <div className="bg-white rounded-lg shadow p-4">
        <h3 className="font-semibold text-gray-700 mb-2">
          {t('載入告警資料', 'Load Alert Data')}
        </h3>
        <textarea
          className="w-full h-24 border rounded p-2 text-sm font-mono"
          placeholder={t(
            '貼上 Alertmanager JSON 或 da-tools alert-correlate --json 輸出...',
            'Paste Alertmanager JSON or da-tools alert-correlate --json output...'
          )}
          value={jsonInput}
          onChange={e => setJsonInput(e.target.value)}
        />
        <div className="flex gap-2 mt-2">
          <button
            className="px-3 py-1 bg-blue-500 text-white rounded text-sm hover:bg-blue-600"
            onClick={handleLoad}
            disabled={!jsonInput.trim()}
          >
            {t('載入', 'Load')}
          </button>
          <button
            className="px-3 py-1 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300"
            onClick={handleLoadSample}
          >
            {t('載入範例', 'Load Sample')}
          </button>
        </div>
        {error && <p className="text-red-500 text-sm mt-2">{error}</p>}
        <p className="text-xs text-gray-400 mt-2">
          {t(`目前載入 ${alerts.length} 筆告警`, `Currently loaded: ${alerts.length} alerts`)}
        </p>
      </div>

      {/* Stats Overview */}
      {analysis && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <StatCard
              label={t('總告警數', 'Total Alerts')}
              value={analysis.totalAlerts}
              color="blue"
            />
            <StatCard
              label={t('平均解決時間 (MTTR)', 'MTTR')}
              value={formatDuration(analysis.mttr)}
              sub={t(`${analysis.resolvedCount} 筆已解決`, `${analysis.resolvedCount} resolved`)}
              color="green"
            />
            <StatCard
              label={t('震盪告警', 'Flapping Alerts')}
              value={analysis.flapping.length}
              sub={t('≥3 次/小時', '≥3 fires/hour')}
              color={analysis.flapping.length ? 'red' : 'green'}
            />
            <StatCard
              label={t('去重空間', 'Dedup Opportunity')}
              value={`${analysis.dedupRate}%`}
              sub={t('可被去重的重複告警', 'Duplicate alerts reducible')}
              color="yellow"
            />
          </div>

          {/* Severity Breakdown */}
          <div className="bg-white rounded-lg shadow p-4">
            <h3 className="font-semibold text-gray-700 mb-3">
              {t('嚴重度分佈', 'Severity Breakdown')}
            </h3>
            <div className="flex gap-4 flex-wrap">
              {Object.entries(analysis.bySeverity).map(([sev, count]) => (
                <div key={sev} className="flex items-center gap-2">
                  <SeverityBadge severity={sev} />
                  <span className="text-lg font-bold">{count}</span>
                  <span className="text-sm text-gray-400">
                    ({(count / analysis.totalAlerts * 100).toFixed(0)}%)
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Top Noisy Alerts */}
          <div className="bg-white rounded-lg shadow p-4">
            <h3 className="font-semibold text-gray-700 mb-3">
              {t('最頻繁告警 Top 5', 'Top 5 Noisiest Alerts')}
            </h3>
            <HorizontalBar
              items={analysis.topNoisy.map(n => ({
                label: n.name, value: n.count, pct: n.pct,
              }))}
              maxVal={Math.max(...analysis.topNoisy.map(n => n.count), 1)}
            />
          </div>

          {/* Tenant Distribution */}
          <div className="bg-white rounded-lg shadow p-4">
            <h3 className="font-semibold text-gray-700 mb-3">
              {t('租戶分佈', 'Tenant Distribution')}
            </h3>
            <HorizontalBar
              items={Object.entries(analysis.byTenant)
                .sort((a, b) => b[1] - a[1])
                .map(([name, count]) => ({
                  label: name, value: count,
                  pct: (count / analysis.totalAlerts * 100).toFixed(1),
                }))}
              maxVal={Math.max(...Object.values(analysis.byTenant), 1)}
            />
          </div>

          {/* Flapping Alerts Detail */}
          {analysis.flapping.length > 0 && (
            <div className="bg-red-50 border border-red-200 rounded-lg p-4">
              <h3 className="font-semibold text-red-700 mb-2">
                {t('震盪告警詳情', 'Flapping Alert Details')}
              </h3>
              <p className="text-sm text-red-600 mb-2">
                {t('以下告警在 1 小時內觸發 ≥3 次，建議調整閾值或增加 for 持續時間。',
                   'These alerts fired ≥3 times within 1 hour. Consider adjusting thresholds or increasing `for` duration.')}
              </p>
              <ul className="space-y-1">
                {analysis.flapping.map(f => (
                  <li key={f.key} className="text-sm font-mono bg-white px-2 py-1 rounded">
                    {f.key} — {f.count} {t('次', 'fires')}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Recommendations */}
          <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
            <h3 className="font-semibold text-blue-700 mb-2">
              {t('改善建議', 'Recommendations')}
            </h3>
            <ul className="text-sm text-blue-800 space-y-1 list-disc list-inside">
              {analysis.flapping.length > 0 && (
                <li>{t('調整震盪告警的 for 持續時間或 group_wait 參數',
                       'Tune flapping alerts: increase `for` duration or `group_wait`')}</li>
              )}
              {parseFloat(analysis.dedupRate) > 30 && (
                <li>{t('啟用 Alertmanager group_by 以減少重複通知',
                       'Enable Alertmanager group_by to reduce duplicate notifications')}</li>
              )}
              {analysis.mttr > 60 && (
                <li>{t('MTTR 偏高，建議增加 runbook_url 加速處理',
                       'MTTR is high — add runbook_url annotations to speed resolution')}</li>
              )}
              {analysis.topNoisy[0] && analysis.topNoisy[0].count > analysis.totalAlerts * 0.3 && (
                <li>{t(`"${analysis.topNoisy[0].name}" 佔 ${analysis.topNoisy[0].pct}% 告警量，建議重新評估閾值`,
                       `"${analysis.topNoisy[0].name}" accounts for ${analysis.topNoisy[0].pct}% of alerts — review threshold`)}</li>
              )}
              {Object.keys(analysis.bySeverity).includes('critical') &&
               analysis.bySeverity.critical > analysis.totalAlerts * 0.2 && (
                <li>{t('Critical 告警比例偏高，建議檢查是否濫用嚴重度',
                       'High critical ratio — review if severity levels are being used appropriately')}</li>
              )}
            </ul>
          </div>
        </>
      )}
    </div>
  );
}
