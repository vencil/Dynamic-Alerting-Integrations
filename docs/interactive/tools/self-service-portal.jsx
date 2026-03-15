---
title: "Tenant Self-Service Portal"
tags: [self-service, validation, routing, alerts, tenant]
audience: ["platform-engineer", "domain-expert", "tenant"]
version: v2.0.0
lang: en
related: [playground, config-lint, alert-simulator, schema-explorer]
---

import React, { useState, useMemo, useCallback } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Reserved keys and validation constants ── */
const RESERVED_KEYS = new Set([
  '_silent_mode', '_severity_dedup', '_namespaces', '_metadata', '_profile'
]);
const RESERVED_PREFIXES = ['_state_', '_routing'];
const RECEIVER_TYPES = ['webhook', 'email', 'slack', 'teams', 'rocketchat', 'pagerduty'];
const RECEIVER_REQUIRED = {
  webhook: ['url'], email: ['to', 'smarthost'], slack: ['api_url'],
  teams: ['webhook_url'], rocketchat: ['url'], pagerduty: ['service_key'],
};
const TIMING_GUARDRAILS = {
  group_wait: { min: 5, max: 300, unit: 's' },
  group_interval: { min: 5, max: 300, unit: 's' },
  repeat_interval: { min: 60, max: 259200, unit: 's' },
};

const SAMPLE_YAML = `# Tenant YAML 範例
mysql_connections: "80"
mysql_cpu: "70"
container_memory: "85"

_routing:
  receiver:
    type: webhook
    url: https://hooks.example.com/alerts
  group_by: [alertname, severity]
  group_wait: "30s"
  repeat_interval: "4h"

_metadata:
  runbook_url: https://runbooks.example.com/db-a
  owner: platform-team
  tier: production

_severity_dedup: enable
`;

const DEFAULT_METRICS = {
  mysql_connections: { current: 85, unit: 'connections' },
  mysql_cpu: { current: 45, unit: '%' },
  container_memory: { current: 72, unit: '%' },
  container_cpu: { current: 60, unit: '%' },
  redis_memory_percent: { current: 55, unit: '%' },
};

/* ── Duration parser ── */
function parseDuration(str) {
  if (!str) return null;
  const m = String(str).match(/^(\d+\.?\d*)([smhd])$/);
  if (!m) return null;
  const multi = { s: 1, m: 60, h: 3600, d: 86400 };
  return parseFloat(m[1]) * (multi[m[2]] || 1);
}

/* ── Simple YAML parser (for tenant config subset) ── */
const UNSAFE_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
const MAX_YAML_SIZE = 100000;  // 100KB limit

