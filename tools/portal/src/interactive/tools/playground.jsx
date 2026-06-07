---
title: "YAML Playground"
tags: [validation, yaml, live preview]
audience: ["platform-engineer", tenant]
version: v2.7.0
lang: en
related: [config-lint, schema-explorer, template-gallery]
dependencies: [
  "playground/validation.js"
]
---

import React, { useState, useEffect, useMemo } from 'react';

// PR-portal-16: hand-rolled YAML parser + tenant-config validator extracted
// to a unit-testable module (was inline + 0%-covered). Kept playground-local
// (NOT merged with _common/validation — parseDuration contracts differ).
import { validateTenantConfig } from './playground/validation.js';

const t = window.__t || ((zh, en) => en);

// [ADR-014 / DEC-A] Migrated from hardcoded Tailwind palette colors (gray-50, blue-500, etc.)
// to design tokens using arbitrary-value pattern (bg-[color:var(--da-color-*)])
// This enables consistent theming and dark mode support via CSS variables.

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
    <div className="flex h-screen bg-[color:var(--da-color-surface)]">
      {/* Header */}
      <div className="fixed top-0 left-0 right-0 bg-[color:var(--da-color-card-bg)] border-b border-[color:var(--da-color-surface-border)] p-4 shadow-sm z-10">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-[color:var(--da-color-fg)]">{t('租戶 YAML 驗證器', 'Tenant YAML Validator')}</h1>
            <p className="text-sm text-[color:var(--da-color-muted)] mt-1">{t('用於租戶配置驗證的互動式遊樂場', 'Interactive playground for tenant configuration validation')}</p>
          </div>
          <div className="flex gap-3">
            <select
              value={selectedTemplate}
              onChange={handleTemplateChange}
              aria-label={t('選擇範本', 'Select template')}
              className="px-3 py-2 bg-[color:var(--da-color-card-bg)] border border-[color:var(--da-color-surface-border)] rounded-md text-sm font-medium text-[color:var(--da-color-fg)] hover:bg-[color:var(--da-color-surface)] focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-accent)]"
            >
              <option value="minimal">{t('最小化 (3行!)', 'Minimal (3 lines!)')}</option>
              <option value="mariadb">{t('MariaDB 示例', 'MariaDB Example')}</option>
              <option value="postgresql">{t('PostgreSQL 示例', 'PostgreSQL Example')}</option>
              <option value="redis">{t('Redis 示例', 'Redis Example')}</option>
              <option value="kafka">{t('Kafka 示例', 'Kafka Example')}</option>
            </select>
            <button
              onClick={handleResetExample}
              className="px-4 py-2 bg-[color:var(--da-color-fg)] text-[color:var(--da-color-card-bg)] rounded-md text-sm font-medium hover:bg-[color:var(--da-color-accent-hover)] focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-fg)]"
            >
              {t('重置', 'Reset')}
            </button>
            <button
              onClick={() => setShowDiff(!showDiff)}
              className={`px-4 py-2 rounded-md text-sm font-medium focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-info)] ${
                showDiff ? 'bg-[color:var(--da-color-info)] text-[color:var(--da-color-card-bg)]' : 'bg-[color:var(--da-color-info-soft)] text-[color:var(--da-color-info)] hover:bg-[color:var(--da-color-info-soft)]'
              }`}
            >
              {showDiff ? t('隱藏差異', 'Hide Diff') : t('差異對比', 'Diff')}
              {hasChanges && !showDiff && <span className="ml-1 text-xs">●</span>}
            </button>
            <button
              onClick={handleExport}
              disabled={!validation.valid}
              className="px-4 py-2 bg-[color:var(--da-color-success)] text-[color:var(--da-color-card-bg)] rounded-md text-sm font-medium hover:bg-[color:var(--da-color-success)] disabled:opacity-40 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-success)]"
            >
              {t('匯出 .yaml', 'Export .yaml')}
            </button>
            <button
              onClick={handleShareLink}
              className={`px-4 py-2 rounded-md text-sm font-medium focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-accent)] ${
                shareCopied ? 'bg-[color:var(--da-color-accent)] text-[color:var(--da-color-card-bg)]' : 'bg-[color:var(--da-color-accent-soft)] text-[color:var(--da-color-accent)] hover:bg-[color:var(--da-color-accent-soft)]'
              }`}
            >
              {shareCopied ? <><span aria-hidden="true">✓</span> {t('已複製', 'Link Copied')}</> : t('分享連結', 'Share Link')}
            </button>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="flex w-full pt-24">
        {/* Left Pane: YAML Editor */}
        <div className="w-1/2 border-r border-[color:var(--da-color-surface-border)] flex flex-col bg-[color:var(--da-color-card-bg)]">
          <div className="px-6 py-4 border-b border-[color:var(--da-color-surface-border)]">
            <h2 className="text-lg font-semibold text-[color:var(--da-color-fg)]">{t('租戶 YAML', 'Tenant YAML')}</h2>
            <p className="text-xs text-[color:var(--da-color-muted)] mt-1">{t('在下方編輯 YAML。驗證實時更新。', 'Edit YAML below. Validation updates in real-time.')}</p>
          </div>
          {showDiff && (
            <div className="border-b border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] px-6 py-3 max-h-48 overflow-y-auto">
              <div className="text-xs font-semibold text-[color:var(--da-color-fg)] mb-2">{t('相對於範本的變化:', 'Changes vs. template:')}</div>
              <pre className="font-mono text-xs leading-relaxed">
                {diff.map((d, i) => {
                  if (d.type === 'same') return null;
                  const color = d.type === 'added' ? 'text-[color:var(--da-color-success)] bg-[color:var(--da-color-success-soft)]' : d.type === 'removed' ? 'text-[color:var(--da-color-error)] bg-[color:var(--da-color-error-soft)]' : 'text-[color:var(--da-color-warning)] bg-[color:var(--da-color-warning-soft)]';
                  return (
                    <div key={i} className={`${color} px-2 py-0.5 rounded`}>
                      <span className="text-[color:var(--da-color-muted)] mr-2">{d.num}</span>
                      {d.type === 'removed' ? '- ' : d.type === 'added' ? '+ ' : '~ '}{d.line}
                    </div>
                  );
                })}
                {!hasChanges && <div className="text-[color:var(--da-color-muted)]">{t('相對於範本沒有更改。', 'No changes from template.')}</div>}
              </pre>
            </div>
          )}
          <div className="flex-1 overflow-hidden flex">
            <div className="w-12 bg-[color:var(--da-color-tag-bg)] border-r border-[color:var(--da-color-surface-border)] flex flex-col items-center py-4 text-xs text-[color:var(--da-color-tag-fg)] font-mono">
              {yaml.split('\n').map((_, i) => (
                <div key={i} className="h-6 flex items-center justify-center">
                  {i + 1}
                </div>
              ))}
            </div>
            <textarea
              value={yaml}
              onChange={(e) => setYaml(e.target.value)}
              aria-label={t('租戶 YAML 編輯器', 'Tenant YAML editor')}
              className="flex-1 p-4 font-mono text-sm text-[color:var(--da-color-fg)] bg-[color:var(--da-color-card-bg)] focus:outline-none focus:ring-2 focus:ring-inset focus:ring-[color:var(--da-color-accent)] resize-none"
              spellCheck="false"
            />
          </div>
        </div>

        {/* Right Pane: Validation Results */}
        <div className="w-1/2 flex flex-col bg-[color:var(--da-color-surface)] overflow-hidden">
          {/* Validation Summary */}
          <div className="px-6 py-4 border-b border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-card-bg)]">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-lg font-semibold text-[color:var(--da-color-fg)]">{t('驗證結果', 'Validation Results')}</h2>
                <p className="text-xs text-[color:var(--da-color-muted)] mt-1">
                  {validation.errors.length === 0
                    ? t('所有檢查都通過了!', 'All checks passed!')
                    : t(`找到 ${validation.errors.length} 個錯誤`, `${validation.errors.length} error(s) found`)}
                </p>
              </div>
              <div className="text-right">
                <div className="text-3xl font-bold">
                  {validation.valid ? (
                    <span className="text-[color:var(--da-color-success)]"><span aria-hidden="true">✓</span></span>
                  ) : (
                    <span className="text-[color:var(--da-color-error)]"><span aria-hidden="true">✗</span></span>
                  )}
                </div>
              </div>
            </div>
          </div>

          {/* Results Scroll Area */}
          <div className="flex-1 overflow-y-auto p-6 space-y-6">
            {/* Summary Stats */}
            <div className="grid grid-cols-3 gap-4">
              <div className="bg-[color:var(--da-color-card-bg)] rounded-lg p-4 border border-[color:var(--da-color-surface-border)]">
                <div className="text-2xl font-bold text-[color:var(--da-color-accent)]">{validation.summary.thresholds}</div>
                <div className="text-xs text-[color:var(--da-color-fg)] mt-1">{t('已配置的閾值', 'Thresholds Configured')}</div>
              </div>
              <div className="bg-[color:var(--da-color-card-bg)] rounded-lg p-4 border border-[color:var(--da-color-surface-border)]">
                <div className="text-2xl font-bold text-[color:var(--da-color-info)]">{validation.summary.specialKeys}</div>
                <div className="text-xs text-[color:var(--da-color-fg)] mt-1">{t('特殊鍵', 'Special Keys')}</div>
              </div>
              <div className="bg-[color:var(--da-color-card-bg)] rounded-lg p-4 border border-[color:var(--da-color-surface-border)]">
                <div
                  className={`text-2xl font-bold ${
                    validation.summary.routing === 'configured'
                      ? 'text-[color:var(--da-color-success)]'
                      : validation.summary.routing === 'error'
                      ? 'text-[color:var(--da-color-error)]'
                      : 'text-[color:var(--da-color-muted)]'
                  }`}
                >
                  {validation.summary.routing === 'configured' ? <span aria-hidden="true">✓</span> : '○'}
                </div>
                <div className="text-xs text-[color:var(--da-color-fg)] mt-1">{t('路由狀態', 'Routing Status')}</div>
                <div className="text-xs text-[color:var(--da-color-muted)] mt-2">
                  {validation.summary.routing === 'configured' ? t('已配置', 'configured') : validation.summary.routing === 'error' ? t('錯誤', 'error') : t('未配置', 'not configured')}
                </div>
              </div>
            </div>

            {/* Errors */}
            {validation.errors.length > 0 && (
              <div>
                <h3 className="font-semibold text-[color:var(--da-color-error)] mb-3 flex items-center gap-2">
                  <span className="text-lg"><span aria-hidden="true">✗</span></span> {t('錯誤', 'Errors')} ({validation.errors.length})
                </h3>
                <div className="space-y-2">
                  {validation.errors.map((err, i) => (
                    <div
                      key={i}
                      className="bg-[color:var(--da-color-error-soft)] border border-[color:var(--da-color-error)] rounded-md p-3 text-sm text-[color:var(--da-color-error)]"
                    >
                      <div className="font-mono font-bold text-xs text-[color:var(--da-color-error)] mb-1">{err.rule}</div>
                      <div>{err.message}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Warnings */}
            {validation.warnings.length > 0 && (
              <div>
                <h3 className="font-semibold text-[color:var(--da-color-warning)] mb-3 flex items-center gap-2">
                  <span className="text-lg"><span aria-hidden="true">⚠</span></span> {t('警告', 'Warnings')} ({validation.warnings.length})
                </h3>
                <div className="space-y-2">
                  {validation.warnings.map((warn, i) => (
                    <div
                      key={i}
                      className="bg-[color:var(--da-color-warning-soft)] border border-[color:var(--da-color-warning)] rounded-md p-3 text-sm text-[color:var(--da-color-warning)]"
                    >
                      <div className="font-mono font-bold text-xs text-[color:var(--da-color-warning)] mb-1">{warn.rule}</div>
                      <div>{warn.message}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Metrics Preview */}
            {validation.metrics.length > 0 && (
              <div>
                <h3 className="font-semibold text-[color:var(--da-color-fg)] mb-3 flex items-center gap-2">
                  <span className="text-lg">📊</span> {t('匯出的指標', 'Exported Metrics')} ({validation.metrics.length})
                </h3>
                <div className="space-y-2">
                  {validation.metrics.map((metric, i) => (
                    <div
                      key={i}
                      className="bg-[color:var(--da-color-accent-soft)] border border-[color:var(--da-color-accent)] rounded-md p-3 text-sm font-mono text-[color:var(--da-color-fg)]"
                    >
                      <div className="text-[color:var(--da-color-accent)] font-semibold">{metric.name}</div>
                      <div className="text-xs text-[color:var(--da-color-muted)] mt-1">
                        tenant="{metric.tenant}" severity="warning" value={metric.value}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* All Valid */}
            {validation.valid && validation.errors.length === 0 && (
              <div className="bg-[color:var(--da-color-success-soft)] border border-[color:var(--da-color-success)] rounded-md p-4 text-center">
                <div className="text-2xl mb-2"><span aria-hidden="true">✓</span></div>
                <div className="text-[color:var(--da-color-success)] font-semibold">{t('配置有效!', 'Configuration is valid!')}</div>
                <div className="text-xs text-[color:var(--da-color-success)] mt-2">
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
