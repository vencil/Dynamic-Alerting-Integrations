---
title: "Config Lint Report"
tags: [lint, validation, best practices]
audience: ["platform-engineer", tenant]
version: v2.7.0
lang: en
related: [config-diff, playground, schema-explorer]
dependencies: [
  "config-lint/lint.js"
]
---

import React, { useState, useMemo } from 'react';

// PR-portal-20: lint engine (YAML parser + LINT_RULES + lintConfig) extracted
// to a unit-testable module (was inline + 0%-covered). LINT_RULES is imported
// back for the rule-list render.
import { lintConfig, LINT_RULES } from './config-lint/lint.js';

const t = window.__t || ((zh, en) => en);

const SAMPLE_YAML = `_defaults:
  mariadb_connections_warning: 150
  mariadb_connections_warning_critical: 200
  redis_memory_usage_warning: 75

db-a:
  mariadb_connections_warning: 300
  mariadb_cpu_usage_warning: 95
  mariadb_cpu_usage_warning_critical: 98
  redis_memory_usage_warning: disable
  _routing:
    receiver_type: webhook
    webhook_url: https://hooks.example.com/alerts
    group_wait: 1s
    repeat_interval: 100h

db-b:
  mariadb_connections_warning: 50
  mariadb_replication_lag_warning: 30
  kafka_consumer_lag_warning: 10000
`;

/* ── Lint rules ── */

// v2.7.0 Phase .a0 Day 4: migrated to design tokens (--da-color-*).
// Severity colors map to semantic tokens:
//   error   → --da-color-error / error-soft
//   warning → --da-color-warning / warning-soft
//   info    → --da-color-info / info-soft
const SEVERITY_COLORS = {
  error: {
    bg: 'bg-[color:var(--da-color-error-soft)]',
    border: 'border-[color:var(--da-color-error)]/30',
    text: 'text-[color:var(--da-color-error)]',
    badge: 'bg-[color:var(--da-color-error)]/15 text-[color:var(--da-color-error)]',
  },
  warning: {
    bg: 'bg-[color:var(--da-color-warning-soft)]',
    border: 'border-[color:var(--da-color-warning)]/30',
    text: 'text-[color:var(--da-color-warning)]',
    badge: 'bg-[color:var(--da-color-warning)]/15 text-[color:var(--da-color-warning)]',
  },
  info: {
    bg: 'bg-[color:var(--da-color-info-soft)]',
    border: 'border-[color:var(--da-color-info)]/30',
    text: 'text-[color:var(--da-color-info)]',
    badge: 'bg-[color:var(--da-color-info)]/15 text-[color:var(--da-color-info)]',
  },
};


