---
title: "YAML Playground"
tags: [validation, yaml, live preview]
audience: ["platform-engineer", tenant]
version: v2.4.0
lang: en
related: [config-lint, schema-explorer, template-gallery]
---

import React, { useState, useEffect, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

const YAML_TEMPLATES = {
  minimal: `# This is ALL a tenant needs to write — just 3 lines!
tenants:
  my-app:
    mysql_connections: "100"`,
  mariadb: `tenants:
  db-a:
    mysql_connections: "70"
    mysql_connections_critical: "95"
    mysql_cpu: "80"
    _silent_mode: "disable"
    _routing:
      receiver_type: "webhook"
      webhook_url: "https://webhook.example.com/alerts"
      group_wait: "30s"
      repeat_interval: "4h"`,
  postgresql: `tenants:
  db-b:
    pg_connections: "150"
    pg_connections_critical: "200"
    pg_cache_hit_ratio: "85"
    pg_query_time: "5000"
    _state_maintenance:
      expires: "2026-03-20T06:00:00Z"
    _routing:
      receiver_type: "slack"
      webhook_url: "https://hooks.slack.com/services/example"
      group_wait: "1m"
      group_interval: "5m"
      repeat_interval: "12h"`,
  redis: `tenants:
  cache:
    redis_memory: "80"
    redis_memory_critical: "95"
    redis_evictions: "1000"
    redis_connected_clients: "5000"
    _silent_mode:
      expires: "2026-03-13T00:00:00Z"
    _routing:
      receiver_type: "email"
      webhook_url: "mailto:ops@example.com"
      group_wait: "45s"
      repeat_interval: "6h"`,
  kafka: `tenants:
  streaming:
    kafka_lag: "100000"
    kafka_lag_critical: "500000"
    kafka_broker_active: "3"
    kafka_controller_active: "1"
    kafka_isr_shrank: "0"
    _routing:
      receiver_type: "teams"
      webhook_url: "https://teams.example.com/webhook"
      group_wait: "2m"
      group_interval: "3m"
      repeat_interval: "24h"`,
  'routing-profiles': `# v2.1.0: Cross-Domain Routing Profiles (ADR-007)
_routing_defaults:
  receiver_type: "webhook"
  group_wait: "30s"
  repeat_interval: "4h"

routing_profiles:
  standard-webhook:
    receiver_type: "webhook"
    group_wait: "30s"
    repeat_interval: "4h"
  urgent-slack:
    receiver_type: "slack"
    group_wait: "10s"
    repeat_interval: "1h"

tenants:
  db-a:
    mysql_connections: "80"
    _routing:
      profile: "standard-webhook"
      webhook_url: "https://hooks.example.com/db-a"
  db-b:
    pg_connections: "120"
    _routing:
      profile: "urgent-slack"
      webhook_url: "https://hooks.slack.com/services/db-b"

_domain_policy:
  allowed_domains: ["*.example.com", "hooks.slack.com"]
  denied_domains: ["*.internal.corp"]`
};

const KNOWN_METRIC_KEYS = new Set([
  'mysql_connections',
  'mysql_connections_critical',
  'mysql_cpu',
  'mysql_memory',
  'mysql_slow_queries',
  'mysql_query_errors',
  'mysql_replication_lag',
  'pg_connections',
  'pg_connections_critical',
  'pg_cache_hit_ratio',
  'pg_query_time',
  'pg_disk_usage',
  'pg_replication_lag',
  'redis_memory',
  'redis_memory_critical',
  'redis_evictions',
  'redis_connected_clients',
  'redis_keyspace_hits',
  'kafka_lag',
  'kafka_lag_critical',
  'kafka_broker_active',
  'kafka_controller_active',
  'kafka_isr_shrank',
  'kafka_under_replicated',
  'mongo_connections',
  'mongo_connections_critical',
  'mongo_memory',
  'elasticsearch_heap',
  'elasticsearch_heap_critical',
  'elasticsearch_unassigned_shards'
]);

const RECEIVER_TYPES = new Set(['webhook', 'email', 'slack', 'teams', 'rocketchat', 'pagerduty']);

// Strip surrounding quotes from a YAML value
function stripQuotes(s) {
  if (!s) return s;
  if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
    return s.slice(1, -1);
  }
  return s;
}

