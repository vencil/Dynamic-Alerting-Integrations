---
title: "_common — Rule Pack catalog accessor"
purpose: |
  Single source of truth for the in-browser Rule Pack catalog (DB /
  middleware / runtime defaults + metric lists + display labels) and
  the helper that flattens defaults into a [{key, pack, label, ...}]
  list for autocomplete / validation.

  Layered fallback at module-eval time:
    1. window.__PLATFORM_DATA.rulePacks — if `make platform-data`
       generated assets/platform-data.json and jsx-loader pre-fetched
       it (production deployment path)
    2. inline catalog below — last-resort offline / standalone bundle
       (lets a JSX file be opened from disk for quick smoke testing)

  The inline catalog is a FULL mirror of platform-data.json's rulePacks:
  label / category / exporter / configMap / recordingRules / alertRules /
  required / defaults / metrics. The four derived fields (exporter,
  configMap, recordingRules, alertRules) were added by canonicalize PR-3
  so the pack-count consumers (rule-pack-matrix, rule-pack-selector,
  capacity-planner, dependency-graph) can converge here instead of each
  baking its own count table — four hand-maintained copies that had all
  drifted to a v2.7.0-era snapshot (mariadb 11/8 vs 14/18, kubernetes 7/4
  vs 30/14, platform 0/4 vs 0/34, `liveness` missing entirely).

  Regenerate after `make platform-data`; rule-packs-fallback-drift.test.ts
  fails on any divergence.

  Pre-PR-portal-3 this lived in portal-shared.jsx tied to the alert
  builder / validator / simulator UI components. Pulling it into
  _common/data/ lets new tools (capacity-planner, multi-tenant-
  comparison, etc.) consume it without dragging the React component
  surface along.

  Public API:
    RULE_PACK_DATA            map of packId to {label, category, defaults, metrics, ...}
    PACK_ORDER                ordered packId list (window.__PLATFORM_DATA.packOrder || Object.keys(RULE_PACK_DATA))
    CATEGORY_LABELS           map of category to i18n thunk
    getAllMetricKeys(packs)   flatten defaults to [{key, pack, label, value, unit, desc}]
    DECLARED_KEYS             window.__PLATFORM_DATA.declaredKeys || {} — keys the
                              platform recognises but assigns NO value to
    getDeclaredKeys(packs)    flatten those to [{key, pack, label, value, unit, desc}]
                              where `value` is a REFERENCE number only

  ⛔ DECLARED_KEYS is deliberately a separate accessor, not extra entries in
  getAllMetricKeys: consumers of that list read `value` as "the default the
  platform supplies", and for these keys there is no such thing — omitting one
  is silence, not a fallback.

  Per-default optional field `metricClass` ('saturation') mirrors
  scaffold_tenant.py RULE_PACKS `metric_class` (via platform-data
  `metricClass`) — consumers use it to show the saturation `_critical`
  educational hint (display-only, never blocks validation).

  Closure deps: none. Pure data + one helper.

  Consumers (the 3 Tab files + portal-shared.jsx) import these
  directly via ESM (dev-rules §S6).
---

const t = window.__t || ((zh, en) => en);

