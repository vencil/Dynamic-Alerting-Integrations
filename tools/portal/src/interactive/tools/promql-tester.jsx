---
title: "Prometheus Query Tester"
tags: [PromQL, testing, recording rules]
audience: [platform-engineer, domain-expert]
version: v2.7.0
lang: en
related: [rule-pack-detail, schema-explorer, migration-simulator]
---

import React, { useState, useMemo } from 'react';
import { analyzeQuery, simulateResults, RECORDING_RULES } from './promql-tester/parse.js';

const t = window.__t || ((zh, en) => en);

export default function PromQLTester() {
  const [query, setQuery] = useState('rate(mysql_global_status_threads_connected[5m])');
  const [showAllRules, setShowAllRules] = useState(false);

  const analysis = useMemo(() => analyzeQuery(query), [query]);
  const simulated = useMemo(() => simulateResults(query), [query]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-5xl mx-auto">
        <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('PromQL 查詢測試器', 'Prometheus Query Tester')}</h1>
        <p className="text-slate-600 mb-6">{t('輸入 PromQL 表達式，預覽模擬結果，看看 Rule Pack 是否已包含此查詢', 'Enter a PromQL expression, preview simulated results, and see if a Rule Pack recording rule already covers it')}</p>

        {/* Query input */}
        <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 mb-6">
          <label className="text-sm font-semibold text-slate-700 block mb-2">{t('PromQL 表達式', 'PromQL Expression')}</label>
          <div className="flex gap-3">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="rate(mysql_global_status_threads_connected[5m])"
              className="flex-1 font-mono text-sm px-4 py-3 border border-slate-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 bg-slate-50"
              spellCheck={false}
            />
          </div>
          {/* Quick examples */}
          <div className="flex flex-wrap gap-2 mt-3">
            <span className="text-xs text-slate-400">{t('快速範例', 'Quick examples')}:</span>
            {[
              'redis_connected_clients',
              'rate(kube_pod_container_status_restarts_total[5m])',
              'da:mariadb_replication_lag:seconds > 5',
              'kafka_consumergroup_lag > 1000',
              'da:node_cpu_usage:percent',
            ].map((ex, i) => (
              <button key={i} onClick={() => setQuery(ex)}
                className="text-xs px-2 py-1 bg-slate-100 text-slate-600 rounded hover:bg-blue-50 hover:text-blue-700 font-mono truncate max-w-xs">
                {ex}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Analysis panel */}
          <div className="space-y-4">
            {/* Parsed components */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-5">
              <h2 className="text-sm font-semibold text-slate-800 mb-3">{t('解析結果', 'Query Analysis')}</h2>
              <div className="space-y-3">
                {analysis.functions.length > 0 && (
                  <div>
                    <span className="text-xs text-slate-500">{t('函數', 'Functions')}:</span>
                    <div className="flex flex-wrap gap-1 mt-1">
                      {analysis.functions.map((f, i) => (
                        <span key={i} className="text-xs font-mono px-2 py-0.5 bg-purple-50 text-purple-700 rounded">{f}()</span>
                      ))}
                    </div>
                  </div>
                )}
                {analysis.metrics.length > 0 && (
                  <div>
                    <span className="text-xs text-slate-500">{t('Metrics', 'Metrics')}:</span>
                    <div className="flex flex-wrap gap-1 mt-1">
                      {analysis.metrics.map((m, i) => (
                        <span key={i} className="text-xs font-mono px-2 py-0.5 bg-blue-50 text-blue-700 rounded">{m}</span>
                      ))}
                    </div>
                  </div>
                )}
                {analysis.labels.length > 0 && (
                  <div>
                    <span className="text-xs text-slate-500">{t('Labels', 'Labels')}:</span>
                    <div className="flex flex-wrap gap-1 mt-1">
                      {analysis.labels.map((l, i) => (
                        <span key={i} className="text-xs font-mono px-2 py-0.5 bg-green-50 text-green-700 rounded">{l}</span>
                      ))}
                    </div>
                  </div>
                )}
                {analysis.duration && (
                  <div>
                    <span className="text-xs text-slate-500">{t('Range', 'Range')}:</span>
                    <span className="ml-2 text-xs font-mono px-2 py-0.5 bg-amber-50 text-amber-700 rounded">{analysis.duration}</span>
                  </div>
                )}
              </div>
            </div>

            {/* Warnings & suggestions */}
            {(analysis.warnings.length > 0 || analysis.suggestions.length > 0) && (
              <div className="space-y-2">
                {analysis.warnings.map((w, i) => (
                  <div key={`w-${i}`} className="p-3 bg-amber-50 border border-amber-200 rounded-lg text-xs text-amber-800">
                    <span aria-hidden="true">⚠</span>️ {w}
                  </div>
                ))}
                {analysis.suggestions.map((s, i) => (
                  <div key={`s-${i}`} className="p-3 bg-blue-50 border border-blue-200 rounded-lg text-xs text-blue-800">
                    💡 {s}
                  </div>
                ))}
              </div>
            )}

            {/* Simulated results */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-5">
              <h2 className="text-sm font-semibold text-slate-800 mb-3">{t('模擬結果預覽', 'Simulated Results Preview')}</h2>
              {simulated ? (
                <>
                  <div className="h-32 flex items-end gap-1">
                    {simulated.map((v, i) => {
                      const numV = typeof v === 'string' ? parseFloat(v) : v;
                      const max = Math.max(...simulated.map(x => typeof x === 'string' ? parseFloat(x) : x));
                      const pct = max > 0 ? (numV / max) * 100 : 0;
                      const barHeight = Math.max(pct, 2) + '%';
                      const barStyle = { height: barHeight };
                      const label = typeof v === 'number' ? (v > 1000 ? (v / 1000).toFixed(0) + 'K' : v > 100 ? Math.round(v) : v.toFixed ? v.toFixed(1) : v) : v;
                      return (
                        <div key={i} className="flex-1 flex flex-col items-center gap-1">
                          <span className="text-xs text-slate-400 font-mono text-[8px]">
                            {label}
                          </span>
                          <div className="w-full bg-blue-500 rounded-t" style={barStyle}></div>
                        </div>
                      );
                    })}
                  </div>
                  <div className="text-xs text-slate-400 mt-2 text-center">{t('10 個模擬數據點（15s 間隔）', '10 simulated data points (15s interval)')}</div>
                </>
              ) : (
                <div className="text-sm text-slate-400 text-center py-6">
                  {t('此 metric 無模擬資料。在生產環境中請使用 Prometheus UI。', 'No simulated data for this metric. Use Prometheus UI in production.')}
                </div>
              )}
            </div>
          </div>

          {/* Recording rules match panel */}
          <div className="space-y-4">
            {/* Matched rules */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-5">
              <h2 className="text-sm font-semibold text-slate-800 mb-3">
                {t('匹配的 Recording Rules', 'Matched Recording Rules')}
                <span className="ml-2 text-xs font-normal text-slate-400">({analysis.matchedRules.length})</span>
              </h2>
              {analysis.matchedRules.length > 0 ? (
                <div className="space-y-3">
                  {analysis.matchedRules.map((rule, i) => (
                    <div key={i} className={`p-3 rounded-lg border ${rule.matchType === 'direct' ? 'bg-green-50 border-green-200' : 'bg-blue-50 border-blue-200'}`}>
                      <div className="flex items-center gap-2 mb-1">
                        <span className={`text-xs px-1.5 py-0.5 rounded font-bold ${rule.matchType === 'direct' ? 'bg-green-200 text-green-800' : 'bg-blue-200 text-blue-800'}`}>
                          {rule.matchType === 'direct' ? t('直接使用', 'Direct') : t('底層 metric', 'Underlying')}
                        </span>
                        <span className="text-xs px-1.5 py-0.5 rounded bg-purple-100 text-purple-700">{rule.pack}</span>
                      </div>
                      <code className="text-xs font-mono font-bold text-slate-800 block">{rule.rule}</code>
                      <p className="text-xs text-slate-600 mt-1">{rule.desc}</p>
                      <details className="mt-2">
                        <summary className="text-xs text-slate-400 cursor-pointer hover:text-slate-600">{t('查看原始表達式', 'View source expression')}</summary>
                        <code className="text-xs font-mono text-slate-500 block mt-1 bg-slate-100 px-2 py-1 rounded break-all">{rule.expr}</code>
                      </details>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-slate-400 text-center py-4">
                  {query.trim() ? t('無匹配的 Recording Rule', 'No matching Recording Rules') : t('輸入查詢以匹配', 'Enter a query to match')}
                </div>
              )}
            </div>

            {/* All recording rules reference */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-5">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-slate-800">
                  {t('所有 Recording Rules', 'All Recording Rules')}
                  <span className="ml-2 text-xs font-normal text-slate-400">({RECORDING_RULES.length})</span>
                </h2>
                <button onClick={() => setShowAllRules(!showAllRules)}
                  className="text-xs text-blue-600 hover:underline">
                  {showAllRules ? t('收合', 'Collapse') : t('展開', 'Expand')}
                </button>
              </div>
              {showAllRules && (
                <div className="space-y-1 max-h-80 overflow-y-auto">
                  {RECORDING_RULES.map((rule, i) => (
                    <button key={i} onClick={() => setQuery(rule.rule)}
                      className="w-full text-left p-2 rounded-lg hover:bg-slate-50 transition-colors flex items-center gap-2">
                      <span className="text-xs px-1 py-0.5 rounded bg-purple-50 text-purple-600 flex-shrink-0">{rule.pack}</span>
                      <code className="text-xs font-mono text-slate-700 truncate">{rule.rule}</code>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