// Coerce YAML scalars: booleans and numbers stay as-is (string), but quotes are stripped
function coerceValue(raw) {
  const s = stripQuotes(raw);
  if (s === 'true') return true;
  if (s === 'false') return false;
  return s;
}

// Parse inline YAML array: ["a", "b"] → ['a', 'b']
function parseInlineArray(s) {
  if (!s.startsWith('[') || !s.endsWith(']')) return null;
  return s.slice(1, -1).split(',').map(item => stripQuotes(item.trim())).filter(Boolean);
}

// Split "key: value" on the FIRST colon only (preserves URLs like https://...)
function splitKeyValue(line) {
  const idx = line.indexOf(':');
  if (idx === -1) return null;
  const key = line.slice(0, idx).trim();
  const value = line.slice(idx + 1).trim();
  return { key, value };
}

// Simple YAML parser (handles tenant config structure up to 4 indent levels)
function parseYAML(text) {
  try {
    const lines = text.split('\n');
    const result = {};
    let currentTenant = null;
    let level4Key = null;     // key at indent 4 (e.g., _routing)
    let level6Key = null;     // key at indent 6 (e.g., receiver)

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (!line.trim() || line.trim().startsWith('#')) continue;

      const indent = line.search(/\S/);
      const trimmed = line.trim();

      // Reset deeper context when indent decreases
      if (indent <= 4) level6Key = null;
      if (indent <= 2) level4Key = null;

      // Pure key line (ends with ":" and nothing after)
      if (trimmed.endsWith(':') && !trimmed.includes(': ')) {
        const key = trimmed.slice(0, -1);
        if (indent === 0) {
          if (key === 'tenants') result.tenants = {};
        } else if (indent === 2 && result.tenants) {
          currentTenant = key;
          result.tenants[currentTenant] = {};
          level4Key = null;
          level6Key = null;
        } else if (indent === 4 && currentTenant) {
          level4Key = key;
          level6Key = null;
          result.tenants[currentTenant][key] = {};
        } else if (indent === 6 && currentTenant && level4Key) {
          level6Key = key;
          if (!result.tenants[currentTenant][level4Key]) {
            result.tenants[currentTenant][level4Key] = {};
          }
          result.tenants[currentTenant][level4Key][key] = {};
        }
        continue;
      }

      // Key-value line
      const kv = splitKeyValue(trimmed);
      if (kv && kv.value !== '') {
        const key = kv.key;
        // Check for inline array
        const arr = parseInlineArray(kv.value);
        const value = arr !== null ? arr : coerceValue(kv.value);

        if (indent === 4 && currentTenant) {
          result.tenants[currentTenant][key] = value;
          // If this is a special key with a simple value (e.g., _silent_mode: "disable")
          if (key.startsWith('_') && typeof value === 'string') {
            level4Key = null; // not a nested object
          }
        } else if (indent === 6 && currentTenant && level4Key) {
          if (!result.tenants[currentTenant][level4Key]) {
            result.tenants[currentTenant][level4Key] = {};
          }
          result.tenants[currentTenant][level4Key][key] = value;
        } else if (indent === 8 && currentTenant && level4Key && level6Key) {
          if (!result.tenants[currentTenant][level4Key][level6Key]) {
            result.tenants[currentTenant][level4Key][level6Key] = {};
          }
          result.tenants[currentTenant][level4Key][level6Key][key] = value;
        }
        continue;
      }

      // List item (- value)
      if (trimmed.startsWith('- ')) {
        const itemValue = stripQuotes(trimmed.slice(2).trim());
        if (indent === 6 && currentTenant && level4Key) {
          const target = result.tenants[currentTenant][level4Key];
          // Convert to array if needed (for group_by etc.)
          // This handles rare cases of block-style arrays at indent 6
        }
      }
    }

    return { success: true, data: result };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

// Duration parser (e.g., "30s", "5m", "1h")
function parseDuration(str) {
  if (!str) return null;
  const match = str.match(/^(\d+)([smh])$/);
  if (!match) return null;
  const [_, value, unit] = match;
  const v = parseInt(value);
  if (unit === 's') return { ms: v * 1000, value: v, unit: 's' };
  if (unit === 'm') return { ms: v * 60 * 1000, value: v, unit: 'm' };
  if (unit === 'h') return { ms: v * 60 * 60 * 1000, value: v, unit: 'h' };
  return null;
}

// Validate duration is within guardrails
function validateDurationRange(str, minMs, maxMs) {

  const dur = parseDuration(str);
  if (!dur) return { valid: false, error: t('無效格式', 'Invalid format') };
  if (dur.ms < minMs || dur.ms > maxMs) {
    return { valid: false, error: t(`超出範圍 (${minMs / 1000}s - ${maxMs / 1000}s)`, `Out of range (${minMs / 1000}s - ${maxMs / 1000}s)`) };
  }
  return { valid: true };
}

// Validate ISO 8601 timestamp
function isValidISO8601(str) {
  if (!str) return false;
  try {
    const date = new Date(str);
    return !isNaN(date.getTime()) && str.includes('T') && str.includes('Z');
  } catch {
    return false;
  }
}

// Main validation logic
function validateTenantConfig(yamlText) {

  const errors = [];
  const warnings = [];
  const metrics = [];
  let thresholdCount = 0;
  let specialKeysCount = 0;
  let routingStatus = 'not_configured';

  // Step 1: Parse YAML
  const parsed = parseYAML(yamlText);
  if (!parsed.success) {
    return {
      valid: false,
      errors: [{ rule: t('YAML 語法', 'YAML Syntax'), message: t(`解析錯誤: ${parsed.error}`, `Parse error: ${parsed.error}`) }],
      warnings: [],
      metrics: [],
      summary: { thresholds: 0, specialKeys: 0, routing: 'error' }
    };
  }

  const config = parsed.data;
  if (!config.tenants) {
    return {
      valid: false,
      errors: [{ rule: t('結構', 'Structure'), message: t('未找到 "tenants" 根鍵', 'No "tenants" root key found') }],
      warnings: [],
      metrics: [],
      summary: { thresholds: 0, specialKeys: 0, routing: 'error' }
    };
  }

  // Step 2: Validate each tenant
  Object.entries(config.tenants).forEach(([tenantId, tenantConfig]) => {
    if (typeof tenantConfig !== 'object' || tenantConfig === null) {
      errors.push({ rule: t('租戶結構', 'Tenant Structure'), message: t(`租戶 "${tenantId}" 不是一個對象`, `Tenant "${tenantId}" is not an object`) });
      return;
    }

    Object.entries(tenantConfig).forEach(([key, value]) => {
      // Threshold validation
      if (KNOWN_METRIC_KEYS.has(key)) {
        thresholdCount++;
        const strVal = String(value);
        if (!/^\d+$/.test(strVal)) {
          errors.push({
            rule: t('閾值格式', 'Threshold Format'),
            message: t(`${tenantId}.${key}: 必須是數字字串, 得到 "${strVal}"`, `${tenantId}.${key}: must be a number string, got "${strVal}"`)
          });
        } else {
          metrics.push({
            name: `${key}`,
            tenant: tenantId,
            value: value,
            labels: { tenant: tenantId, severity: 'warning' }
          });
        }
      } else if (key.startsWith('_')) {
        // Special keys validation
        specialKeysCount++;

        if (key === '_silent_mode') {
          if (value === 'disable') {
            // Valid
          } else if (typeof value === 'object' && value !== null) {
            if (value.expires && !isValidISO8601(value.expires)) {
              errors.push({
                rule: '_silent_mode',
                message: t(`${tenantId}._silent_mode.expires: 無效的 ISO 8601 時間戳`, `${tenantId}._silent_mode.expires: invalid ISO 8601 timestamp`)
              });
            }
          } else {
            errors.push({
              rule: '_silent_mode',
              message: t(`${tenantId}._silent_mode: 必須是 "disable" 或具有 expires 的對象`, `${tenantId}._silent_mode: must be "disable" or object with expires`)
            });
          }
        } else if (key === '_state_maintenance') {
          if (typeof value === 'object' && value !== null) {
            if (value.expires && !isValidISO8601(value.expires)) {
              errors.push({
                rule: '_state_maintenance',
                message: t(`${tenantId}._state_maintenance.expires: 無效的 ISO 8601 時間戳`, `${tenantId}._state_maintenance.expires: invalid ISO 8601 timestamp`)
              });
            }
          } else {
            errors.push({
              rule: '_state_maintenance',
              message: t(`${tenantId}._state_maintenance: 必須是對象`, `${tenantId}._state_maintenance: must be object`)
            });
          }
        } else if (key === '_routing') {
          routingStatus = 'configured';
          if (typeof value === 'object' && value !== null) {
            const routing = value;
            // v2.1.0: receiver_type (flat) or legacy receiver.type (nested)
            const recType = routing.receiver_type || (routing.receiver && routing.receiver.type);
            if (!recType && !routing.profile) {
              errors.push({
                rule: '_routing',
                message: t(`${tenantId}._routing: 需要 "receiver_type"、"profile" 或 "receiver" 之一`, `${tenantId}._routing: needs "receiver_type", "profile", or "receiver"`)
              });
            } else if (recType && !RECEIVER_TYPES.has(recType)) {
              errors.push({
                rule: '_routing.receiver_type',
                message: t(`${tenantId}._routing.receiver_type: 未知的類型 "${recType}"`, `${tenantId}._routing.receiver_type: unknown type "${recType}"`)
              });
            }

            // Timing guardrails (values are already unquoted strings)
            if (routing.group_wait) {
              const gwCheck = validateDurationRange(String(routing.group_wait), 5000, 5 * 60 * 1000);
              if (!gwCheck.valid) {
                errors.push({
                  rule: '_routing.group_wait',
                  message: `${tenantId}._routing.group_wait: ${gwCheck.error}`
                });
              }
            }
            if (routing.group_interval) {
              const giCheck = validateDurationRange(String(routing.group_interval), 5000, 5 * 60 * 1000);
              if (!giCheck.valid) {
                errors.push({
                  rule: '_routing.group_interval',
                  message: `${tenantId}._routing.group_interval: ${giCheck.error}`
                });
              }
            }
            if (routing.repeat_interval) {
              const riCheck = validateDurationRange(String(routing.repeat_interval), 60000, 72 * 60 * 60 * 1000);
              if (!riCheck.valid) {
                errors.push({
                  rule: '_routing.repeat_interval',
                  message: `${tenantId}._routing.repeat_interval: ${riCheck.error}`
                });
              }
            }
          }
        } else {
          warnings.push({
            rule: t('未知特殊鍵', 'Unknown Special Key'),
            message: t(`${tenantId}.${key}: 未知的特殊鍵 (以 _ 開頭)`, `${tenantId}.${key}: unknown special key (starts with _)`)
          });
        }
      } else {
        // Unknown regular key
        warnings.push({
          rule: t('未知鍵', 'Unknown Key'),
          message: t(`${tenantId}.${key}: 不在已知指標列表中 (可能是拼寫錯誤?)`, `${tenantId}.${key}: not in known metrics list (possible typo?)`)
        });
      }
    });
  });

  return {
    valid: errors.length === 0,
    errors,
    warnings,
    metrics,
    summary: {
      thresholds: thresholdCount,
      specialKeys: specialKeysCount,
      routing: routingStatus
    }
  };
}

// Simple line diff: compare current yaml to selected template
function computeDiff(current, template) {
  const curLines = current.split('\n');
  const tplLines = template.split('\n');
  const maxLen = Math.max(curLines.length, tplLines.length);
  const result = [];
  for (let i = 0; i < maxLen; i++) {
    const cl = curLines[i];
    const tl = tplLines[i];
    if (cl === undefined) result.push({ type: 'removed', line: tl, num: i + 1 });
    else if (tl === undefined) result.push({ type: 'added', line: cl, num: i + 1 });
    else if (cl !== tl) result.push({ type: 'changed', line: cl, oldLine: tl, num: i + 1 });
    else result.push({ type: 'same', line: cl, num: i + 1 });
  }
  return result;
}

// Share link: compress YAML into URL-safe base64
function encodeYAML(yamlText) {
  try { return btoa(unescape(encodeURIComponent(yamlText))); } catch { return ''; }
}
function decodeYAML(encoded) {
  try { return decodeURIComponent(escape(atob(encoded))); } catch { return null; }
}
function readPlaygroundHash() {
  try {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const encoded = params.get('yaml');
    const tpl = params.get('tpl');
    return { yaml: encoded ? decodeYAML(encoded) : null, tpl: tpl || null };
  } catch { return { yaml: null, tpl: null }; }
}

export default function TenantYAMLPlayground() {

  const initial = readPlaygroundHash();
  const [yaml, setYaml] = useState(initial.yaml || YAML_TEMPLATES[initial.tpl] || YAML_TEMPLATES.mariadb);
  const [selectedTemplate, setSelectedTemplate] = useState(initial.tpl || 'mariadb');
  const [showDiff, setShowDiff] = useState(false);
  const [shareLink, setShareLink] = useState('');
  const [shareCopied, setShareCopied] = useState(false);

  const validation = useMemo(() => validateTenantConfig(yaml), [yaml]);
  const diff = useMemo(() => computeDiff(yaml, YAML_TEMPLATES[selectedTemplate]), [yaml, selectedTemplate]);
  const hasChanges = diff.some(d => d.type !== 'same');

  const handleResetExample = () => {
    setYaml(YAML_TEMPLATES[selectedTemplate]);
  };

  const handleTemplateChange = (e) => {
    const template = e.target.value;
    setSelectedTemplate(template);
    setYaml(YAML_TEMPLATES[template]);
  };

  const handleExport = () => {
    const blob = new Blob([yaml], { type: 'application/x-yaml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'tenant-config.yaml';
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleShareLink = () => {
    const encoded = encodeYAML(yaml);
    const base = window.location.origin + window.location.pathname + window.location.search;
    const link = base + '#yaml=' + encoded;
    setShareLink(link);
    navigator.clipboard.writeText(link).then(() => {
      setShareCopied(true);
      setTimeout(() => setShareCopied(false), 2500);
    });
  };

  return (
    <div className="flex h-screen bg-gray-50">
      {/* Header */}
      <div className="fixed top-0 left-0 right-0 bg-white border-b border-gray-200 p-4 shadow-sm z-10">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">{t('租戶 YAML 驗證器', 'Tenant YAML Validator')}</h1>
            <p className="text-sm text-gray-500 mt-1">{t('用於租戶配置驗證的互動式遊樂場', 'Interactive playground for tenant configuration validation')}</p>
          </div>
          <div className="flex gap-3">
            <select
              value={selectedTemplate}
              onChange={handleTemplateChange}
              className="px-3 py-2 bg-white border border-gray-300 rounded-md text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="minimal">{t('最小化 (3行!)', 'Minimal (3 lines!)')}</option>
              <option value="mariadb">{t('MariaDB 示例', 'MariaDB Example')}</option>
              <option value="postgresql">{t('PostgreSQL 示例', 'PostgreSQL Example')}</option>
              <option value="redis">{t('Redis 示例', 'Redis Example')}</option>
              <option value="kafka">{t('Kafka 示例', 'Kafka Example')}</option>
            </select>
            <button
              onClick={handleResetExample}
              className="px-4 py-2 bg-gray-600 text-white rounded-md text-sm font-medium hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-gray-500"
            >
              {t('重置', 'Reset')}
            </button>
            <button
              onClick={() => setShowDiff(!showDiff)}
              className={`px-4 py-2 rounded-md text-sm font-medium focus:outline-none focus:ring-2 focus:ring-purple-500 ${
                showDiff ? 'bg-purple-600 text-white' : 'bg-purple-100 text-purple-700 hover:bg-purple-200'
              }`}
            >
              {showDiff ? t('隱藏差異', 'Hide Diff') : t('差異對比', 'Diff')}
              {hasChanges && !showDiff && <span className="ml-1 text-xs">●</span>}
            </button>
            <button
              onClick={handleExport}
              disabled={!validation.valid}
              className="px-4 py-2 bg-green-600 text-white rounded-md text-sm font-medium hover:bg-green-700 disabled:opacity-40 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-green-500"
            >
              {t('匯出 .yaml', 'Export .yaml')}
            </button>
            <button
              onClick={handleShareLink}
              className={`px-4 py-2 rounded-md text-sm font-medium focus:outline-none focus:ring-2 focus:ring-indigo-500 ${
                shareCopied ? 'bg-indigo-600 text-white' : 'bg-indigo-100 text-indigo-700 hover:bg-indigo-200'
              }`}
            >
              {shareCopied ? t('✓ 已複製', '✓ Link Copied') : t('分享連結', 'Share Link')}
            </button>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="flex w-full pt-24">
        {/* Left Pane: YAML Editor */}
        <div className="w-1/2 border-r border-gray-200 flex flex-col bg-white">
          <div className="px-6 py-4 border-b border-gray-200">
            <h2 className="text-lg font-semibold text-gray-900">{t('租戶 YAML', 'Tenant YAML')}</h2>
            <p className="text-xs text-gray-500 mt-1">{t('在下方編輯 YAML。驗證實時更新。', 'Edit YAML below. Validation updates in real-time.')}</p>
          </div>
          {showDiff && (
            <div className="border-b border-gray-200 bg-gray-50 px-6 py-3 max-h-48 overflow-y-auto">
              <div className="text-xs font-semibold text-gray-600 mb-2">{t('相對於範本的變化:', 'Changes vs. template:')}</div>
              <pre className="font-mono text-xs leading-relaxed">
                {diff.map((d, i) => {
                  if (d.type === 'same') return null;
                  const color = d.type === 'added' ? 'text-green-700 bg-green-50' : d.type === 'removed' ? 'text-red-700 bg-red-50' : 'text-amber-700 bg-amber-50';
                  return (
                    <div key={i} className={`${color} px-2 py-0.5 rounded`}>
                      <span className="text-gray-400 mr-2">{d.num}</span>
                      {d.type === 'removed' ? '- ' : d.type === 'added' ? '+ ' : '~ '}{d.line}
                    </div>
                  );
                })}
                {!hasChanges && <div className="text-gray-400">{t('相對於範本沒有更改。', 'No changes from template.')}</div>}
              </pre>
            </div>
          )}
          <div className="flex-1 overflow-hidden flex">
            <div className="w-12 bg-gray-100 border-r border-gray-200 flex flex-col items-center py-4 text-xs text-gray-500 font-mono">
              {yaml.split('\n').map((_, i) => (
                <div key={i} className="h-6 flex items-center justify-center">
                  {i + 1}
                </div>
              ))}
            </div>
            <textarea
              value={yaml}
              onChange={(e) => setYaml(e.target.value)}
              className="flex-1 p-4 font-mono text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-inset focus:ring-blue-500 resize-none"
              spellCheck="false"
            />
          </div>
        </div>

        {/* Right Pane: Validation Results */}
        <div className="w-1/2 flex flex-col bg-gray-50 overflow-hidden">
          {/* Validation Summary */}
          <div className="px-6 py-4 border-b border-gray-200 bg-white">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-lg font-semibold text-gray-900">{t('驗證結果', 'Validation Results')}</h2>
                <p className="text-xs text-gray-500 mt-1">
                  {validation.errors.length === 0
                    ? t('所有檢查都通過了!', 'All checks passed!')
                    : t(`找到 ${validation.errors.length} 個錯誤`, `${validation.errors.length} error(s) found`)}
                </p>
              </div>
              <div className="text-right">
                <div className="text-3xl font-bold">
                  {validation.valid ? (
                    <span className="text-green-600">✓</span>
                  ) : (
                    <span className="text-red-600">✗</span>
                  )}
                </div>
              </div>
            </div>
          </div>

          {/* Results Scroll Area */}
          <div className="flex-1 overflow-y-auto p-6 space-y-6">
            {/* Summary Stats */}
            <div className="grid grid-cols-3 gap-4">
              <div className="bg-white rounded-lg p-4 border border-gray-200">
                <div className="text-2xl font-bold text-blue-600">{validation.summary.thresholds}</div>
                <div className="text-xs text-gray-600 mt-1">{t('已配置的閾值', 'Thresholds Configured')}</div>
              </div>
              <div className="bg-white rounded-lg p-4 border border-gray-200">
                <div className="text-2xl font-bold text-purple-600">{validation.summary.specialKeys}</div>
                <div className="text-xs text-gray-600 mt-1">{t('特殊鍵', 'Special Keys')}</div>
              </div>
              <div className="bg-white rounded-lg p-4 border border-gray-200">
                <div
                  className={`text-2xl font-bold ${
                    validation.summary.routing === 'configured'
                      ? 'text-green-600'
                      : validation.summary.routing === 'error'
                      ? 'text-red-600'
                      : 'text-gray-400'
                  }`}
                >
                  {validation.summary.routing === 'configured' ? '✓' : '○'}
                </div>
                <div className="text-xs text-gray-600 mt-1">{t('路由狀態', 'Routing Status')}</div>
                <div className="text-xs text-gray-500 mt-2">
                  {validation.summary.routing === 'configured' ? t('已配置', 'configured') : validation.summary.routing === 'error' ? t('錯誤', 'error') : t('未配置', 'not configured')}
                </div>
              </div>
            </div>

            {/* Errors */}
            {validation.errors.length > 0 && (
              <div>
                <h3 className="font-semibold text-red-700 mb-3 flex items-center gap-2">
                  <span className="text-lg">✗</span> {t('錯誤', 'Errors')} ({validation.errors.length})
                </h3>
                <div className="space-y-2">
                  {validation.errors.map((err, i) => (
                    <div
                      key={i}
                      className="bg-red-50 border border-red-200 rounded-md p-3 text-sm text-red-800"
                    >
                      <div className="font-mono text-xs text-red-700 mb-1">{err.rule}</div>
                      <div>{err.message}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Warnings */}
            {validation.warnings.length > 0 && (
              <div>
                <h3 className="font-semibold text-amber-700 mb-3 flex items-center gap-2">
                  <span className="text-lg">⚠</span> {t('警告', 'Warnings')} ({validation.warnings.length})
                </h3>
                <div className="space-y-2">
                  {validation.warnings.map((warn, i) => (
                    <div
                      key={i}
                      className="bg-amber-50 border border-amber-200 rounded-md p-3 text-sm text-amber-800"
                    >
                      <div className="font-mono text-xs text-amber-700 mb-1">{warn.rule}</div>
                      <div>{warn.message}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Metrics Preview */}
            {validation.metrics.length > 0 && (
              <div>
                <h3 className="font-semibold text-gray-900 mb-3 flex items-center gap-2">
                  <span className="text-lg">📊</span> {t('匯出的指標', 'Exported Metrics')} ({validation.metrics.length})
                </h3>
                <div className="space-y-2">
                  {validation.metrics.map((metric, i) => (
                    <div
                      key={i}
                      className="bg-blue-50 border border-blue-200 rounded-md p-3 text-sm font-mono text-gray-800"
                    >
                      <div className="text-blue-700 font-semibold">{metric.name}</div>
                      <div className="text-xs text-gray-600 mt-1">
                        tenant="{metric.tenant}" severity="warning" value={metric.value}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* All Valid */}
            {validation.valid && validation.errors.length === 0 && (
              <div className="bg-green-50 border border-green-200 rounded-md p-4 text-center">
                <div className="text-2xl mb-2">✓</div>
                <div className="text-green-800 font-semibold">{t('配置有效!', 'Configuration is valid!')}</div>
                <div className="text-xs text-green-700 mt-2">
                  {validation.summary.thresholds} {t('閾值', 'thresholds')} • {validation.summary.specialKeys} {t('特殊鍵', 'special keys')} •
                  {validation.summary.routing === 'configured' ? t(' 已配置路由', ' routing configured') : t(' 未配置路由', ' no routing')}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
