---
title: "Prometheus Query Tester"
tags: [promql, testing, simulation, interactive]
audience: [platform-engineer, domain-expert]
version: v2.0.0-preview.2
lang: en
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Known recording rules from Rule Packs ── */
const RECORDING_RULES = [
  { pack: 'mariadb', rule: 'da:mariadb_connections:current', expr: 'mysql_global_status_threads_connected', desc: t('當前 MariaDB 連線數', 'Current MariaDB connections') },
  { pack: 'mariadb', rule: 'da:mariadb_cpu_usage:percent', expr: 'rate(process_cpu_seconds_total{job=~".*mysql.*"}[5m]) * 100', desc: t('MariaDB CPU 使用率', 'MariaDB CPU usage percent') },
  { pack: 'mariadb', rule: 'da:mariadb_replication_lag:seconds', expr: 'mysql_slave_status_seconds_behind_master', desc: t('複製延遲秒數', 'Replication lag in seconds') },
  { pack: 'mariadb', rule: 'da:mariadb_slow_queries:rate5m', expr: 'rate(mysql_global_status_slow_queries[5m])', desc: t('慢查詢速率', 'Slow queries rate') },
  { pack: 'redis', rule: 'da:redis_memory_usage:percent', expr: 'redis_memory_used_bytes / redis_memory_max_bytes * 100', desc: t('Redis 記憶體使用率', 'Redis memory usage percent') },
  { pack: 'redis', rule: 'da:redis_cache_hit_ratio:percent', expr: 'rate(redis_keyspace_hits_total[5m]) / (rate(redis_keyspace_hits_total[5m]) + rate(redis_keyspace_misses_total[5m])) * 100', desc: t('快取命中率', 'Cache hit ratio') },
  { pack: 'redis', rule: 'da:redis_connections:current', expr: 'redis_connected_clients', desc: t('Redis 連線數', 'Redis connected clients') },
  { pack: 'postgresql', rule: 'da:postgresql_connections:current', expr: 'pg_stat_activity_count', desc: t('PostgreSQL 連線數', 'PostgreSQL active connections') },
  { pack: 'postgresql', rule: 'da:postgresql_deadlocks:rate5m', expr: 'rate(pg_stat_database_deadlocks[5m])', desc: t('Deadlock 速率', 'Deadlock rate') },
  { pack: 'postgresql', rule: 'da:postgresql_cache_hit_ratio:percent', expr: 'pg_stat_database_blks_hit / (pg_stat_database_blks_hit + pg_stat_database_blks_read) * 100', desc: t('快取命中率', 'Cache hit ratio') },
  { pack: 'kafka', rule: 'da:kafka_consumer_lag:total', expr: 'kafka_consumergroup_lag', desc: t('Consumer lag 總計', 'Consumer lag total') },
  { pack: 'kafka', rule: 'da:kafka_messages_in:rate5m', expr: 'rate(kafka_topic_partition_current_offset[5m])', desc: t('每秒進入訊息數', 'Messages in per second') },
  { pack: 'elasticsearch', rule: 'da:es_heap_usage:percent', expr: 'elasticsearch_jvm_memory_used_bytes{area="heap"} / elasticsearch_jvm_memory_max_bytes{area="heap"} * 100', desc: t('ES Heap 使用率', 'ES Heap usage percent') },
  { pack: 'elasticsearch', rule: 'da:es_indexing_rate:rate5m', expr: 'rate(elasticsearch_indices_indexing_index_total[5m])', desc: t('索引速率', 'Indexing rate') },
  { pack: 'kubernetes', rule: 'da:k8s_pod_restart:total', expr: 'kube_pod_container_status_restarts_total', desc: t('Pod 重啟次數', 'Pod restart count') },
  { pack: 'kubernetes', rule: 'da:k8s_cpu_throttle:percent', expr: 'rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m]) * 100', desc: t('CPU throttle 比率', 'CPU throttle ratio') },
  { pack: 'kubernetes', rule: 'da:k8s_memory_usage:percent', expr: 'container_memory_working_set_bytes / kube_pod_container_resource_limits{resource="memory"} * 100', desc: t('記憶體使用率', 'Memory usage percent') },
  { pack: 'node', rule: 'da:node_disk_usage:percent', expr: '(1 - node_filesystem_avail_bytes / node_filesystem_size_bytes) * 100', desc: t('磁碟使用率', 'Disk usage percent') },
  { pack: 'node', rule: 'da:node_cpu_usage:percent', expr: '100 - (avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)', desc: t('節點 CPU 使用率', 'Node CPU usage percent') },
  { pack: 'node', rule: 'da:node_memory_usage:percent', expr: '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100', desc: t('節點記憶體使用率', 'Node memory usage percent') },
  { pack: 'jvm', rule: 'da:jvm_gc_pause:seconds', expr: 'rate(jvm_gc_pause_seconds_sum[5m]) / rate(jvm_gc_pause_seconds_count[5m])', desc: t('GC 平均暫停時間', 'GC average pause time') },
  { pack: 'jvm', rule: 'da:jvm_heap_usage:percent', expr: 'jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"} * 100', desc: t('JVM Heap 使用率', 'JVM Heap usage percent') },
];

/* ── Simulated metric data ── */
const MOCK_SERIES = {
  'mysql_global_status_threads_connected': [120, 135, 142, 128, 150, 165, 148, 130, 125, 118],
  'redis_connected_clients': [45, 48, 52, 50, 55, 60, 58, 47, 44, 42],
  'redis_memory_used_bytes': [1.2e9, 1.3e9, 1.35e9, 1.4e9, 1.5e9, 1.55e9, 1.5e9, 1.45e9, 1.4e9, 1.35e9],
  'pg_stat_activity_count': [30, 35, 40, 38, 45, 50, 42, 36, 32, 28],
  'kafka_consumergroup_lag': [500, 800, 1200, 1800, 2500, 3000, 2200, 1500, 900, 400],
  'kube_pod_container_status_restarts_total': [0, 0, 1, 1, 2, 3, 3, 4, 4, 5],
  'node_cpu_seconds_total': [0.15, 0.22, 0.35, 0.48, 0.62, 0.70, 0.55, 0.40, 0.30, 0.20],
};

