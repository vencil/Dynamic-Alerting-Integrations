---
title: "PromQL Tester — simplified regex PromQL parser + mock series"
purpose: |
  Pure data + functions extracted from promql-tester.jsx (portal ROI extraction,
  mirroring the threshold-calculator / multi-tenant-comparison waves) so the
  simplified regex PromQL parser can be exercised without React. Pre-extraction
  analyzeQuery / simulateResults were inline in the .jsx with 0% unit coverage:
  the regex-based token / function / label / duration / recording-rule matching
  had no tests, so any drift in what the parser detects was invisible.

  Behavior is a VERBATIM, byte-identical move, not a re-derivation: the
  orchestrator now imports these back and keeps only React state + render. This
  is a regex PARSER — several detections deliberately over-approximate (function
  names and label values leak into `.metrics`; `matchedRules` 'underlying' is a
  substring match; the duration regex only handles a single `\d+[smhd]` unit).
  Those quirks are PINNED by promql-tester-parse.test.ts, NOT fixed here. Any
  change to what the parser emits is a behavior change and must be intentional.

  i18n: analyzeQuery's warning / suggestion strings call `t()`. We reproduce the
  .jsx's module-scope `const t = window.__t || ((zh, en) => en)` so the strings
  resolve identically (in tests `window.__t` is undefined ⇒ `t` returns the
  English arg).

  Public API:
    RECORDING_RULES  [{ pack, rule, expr, desc }]  known Rule-Pack recording rules
    MOCK_SERIES      { metric: number[] }           simulated 10-point series
    analyzeQuery(query)
      -> { functions, metrics, labels, duration, matchedRules, warnings, suggestions }
      functions : allowlisted `\w+(` names, NOT deduplicated
      metrics   : lowercased `[a-z_][a-z0-9_:]*` tokens with `_`, len>3, minus a
                  keyword denylist (Set-deduped, insertion order); OVER-DETECTS
                  underscore-bearing function names + label values.
      labels    : label KEYS from `{...}` (matcher =/=~/!=), from the ORIGINAL-case query
      duration  : first `[\d+<s|m|h|d>]` only (no compound / float / subquery ranges)
      matchedRules : recording rules matched 'direct' (query contains rule name)
                  or 'underlying' (a detected metric is a SUBSTRING of rule.expr);
                  deduped by rule name keeping the first push ⇒ 'direct' wins.
      warnings / suggestions : t()-localized, derived from the fields above.
    simulateResults(query)
      -> number[] | string[] | null  MOCK_SERIES lookup (rate()/irate() ⇒ per-15s
         deltas as toFixed(4) strings, dropping the first point), else null.

  Closure deps: `t` (module-scope, above). Otherwise pure.
---

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

  // Whether ANY underlying-metric match existed BEFORE dedup. The recording-rule
  // suggestion below keys off this, NOT the deduped list: a rule matched both
  // directly AND via its underlying metric collapses to a single 'direct' entry,
  // which would otherwise wrongly suppress a still-useful hint. (fixes Q7)
  const hadUnderlyingMatch = analysis.matchedRules.some(r => r.matchType === 'underlying');

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

  if (analysis.metrics.length > 0 && hadUnderlyingMatch) {
    analysis.suggestions.push(t('此查詢的 metric 已有對應的 Recording Rule，建議直接使用 recording rule 名稱以提升效能', 'This metric has a matching Recording Rule — consider using the recording rule name for better performance'));
  }

  // A range vector `[5m]` / subquery `[5m:1m]` / float `[1.5h]` all open with
  // `[` + digit(s) + a time UNIT (`[\d[\d.]*[smhdwy]`); warn only when no such range
  // bracket is present. (Keying off `analysis.duration` mis-fired on subqueries the
  // single-unit durPattern can't parse. The unit anchor is load-bearing: a bare
  // `[\d` would also swallow a digit-only bracket in a LABEL VALUE — e.g.
  // `{path="[404]"}` — and wrongly suppress a genuinely-needed warning.) (fixes Q5)
  if (/\brate\s*\(/.test(normalized) && !/\[\d[\d.]*[smhdwy]/.test(query)) {
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

export { analyzeQuery, simulateResults, RECORDING_RULES, MOCK_SERIES };