export default function ConfigLint() {
  const [yaml, setYaml] = useState(SAMPLE_YAML);

  const results = useMemo(() => lintConfig(yaml), [yaml]);

  const counts = useMemo(() => {
    const c = { error: 0, warning: 0, info: 0 };
    results.findings.forEach(f => c[f.severity]++);
    return c;
  }, [results]);

  return (
    <div className="min-h-screen bg-[color:var(--da-color-bg)] p-8">
      <div className="max-w-5xl mx-auto">
        <h1 className="text-3xl font-bold text-[color:var(--da-color-fg)] mb-2">{t('配置 Lint 報告', 'Config Lint Report')}</h1>
        <p className="text-[color:var(--da-color-muted)] mb-6">{t('貼入 YAML 取得最佳實踐建議、缺少配對、路由問題等', 'Paste YAML to get best-practice suggestions, missing pairs, routing issues, and more')}</p>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Editor */}
          <div className="bg-[color:var(--da-color-surface)] rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] p-4">
            <h2 className="text-sm font-semibold text-[color:var(--da-color-fg)] mb-3">
              <label htmlFor="config-lint-yaml-input">{t('Tenant YAML', 'Tenant YAML')}</label>
            </h2>
            <textarea
              id="config-lint-yaml-input"
              aria-label={t('Tenant YAML 輸入區', 'Tenant YAML input')}
              value={yaml}
              onChange={(e) => setYaml(e.target.value)}
              rows={24}
              spellCheck={false}
              className="w-full font-mono text-xs border border-[color:var(--da-color-surface-border)] rounded-lg p-3 focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)] focus:border-[color:var(--da-color-accent)] bg-[color:var(--da-color-surface-hover)] resize-none"
            />
          </div>

          {/* Report */}
          <div className="space-y-4">
            {/* Summary */}
            <div className="bg-[color:var(--da-color-surface)] rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] p-4" role="status" aria-live="polite" aria-atomic="true">
              <h2 className="text-sm font-semibold text-[color:var(--da-color-fg)] mb-3">{t('摘要', 'Summary')}</h2>
              <div className="flex gap-4">
                {[
                  { label: t('錯誤', 'Errors'), count: counts.error, color: 'text-[color:var(--da-color-error)] bg-[color:var(--da-color-error-soft)]' },
                  { label: t('警告', 'Warnings'), count: counts.warning, color: 'text-[color:var(--da-color-warning)] bg-[color:var(--da-color-warning-soft)]' },
                  { label: t('建議', 'Info'), count: counts.info, color: 'text-[color:var(--da-color-info)] bg-[color:var(--da-color-info-soft)]' },
                ].map((s, i) => (
                  <div key={i} className={`flex-1 p-3 rounded-lg ${s.color} text-center`}>
                    <div className="text-2xl font-bold">{s.count}</div>
                    <div className="text-xs mt-1">{s.label}</div>
                  </div>
                ))}
              </div>
              <div className="mt-3 text-xs text-[color:var(--da-color-muted)]">
                {t(`分析了 ${results.tenantCount} 個 tenant`, `Analyzed ${results.tenantCount} tenants`)}
                {results.findings.length === 0 && results.ok && (
                  <span className="ml-2 text-[color:var(--da-color-success)] font-medium">✅ {t('一切看起來很好！', 'Everything looks good!')}</span>
                )}
              </div>
            </div>

            {/* Findings — role="alert" so screen readers announce parser errors immediately (TRK-202 fix). */}
            <div className="space-y-3 max-h-96 overflow-y-auto" role="alert" aria-live="polite" aria-atomic="false" aria-label={t('Lint 結果清單', 'Lint findings')}>
              {!results.ok && (
                <div className="p-4 bg-[color:var(--da-color-error-soft)] border border-[color:var(--da-color-error)]/30 rounded-xl text-sm text-[color:var(--da-color-error)]">
                  {t('YAML 解析錯誤', 'YAML parse error')}: {results.error}
                </div>
              )}
              {results.findings.map((f, i) => {
                const colors = SEVERITY_COLORS[f.severity];
                return (
                  <div key={i} className={`${colors.bg} ${colors.border} border rounded-xl p-4`}>
                    <div className="flex items-start gap-2">
                      <span className={`${colors.badge} text-xs font-bold px-2 py-0.5 rounded flex-shrink-0`}>
                        {f.severity.toUpperCase()}
                      </span>
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <span className="text-xs text-[color:var(--da-color-muted)]">{f.category}</span>
                          <span className="text-xs text-[color:var(--da-color-muted)]">•</span>
                          <span className="text-xs font-mono text-[color:var(--da-color-muted)]">{f.tenant}</span>
                        </div>
                        <p className={`text-sm ${colors.text}`}>{f.message}</p>
                        {f.key !== '-' && (
                          <code className="text-xs text-[color:var(--da-color-muted)] mt-1 block">{f.key}</code>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Rules reference */}
            <div className="bg-[color:var(--da-color-surface)] rounded-xl shadow-sm border border-[color:var(--da-color-surface-border)] p-4">
              <h3 className="text-xs font-semibold text-[color:var(--da-color-muted)] uppercase mb-2">{t('檢查項目', 'Lint Rules')}</h3>
              <div className="space-y-1 text-xs">
                {LINT_RULES.map(r => (
                  <div key={r.id} className="flex items-center gap-2">
                    <span className={`${SEVERITY_COLORS[r.severity].badge} px-1.5 py-0.5 rounded text-xs`}>{r.severity}</span>
                    <span className="text-[color:var(--da-color-muted)]">{r.id.replace(/-/g, ' ')}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