/* ── PromQL parser (simplified) ── */
function analyzeQuery(query) {
  const normalized = query.trim().toLowerCase();
  const analysis = {
    functions: [],
    metrics: [],
    labels: [],
    duration: null,
    matchedRules: [],
    warnings: [],
    suggestions: [],
  };

  // Extract functions
  const funcPattern = /(\w+)\s*\(/g;
  let match;
  while ((match = funcPattern.exec(query)) !== null) {
    const fn = match[1].toLowerCase();
    if (['rate', 'irate', 'increase', 'sum', 'avg', 'max', 'min', 'count', 'histogram_quantile', 'topk', 'bottomk', 'absent', 'delta', 'deriv', 'predict_linear', 'label_replace', 'label_join', 'group_left', 'group_right'].includes(fn)) {
      analysis.functions.push(fn);
    }
  }

  // Extract metrics (words that look like metric names)
  const metricPattern = /[a-z_][a-z0-9_:]*/g;
  const allTokens = new Set();
  while ((match = metricPattern.exec(normalized)) !== null) {
    const token = match[0];
    if (token.includes('_') && token.length > 3 && !['rate', 'irate', 'increase', 'sum', 'avg', 'max', 'min', 'count', 'by', 'without', 'bool', 'on', 'ignoring', 'group_left', 'group_right', 'offset', 'and', 'or', 'unless'].includes(token)) {
      allTokens.add(token);
    }
  }
  analysis.metrics = [...allTokens];

  // Extract labels
  const labelPattern = /\{([^}]*)\}/g;
  while ((match = labelPattern.exec(query)) !== null) {
    const labelsStr = match[1];
    const labelPairs = labelsStr.split(',').map(s => s.trim()).filter(Boolean);
    for (const pair of labelPairs) {
      const eqIdx = pair.search(/[=~!]/);
      if (eqIdx > 0) {
        analysis.labels.push(pair.substring(0, eqIdx).trim());
      }
    }
  }

  // Extract duration
  const durPattern = /\[(\d+[smhd])\]/;
  const durMatch = query.match(durPattern);
  if (durMatch) analysis.duration = durMatch[1];

  // Match against recording rules
  for (const rule of RECORDING_RULES) {
    // Check if user query uses the recording rule name
    if (normalized.includes(rule.rule.toLowerCase())) {
      analysis.matchedRules.push({ ...rule, matchType: 'direct' });
    }
    // Check if user query uses the underlying metric
    for (const metric of analysis.metrics) {
      if (rule.expr.toLowerCase().includes(metric)) {
        analysis.matchedRules.push({ ...rule, matchType: 'underlying' });
        break;
      }
    }
  }

  // Deduplicate
  const seen = new Set();
  analysis.matchedRules = analysis.matchedRules.filter(r => {
    const key = r.rule;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  // Warnings and suggestions
  if (analysis.functions.includes('irate') && analysis.duration) {
    const durNum = parseInt(analysis.duration);
    const durUnit = analysis.duration.slice(-1);
    if (durUnit === 'm' && durNum > 5) {
      analysis.warnings.push(t('irate 通常搭配短窗口（如 5m），較長窗口建議用 rate', 'irate is typically used with short windows (e.g., 5m); for longer windows, consider rate'));
    }
  }

  if (analysis.metrics.length > 0 && analysis.matchedRules.some(r => r.matchType === 'underlying')) {
    analysis.suggestions.push(t('此查詢的 metric 已有對應的 Recording Rule，建議直接使用 recording rule 名稱以提升效能', 'This metric has a matching Recording Rule — consider using the recording rule name for better performance'));
  }

  if (normalized.includes('rate') && !analysis.duration) {
    analysis.warnings.push(t('rate() 需要 range vector，請確認有加 [duration]', 'rate() requires a range vector — ensure you include [duration]'));
  }

  return analysis;
}

/* ── Simulated result preview ── */
function simulateResults(query) {
  const normalized = query.toLowerCase();
  for (const [metric, values] of Object.entries(MOCK_SERIES)) {
    if (normalized.includes(metric.toLowerCase())) {
      if (normalized.includes('rate(') || normalized.includes('irate(')) {
        return values.map((v, i) => i === 0 ? 0 : ((values[i] - values[i - 1]) / 15).toFixed(4)).slice(1);
      }
      return values;
    }
  }
  // Check recording rules
  for (const rule of RECORDING_RULES) {
    if (normalized.includes(rule.rule.toLowerCase())) {
      for (const [metric, values] of Object.entries(MOCK_SERIES)) {
        if (rule.expr.toLowerCase().includes(metric.toLowerCase())) {
          return values;
        }
      }
    }
  }
  return null;
}

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
                    ⚠️ {w}
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
                      return (
                        <div key={i} className="flex-1 flex flex-col items-center gap-1">
                          <span className="text-xs text-slate-400 font-mono" style={{ fontSize: 8 }}>
                            {typeof v === 'number' ? (v > 1000 ? `${(v / 1000).toFixed(0)}K` : v > 100 ? Math.round(v) : v.toFixed ? v.toFixed(1) : v) : v}
                          </span>
                          <div className="w-full bg-blue-500 rounded-t" style={{ height: `${Math.max(pct, 2)}%` }} />
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
