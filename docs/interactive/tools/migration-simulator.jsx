---
title: "Migration Dry-Run Simulator"
tags: [migration, PromQL, dry-run]
audience: [platform-engineer]
version: v2.2.0
lang: en
related: [playground, promql-tester, config-diff]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

const EXAMPLE_RULES = `groups:
  - name: mysql_alerts
    rules:
      - alert: HighMySQLConnections
        expr: mysql_global_status_threads_connected > 100
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High MySQL connections on {{ $labels.instance }}"
      - alert: HighMySQLConnectionsCritical
        expr: mysql_global_status_threads_connected > 200
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Critical MySQL connections on {{ $labels.instance }}"
      - alert: MySQLSlowQueries
        expr: rate(mysql_global_status_slow_queries[5m]) > 10
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Slow query rate above threshold"`;

// Simple PromQL parser that extracts metric, operator, threshold from basic expressions
function parsePromQLExpr(expr) {
  expr = expr.trim();
  // Handle rate(...[5m]) > N
  const rateMatch = expr.match(/^rate\((\w+)(?:\{[^}]*\})?\[(\w+)\]\)\s*([><=!]+)\s*(\d+(?:\.\d+)?)$/);
  if (rateMatch) {
    return {
      metric: rateMatch[1],
      window: rateMatch[2],
      operator: rateMatch[3],
      threshold: rateMatch[4],
      type: 'rate',
    };
  }
  // Handle metric{labels} > N or metric > N
  const simpleMatch = expr.match(/^(\w+)(?:\{[^}]*\})?\s*([><=!]+)\s*(\d+(?:\.\d+)?)$/);
  if (simpleMatch) {
    return {
      metric: simpleMatch[1],
      operator: simpleMatch[2],
      threshold: simpleMatch[3],
      type: 'instant',
    };
  }
  return null;
}

// Map common Prometheus metric names to Dynamic Alerting threshold keys
const METRIC_MAP = {
  mysql_global_status_threads_connected: 'mysql_connections',
  mysql_global_status_slow_queries: 'mysql_slow_queries',
  mysql_slave_status_seconds_behind_master: 'mysql_replication_lag',
  pg_stat_activity_count: 'pg_connections',
  redis_memory_used_bytes: 'redis_memory',
  redis_connected_clients: 'redis_connected_clients',
  redis_evicted_keys_total: 'redis_evictions',
  kafka_consumergroup_lag: 'kafka_lag',
};

function convertRules(input) {
  const results = [];
  const thresholds = {};
  const unconverted = [];

  // Very simple YAML rule extractor (regex-based, not a full parser)
  const alertBlocks = input.split(/^\s*- alert:\s*/m).slice(1);

  alertBlocks.forEach(block => {
    const lines = block.split('\n');
    const alertName = lines[0].trim();
    let expr = '', severity = 'warning', forDur = '';

    lines.forEach(line => {
      const trimmed = line.trim();
      if (trimmed.startsWith('expr:')) expr = trimmed.replace('expr:', '').trim();
      if (trimmed.startsWith('severity:')) severity = trimmed.replace('severity:', '').trim();
      if (trimmed.startsWith('for:')) forDur = trimmed.replace('for:', '').trim();
    });

    const parsed = parsePromQLExpr(expr);
    if (!parsed) {
      unconverted.push({ alertName, expr, reason: 'Complex PromQL expression — manual review needed' });
      return;
    }

    const mappedKey = METRIC_MAP[parsed.metric];
    if (!mappedKey) {
      unconverted.push({ alertName, expr, reason: `Unknown metric "${parsed.metric}" — not in standard Rule Pack metrics` });
      return;
    }

    const thresholdKey = severity === 'critical' ? mappedKey + '_critical' : mappedKey;
    thresholds[thresholdKey] = parsed.threshold;

    results.push({
      alertName,
      expr,
      severity,
      forDur,
      mappedKey: thresholdKey,
      threshold: parsed.threshold,
      type: parsed.type,
      status: 'converted',
    });
  });

  // Generate YAML
  const yamlLines = ['tenants:', '  migrated-tenant:'];
  Object.entries(thresholds).forEach(([key, val]) => {
    yamlLines.push(`    ${key}: "${val}"`);
  });

  return {
    results,
    unconverted,
    yaml: yamlLines.join('\n'),
    thresholdCount: Object.keys(thresholds).length,
  };
}