const RULE_PACK_DATA = window.__PLATFORM_DATA?.rulePacks || {
  mariadb: { label: 'MariaDB/MySQL', category: 'database', exporter: 'mysqld_exporter', configMap: 'prometheus-rules-mariadb', recordingRules: 14, alertRules: 18, defaults: { mysql_connections: { value: 80, unit: 'count', desc: 'Max threads_connected warning', metricClass: 'saturation' }, mysql_threads_running: { value: 30, unit: 'threads', desc: 'threads_running saturation, NOT host CPU% — running-threads 1m-avg warning; 80→30 = PMM/Nichter \'high\' (#944); renamed from mysql_cpu (#1231 owner-approved reversal of the #944 keep — 2-release alias window, old spelling still resolves)', metricClass: 'saturation' }, mysql_replication_lag: { value: 30, unit: 'seconds', desc: 'Async replication lag warning (seconds behind primary, sql_delay-adjusted); 30 = APA/mysqld-mixin consensus trigger (they page critical at 30s; demoted to warning — PMM\'s lag-template family default is 600s warning). Enabled by default (#1200 WS5-P0-a Q3=C)' } }, metrics: ['connections', 'cpu', 'threads_running', 'memory', 'slow_queries', 'replication_lag', 'query_errors'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  postgresql: { label: 'PostgreSQL', category: 'database', exporter: 'postgres_exporter', configMap: 'prometheus-rules-postgresql', recordingRules: 11, alertRules: 9, defaults: { pg_connections: { value: 80, unit: '% of max_connections', desc: 'Connection usage % warning', metricClass: 'saturation' }, pg_replication_lag: { value: 30, unit: 'seconds', desc: 'Replication lag warning' } }, metrics: ['connections', 'cache_hit', 'query_time', 'disk_usage', 'replication_lag'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  redis: { label: 'Redis', category: 'database', exporter: 'redis_exporter', configMap: 'prometheus-rules-redis', recordingRules: 11, alertRules: 6, defaults: { redis_memory_used_bytes: { value: 4294967296, unit: 'bytes (4GB)', desc: 'Memory usage warning', metricClass: 'saturation' }, redis_connected_clients: { value: 200, unit: 'count', desc: 'Connected clients warning', metricClass: 'saturation' }, redis_evicted_keys_rate: { value: 100, unit: 'keys/s', desc: 'Key eviction rate; #1196 B identity fix: renamed from redis_evicted_keys_total (alert demands _rate) + moved optional_overrides→defaults — unwired dead key, no alias' }, redis_replication_lag: { value: 30, unit: 'seconds', desc: 'Replica lag warning; 30 = same anchor as mysql/pg replication_lag — redis replication is normally sub-second, so 30s is unambiguous trouble (noise-averse pick, #1231 owner call); #1196 C identity fix, no alias' } }, metrics: ['memory', 'evictions', 'connected_clients', 'keyspace_hits'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  mongodb: { label: 'MongoDB', category: 'database', exporter: 'mongodb_exporter', configMap: 'prometheus-rules-mongodb', recordingRules: 10, alertRules: 8, defaults: { mongodb_connections_current: { value: 300, unit: 'count', desc: 'Current connections warning', metricClass: 'saturation' }, mongodb_replication_lag: { value: 10, unit: 'seconds', desc: 'Replication lag warning; #1196 C identity fix: renamed from mongodb_repl_lag_seconds (alert demands mongodb_replication_lag) — unwired dead key, no alias' }, mongodb_opcounters_rate: { value: 10000, unit: 'ops/s', desc: 'Total operations rate; #1196 B identity fix: renamed from mongodb_opcounters_total (alert demands _rate) + moved optional_overrides→defaults — unwired dead key, no alias' } }, metrics: ['connections', 'memory', 'page_faults', 'replication'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  elasticsearch: { label: 'Elasticsearch', category: 'database', exporter: 'elasticsearch_exporter', configMap: 'prometheus-rules-elasticsearch', recordingRules: 11, alertRules: 7, defaults: { es_heap_usage_percent: { value: 85, unit: '%', desc: 'JVM heap usage warning; #1196 C identity fix: renamed from es_jvm_memory_used_percent (alert demands es_heap_usage_percent) — unwired dead key, no alias', metricClass: 'saturation' }, es_disk_usage_percent: { value: 85, unit: '%', desc: 'Disk USAGE % warning (used = 1 - available/size, recording rule tenant:es_disk_usage_percent:max, compared with >); 85 = same band as oracle/db2 tablespace_used_percent; #1196 C semantic flip: replaces es_filesystem_free_percent 15 (FREE %) — carrying 15 into a used-% rule would fire permanently' }, es_pending_tasks: { value: 50, unit: 'count', desc: 'Pending cluster tasks warning; 50 = existing de-facto shipped value (init_project.py catalog + examples/elasticsearch-tenant.yaml); #1196 D: key was alert-demanded but absent from scaffold' }, es_search_latency_ms: { value: 1000, unit: 'ms', desc: 'Avg search latency warning (rate(query_time)/rate(query_total), per-query mean); 1000 = Awesome Prometheus Alerts ElasticsearchHighQueryLatency anchor (same formula, 1s/5m warning) — the only community rule semantically identical to ours; full derivation + calibration caveat in the pack header (#1196 D)' } }, metrics: ['heap', 'unassigned_shards', 'cluster_health', 'indexing_rate'], dependencies: { suggests: ['kubernetes', 'jvm'], reason: { en: 'ES runs on JVM; K8s monitors container resources', zh: 'ES 運行在 JVM 上；K8s 監控容器資源' } } },
  oracle: { label: 'Oracle', category: 'database', exporter: 'oracledb_exporter', configMap: 'prometheus-rules-oracle', recordingRules: 11, alertRules: 7, defaults: { oracle_sessions_active: { value: 200, unit: 'count', desc: 'Active sessions warning', metricClass: 'saturation', valueCounterexample: { issue: 1176, direction: 'over_fire', observed: 'a healthy diurnal peak reaches 240 active sessions', observed_zh: '健康的日夜週期尖峰會到 240 個 active session' } }, oracle_tablespace_used_percent: { value: 85, unit: '%', desc: 'Tablespace usage warning' } }, metrics: ['sessions', 'tablespace', 'wait_events', 'redo_log'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  db2: { label: 'DB2', category: 'database', exporter: 'db2_exporter', configMap: 'prometheus-rules-db2', recordingRules: 13, alertRules: 8, defaults: { db2_connections_active: { value: 200, unit: 'count', desc: 'Active connections warning', metricClass: 'saturation', valueCounterexample: { issue: 1176, direction: 'over_fire', observed: 'a peak-hour workload reaches 360 connections', observed_zh: '尖峰時段的工作負載會到 360 個連線' } }, db2_bufferpool_hit_ratio: { value: 0.95, unit: 'ratio', desc: 'Bufferpool hit ratio warning' }, db2_lock_wait_time: { value: 10, unit: 's/s', desc: 'Lock wait accumulation rate warning (rate over 5m, summed across the tenant\'s instances); 10 ≈ 6.7x the 1.5 normal level both reference libraries agree on, below both fault levels (25/60) — full derivation + calibration workflow in the pack header (#1196 D)', valueCounterexample: { issue: 1176, direction: 'over_fire', observed: 'a benign batch lock-wait transient holds 18 s/s for 7 min (negative-db2.yaml)', observed_zh: '良性批次的 lock-wait 短暫維持 18 s/s 達 7 分鐘（negative-db2.yaml）' } } }, metrics: ['connections', 'bufferpool', 'tablespace', 'lock_waits'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  clickhouse: { label: 'ClickHouse', category: 'database', exporter: 'clickhouse_exporter', configMap: 'prometheus-rules-clickhouse', recordingRules: 12, alertRules: 7, defaults: { clickhouse_queries_rate: { value: 500, unit: 'qps', desc: 'Query rate warning (5m)' }, clickhouse_active_connections: { value: 200, unit: 'count', desc: 'Active TCP connections warning', metricClass: 'saturation' } }, metrics: ['queries', 'merges', 'replicated_lag', 'memory'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  kafka: { label: 'Kafka', category: 'messaging', exporter: 'kafka_exporter', configMap: 'prometheus-rules-kafka', recordingRules: 13, alertRules: 9, defaults: { kafka_consumer_lag: { value: 1000, unit: 'messages', desc: 'Consumer lag warning', metricClass: 'saturation' }, kafka_under_replicated_partitions: { value: 0, unit: 'count', desc: 'Under-replicated partitions (should be 0)' }, kafka_broker_count: { value: 3, unit: 'count', desc: 'Minimum broker count warning' }, kafka_active_controllers: { value: 1, unit: 'count', desc: 'Minimum active controllers (should be 1)' }, kafka_request_rate: { value: 10000, unit: 'msg/s', desc: 'Message rate warning (5m)' } }, metrics: ['consumer_lag', 'broker_active', 'controller', 'isr_shrink', 'under_replicated'], dependencies: { suggests: ['kubernetes', 'jvm'], reason: { en: 'Kafka brokers run on JVM; K8s monitors pods', zh: 'Kafka broker 運行在 JVM 上；K8s 監控 Pod' } } },
  rabbitmq: { label: 'RabbitMQ', category: 'messaging', exporter: 'rabbitmq_exporter', configMap: 'prometheus-rules-rabbitmq', recordingRules: 12, alertRules: 8, defaults: { rabbitmq_queue_messages: { value: 100000, unit: 'messages', desc: 'Queue depth warning', metricClass: 'saturation' }, rabbitmq_node_mem_percent: { value: 80, unit: '%', desc: 'Node memory usage warning', metricClass: 'saturation' }, rabbitmq_connections: { value: 1000, unit: 'count', desc: 'Connection count warning', metricClass: 'saturation' }, rabbitmq_consumers: { value: 5, unit: 'count', desc: 'Minimum consumer count warning' }, rabbitmq_unacked_messages: { value: 10000, unit: 'messages', desc: 'Unacknowledged messages warning', metricClass: 'saturation' } }, metrics: ['queue_depth', 'consumers', 'memory', 'disk_free', 'connections'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement MQ monitoring', zh: '容器資源告警補充 MQ 監控' } } },
  jvm: { label: 'JVM', category: 'runtime', exporter: 'jmx_exporter', configMap: 'prometheus-rules-jvm', recordingRules: 9, alertRules: 7, defaults: { jvm_gc_pause: { value: 0.5, unit: 'seconds/5m', desc: 'GC pause duration rate warning' }, jvm_memory: { value: 80, unit: '%', desc: 'Heap memory usage warning', metricClass: 'saturation' }, jvm_threads: { value: 500, unit: 'count', desc: 'Active thread count warning', metricClass: 'saturation' } }, metrics: ['gc_pause', 'heap_usage', 'thread_pool', 'class_loading'], dependencies: { suggests: ['kubernetes'], reason: { en: 'JVM apps typically run in K8s pods', zh: 'JVM 應用通常運行在 K8s Pod 中' } } },
  nginx: { label: 'Nginx', category: 'webserver', exporter: 'nginx-prometheus-exporter', configMap: 'prometheus-rules-nginx', recordingRules: 9, alertRules: 6, defaults: { nginx_connections: { value: 1000, unit: 'count', desc: 'Active connections warning', metricClass: 'saturation' }, nginx_request_rate: { value: 5000, unit: 'req/s', desc: 'Request rate warning' }, nginx_waiting: { value: 200, unit: 'count', desc: 'Waiting connections (backlog) warning', metricClass: 'saturation' } }, metrics: ['active_connections', 'request_rate', 'connection_backlog'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Ingress/proxy pods benefit from K8s resource alerts', zh: 'Ingress/proxy Pod 受益於 K8s 資源告警' } } },
  kubernetes: { label: 'Kubernetes', category: 'infrastructure', exporter: 'cAdvisor + kube-state-metrics', configMap: 'prometheus-rules-kubernetes', recordingRules: 30, alertRules: 14, defaults: { container_cpu: { value: 80, unit: '%', desc: 'Container CPU % of limit (weakest link)', metricClass: 'saturation' }, container_cpu_throttle: { value: 25, unit: '%', desc: 'Chronic CFS throttle: % of ACTIVE 100ms periods throttled, NOT % CPU lost (critical tier opt-in via container_cpu_throttle_critical, suggest 50; #944)', metricClass: 'saturation' }, container_memory: { value: 85, unit: '%', desc: 'Container memory % of limit (weakest link)', metricClass: 'saturation' } }, metrics: ['pod_restart', 'cpu_limit', 'memory_limit', 'pvc_usage'] },
  liveness: { label: 'Exporter Liveness', category: 'infrastructure', exporter: 'threshold-exporter', configMap: 'prometheus-rules-liveness', recordingRules: 0, alertRules: 1, required: true, defaults: {}, metrics: ['expected_exporter', 'exporter_up'] },
  operational: { label: 'Operational', category: 'infrastructure', exporter: 'threshold-exporter', configMap: 'prometheus-rules-operational', recordingRules: 0, alertRules: 4, required: true, defaults: {}, metrics: ['exporter_health', 'config_reload'] },
  platform: { label: 'Platform', category: 'infrastructure', exporter: 'threshold-exporter', configMap: 'prometheus-rules-platform', recordingRules: 0, alertRules: 42, required: true, defaults: {}, metrics: ['threshold_metric_count', 'recording_rule_health', 'scrape_success'] },
};

// Ordered packId list — mirrors RULE_PACK_DATA's layered resolution: the live
// packOrder from platform-data when present, else the baked-in catalog's key
// order (offline / standalone fallback). Consumed by tools that render packs in
// a stable order (e.g. threshold-heatmap's Rule Pack filter).
const PACK_ORDER = window.__PLATFORM_DATA?.packOrder || Object.keys(RULE_PACK_DATA);

const CATEGORY_LABELS = {
  database: () => t('資料庫', 'Databases'),
  messaging: () => t('訊息佇列', 'Messaging'),
  runtime: () => t('運行環境', 'Runtime'),
  webserver: () => t('網頁伺服器', 'Web Servers'),
  infrastructure: () => t('基礎設施', 'Infrastructure'),
};

function getAllMetricKeys(selectedPacks) {
  const keys = [];
  const packs = selectedPacks && selectedPacks.length > 0
    ? selectedPacks
    : Object.keys(RULE_PACK_DATA);
  for (const packId of packs) {
    const pack = RULE_PACK_DATA[packId];
    if (!pack || !pack.defaults) continue;
    for (const [key, meta] of Object.entries(pack.defaults)) {
      keys.push({ key, pack: packId, label: pack.label, ...meta });
    }
  }
  return keys;
}

// Keys the platform RECOGNISES but assigns no value to (registry tier
// `optional_overrides`, flat spellings). A tenant may set one and it fires;
// leaving it out is silence, not a default — there is nothing to fall back to.
//
// Kept OUT of getAllMetricKeys on purpose: every consumer of that list treats
// an entry as "a default the platform supplies" (it spreads `value`/`unit`
// straight from `pack.defaults`), so folding these in would hand them a
// `value` the platform does not stand behind. Separate accessor, separate
// meaning.
//
// Top-level rather than a field on RULE_PACK_DATA, and NOT because the drift
// gate forces it: `carried()` in rule-packs-fallback-drift.test.ts is a
// ten-field WHITELIST, so a new per-pack field would be dropped on both sides
// and never compared at all. That is the actual reason — a per-pack field
// would silently need hand-copying into the inline catalog with nothing to
// catch it. A top-level field gets its own drift gate instead
// (declared-keys.test.ts), which is the same shape images.js uses.
//
// The inline fallback below is a mirror, not a guess: it exists so the
// standalone / file:// / fetch-failed path does not fall back into telling a
// tenant that a documented key is unknown. ⛔ Do NOT hand-edit it — regenerate
// platform-data.json and copy; the drift test compares it key-for-key.
const DECLARED_KEYS = window.__PLATFORM_DATA?.declaredKeys || {
  oracle: [
    { key: 'oracle_wait_time_rate', value: 50, unit: 's/s', desc: 'Wait time rate (5m)' },
    { key: 'oracle_process_count', value: 300, unit: 'count', desc: 'Active processes warning', valueCounterexample: { issue: 1176, direction: 'over_fire', observed: 'a routine backup batch reaches 560 processes', observed_zh: '例行備份批次會到 560 個 process' } },
    { key: 'oracle_pga_allocated_bytes', value: 4294967296, unit: 'bytes (4GB)', desc: 'PGA allocation warning', valueCounterexample: { issue: 1176, direction: 'over_fire', observed: 'a planned stats-gather reaches 22 GB PGA', observed_zh: '計畫性 stats-gather 會用到 22 GB PGA' } },
  ],
  db2: [
    { key: 'db2_log_usage_percent', value: 70, unit: '%', desc: 'Transaction log usage warning' },
    { key: 'db2_deadlock_rate', value: 5, unit: 'count/s', desc: 'Deadlock rate (5m)', valueCounterexample: { issue: 1176, direction: 'under_fire', observed: 'a real ~90/min deadlock storm stays under 5/s (=300/min) and is missed', observed_zh: '真實的 ~90/min deadlock storm 低於 5/s（＝300/min），因此接不到' } },
    { key: 'db2_tablespace_used_percent', value: 85, unit: '%', desc: 'Tablespace usage warning' },
  ],
  clickhouse: [
    { key: 'clickhouse_max_part_count', value: 300, unit: 'count', desc: 'Max part count per partition' },
    { key: 'clickhouse_replication_queue', value: 50, unit: 'count', desc: 'Replication queue size' },
    { key: 'clickhouse_memory_tracking_bytes', value: 8589934592, unit: 'bytes (8GB)', desc: 'Memory tracking warning' },
  ],
};

function getDeclaredKeys(selectedPacks) {
  const keys = [];
  const packs = selectedPacks && selectedPacks.length > 0
    ? selectedPacks
    : Object.keys(DECLARED_KEYS);
  for (const packId of packs) {
    const rows = DECLARED_KEYS[packId];
    if (!rows) continue;
    const label = RULE_PACK_DATA[packId]?.label || packId;
    for (const row of rows) keys.push({ pack: packId, label, ...row });
  }
  return keys;
}

/**
 * The measured counter-example for a threshold key, or null (#1176).
 *
 * Looked up from RULE_PACK_DATA rather than re-listed, so any tool that shows a
 * platform number can show what we know about it without keeping its own copy —
 * which is exactly how multi-tenant-comparison ended up rendering
 * "oracle_sessions_active (default: 200)" with no caveat while the starter YAML
 * next door carried one. Absence means "nothing measured", never "validated".
 */
function getValueCounterexample(metricKey) {
  for (const pack of Object.values(RULE_PACK_DATA)) {
    const meta = pack?.defaults?.[metricKey];
    if (meta?.valueCounterexample) return meta.valueCounterexample;
  }
  // ⛔ BOTH tiers. Looking only at `defaults` made this blind to exactly the
  // keys #1320 D1 says a tenant can fill in today — the reference numbers most
  // likely to be copied were the ones with no caveat attached.
  for (const rows of Object.values(DECLARED_KEYS)) {
    for (const row of rows || []) {
      if (row.key === metricKey && row.valueCounterexample) return row.valueCounterexample;
    }
  }
  return null;
}

/**
 * What the measured counter-example says about the value — or null (#1176).
 *
 * ⛔ Lookup, NOT a ternary. Both portal callers used to write
 * `ce.direction === 'over_fire' ? A : B`, so an unrecognised direction (a third
 * enum value, or data from an older generator) rendered as the OTHER
 * direction's wording: "this value fires on benign load" for a measurement that
 * said the fault is being MISSED. That points an operator at the wrong knob, so
 * unknown returns null and the caller renders nothing.
 *
 * ⚠️ NOT the same posture as Python. `_registry_lib.counterexample_verdict`
 * RAISES; this returns null and the surface silently loses its caveat while
 * still showing the number. That asymmetry is deliberate — a thrown exception
 * in a portal tool blanks the whole page for a reader who only wanted to look
 * at thresholds — but do not read it as fail-loud. The loud half is the
 * generator: bad data cannot reach `platform-data.json` because the Python
 * side refuses to render it first.
 *
 * ⛔ ONE table for the wording, mirroring `_registry_lib.COUNTEREXAMPLE_DIRECTION`.
 * The second hand-written copy had already drifted (half-width vs full-width
 * parentheses) before any blind review looked at it (#1344).
 */
const COUNTEREXAMPLE_DIRECTION = {
  over_fire: ['此值會對正常運作誤警', 'fires on benign load'],
  under_fire: ['此值接不到該抓的故障', 'misses the fault it should catch'],
};

function counterexampleVerdict(ce) {
  const t = window.__t || ((zh, en) => en);
  const pair = COUNTEREXAMPLE_DIRECTION[ce?.direction];
  return pair ? t(pair[0], pair[1]) : null;
}

/**
 * The label, and the punctuation that frames it — both localised.
 *
 * ⛔ The mark was hand-copied in two places after the direction table was
 * unified, which is the same failure one level down. And the JSX copy wrote the
 * separators as literal full-width `：（）`, so the EN locale rendered
 * `measured counter-example: … （fires on benign load）` — an all-English
 * sentence with two CJK brackets in it. Punctuation is part of the language,
 * so it goes through `t()` like everything else (#1344).
 */
function counterexampleLabel() {
  const t = window.__t || ((zh, en) => en);
  return {
    mark: t('實測反例', 'measured counter-example'),
    colon: t('：', ': '),
    open: t('（', ' ('),
    close: t('）', ')'),
  };
}

/**
 * The measured clause in the reader's language (#1344, owner decision).
 *
 * ⛔ Falls back to the English `observed` rather than rendering nothing: a
 * missing translation must not make the caveat disappear, because absence is
 * this repo's signal for "nothing was measured". The Python-side gate requires
 * `observed_zh` on every shipped counter-example, so the fallback is a safety
 * net, not a path. Registry-spelled key, because `platform-data.json` carries
 * the `value_counterexample` object verbatim under a camelCase wrapper.
 */
function counterexampleObserved(ce) {
  const t = window.__t || ((zh, en) => en);
  return t(ce?.observed_zh || ce?.observed, ce?.observed);
}

/**
 * The one-line plain-text form, for surfaces that emit a comment rather than
 * markup (the starter YAML). Markup callers compose from `counterexampleVerdict`
 * instead — a React element with an aria-hidden glyph cannot be this string.
 */
function counterexampleComment(ce) {
  const verdict = counterexampleVerdict(ce);
  if (!verdict) return null;
  // Collapse whitespace: this lands inside a single `#` comment line, and a
  // newline in `observed` would put the tail at column 0 and break the YAML a
  // tenant is about to paste. `_registry_lib.require_counterexample` refuses
  // it at the source; this is the belt to that pair of braces.
  const observed = String(counterexampleObserved(ce) ?? '').replace(/\s+/g, ' ').trim();
  // ⛔ A missing/blank `observed` renders `⚠ #1 measured counter-example:
  // (fires on benign load)` — an empty clause, which reads as "measured and
  // fine". Python raises on this shape; returning null here is the same
  // refusal expressed the only way a browser can (#1344).
  if (!observed || ce.issue == null) return null;
  const { mark, colon, open, close } = counterexampleLabel();
  return `⚠ #${ce.issue} ${mark}${colon}${observed}${open}${verdict}${close}`;
}

/** The platform-asserted number for a key, across both tiers — or null. */
function platformValue(metricKey) {
  for (const pack of Object.values(RULE_PACK_DATA)) {
    const meta = pack?.defaults?.[metricKey];
    if (meta && meta.value != null) return Number(meta.value);
  }
  for (const rows of Object.values(DECLARED_KEYS)) {
    for (const row of rows || []) {
      if (row.key === metricKey && row.value != null) return Number(row.value);
    }
  }
  return null;
}

/**
 * Put the caveat next to every threshold key a block of YAML hands over (#1176).
 *
 * ⛔ Derived at render time, never authored into the asset. `template-data.json`
 * ships `oracle_sessions_active: "200"` and `db2_connections_active: "200"` —
 * both platform-asserted values with a measured counter-example — as ONE-CLICK
 * COPY yaml, with nothing said about either. Writing the caveat into that JSON
 * by hand would create the eighth copy of a fact the registry already owns and
 * would go stale the next time a value is recalibrated; annotating here covers
 * every template that exists now and every one added later, for free.
 *
 * Blind review found this face (#1344); `check_threshold_unit_sanity.py` had
 * already classified the same file as threshold-carrying, so the platform knew.
 */
function annotateCounterexamples(yamlText) {
  const out = [];
  for (const line of String(yamlText ?? '').split('\n')) {
    // Top-level `key: value` only — the same shape the exporter reads a tenant
    // threshold from. Indented keys are inside `_metadata`/`_routing_*` blocks.
    const m = /^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$/.exec(line);
    // ⛔ Only when THIS LINE carries the measured number. A counter-example is
    // about one specific value, not about a key — the same reasoning that
    // makes `<base>_critical` ineligible (a benign peak of 240 says nothing
    // about a critical threshold of 250). Templates already ship
    // `oracle_sessions_active: "100"` and `"150"`, and a blanket key match
    // would staple "a healthy peak reaches 240" onto a value nobody measured;
    // for an under_fire key set BELOW the reference the pasted sentence would
    // be outright false (blind review, #1344).
    if (m && sameNumber(m[2], platformValue(m[1]))) {
      const comment = counterexampleComment(getValueCounterexample(m[1]));
      if (comment) out.push(`# ${comment}`);
    }
    out.push(line);
  }
  return out.join('\n');
}

function sameNumber(rawScalar, value) {
  if (value == null) return false;
  const n = Number(String(rawScalar).trim().replace(/^["']|["']$/g, ''));
  return Number.isFinite(n) && n === Number(value);
}

export {
  RULE_PACK_DATA, CATEGORY_LABELS, getAllMetricKeys, PACK_ORDER,
  DECLARED_KEYS, getDeclaredKeys, getValueCounterexample,
  counterexampleVerdict, counterexampleComment, counterexampleLabel,
  counterexampleObserved, annotateCounterexamples,
};