function parseYaml(text) {
  const errors = [];
  if (text.length > MAX_YAML_SIZE) {
    return { config: {}, errors: [t('YAML 超過大小限制（100KB）', 'YAML exceeds size limit (100KB)')] };
  }
  const config = {};
  let currentKey = null;
  let currentObj = null;
  let indent = 0;

  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.replace(/#.*$/, '').trimEnd();
    if (!trimmed || trimmed.trim() === '') continue;

    const lineIndent = line.search(/\S/);
    const content = trimmed.trim();

    // Simple key: value
    const kvMatch = content.match(/^([^:]+):\s*(.+)$/);
    const objMatch = content.match(/^([^:]+):\s*$/);

    if (lineIndent === 0 && kvMatch) {
      const key = kvMatch[1].trim();
      if (UNSAFE_KEYS.has(key)) continue;  // Prototype pollution guard
      let val = kvMatch[2].trim();
      if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
      if (val.startsWith("'") && val.endsWith("'")) val = val.slice(1, -1);
      if (val.startsWith('[') && val.endsWith(']')) {
        val = val.slice(1, -1).split(',').map(s => s.trim().replace(/"/g, '').replace(/'/g, ''));
      }
      config[key] = val;
      currentKey = null;
      currentObj = null;
    } else if (lineIndent === 0 && objMatch) {
      const key = objMatch[1].trim();
      if (UNSAFE_KEYS.has(key)) continue;  // Prototype pollution guard
      config[key] = {};
      currentKey = key;
      currentObj = config[key];
      indent = lineIndent;
    } else if (currentKey && lineIndent > 0 && kvMatch) {
      const key = kvMatch[1].trim();
      let val = kvMatch[2].trim();
      if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
      if (val.startsWith("'") && val.endsWith("'")) val = val.slice(1, -1);
      if (val.startsWith('[') && val.endsWith(']')) {
        val = val.slice(1, -1).split(',').map(s => s.trim().replace(/"/g, '').replace(/'/g, ''));
      }

      // Nested object navigation
      if (currentKey === '_routing' || currentKey === '_metadata') {
        const parts = [];
        let tempLine = line;
        const baseIndent = 2;
        const depth = Math.floor(lineIndent / baseIndent) - 1;

        if (depth === 0) {
          currentObj[key] = val;
        } else if (depth === 1 && typeof currentObj[Object.keys(currentObj).pop()] === 'object') {
          const parentKey = Object.keys(currentObj).pop();
          if (typeof currentObj[parentKey] === 'object') {
            currentObj[parentKey][key] = val;
          }
        }
      }
    } else if (currentKey && lineIndent > 0 && objMatch) {
      const key = objMatch[1].trim();
      if (currentObj) {
        currentObj[key] = {};
      }
    }
  }
  return { config, errors };
}

/* ── Validation engine ── */
function validateConfig(config) {
  const issues = [];
  const info = [];

  // Check metric thresholds
  for (const [key, val] of Object.entries(config)) {
    if (key.startsWith('_')) continue;
    const numVal = parseFloat(val);
    if (!isNaN(numVal)) {
      if (numVal > 100) {
        issues.push({ level: 'warning', field: key,
          msg: t(`閾值 ${numVal} 超過 100，確認是否正確`, `Threshold ${numVal} exceeds 100, verify if correct`) });
      }
      if (numVal <= 0) {
        issues.push({ level: 'error', field: key,
          msg: t(`閾值 ${numVal} ≤ 0，無效`, `Threshold ${numVal} ≤ 0, invalid`) });
      }
      if (String(val) === 'disable') {
        info.push({ level: 'info', field: key,
          msg: t('已禁用此指標', 'Metric disabled') });
      }
    }
  }

  // Check routing
  const routing = config._routing;
  if (routing && typeof routing === 'object') {
    const receiver = routing.receiver;
    if (receiver) {
      const rtype = receiver.type;
      if (rtype && !RECEIVER_TYPES.includes(rtype)) {
        issues.push({ level: 'error', field: '_routing.receiver.type',
          msg: t(`不支援的 receiver 類型: ${rtype}`, `Unsupported receiver type: ${rtype}`) });
      }
      if (rtype && RECEIVER_REQUIRED[rtype]) {
        for (const req of RECEIVER_REQUIRED[rtype]) {
          if (!receiver[req]) {
            issues.push({ level: 'error', field: `_routing.receiver.${req}`,
              msg: t(`${rtype} receiver 必須指定 ${req}`, `${rtype} receiver requires ${req}`) });
          }
        }
      }
      // URL scheme validation
      if (rtype === 'webhook' && receiver.url) {
        try {
          const parsed = new URL(receiver.url);
          if (!['http:', 'https:'].includes(parsed.protocol)) {
            issues.push({ level: 'error', field: '_routing.receiver.url',
              msg: t('僅允許 http/https URL', 'Only http/https URLs allowed') });
          } else if (parsed.protocol !== 'https:') {
            issues.push({ level: 'error', field: '_routing.receiver.url',
              msg: t('生產環境必須使用 HTTPS — HTTP 會導致告警通知明文傳輸', 'Production requires HTTPS — HTTP transmits alert notifications in plaintext') });
          }
        } catch (e) {
          issues.push({ level: 'error', field: '_routing.receiver.url',
            msg: t('無效的 URL 格式', 'Invalid URL format') });
        }
      }
    }

    // Timing guardrails
    for (const [param, guard] of Object.entries(TIMING_GUARDRAILS)) {
      const val = routing[param];
      if (val) {
        const secs = parseDuration(val);
        if (secs !== null) {
          if (secs < guard.min) {
            issues.push({ level: 'warning', field: `_routing.${param}`,
              msg: t(`${val} 低於下限 ${guard.min}s`, `${val} below minimum ${guard.min}s`) });
          }
          if (secs > guard.max) {
            issues.push({ level: 'warning', field: `_routing.${param}`,
              msg: t(`${val} 超過上限 ${guard.max}s`, `${val} exceeds maximum ${guard.max}s`) });
          }
        }
      }
    }
  }

  // Check metadata
  if (config._metadata && typeof config._metadata === 'object') {
    if (!config._metadata.runbook_url) {
      issues.push({ level: 'info', field: '_metadata.runbook_url',
        msg: t('建議配置 runbook URL', 'Consider adding runbook URL') });
    }
  }

  // Check unknown keys
  for (const key of Object.keys(config)) {
    if (key.startsWith('_')) {
      if (!RESERVED_KEYS.has(key) && !RESERVED_PREFIXES.some(p => key.startsWith(p))) {
        issues.push({ level: 'warning', field: key,
          msg: t(`未知的保留字 key: ${key}`, `Unknown reserved key: ${key}`) });
      }
    }
  }

  return { issues: [...issues, ...info] };
}

/* ── Alert preview engine ── */
function simulateAlerts(config, metricValues) {
  const alerts = [];

  for (const [metric, val] of Object.entries(metricValues)) {
    const threshold = config[metric];
    if (!threshold || threshold === 'disable') continue;

    const thresholdNum = parseFloat(threshold);
    if (isNaN(thresholdNum)) continue;

    const currentVal = val.current;
    const firing = currentVal >= thresholdNum;

    // Check critical threshold
    const critKey = `${metric}_critical`;
    const critThreshold = config[critKey] ? parseFloat(config[critKey]) : null;
    const critFiring = critThreshold !== null && currentVal >= critThreshold;

    alerts.push({
      metric,
      current: currentVal,
      threshold: thresholdNum,
      critical_threshold: critThreshold,
      firing,
      critical_firing: critFiring,
      severity: critFiring ? 'critical' : (firing ? 'warning' : 'ok'),
      unit: val.unit,
    });
  }

  return alerts;
}

/* ── Routing tree builder ── */
function buildRoutingTree(config) {
  const tree = {
    name: 'root',
    match: 'default',
    children: [],
  };

  const routing = config._routing;
  if (!routing) {
    tree.children.push({
      name: t('平台預設路由', 'Platform default route'),
      match: t('繼承 _routing_defaults', 'Inherits _routing_defaults'),
      type: 'default',
    });
    return tree;
  }

  const receiver = routing.receiver || {};
  const mainRoute = {
    name: `${receiver.type || 'unknown'} receiver`,
    match: t('所有告警', 'All alerts'),
    type: receiver.type || 'unknown',
    details: {
      group_by: routing.group_by || ['alertname', 'tenant'],
      group_wait: routing.group_wait || '30s',
      repeat_interval: routing.repeat_interval || '4h',
    },
  };

  if (receiver.url) mainRoute.details.url = receiver.url;
  if (receiver.to) mainRoute.details.to = receiver.to;

  tree.children.push(mainRoute);

  // Overrides
  const overrides = routing.overrides;
  if (Array.isArray(overrides)) {
    for (const ovr of overrides) {
      tree.children.push({
        name: `Override: ${ovr.alertname || ovr.metric_group || '?'}`,
        match: ovr.alertname ? `alertname="${ovr.alertname}"` : `metric_group="${ovr.metric_group}"`,
        type: ovr.receiver?.type || 'override',
      });
    }
  }

  // Dedup
  if (config._severity_dedup === 'enable') {
    tree.children.push({
      name: t('嚴重度去重 (inhibit)', 'Severity dedup (inhibit)'),
      match: 'severity=critical → inhibit warning',
      type: 'inhibit',
    });
  }

  return tree;
}

/* ── Tab: YAML Validator ── */
function YamlValidatorTab() {
  const [yaml, setYaml] = useState(SAMPLE_YAML);
  const [result, setResult] = useState(null);

  const validate = useCallback(() => {
    const { config, errors } = parseYaml(yaml);
    const validation = validateConfig(config);
    setResult({ config, parseErrors: errors, ...validation });
  }, [yaml]);

  const issueIcon = (level) => {
    if (level === 'error') return '🔴';
    if (level === 'warning') return '🟡';
    return '🔵';
  };

  return (
    <div>
      <h3 className="text-lg font-semibold mb-3">
        {t('YAML 驗證', 'YAML Validation')}
      </h3>
      <p className="text-sm text-gray-600 mb-4">
        {t('貼入 tenant YAML，即時檢查 schema、routing、policy 問題。',
           'Paste tenant YAML to check schema, routing, and policy issues.')}
      </p>

      <textarea
        value={yaml}
        onChange={(e) => setYaml(e.target.value)}
        className="w-full h-64 font-mono text-sm p-3 border rounded-lg bg-gray-50 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
        placeholder={t('貼入 tenant YAML...', 'Paste tenant YAML...')}
      />

      <button
        onClick={validate}
        className="mt-3 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
      >
        {t('驗證', 'Validate')}
      </button>

      {result && (
        <div className="mt-4 space-y-2">
          {result.issues.length === 0 ? (
            <div className="p-3 bg-green-50 border border-green-200 rounded-lg text-green-800">
              {t('✓ 所有檢查通過', '✓ All checks passed')}
            </div>
          ) : (
            result.issues.map((issue, i) => (
              <div key={i} className={`p-3 rounded-lg border ${
                issue.level === 'error' ? 'bg-red-50 border-red-200 text-red-800' :
                issue.level === 'warning' ? 'bg-yellow-50 border-yellow-200 text-yellow-800' :
                'bg-blue-50 border-blue-200 text-blue-800'
              }`}>
                <span className="mr-2">{issueIcon(issue.level)}</span>
                <code className="text-xs font-mono bg-white bg-opacity-50 px-1 rounded">{issue.field}</code>
                <span className="ml-2">{issue.msg}</span>
              </div>
            ))
          )}

          <div className="mt-3 p-3 bg-gray-50 rounded-lg">
            <span className="text-sm font-medium text-gray-700">
              {t(`共 ${result.issues.filter(i => i.level === 'error').length} 錯誤、` +
                 `${result.issues.filter(i => i.level === 'warning').length} 警告、` +
                 `${result.issues.filter(i => i.level === 'info').length} 建議`,
                 `${result.issues.filter(i => i.level === 'error').length} errors, ` +
                 `${result.issues.filter(i => i.level === 'warning').length} warnings, ` +
                 `${result.issues.filter(i => i.level === 'info').length} info`)}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Tab: Alert Preview ── */
function AlertPreviewTab() {
  const [yaml, setYaml] = useState(SAMPLE_YAML);
  const [metrics, setMetrics] = useState({ ...DEFAULT_METRICS });
  const [alerts, setAlerts] = useState(null);

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

  return (
    <div>
      <h3 className="text-lg font-semibold mb-3">
        {t('告警預覽', 'Alert Preview')}
      </h3>
      <p className="text-sm text-gray-600 mb-4">
        {t('調整模擬指標值，預覽哪些告警會觸發。',
           'Adjust simulated metric values to preview which alerts would fire.')}
      </p>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div>
          <h4 className="text-sm font-medium text-gray-700 mb-2">
            {t('Tenant 配置', 'Tenant Config')}
          </h4>
          <textarea
            value={yaml}
            onChange={(e) => setYaml(e.target.value)}
            className="w-full h-40 font-mono text-xs p-2 border rounded-lg bg-gray-50"
          />
        </div>

        <div>
          <h4 className="text-sm font-medium text-gray-700 mb-2">
            {t('模擬指標值', 'Simulated Metrics')}
          </h4>
          <div className="space-y-2">
            {Object.entries(metrics).map(([key, val]) => (
              <div key={key} className="flex items-center gap-2">
                <label className="text-xs font-mono w-40 text-gray-600">{key}</label>
                <input
                  type="range"
                  min="0" max="100"
                  value={val.current}
                  onChange={(e) => updateMetric(key, e.target.value)}
                  className="flex-1"
                />
                <span className="text-sm font-mono w-16 text-right">
                  {val.current}{val.unit === '%' ? '%' : ''}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <button
        onClick={simulate}
        className="mt-3 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
      >
        {t('模擬', 'Simulate')}
      </button>

      {alerts && (
        <div className="mt-4">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-100">
                  <th className="px-3 py-2 text-left">{t('指標', 'Metric')}</th>
                  <th className="px-3 py-2 text-right">{t('目前值', 'Current')}</th>
                  <th className="px-3 py-2 text-right">{t('閾值', 'Threshold')}</th>
                  <th className="px-3 py-2 text-center">{t('狀態', 'Status')}</th>
                </tr>
              </thead>
              <tbody>
                {alerts.map((a, i) => (
                  <tr key={i} className={
                    a.critical_firing ? 'bg-red-50' :
                    a.firing ? 'bg-yellow-50' : 'bg-green-50'
                  }>
                    <td className="px-3 py-2 font-mono text-xs">{a.metric}</td>
                    <td className="px-3 py-2 text-right">{a.current}{a.unit === '%' ? '%' : ''}</td>
                    <td className="px-3 py-2 text-right">
                      {a.threshold}
                      {a.critical_threshold ? ` / ${a.critical_threshold}` : ''}
                    </td>
                    <td className="px-3 py-2 text-center">
                      {a.critical_firing ? (
                        <span className="px-2 py-1 bg-red-100 text-red-800 rounded-full text-xs font-medium">
                          CRITICAL
                        </span>
                      ) : a.firing ? (
                        <span className="px-2 py-1 bg-yellow-100 text-yellow-800 rounded-full text-xs font-medium">
                          FIRING
                        </span>
                      ) : (
                        <span className="px-2 py-1 bg-green-100 text-green-800 rounded-full text-xs font-medium">
                          OK
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-3 text-sm text-gray-600">
            {t(`${alerts.filter(a => a.firing).length} 個告警觸發中（共 ${alerts.length} 個指標）`,
               `${alerts.filter(a => a.firing).length} alerts firing (${alerts.length} metrics total)`)}
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Tab: Routing Visualization ── */
function RoutingVizTab() {
  const [yaml, setYaml] = useState(SAMPLE_YAML);
  const [tree, setTree] = useState(null);

  const visualize = useCallback(() => {
    const { config } = parseYaml(yaml);
    setTree(buildRoutingTree(config));
  }, [yaml]);

  const typeColors = {
    webhook: 'bg-purple-100 text-purple-800 border-purple-200',
    email: 'bg-blue-100 text-blue-800 border-blue-200',
    slack: 'bg-green-100 text-green-800 border-green-200',
    teams: 'bg-indigo-100 text-indigo-800 border-indigo-200',
    pagerduty: 'bg-red-100 text-red-800 border-red-200',
    rocketchat: 'bg-orange-100 text-orange-800 border-orange-200',
    default: 'bg-gray-100 text-gray-800 border-gray-200',
    inhibit: 'bg-yellow-100 text-yellow-800 border-yellow-200',
    override: 'bg-pink-100 text-pink-800 border-pink-200',
  };

  return (
    <div>
      <h3 className="text-lg font-semibold mb-3">
        {t('路由視覺化', 'Routing Visualization')}
      </h3>
      <p className="text-sm text-gray-600 mb-4">
        {t('以樹狀圖呈現 Alertmanager 路由結構。',
           'Tree view of your Alertmanager routing structure.')}
      </p>

      <textarea
        value={yaml}
        onChange={(e) => setYaml(e.target.value)}
        className="w-full h-40 font-mono text-xs p-2 border rounded-lg bg-gray-50"
      />

      <button
        onClick={visualize}
        className="mt-3 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
      >
        {t('產生路由圖', 'Generate Route Tree')}
      </button>

      {tree && (
        <div className="mt-4 p-4 bg-gray-50 rounded-lg">
          {/* Root */}
          <div className="flex items-center gap-2 mb-4">
            <div className="w-3 h-3 bg-gray-400 rounded-full"></div>
            <span className="font-medium">{t('Alertmanager 根路由', 'Alertmanager Root Route')}</span>
          </div>

          {/* Children */}
          <div className="ml-6 space-y-3">
            {tree.children.map((node, i) => (
              <div key={i} className="relative">
                <div className="absolute left-0 top-0 bottom-0 w-px bg-gray-300" style={{ left: '-12px' }}></div>
                <div className="absolute w-3 h-px bg-gray-300" style={{ left: '-12px', top: '50%' }}></div>

                <div className={`p-3 rounded-lg border ${typeColors[node.type] || typeColors.default}`}>
                  <div className="flex items-center justify-between">
                    <span className="font-medium text-sm">{node.name}</span>
                    <span className="text-xs px-2 py-0.5 bg-white bg-opacity-50 rounded">
                      {node.type}
                    </span>
                  </div>
                  <div className="text-xs mt-1 opacity-75">
                    {t('匹配', 'Match')}: {node.match}
                  </div>
                  {node.details && (
                    <div className="mt-2 text-xs space-y-0.5 opacity-75">
                      {node.details.group_by && (
                        <div>group_by: [{Array.isArray(node.details.group_by) ? node.details.group_by.join(', ') : node.details.group_by}]</div>
                      )}
                      {node.details.group_wait && <div>group_wait: {node.details.group_wait}</div>}
                      {node.details.repeat_interval && <div>repeat_interval: {node.details.repeat_interval}</div>}
                      {node.details.url && <div>url: {node.details.url}</div>}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Main Portal Component ── */
const TABS = [
  { id: 'validate', label: () => t('YAML 驗證', 'YAML Validation'), icon: '🔍' },
  { id: 'alerts', label: () => t('告警預覽', 'Alert Preview'), icon: '🔔' },
  { id: 'routing', label: () => t('路由視覺化', 'Routing Viz'), icon: '🌐' },
];

export default function SelfServicePortal() {
  const [activeTab, setActiveTab] = useState('validate');

  return (
    <div className="max-w-4xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">
          {t('租戶自助入口', 'Tenant Self-Service Portal')}
        </h1>
        <p className="text-gray-600 mt-1">
          {t('驗證配置、預覽告警、視覺化路由 — 無需 CLI 或部署。',
             'Validate configs, preview alerts, visualize routing — no CLI or deployment needed.')}
        </p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-gray-100 p-1 rounded-lg mb-6">
        {TABS.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex-1 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
              activeTab === tab.id
                ? 'bg-white text-blue-600 shadow-sm'
                : 'text-gray-600 hover:text-gray-800'
            }`}
          >
            <span className="mr-1">{tab.icon}</span>
            {tab.label()}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="bg-white rounded-lg border p-6">
        {activeTab === 'validate' && <YamlValidatorTab />}
        {activeTab === 'alerts' && <AlertPreviewTab />}
        {activeTab === 'routing' && <RoutingVizTab />}
      </div>

      {/* Footer info */}
      <div className="mt-6 p-4 bg-blue-50 rounded-lg border border-blue-100">
        <h4 className="text-sm font-medium text-blue-800 mb-2">
          {t('提示', 'Tips')}
        </h4>
        <ul className="text-sm text-blue-700 space-y-1">
          <li>{t('• 此工具在瀏覽器端執行，YAML 不會送往任何伺服器。',
                 '• This tool runs entirely in your browser — YAML is never sent to any server.')}</li>
          <li>{t('• 完整驗證請使用 CLI: da-tools validate-config --config-dir conf.d/',
                 '• For full validation use CLI: da-tools validate-config --config-dir conf.d/')}</li>
          <li>{t('• Policy-as-Code 策略需透過 CLI 評估: da-tools evaluate-policy --config-dir conf.d/',
                 '• Policy-as-Code evaluation via CLI: da-tools evaluate-policy --config-dir conf.d/')}</li>
        </ul>
      </div>
    </div>
  );
}
