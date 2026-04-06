---
title: "Notification Template Previewer"
tags: [notification, preview, routing, receiver, template]
audience: ["platform-engineer", "tenant"]
version: v2.5.0
lang: en
related: [self-service-portal, alert-simulator, template-gallery]
---

import React, { useState, useMemo, useCallback } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Receiver types and their notification templates ── */
const RECEIVER_TYPES = {
  slack: {
    label: 'Slack',
    icon: '💬',
    color: 'bg-purple-50 border-purple-300',
    fields: [
      { key: 'channel', label: '#channel', example: '#alerts-prod' },
      { key: 'api_url', label: 'Webhook URL', example: 'https://hooks.slack.com/services/T.../B.../xxx' },
    ],
  },
  webhook: {
    label: 'Webhook (Generic)',
    icon: '🔗',
    color: 'bg-blue-50 border-blue-300',
    fields: [
      { key: 'url', label: 'Endpoint URL', example: 'https://hooks.example.com/alerts' },
    ],
  },
  email: {
    label: 'Email',
    icon: '📧',
    color: 'bg-green-50 border-green-300',
    fields: [
      { key: 'to', label: 'To', example: 'sre-team@example.com' },
      { key: 'smarthost', label: 'SMTP Host', example: 'smtp.example.com:587' },
    ],
  },
  pagerduty: {
    label: 'PagerDuty',
    icon: '🚨',
    color: 'bg-red-50 border-red-300',
    fields: [
      { key: 'service_key', label: 'Service Key', example: 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' },
      { key: 'severity', label: 'PD Severity', example: 'critical' },
    ],
  },
  teams: {
    label: 'Microsoft Teams',
    icon: '🟦',
    color: 'bg-indigo-50 border-indigo-300',
    fields: [
      { key: 'webhook_url', label: 'Webhook URL', example: 'https://outlook.office.com/webhook/...' },
    ],
  },
  rocketchat: {
    label: 'Rocket.Chat',
    icon: '🚀',
    color: 'bg-orange-50 border-orange-300',
    fields: [
      { key: 'url', label: 'Webhook URL', example: 'https://rocket.example.com/hooks/...' },
    ],
  },
  opsgenie: {
    label: 'OpsGenie',
    icon: '🔔',
    color: 'bg-yellow-50 border-yellow-300',
    fields: [
      { key: 'api_key', label: 'API Key', example: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' },
      { key: 'api_url', label: 'API URL', example: 'https://api.opsgenie.com' },
    ],
  },
};

/* ── Sample alert data ── */
const SAMPLE_ALERTS = [
  {
    id: 'high-conn',
    name: 'MariaDBHighConnections',
    metric: 'mysql_connections',
    severity: 'warning',
    tenant: 'prod-mariadb',
    current: 165,
    threshold: 150,
    pack: 'MariaDB',
    labels: { alertname: 'MariaDBHighConnections', severity: 'warning', tenant: 'prod-mariadb', rule_pack: 'mariadb' },
    annotations: {
      summary: 'MySQL connections at 165 (threshold: 150)',
      summary_zh: 'MySQL 連線數達到 165（閾值：150）',
      platform_summary: '[prod-mariadb] MariaDB connections warning — 165/150',
      runbook_url: 'https://runbooks.example.com/mariadb-connections',
    },
  },
  {
    id: 'crit-conn',
    name: 'MariaDBHighConnections',
    metric: 'mysql_connections',
    severity: 'critical',
    tenant: 'prod-mariadb',
    current: 215,
    threshold: 200,
    pack: 'MariaDB',
    labels: { alertname: 'MariaDBHighConnections', severity: 'critical', tenant: 'prod-mariadb', rule_pack: 'mariadb' },
    annotations: {
      summary: 'MySQL connections at 215 (threshold: 200)',
      summary_zh: 'MySQL 連線數達到 215（閾值：200）',
      platform_summary: '[prod-mariadb] MariaDB connections CRITICAL — 215/200',
      runbook_url: 'https://runbooks.example.com/mariadb-connections',
    },
  },
  {
    id: 'kafka-lag',
    name: 'KafkaHighConsumerLag',
    metric: 'kafka_consumer_lag',
    severity: 'warning',
    tenant: 'prod-kafka',
    current: 75000,
    threshold: 50000,
    pack: 'Kafka',
    labels: { alertname: 'KafkaHighConsumerLag', severity: 'warning', tenant: 'prod-kafka', rule_pack: 'kafka' },
    annotations: {
      summary: 'Kafka consumer lag at 75000 (threshold: 50000)',
      summary_zh: 'Kafka 消費者延遲達到 75000（閾值：50000）',
      platform_summary: '[prod-kafka] Kafka consumer lag warning — 75000/50000',
      runbook_url: 'https://runbooks.example.com/kafka-lag',
    },
  },
  {
    id: 'redis-mem',
    name: 'RedisHighMemory',
    metric: 'redis_memory_used_bytes',
    severity: 'critical',
    tenant: 'prod-redis',
    current: 4500000000,
    threshold: 4294967296,
    pack: 'Redis',
    labels: { alertname: 'RedisHighMemory', severity: 'critical', tenant: 'prod-redis', rule_pack: 'redis' },
    annotations: {
      summary: 'Redis memory at 4.5GB (threshold: 4GB)',
      summary_zh: 'Redis 記憶體達到 4.5GB（閾值：4GB）',
      platform_summary: '[prod-redis] Redis memory CRITICAL — 4.5GB/4GB',
      runbook_url: 'https://runbooks.example.com/redis-memory',
    },
  },
];

/* ── Render Slack message ── */
function SlackPreview({ alert, config }) {
  const isCritical = alert.severity === 'critical';
  const color = isCritical ? '#dc2626' : '#f59e0b';
  const sideBarStyle = { backgroundColor: color };
  return (
    <div className="bg-white rounded-lg border shadow-sm overflow-hidden max-w-md">
      <div className="flex items-start gap-3 p-3">
        <div className="w-1 self-stretch rounded" style={sideBarStyle} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-bold">{isCritical ? '🔴' : '🟡'} [{alert.severity.toUpperCase()}]</span>
            <span className="text-sm font-medium text-gray-900">{alert.name}</span>
          </div>
          <div className="text-xs text-gray-600 space-y-0.5">
            <div><span className="font-medium">tenant:</span> {alert.tenant}</div>
            <div><span className="font-medium">metric:</span> {alert.metric} = {alert.current} (threshold: {alert.threshold})</div>
            <div>{alert.annotations.summary}</div>
          </div>
          {alert.annotations.runbook_url && (
            <div className="mt-1">
              <span className="text-xs text-blue-600 underline">{alert.annotations.runbook_url}</span>
            </div>
          )}
          <div className="mt-2 flex gap-2">
            <span className="px-2 py-0.5 bg-gray-100 rounded text-xs font-mono">{alert.labels.rule_pack}</span>
            <span className="px-2 py-0.5 bg-gray-100 rounded text-xs">{config.channel || '#alerts'}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── Render Email preview ── */
function EmailPreview({ alert, config }) {
  const isCritical = alert.severity === 'critical';
  return (
    <div className="bg-white rounded-lg border shadow-sm max-w-md font-sans">
      <div className={`px-4 py-2 ${isCritical ? 'bg-red-600' : 'bg-yellow-500'} text-white text-sm font-bold rounded-t-lg`}>
        [{alert.severity.toUpperCase()}] {alert.name} — {alert.tenant}
      </div>
      <div className="p-4 text-sm space-y-2">
        <div className="text-xs text-gray-500">
          To: {config.to || 'sre-team@example.com'}
        </div>
        <div className="border-b pb-2">
          <span className="font-medium">{t('告警摘要', 'Alert Summary')}:</span> {alert.annotations.summary}
        </div>
        <table className="text-xs w-full">
          <tbody>
            <tr><td className="py-0.5 font-medium w-24">Tenant</td><td>{alert.tenant}</td></tr>
            <tr><td className="py-0.5 font-medium">Metric</td><td className="font-mono">{alert.metric}</td></tr>
            <tr><td className="py-0.5 font-medium">Current</td><td>{alert.current}</td></tr>
            <tr><td className="py-0.5 font-medium">Threshold</td><td>{alert.threshold}</td></tr>
            <tr><td className="py-0.5 font-medium">Severity</td><td><span className={isCritical ? 'text-red-600 font-bold' : 'text-yellow-600 font-bold'}>{alert.severity}</span></td></tr>
            <tr><td className="py-0.5 font-medium">Rule Pack</td><td>{alert.labels.rule_pack}</td></tr>
          </tbody>
        </table>
        {alert.annotations.runbook_url && (
          <div className="pt-2 border-t text-xs">
            <span className="text-blue-600 underline">{alert.annotations.runbook_url}</span>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Render PagerDuty preview ── */
function PagerDutyPreview({ alert }) {
  const isCritical = alert.severity === 'critical';
  return (
    <div className="bg-white rounded-lg border shadow-sm max-w-md">
      <div className={`px-4 py-2 ${isCritical ? 'bg-red-700' : 'bg-yellow-600'} text-white rounded-t-lg flex items-center gap-2`}>
        <span className="text-lg">🚨</span>
        <span className="text-sm font-bold">PagerDuty Incident</span>
      </div>
      <div className="p-4 space-y-2">
        <div className="text-sm font-bold text-gray-900">{alert.name}</div>
        <div className="text-xs text-gray-600">{alert.annotations.platform_summary}</div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="p-2 bg-gray-50 rounded">
            <div className="text-gray-500">Severity</div>
            <div className={`font-bold ${isCritical ? 'text-red-700' : 'text-yellow-600'}`}>{alert.severity}</div>
          </div>
          <div className="p-2 bg-gray-50 rounded">
            <div className="text-gray-500">Tenant</div>
            <div className="font-medium">{alert.tenant}</div>
          </div>
          <div className="p-2 bg-gray-50 rounded">
            <div className="text-gray-500">Metric</div>
            <div className="font-mono">{alert.metric}</div>
          </div>
          <div className="p-2 bg-gray-50 rounded">
            <div className="text-gray-500">Value</div>
            <div className="font-mono">{alert.current} / {alert.threshold}</div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── Render Webhook JSON preview ── */
function WebhookPreview({ alert, config }) {
  const payload = {
    status: 'firing',
    alerts: [{
      status: 'firing',
      labels: alert.labels,
      annotations: alert.annotations,
      startsAt: new Date().toISOString(),
      generatorURL: `http://prometheus:9090/graph?g0.expr=${alert.metric}`,
    }],
    groupLabels: { alertname: alert.name, tenant: alert.tenant },
    commonLabels: alert.labels,
    externalURL: 'http://alertmanager:9093',
  };

  return (
    <div className="bg-white rounded-lg border shadow-sm max-w-md">
      <div className="px-4 py-2 bg-gray-800 text-gray-200 rounded-t-lg text-xs font-mono flex items-center gap-2">
        <span className="text-green-400">POST</span>
        <span className="truncate">{config.url || 'https://hooks.example.com/alerts'}</span>
      </div>
      <pre className="p-3 text-xs font-mono bg-gray-900 text-gray-100 overflow-x-auto rounded-b-lg max-h-48 overflow-y-auto">
        {JSON.stringify(payload, null, 2)}
      </pre>
    </div>
  );
}

/* ── Render Teams preview ── */
function TeamsPreview({ alert }) {
  const isCritical = alert.severity === 'critical';
  return (
    <div className="bg-white rounded-lg border shadow-sm max-w-md">
      <div className={`h-1 ${isCritical ? 'bg-red-600' : 'bg-yellow-500'} rounded-t-lg`} />
      <div className="p-4 space-y-2">
        <div className="flex items-center gap-2">
          <span className="text-lg">🟦</span>
          <span className="text-sm font-bold text-gray-900">{alert.name}</span>
          <span className={`text-xs px-1.5 py-0.5 rounded ${isCritical ? 'bg-red-100 text-red-800' : 'bg-yellow-100 text-yellow-800'}`}>
            {alert.severity}
          </span>
        </div>
        <div className="text-xs text-gray-600">{alert.annotations.summary}</div>
        <div className="border-t pt-2 text-xs space-y-1">
          <div><span className="font-medium">Tenant:</span> {alert.tenant}</div>
          <div><span className="font-medium">Metric:</span> <code className="bg-gray-100 px-1 rounded">{alert.metric}</code> = {alert.current}</div>
          <div><span className="font-medium">Threshold:</span> {alert.threshold}</div>
        </div>
        {alert.annotations.runbook_url && (
          <div className="text-xs text-blue-600 underline">{t('查看 Runbook', 'View Runbook')}</div>
        )}
      </div>
    </div>
  );
}

/* ── Generic preview (Rocket.Chat, etc.) ── */
function GenericPreview({ alert }) {
  return (
    <div className="bg-white rounded-lg border shadow-sm max-w-md p-4 space-y-2">
      <div className="text-sm font-bold">[{alert.severity.toUpperCase()}] {alert.name}</div>
      <div className="text-xs text-gray-600">{alert.annotations.summary}</div>
      <div className="text-xs font-mono bg-gray-50 p-2 rounded">
        tenant={alert.tenant} metric={alert.metric} value={alert.current}
      </div>
    </div>
  );
}

/* ── Dual-perspective annotation preview ── */
function DualPerspective({ alert }) {
  return (
    <div className="p-3 bg-gray-50 rounded-lg border text-xs space-y-2">
      <div className="font-medium text-gray-700">{t('雙視角 Annotation (ADR-007)', 'Dual-Perspective Annotations (ADR-007)')}</div>
      <div className="grid grid-cols-1 gap-2">
        <div className="p-2 bg-white rounded border">
          <div className="text-gray-500 text-xs mb-0.5">summary <span className="text-gray-400">({t('租戶視角', 'tenant view')})</span></div>
          <div className="font-mono">{alert.annotations.summary}</div>
        </div>
        <div className="p-2 bg-white rounded border">
          <div className="text-gray-500 text-xs mb-0.5">summary_zh <span className="text-gray-400">({t('中文版', 'Chinese')})</span></div>
          <div className="font-mono">{alert.annotations.summary_zh}</div>
        </div>
        <div className="p-2 bg-amber-50 rounded border border-amber-200">
          <div className="text-amber-700 text-xs mb-0.5">platform_summary <span className="text-gray-400">({t('平台 NOC 視角', 'platform NOC view')})</span></div>
          <div className="font-mono">{alert.annotations.platform_summary}</div>
        </div>
      </div>
    </div>
  );
}

/* ── Inhibit rule explanation ── */
function InhibitExplanation({ alerts }) {
  const hasWarning = alerts.some(a => a.severity === 'warning');
  const hasCritical = alerts.some(a => a.severity === 'critical');
  const sameMetric = hasWarning && hasCritical &&
    alerts.filter(a => a.severity === 'warning')[0]?.metric === alerts.filter(a => a.severity === 'critical')[0]?.metric;

  if (!sameMetric) return null;

  return (
    <div className="p-3 bg-purple-50 rounded-lg border border-purple-200 text-xs">
      <div className="font-medium text-purple-800 mb-1">
        {t('Severity Dedup 生效', 'Severity Dedup Active')}
      </div>
      <div className="text-purple-700">
        {t(
          '同一 metric 同時有 warning 和 critical 觸發 — Alertmanager inhibit rule 將抑制 WARNING，只發送 CRITICAL 通知。',
          'Same metric has both warning and critical firing — Alertmanager inhibit rule will suppress WARNING, only CRITICAL notification is sent.'
        )}
      </div>
      <div className="mt-1 flex items-center gap-2 text-purple-600">
        <span className="line-through opacity-50">🟡 WARNING</span>
        <span>&rarr;</span>
        <span className="font-bold">🔴 CRITICAL {t('（唯一送達）', '(only one delivered)')}</span>
      </div>
    </div>
  );
}

/* ── Main Component ── */
export default function NotificationPreviewer() {
  const [receiverType, setReceiverType] = useState('slack');
  const [selectedAlert, setSelectedAlert] = useState('high-conn');
  const [receiverConfig, setReceiverConfig] = useState({});
  const [showDual, setShowDual] = useState(false);

  const alert = useMemo(() => SAMPLE_ALERTS.find(a => a.id === selectedAlert), [selectedAlert]);
  const receiver = RECEIVER_TYPES[receiverType];

  const renderPreview = useCallback(() => {
    if (!alert) return null;
    switch (receiverType) {
      case 'slack': return <SlackPreview alert={alert} config={receiverConfig} />;
      case 'email': return <EmailPreview alert={alert} config={receiverConfig} />;
      case 'pagerduty': return <PagerDutyPreview alert={alert} />;
      case 'webhook': return <WebhookPreview alert={alert} config={receiverConfig} />;
      case 'teams': return <TeamsPreview alert={alert} />;
      case 'rocketchat': return <GenericPreview alert={alert} />;
      case 'opsgenie': return <GenericPreview alert={alert} />;
      default: return <GenericPreview alert={alert} />;
    }
  }, [alert, receiverType, receiverConfig]);

  return (
    <div className="max-w-4xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">
          {t('通知模板預覽器', 'Notification Template Previewer')}
        </h1>
        <p className="text-gray-600 mt-1">
          {t('預覽不同 receiver 類型的告警通知長什麼樣子 — 包括 Slack、Email、PagerDuty、Teams、Webhook JSON。',
             'Preview what alert notifications look like for each receiver type — Slack, Email, PagerDuty, Teams, Webhook JSON.')}
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left: Controls */}
        <div className="space-y-4">
          {/* Receiver type selector */}
          <div>
            <div className="text-sm font-medium text-gray-700 mb-2">
              {t('Receiver 類型', 'Receiver Type')}
            </div>
            <div className="space-y-1.5">
              {Object.entries(RECEIVER_TYPES).map(([id, r]) => (
                <button
                  key={id}
                  onClick={() => setReceiverType(id)}
                  className={`w-full p-2 rounded-lg border text-left text-sm transition-all ${
                    receiverType === id ? `${r.color} border-2` : 'border-gray-200 hover:border-gray-300'
                  }`}
                >
                  <span className="mr-2">{r.icon}</span>
                  {r.label}
                </button>
              ))}
            </div>
          </div>

          {/* Alert selector */}
          <div>
            <div className="text-sm font-medium text-gray-700 mb-2">
              {t('選擇告警', 'Select Alert')}
            </div>
            <div className="space-y-1.5">
              {SAMPLE_ALERTS.map(a => (
                <button
                  key={a.id}
                  onClick={() => setSelectedAlert(a.id)}
                  className={`w-full p-2 rounded-lg border text-left text-xs transition-all ${
                    selectedAlert === a.id ? 'border-blue-500 bg-blue-50 border-2' : 'border-gray-200 hover:border-gray-300'
                  }`}
                >
                  <span className={`mr-1 ${a.severity === 'critical' ? 'text-red-600' : 'text-yellow-600'}`}>
                    {a.severity === 'critical' ? '🔴' : '🟡'}
                  </span>
                  <span className="font-mono">{a.name}</span>
                  <span className="ml-1 text-gray-400">({a.tenant})</span>
                </button>
              ))}
            </div>
          </div>

          {/* Receiver config */}
          {receiver.fields && receiver.fields.length > 0 && (
            <div>
              <div className="text-sm font-medium text-gray-700 mb-2">
                {t('Receiver 設定', 'Receiver Config')}
              </div>
              {receiver.fields.map(f => (
                <div key={f.key} className="mb-2">
                  <label className="text-xs text-gray-500 block mb-0.5">{f.label}</label>
                  <input
                    type="text"
                    value={receiverConfig[f.key] || ''}
                    onChange={(e) => setReceiverConfig(prev => ({ ...prev, [f.key]: e.target.value }))}
                    placeholder={f.example}
                    className="w-full text-xs px-2 py-1.5 border rounded focus:ring-1 focus:ring-blue-500"
                  />
                </div>
              ))}
            </div>
          )}

          <button
            onClick={() => setShowDual(!showDual)}
            className="text-xs text-blue-600 hover:text-blue-800"
          >
            {showDual
              ? t('▾ 隱藏雙視角 Annotation', '▾ Hide dual-perspective annotations')
              : t('▸ 顯示雙視角 Annotation', '▸ Show dual-perspective annotations')}
          </button>
        </div>

        {/* Right: Preview */}
        <div className="lg:col-span-2 space-y-4">
          <div className="text-sm font-medium text-gray-700">
            {t('通知預覽', 'Notification Preview')}
            <span className="ml-2 text-gray-400 text-xs">
              {receiver.icon} {receiver.label}
            </span>
          </div>

          {/* Notification render */}
          <div className="p-4 bg-gray-100 rounded-lg min-h-48">
            {renderPreview()}
          </div>

          {/* Dual perspective */}
          {showDual && alert && <DualPerspective alert={alert} />}

          {/* Inhibit explanation */}
          <InhibitExplanation alerts={SAMPLE_ALERTS.filter(a =>
            a.metric === alert?.metric && a.tenant === alert?.tenant
          )} />

          {/* Labels table */}
          {alert && (
            <div className="p-3 bg-gray-50 rounded-lg border">
              <div className="text-xs font-medium text-gray-700 mb-2">{t('完整標籤', 'Full Labels')}</div>
              <div className="grid grid-cols-2 gap-1 text-xs">
                {Object.entries(alert.labels).map(([k, v]) => (
                  <div key={k} className="flex gap-2">
                    <span className="font-mono text-gray-500">{k}:</span>
                    <span className="font-mono font-medium">{v}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Footer */}
      <div className="mt-6 p-4 bg-blue-50 rounded-lg border border-blue-100">
        <h4 className="text-sm font-medium text-blue-800 mb-2">{t('提示', 'Tips')}</h4>
        <ul className="text-sm text-blue-700 space-y-1">
          <li>{t('• 實際通知格式由 Alertmanager 模板決定 — 此預覽為概念性展示。',
                 '• Actual notification format is determined by Alertmanager templates — this preview is conceptual.')}</li>
          <li>{t('• 使用 da-tools test-notification 進行真實通知測試。',
                 '• Use da-tools test-notification for real notification testing.')}</li>
          <li>{t('• platform_summary annotation 由 _routing_enforced 路由使用，確保 NOC 可見性。',
                 '• platform_summary annotation is used by _routing_enforced routes for NOC visibility.')}</li>
        </ul>
      </div>
    </div>
  );
}