export default function MigrationSimulator() {
  const [input, setInput] = useState(EXAMPLE_RULES);
  const [showResults, setShowResults] = useState(false);

  const conversion = useMemo(() => convertRules(input), [input]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-6xl mx-auto">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('遷移模擬器', 'Migration Dry-Run Simulator')}</h1>
          <p className="text-slate-600">
            {t('貼上現有的 Prometheus 告警規則，預覽轉換為 Dynamic Alerting YAML 的結果',
               'Paste existing Prometheus alert rules and preview the Dynamic Alerting YAML conversion')}
          </p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Left: Input */}
          <div className="space-y-4">
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold text-slate-900">{t('原始 Prometheus 規則', 'Original Prometheus Rules')}</h3>
                <button
                  onClick={() => setInput(EXAMPLE_RULES)}
                  className="text-xs text-blue-600 hover:underline"
                >
                  {t('載入範例', 'Load Example')}
                </button>
              </div>
              <textarea
                value={input}
                onChange={(e) => { setInput(e.target.value); setShowResults(false); }}
                rows={20}
                className="w-full font-mono text-xs bg-slate-900 text-slate-100 p-4 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                spellCheck="false"
                placeholder={t('貼上 Prometheus rule groups YAML...', 'Paste Prometheus rule groups YAML...')}
              />
              <button
                onClick={() => setShowResults(true)}
                className="mt-3 w-full px-4 py-3 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors"
              >
                {t('模擬轉換', 'Simulate Conversion')}
              </button>
            </div>
          </div>

          {/* Right: Results */}
          <div className="space-y-4">
            {!showResults ? (
              <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 text-center">
                <div className="text-4xl mb-4">🔄</div>
                <p className="text-slate-500 text-sm">
                  {t('點擊「模擬轉換」查看結果', 'Click "Simulate Conversion" to see results')}
                </p>
              </div>
            ) : (
              <>
                {/* Summary */}
                <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
                  <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('轉換摘要', 'Conversion Summary')}</h3>
                  <div className="grid grid-cols-3 gap-3 text-center">
                    <div className="bg-green-50 border border-green-200 rounded-lg p-3">
                      <div className="text-xl font-bold text-green-600">{conversion.results.length}</div>
                      <div className="text-xs text-green-700">{t('已轉換', 'Converted')}</div>
                    </div>
                    <div className="bg-amber-50 border border-amber-200 rounded-lg p-3">
                      <div className="text-xl font-bold text-amber-600">{conversion.unconverted.length}</div>
                      <div className="text-xs text-amber-700">{t('需人工檢查', 'Manual Review')}</div>
                    </div>
                    <div className="bg-blue-50 border border-blue-200 rounded-lg p-3">
                      <div className="text-xl font-bold text-blue-600">{conversion.thresholdCount}</div>
                      <div className="text-xs text-blue-700">{t('閾值', 'Thresholds')}</div>
                    </div>
                  </div>
                </div>

                {/* Converted Rules Detail */}
                {conversion.results.length > 0 && (
                  <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
                    <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('轉換詳情', 'Conversion Details')}</h3>
                    <div className="space-y-3">
                      {conversion.results.map((r, i) => (
                        <div key={i} className="border border-slate-100 rounded-lg p-3">
                          <div className="flex items-center gap-2 mb-2">
                            <span className="text-green-500 text-xs font-bold">✓</span>
                            <span className="font-mono text-sm font-semibold text-slate-900">{r.alertName}</span>
                            <span className={`text-xs px-1.5 py-0.5 rounded ${
                              r.severity === 'critical' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'
                            }`}>{r.severity}</span>
                          </div>
                          <div className="grid grid-cols-2 gap-2 text-xs">
                            <div>
                              <div className="text-slate-500 mb-1">{t('原始', 'Original')}</div>
                              <code className="bg-red-50 text-red-800 px-2 py-1 rounded block overflow-x-auto">{r.expr}</code>
                            </div>
                            <div>
                              <div className="text-slate-500 mb-1">{t('轉換為', 'Converts to')}</div>
                              <code className="bg-green-50 text-green-800 px-2 py-1 rounded block">
                                {r.mappedKey}: "{r.threshold}"
                              </code>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Unconverted */}
                {conversion.unconverted.length > 0 && (
                  <div className="bg-white rounded-xl shadow-sm border border-amber-200 p-6">
                    <h3 className="text-sm font-semibold text-amber-800 mb-3">{t('需人工檢查', 'Needs Manual Review')}</h3>
                    <div className="space-y-2">
                      {conversion.unconverted.map((u, i) => (
                        <div key={i} className="bg-amber-50 rounded-lg p-3 text-sm">
                          <div className="font-mono font-semibold text-slate-900 mb-1">{u.alertName}</div>
                          <code className="text-xs text-amber-800 block mb-1">{u.expr}</code>
                          <div className="text-xs text-amber-600">{u.reason}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Generated YAML */}
                <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
                  <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('生成的 YAML', 'Generated YAML')}</h3>
                  <pre className="bg-slate-900 text-slate-100 p-4 rounded-lg text-xs overflow-x-auto font-mono max-h-48 overflow-y-auto">
                    {conversion.yaml}
                  </pre>
                  <div className="flex gap-2 mt-3">
                    <button
                      onClick={() => navigator.clipboard.writeText(conversion.yaml)}
                      className="flex-1 px-3 py-2 bg-slate-700 text-white rounded-lg text-xs font-medium hover:bg-slate-600"
                    >
                      {t('複製 YAML', 'Copy YAML')}
                    </button>
                    <a
                      href={`../assets/jsx-loader.html?component=../playground.jsx#yaml=${btoa(unescape(encodeURIComponent(conversion.yaml)))}`}
                      className="flex-1 px-3 py-2 bg-green-600 text-white rounded-lg text-xs font-medium hover:bg-green-700 text-center"
                    >
                      {t('在 Playground 驗證', 'Validate in Playground')}
                    </a>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
