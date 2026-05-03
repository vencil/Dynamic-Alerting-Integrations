---
title: "_common — Rule Pack catalog accessor"
purpose: |
  Single source of truth for the in-browser Rule Pack catalog (DB /
  middleware / runtime defaults + metric lists + display labels) and
  the helper that flattens defaults into a [{key, pack, label, ...}]
  list for autocomplete / validation.

  Layered fallback at module-eval time:
    1. window.__platformData.rulePacks  — if `make platform-data`
       generated assets/platform-data.json and jsx-loader pre-fetched
       it (production deployment path)
    2. inline catalog below — last-resort offline / standalone bundle
       (lets a JSX file be opened from disk for quick smoke testing)

  Pre-PR-portal-3 this lived in portal-shared.jsx tied to the alert
  builder / validator / simulator UI components. Pulling it into
  _common/data/ lets new tools (capacity-planner, multi-tenant-
  comparison, etc.) consume it without dragging the React component
  surface along.

  Public API:
    window.__RULE_PACK_DATA            map of packId to {label, category, defaults, metrics, ...}
    window.__CATEGORY_LABELS           map of category to i18n thunk
    window.__getAllMetricKeys(packs)   flatten defaults to [{key, pack, label, value, unit, desc}]

  Closure deps: none. Pure data + one helper.

  Backward compatibility: portal-shared.jsx re-exports these on
  window.__portalShared verbatim, so the 4 existing consumers
  (self-service-portal + 3 Tab files) need no changes.
---

const t = window.__t || ((zh, en) => en);

const RULE_PACK_DATA = window.__platformData?.rulePacks || {
  mariadb: { label: 'MariaDB/MySQL', category: 'database', defaults: { mysql_connections: { value: 80, unit: 'count', desc: 'Max connections warning' }, mysql_cpu: { value: 80, unit: '%', desc: 'CPU threads rate warning' } }, metrics: ['connections', 'cpu', 'memory', 'slow_queries', 'replication_lag', 'query_errors'] },
  postgresql: { label: 'PostgreSQL', category: 'database', defaults: { pg_connections: { value: 80, unit: '%', desc: 'Connection usage warning' }, pg_replication_lag: { value: 30, unit: 'seconds', desc: 'Replication lag warning' } }, metrics: ['connections', 'cache_hit', 'query_time', 'disk_usage', 'replication_lag'] },
  redis: { label: 'Redis', category: 'database', defaults: { redis_memory_used_bytes: { value: 4294967296, unit: 'bytes', desc: 'Memory usage warning' }, redis_connected_clients: { value: 200, unit: 'count', desc: 'Connected clients warning' } }, metrics: ['memory', 'evictions', 'connected_clients', 'keyspace_hits'] },
  mongodb: { label: 'MongoDB', category: 'database', defaults: { mongodb_connections_current: { value: 300, unit: 'count', desc: 'Current connections warning' }, mongodb_repl_lag_seconds: { value: 10, unit: 'seconds', desc: 'Replication lag warning' } }, metrics: ['connections', 'memory', 'page_faults', 'replication'] },
  elasticsearch: { label: 'Elasticsearch', category: 'database', defaults: { es_jvm_memory_used_percent: { value: 85, unit: '%', desc: 'JVM heap usage warning' }, es_filesystem_free_percent: { value: 15, unit: '%', desc: 'Disk free space warning' } }, metrics: ['heap', 'unassigned_shards', 'cluster_health', 'indexing_rate'] },
  oracle: { label: 'Oracle', category: 'database', defaults: { oracle_sessions_active: { value: 200, unit: 'count', desc: 'Active sessions warning' }, oracle_tablespace_used_percent: { value: 85, unit: '%', desc: 'Tablespace usage warning' } }, metrics: ['sessions', 'tablespace', 'wait_events', 'redo_log'] },
  db2: { label: 'DB2', category: 'database', defaults: { db2_connections_active: { value: 200, unit: 'count', desc: 'Active connections warning' }, db2_bufferpool_hit_ratio: { value: 0.95, unit: 'ratio', desc: 'Bufferpool hit ratio warning' } }, metrics: ['connections', 'bufferpool', 'tablespace', 'lock_waits'] },
  clickhouse: { label: 'ClickHouse', category: 'database', defaults: { clickhouse_queries_rate: { value: 500, unit: 'qps', desc: 'Query rate warning' }, clickhouse_active_connections: { value: 200, unit: 'count', desc: 'Active connections warning' } }, metrics: ['queries', 'merges', 'replicated_lag', 'memory'] },
  kafka: { label: 'Kafka', category: 'messaging', defaults: { kafka_consumer_lag: { value: 1000, unit: 'messages', desc: 'Consumer lag warning' }, kafka_under_replicated_partitions: { value: 0, unit: 'count', desc: 'Under-replicated partitions' }, kafka_broker_count: { value: 3, unit: 'count', desc: 'Min broker count' }, kafka_active_controllers: { value: 1, unit: 'count', desc: 'Min active controllers' }, kafka_request_rate: { value: 10000, unit: 'msg/s', desc: 'Message rate warning' } }, metrics: ['consumer_lag', 'broker_active', 'controller', 'isr_shrink', 'under_replicated'] },
  rabbitmq: { label: 'RabbitMQ', category: 'messaging', defaults: { rabbitmq_queue_messages: { value: 100000, unit: 'messages', desc: 'Queue depth warning' }, rabbitmq_node_mem_percent: { value: 80, unit: '%', desc: 'Node memory usage warning' }, rabbitmq_connections: { value: 1000, unit: 'count', desc: 'Connection count warning' }, rabbitmq_consumers: { value: 5, unit: 'count', desc: 'Min consumer count' }, rabbitmq_unacked_messages: { value: 10000, unit: 'messages', desc: 'Unacked messages warning' } }, metrics: ['queue_depth', 'consumers', 'memory', 'disk_free', 'connections'] },
  jvm: { label: 'JVM', category: 'runtime', defaults: { jvm_gc_pause: { value: 0.5, unit: 'seconds/5m', desc: 'GC pause duration warning' }, jvm_memory: { value: 80, unit: '%', desc: 'Heap usage warning' }, jvm_threads: { value: 500, unit: 'count', desc: 'Active thread count warning' } }, metrics: ['gc_pause', 'heap_usage', 'thread_pool', 'class_loading'] },
  nginx: { label: 'Nginx', category: 'webserver', defaults: { nginx_connections: { value: 1000, unit: 'count', desc: 'Active connections warning' }, nginx_request_rate: { value: 5000, unit: 'req/s', desc: 'Request rate warning' }, nginx_waiting: { value: 200, unit: 'count', desc: 'Waiting connections warning' } }, metrics: ['active_connections', 'request_rate', 'connection_backlog'] },
  kubernetes: { label: 'Kubernetes', category: 'infrastructure', defaults: { container_cpu: { value: 80, unit: '%', desc: 'Container CPU % of limit' }, container_memory: { value: 85, unit: '%', desc: 'Container memory % of limit' } }, metrics: ['pod_restart', 'cpu_limit', 'memory_limit', 'pvc_usage'] },
  operational: { label: 'Operational', category: 'infrastructure', required: true, defaults: {}, metrics: ['exporter_health', 'config_reload'] },
  platform: { label: 'Platform', category: 'infrastructure', required: true, defaults: {}, metrics: ['threshold_metric_count', 'recording_rule_health', 'scrape_success'] },
};

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

window.__RULE_PACK_DATA = RULE_PACK_DATA;
window.__CATEGORY_LABELS = CATEGORY_LABELS;
window.__getAllMetricKeys = getAllMetricKeys;
