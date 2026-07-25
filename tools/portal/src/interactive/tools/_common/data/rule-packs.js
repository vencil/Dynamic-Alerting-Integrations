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
  mariadb: { label: 'MariaDB/MySQL', category: 'database', exporter: 'mysqld_exporter', configMap: 'prometheus-rules-mariadb', recordingRules: 14, alertRules: 18, defaults: { mysql_connections: { value: 80, unit: 'count', desc: 'Max threads_connected warning', metricClass: 'saturation' }, mysql_cpu: { value: 30, unit: 'threads', desc: 'threads_running saturation, NOT host CPU% — running-threads 1m-avg warning; 80→30 = PMM/Nichter \'high\' (#944); key kept as mysql_cpu (config-contract stability; metric/alert renamed, #944 closed)', metricClass: 'saturation' }, mysql_replication_lag: { value: 30, unit: 'seconds', desc: 'Async replication lag warning (seconds behind primary, sql_delay-adjusted); 30 = APA/mysqld-mixin consensus trigger (they page critical at 30s; demoted to warning — PMM\'s lag-template family default is 600s warning). Enabled by default (#1200 WS5-P0-a Q3=C)' } }, metrics: ['connections', 'cpu', 'memory', 'slow_queries', 'replication_lag', 'query_errors'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  postgresql: { label: 'PostgreSQL', category: 'database', exporter: 'postgres_exporter', configMap: 'prometheus-rules-postgresql', recordingRules: 11, alertRules: 9, defaults: { pg_connections: { value: 80, unit: '% of max_connections', desc: 'Connection usage % warning', metricClass: 'saturation' }, pg_replication_lag: { value: 30, unit: 'seconds', desc: 'Replication lag warning' } }, metrics: ['connections', 'cache_hit', 'query_time', 'disk_usage', 'replication_lag'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  redis: { label: 'Redis', category: 'database', exporter: 'redis_exporter', configMap: 'prometheus-rules-redis', recordingRules: 11, alertRules: 6, defaults: { redis_memory_used_bytes: { value: 4294967296, unit: 'bytes (4GB)', desc: 'Memory usage warning', metricClass: 'saturation' }, redis_connected_clients: { value: 200, unit: 'count', desc: 'Connected clients warning', metricClass: 'saturation' } }, metrics: ['memory', 'evictions', 'connected_clients', 'keyspace_hits'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  mongodb: { label: 'MongoDB', category: 'database', exporter: 'mongodb_exporter', configMap: 'prometheus-rules-mongodb', recordingRules: 10, alertRules: 8, defaults: { mongodb_connections_current: { value: 300, unit: 'count', desc: 'Current connections warning', metricClass: 'saturation' }, mongodb_repl_lag_seconds: { value: 10, unit: 'seconds', desc: 'Replication lag warning' } }, metrics: ['connections', 'memory', 'page_faults', 'replication'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  elasticsearch: { label: 'Elasticsearch', category: 'database', exporter: 'elasticsearch_exporter', configMap: 'prometheus-rules-elasticsearch', recordingRules: 11, alertRules: 7, defaults: { es_jvm_memory_used_percent: { value: 85, unit: '%', desc: 'JVM heap usage warning', metricClass: 'saturation' }, es_filesystem_free_percent: { value: 15, unit: '%', desc: 'Disk free space warning' } }, metrics: ['heap', 'unassigned_shards', 'cluster_health', 'indexing_rate'], dependencies: { suggests: ['kubernetes', 'jvm'], reason: { en: 'ES runs on JVM; K8s monitors container resources', zh: 'ES 運行在 JVM 上；K8s 監控容器資源' } } },
  oracle: { label: 'Oracle', category: 'database', exporter: 'oracledb_exporter', configMap: 'prometheus-rules-oracle', recordingRules: 11, alertRules: 7, defaults: { oracle_sessions_active: { value: 200, unit: 'count', desc: 'Active sessions warning', metricClass: 'saturation' }, oracle_tablespace_used_percent: { value: 85, unit: '%', desc: 'Tablespace usage warning' } }, metrics: ['sessions', 'tablespace', 'wait_events', 'redo_log'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  db2: { label: 'DB2', category: 'database', exporter: 'db2_exporter', configMap: 'prometheus-rules-db2', recordingRules: 13, alertRules: 8, defaults: { db2_connections_active: { value: 200, unit: 'count', desc: 'Active connections warning', metricClass: 'saturation' }, db2_bufferpool_hit_ratio: { value: 0.95, unit: 'ratio', desc: 'Bufferpool hit ratio warning' } }, metrics: ['connections', 'bufferpool', 'tablespace', 'lock_waits'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  clickhouse: { label: 'ClickHouse', category: 'database', exporter: 'clickhouse_exporter', configMap: 'prometheus-rules-clickhouse', recordingRules: 12, alertRules: 7, defaults: { clickhouse_queries_rate: { value: 500, unit: 'qps', desc: 'Query rate warning (5m)' }, clickhouse_active_connections: { value: 200, unit: 'count', desc: 'Active TCP connections warning', metricClass: 'saturation' } }, metrics: ['queries', 'merges', 'replicated_lag', 'memory'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement DB monitoring', zh: '容器資源告警補充 DB 監控' } } },
  kafka: { label: 'Kafka', category: 'messaging', exporter: 'kafka_exporter', configMap: 'prometheus-rules-kafka', recordingRules: 13, alertRules: 9, defaults: { kafka_consumer_lag: { value: 1000, unit: 'messages', desc: 'Consumer lag warning', metricClass: 'saturation' }, kafka_under_replicated_partitions: { value: 0, unit: 'count', desc: 'Under-replicated partitions (should be 0)' }, kafka_broker_count: { value: 3, unit: 'count', desc: 'Minimum broker count warning' }, kafka_active_controllers: { value: 1, unit: 'count', desc: 'Minimum active controllers (should be 1)' }, kafka_request_rate: { value: 10000, unit: 'msg/s', desc: 'Message rate warning (5m)' } }, metrics: ['consumer_lag', 'broker_active', 'controller', 'isr_shrink', 'under_replicated'], dependencies: { suggests: ['kubernetes', 'jvm'], reason: { en: 'Kafka brokers run on JVM; K8s monitors pods', zh: 'Kafka broker 運行在 JVM 上；K8s 監控 Pod' } } },
  rabbitmq: { label: 'RabbitMQ', category: 'messaging', exporter: 'rabbitmq_exporter', configMap: 'prometheus-rules-rabbitmq', recordingRules: 12, alertRules: 8, defaults: { rabbitmq_queue_messages: { value: 100000, unit: 'messages', desc: 'Queue depth warning', metricClass: 'saturation' }, rabbitmq_node_mem_percent: { value: 80, unit: '%', desc: 'Node memory usage warning', metricClass: 'saturation' }, rabbitmq_connections: { value: 1000, unit: 'count', desc: 'Connection count warning', metricClass: 'saturation' }, rabbitmq_consumers: { value: 5, unit: 'count', desc: 'Minimum consumer count warning' }, rabbitmq_unacked_messages: { value: 10000, unit: 'messages', desc: 'Unacknowledged messages warning', metricClass: 'saturation' } }, metrics: ['queue_depth', 'consumers', 'memory', 'disk_free', 'connections'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Container resource alerts complement MQ monitoring', zh: '容器資源告警補充 MQ 監控' } } },
  jvm: { label: 'JVM', category: 'runtime', exporter: 'jmx_exporter', configMap: 'prometheus-rules-jvm', recordingRules: 9, alertRules: 7, defaults: { jvm_gc_pause: { value: 0.5, unit: 'seconds/5m', desc: 'GC pause duration rate warning' }, jvm_memory: { value: 80, unit: '%', desc: 'Heap memory usage warning', metricClass: 'saturation' }, jvm_threads: { value: 500, unit: 'count', desc: 'Active thread count warning', metricClass: 'saturation' } }, metrics: ['gc_pause', 'heap_usage', 'thread_pool', 'class_loading'], dependencies: { suggests: ['kubernetes'], reason: { en: 'JVM apps typically run in K8s pods', zh: 'JVM 應用通常運行在 K8s Pod 中' } } },
  nginx: { label: 'Nginx', category: 'webserver', exporter: 'nginx-prometheus-exporter', configMap: 'prometheus-rules-nginx', recordingRules: 9, alertRules: 6, defaults: { nginx_connections: { value: 1000, unit: 'count', desc: 'Active connections warning', metricClass: 'saturation' }, nginx_request_rate: { value: 5000, unit: 'req/s', desc: 'Request rate warning' }, nginx_waiting: { value: 200, unit: 'count', desc: 'Waiting connections (backlog) warning', metricClass: 'saturation' } }, metrics: ['active_connections', 'request_rate', 'connection_backlog'], dependencies: { suggests: ['kubernetes'], reason: { en: 'Ingress/proxy pods benefit from K8s resource alerts', zh: 'Ingress/proxy Pod 受益於 K8s 資源告警' } } },
  kubernetes: { label: 'Kubernetes', category: 'infrastructure', exporter: 'cAdvisor + kube-state-metrics', configMap: 'prometheus-rules-kubernetes', recordingRules: 30, alertRules: 14, defaults: { container_cpu: { value: 80, unit: '%', desc: 'Container CPU % of limit (weakest link)', metricClass: 'saturation' }, container_cpu_throttle: { value: 25, unit: '%', desc: 'Chronic CFS throttle: % of ACTIVE 100ms periods throttled, NOT % CPU lost (critical tier opt-in via container_cpu_throttle_critical, suggest 50; #944)', metricClass: 'saturation' }, container_memory: { value: 85, unit: '%', desc: 'Container memory % of limit (weakest link)', metricClass: 'saturation' } }, metrics: ['pod_restart', 'cpu_limit', 'memory_limit', 'pvc_usage'] },
  liveness: { label: 'Exporter Liveness', category: 'infrastructure', exporter: 'threshold-exporter', configMap: 'prometheus-rules-liveness', recordingRules: 0, alertRules: 1, required: true, defaults: {}, metrics: ['expected_exporter', 'exporter_up'] },
  operational: { label: 'Operational', category: 'infrastructure', exporter: 'threshold-exporter', configMap: 'prometheus-rules-operational', recordingRules: 0, alertRules: 4, required: true, defaults: {}, metrics: ['exporter_health', 'config_reload'] },
  platform: { label: 'Platform', category: 'infrastructure', exporter: 'threshold-exporter', configMap: 'prometheus-rules-platform', recordingRules: 0, alertRules: 34, required: true, defaults: {}, metrics: ['threshold_metric_count', 'recording_rule_health', 'scrape_success'] },
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

export { RULE_PACK_DATA, CATEGORY_LABELS, getAllMetricKeys, PACK_ORDER };
