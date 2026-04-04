---
title: "Rule Pack Detail Viewer"
tags: [rules, PromQL, reference]
audience: [platform-engineer, domain-expert]
version: v2.3.0
lang: en
related: [rule-pack-matrix, rule-pack-selector, runbook-viewer]
---

import React, { useState } from 'react';

const t = window.__t || ((zh, en) => en);

const RULE_PACKS = {
  mariadb: {
    label: 'MariaDB/MySQL',
    category: 'Database',
    description: 'Comprehensive MySQL/MariaDB monitoring: connections, replication, query performance, and resource utilization.',
    exporter: 'mysqld_exporter',
    recording: [
      { name: 'tenant:mysql_connections:ratio', expr: 'mysql_global_status_threads_connected / tenant:mysql_connections_threshold:value', desc: 'Current connections as ratio of threshold' },
      { name: 'tenant:mysql_cpu:ratio', expr: 'rate(process_cpu_seconds_total{job=~".*mysql.*"}[5m]) * 100 / tenant:mysql_cpu_threshold:value', desc: 'CPU usage as ratio of threshold' },
      { name: 'tenant:mysql_memory:ratio', expr: 'mysql_global_status_innodb_buffer_pool_bytes_data / mysql_global_variables_innodb_buffer_pool_size * 100 / tenant:mysql_memory_threshold:value', desc: 'Buffer pool usage ratio' },
      { name: 'tenant:mysql_slow_queries:rate5m', expr: 'rate(mysql_global_status_slow_queries[5m])', desc: 'Slow query rate (5m)' },
      { name: 'tenant:mysql_replication_lag:seconds', expr: 'mysql_slave_status_seconds_behind_master', desc: 'Replication lag in seconds' },
    ],
    alerts: [
      { name: 'MariaDBHighConnections', severity: 'warning', expr: 'tenant:mysql_connections:ratio > 1', desc: 'Connections exceed warning threshold', action: 'Check connection pooling, consider raising max_connections' },
      { name: 'MariaDBHighConnectionsCritical', severity: 'critical', expr: 'tenant:mysql_connections_critical:ratio > 1', desc: 'Connections exceed critical threshold', action: 'Immediate: kill idle connections, investigate connection leaks' },
      { name: 'MariaDBHighCPU', severity: 'warning', expr: 'tenant:mysql_cpu:ratio > 1 for 5m', desc: 'CPU usage above threshold for 5 minutes', action: 'Check slow queries, optimize heavy queries' },
      { name: 'MariaDBHighMemory', severity: 'warning', expr: 'tenant:mysql_memory:ratio > 1', desc: 'Buffer pool usage above threshold', action: 'Review buffer pool size, check for memory leaks' },
      { name: 'MariaDBSlowQueries', severity: 'warning', expr: 'tenant:mysql_slow_queries:rate5m > tenant:mysql_slow_queries_threshold:value', desc: 'Slow query rate exceeds threshold', action: 'Enable slow query log, analyze with pt-query-digest' },
      { name: 'MariaDBReplicationLag', severity: 'warning', expr: 'tenant:mysql_replication_lag:seconds > tenant:mysql_replication_lag_threshold:value', desc: 'Replication lag above threshold', action: 'Check replica I/O and SQL threads, network latency' },
      { name: 'MariaDBReplicationDown', severity: 'critical', expr: 'mysql_slave_status_slave_io_running == 0 or mysql_slave_status_slave_sql_running == 0', desc: 'Replication threads stopped', action: 'Immediately investigate SHOW SLAVE STATUS' },
      { name: 'MariaDBPerformanceDegraded', severity: 'critical', expr: 'count(ALERTS{alertname=~"MariaDB.*", severity="warning"}) >= 3', desc: 'Composite: 3+ warnings indicate degradation', action: 'Comprehensive health check needed' },
    ]
  },
  postgresql: {
    label: 'PostgreSQL',
    category: 'Database',
    description: 'PostgreSQL monitoring: connections, cache efficiency, query performance, disk usage, and replication health.',
    exporter: 'postgres_exporter',
    recording: [
      { name: 'tenant:pg_connections:ratio', expr: 'pg_stat_activity_count / tenant:pg_connections_threshold:value', desc: 'Active connections as ratio of threshold' },
      { name: 'tenant:pg_cache_hit_ratio:pct', expr: 'pg_stat_database_blks_hit / (pg_stat_database_blks_hit + pg_stat_database_blks_read) * 100', desc: 'Cache hit ratio percentage' },
      { name: 'tenant:pg_query_time:p95', expr: 'histogram_quantile(0.95, rate(pg_stat_statements_mean_time_bucket[5m]))', desc: '95th percentile query time' },
    ],
    alerts: [
      { name: 'PostgreSQLHighConnections', severity: 'warning', expr: 'tenant:pg_connections:ratio > 1', desc: 'Connections exceed threshold', action: 'Review connection pooling (PgBouncer), check for leaks' },
      { name: 'PostgreSQLHighConnectionsCritical', severity: 'critical', expr: 'tenant:pg_connections_critical:ratio > 1', desc: 'Critical connection threshold', action: 'Terminate idle connections, scale connection pool' },
      { name: 'PostgreSQLLowCacheHit', severity: 'warning', expr: 'tenant:pg_cache_hit_ratio:pct < tenant:pg_cache_hit_ratio_threshold:value', desc: 'Cache hit ratio below threshold', action: 'Increase shared_buffers, check for sequential scans' },
      { name: 'PostgreSQLSlowQueries', severity: 'warning', expr: 'tenant:pg_query_time:p95 > tenant:pg_query_time_threshold:value', desc: 'Query time above threshold', action: 'Analyze pg_stat_statements, add missing indexes' },
      { name: 'PostgreSQLHighDiskUsage', severity: 'warning', expr: 'tenant:pg_disk_usage:ratio > 1', desc: 'Disk usage above threshold', action: 'Run VACUUM FULL, archive old partitions' },
      { name: 'PostgreSQLReplicationLag', severity: 'warning', expr: 'pg_replication_lag > tenant:pg_replication_lag_threshold:value', desc: 'Replication lag detected', action: 'Check WAL sender/receiver, network bandwidth' },
    ]
  },
  redis: {
    label: 'Redis',
    category: 'Database',
    description: 'Redis monitoring: memory usage, eviction rate, connected clients, and keyspace operations.',
    exporter: 'redis_exporter',
    recording: [
      { name: 'tenant:redis_memory:ratio', expr: 'redis_memory_used_bytes / redis_memory_max_bytes * 100 / tenant:redis_memory_threshold:value', desc: 'Memory usage as ratio of threshold' },
      { name: 'tenant:redis_evictions:rate5m', expr: 'rate(redis_evicted_keys_total[5m])', desc: 'Key eviction rate (5m)' },
    ],
    alerts: [
      { name: 'RedisHighMemory', severity: 'warning', expr: 'tenant:redis_memory:ratio > 1', desc: 'Memory usage above threshold', action: 'Review eviction policy, increase maxmemory' },
      { name: 'RedisHighMemoryCritical', severity: 'critical', expr: 'tenant:redis_memory_critical:ratio > 1', desc: 'Critical memory threshold', action: 'Immediate: flush unused keys, scale instance' },
      { name: 'RedisHighEvictions', severity: 'warning', expr: 'tenant:redis_evictions:rate5m > tenant:redis_evictions_threshold:value', desc: 'Eviction rate above threshold', action: 'Increase memory or optimize key TTLs' },
      { name: 'RedisHighConnections', severity: 'warning', expr: 'redis_connected_clients > tenant:redis_connected_clients_threshold:value', desc: 'Connected clients above threshold', action: 'Check for connection leaks, use connection pooling' },
    ]
  },
  kafka: {
    label: 'Kafka',
    category: 'Messaging',
    description: 'Kafka monitoring: consumer lag, broker health, ISR shrink detection, and partition balance.',
    exporter: 'kafka_exporter / JMX exporter',
    recording: [
      { name: 'tenant:kafka_lag:max', expr: 'max by (topic, consumer_group) (kafka_consumergroup_lag)', desc: 'Max consumer lag per group' },
      { name: 'tenant:kafka_broker_active:count', expr: 'count(kafka_server_replicamanager_leadercount)', desc: 'Active broker count' },
    ],
    alerts: [
      { name: 'KafkaHighConsumerLag', severity: 'warning', expr: 'tenant:kafka_lag:max > tenant:kafka_lag_threshold:value', desc: 'Consumer lag exceeds threshold', action: 'Scale consumers, check for slow processing' },
      { name: 'KafkaHighConsumerLagCritical', severity: 'critical', expr: 'tenant:kafka_lag:max > tenant:kafka_lag_critical_threshold:value', desc: 'Critical consumer lag', action: 'Immediate attention: consumers may be down' },
      { name: 'KafkaBrokerDown', severity: 'critical', expr: 'tenant:kafka_broker_active:count < tenant:kafka_broker_active_threshold:value', desc: 'Fewer active brokers than expected', action: 'Check broker health, disk space, ZooKeeper' },
      { name: 'KafkaISRShrunk', severity: 'warning', expr: 'kafka_server_replicamanager_isrshrinkspersec > tenant:kafka_isr_shrank_threshold:value', desc: 'ISR shrink rate above threshold', action: 'Check replica broker health, network issues' },
      { name: 'KafkaUnderReplicatedPartitions', severity: 'critical', expr: 'kafka_server_replicamanager_underreplicatedpartitions > 0', desc: 'Under-replicated partitions detected', action: 'Investigate broker logs, disk I/O' },
    ]
  },
  mongodb: {
    label: 'MongoDB',
    category: 'Database',
    description: 'MongoDB monitoring: connections, replication lag, opcounters, memory and WiredTiger cache.',
    exporter: 'mongodb_exporter',
    recording: [
      { name: 'tenant:mongodb_connections:ratio', expr: 'mongodb_connections_current / tenant:mongodb_connections_threshold:value', desc: 'Current connections as ratio of threshold' },
      { name: 'tenant:mongodb_replication_lag:seconds', expr: 'mongodb_mongod_replset_member_replication_lag', desc: 'Replication lag in seconds' },
    ],
    alerts: [
      { name: 'MongoDBHighConnections', severity: 'warning', expr: 'tenant:mongodb_connections:ratio > 1', desc: 'Connections exceed threshold', action: 'Check connection pooling and client configuration' },
      { name: 'MongoDBReplicationLag', severity: 'warning', expr: 'tenant:mongodb_replication_lag:seconds > tenant:mongodb_replication_lag_threshold:value', desc: 'Replication lag above threshold', action: 'Check secondary node health and network' },
      { name: 'MongoDBHighMemory', severity: 'warning', expr: 'tenant:mongodb_memory:ratio > 1', desc: 'WiredTiger cache usage above threshold', action: 'Tune WiredTiger cache size, check working set' },
    ]
  },
  elasticsearch: {
    label: 'Elasticsearch',
    category: 'Database',
    description: 'Elasticsearch monitoring: cluster health, JVM heap, indexing throughput, search latency.',
    exporter: 'elasticsearch_exporter',
    recording: [
      { name: 'tenant:es_heap_usage:ratio', expr: 'elasticsearch_jvm_memory_used_bytes / elasticsearch_jvm_memory_max_bytes * 100 / tenant:es_heap_threshold:value', desc: 'JVM heap usage ratio' },
      { name: 'tenant:es_indexing_rate:rate5m', expr: 'rate(elasticsearch_indices_indexing_index_total[5m])', desc: 'Indexing rate (5m)' },
    ],
    alerts: [
      { name: 'ElasticsearchHighHeap', severity: 'warning', expr: 'tenant:es_heap_usage:ratio > 1', desc: 'JVM heap above threshold', action: 'Review heap size settings, check for memory leaks' },
      { name: 'ElasticsearchClusterRed', severity: 'critical', expr: 'elasticsearch_cluster_health_status{color="red"} == 1', desc: 'Cluster status is red', action: 'Check unassigned shards, node availability' },
      { name: 'ElasticsearchHighDisk', severity: 'warning', expr: 'tenant:es_disk_usage:ratio > 1', desc: 'Disk usage above threshold', action: 'Delete old indices, add nodes, adjust ILM policy' },
    ]
  },
  oracle: {
    label: 'Oracle',
    category: 'Database',
    description: 'Oracle DB monitoring: tablespace usage, session count, wait events, ASM disk groups.',
    exporter: 'oracledb_exporter',
    recording: [
      { name: 'tenant:oracle_sessions:ratio', expr: 'oracle_sessions_current / tenant:oracle_sessions_threshold:value', desc: 'Active sessions ratio' },
      { name: 'tenant:oracle_tablespace_usage:pct', expr: 'oracle_tablespace_bytes / oracle_tablespace_max_bytes * 100', desc: 'Tablespace usage percentage' },
    ],
    alerts: [
      { name: 'OracleHighSessions', severity: 'warning', expr: 'tenant:oracle_sessions:ratio > 1', desc: 'Sessions exceed threshold', action: 'Check for session leaks, review connection pooling' },
      { name: 'OracleTablespaceFull', severity: 'critical', expr: 'tenant:oracle_tablespace_usage:pct > tenant:oracle_tablespace_critical:value', desc: 'Tablespace nearing capacity', action: 'Extend tablespace, purge old data' },
    ]
  },
  db2: {
    label: 'DB2',
    category: 'Database',
    description: 'IBM DB2 monitoring: bufferpool hit ratio, connections, tablespace status, log usage.',
    exporter: 'db2_exporter',
    recording: [
      { name: 'tenant:db2_connections:ratio', expr: 'db2_connections_current / tenant:db2_connections_threshold:value', desc: 'Connections ratio' },
      { name: 'tenant:db2_bufferpool_hit:pct', expr: 'db2_bufferpool_data_hit_ratio', desc: 'Bufferpool hit ratio' },
    ],
    alerts: [
      { name: 'DB2HighConnections', severity: 'warning', expr: 'tenant:db2_connections:ratio > 1', desc: 'Connections above threshold', action: 'Review max_connections, check for connection leaks' },
      { name: 'DB2LowBufferpoolHit', severity: 'warning', expr: 'tenant:db2_bufferpool_hit:pct < tenant:db2_bufferpool_hit_threshold:value', desc: 'Bufferpool hit ratio below threshold', action: 'Increase bufferpool size, analyze access patterns' },
    ]
  },
  clickhouse: {
    label: 'ClickHouse',
    category: 'Database',
    description: 'ClickHouse monitoring: query performance, merge operations, memory usage, replication queue.',
    exporter: 'clickhouse_exporter',
    recording: [
      { name: 'tenant:clickhouse_queries:rate5m', expr: 'rate(clickhouse_queries_total[5m])', desc: 'Query rate (5m)' },
      { name: 'tenant:clickhouse_memory:ratio', expr: 'clickhouse_memory_usage / tenant:clickhouse_memory_threshold:value', desc: 'Memory usage ratio' },
    ],
    alerts: [
      { name: 'ClickHouseHighMemory', severity: 'warning', expr: 'tenant:clickhouse_memory:ratio > 1', desc: 'Memory usage above threshold', action: 'Review max_memory_usage, optimize queries' },
      { name: 'ClickHouseReplicationQueue', severity: 'warning', expr: 'clickhouse_replication_queue_size > tenant:clickhouse_replication_queue_threshold:value', desc: 'Replication queue growing', action: 'Check network between replicas, disk I/O' },
    ]
  },
  rabbitmq: {
    label: 'RabbitMQ',
    category: 'Messaging',
    description: 'RabbitMQ monitoring: queue depth, consumer count, message rates, node health.',
    exporter: 'rabbitmq_exporter',
    recording: [
      { name: 'tenant:rabbitmq_queue_depth:max', expr: 'max by(queue) (rabbitmq_queue_messages)', desc: 'Max queue depth across queues' },
      { name: 'tenant:rabbitmq_consumers:count', expr: 'sum(rabbitmq_queue_consumers)', desc: 'Total consumer count' },
    ],
    alerts: [
      { name: 'RabbitMQHighQueueDepth', severity: 'warning', expr: 'tenant:rabbitmq_queue_depth:max > tenant:rabbitmq_queue_depth_threshold:value', desc: 'Queue depth above threshold', action: 'Scale consumers, check for processing bottlenecks' },
      { name: 'RabbitMQNoConsumers', severity: 'critical', expr: 'tenant:rabbitmq_consumers:count == 0', desc: 'No active consumers', action: 'Check consumer processes, restart if needed' },
    ]
  },
  kubernetes: {
    label: 'Kubernetes',
    category: 'Infrastructure',
    description: 'Kubernetes monitoring: pod restarts, CPU throttling, OOMKill detection, node resource pressure.',
    exporter: 'kube-state-metrics + node_exporter',
    recording: [
      { name: 'tenant:k8s_pod_restarts:rate1h', expr: 'increase(kube_pod_container_status_restarts_total[1h])', desc: 'Pod restart count (1h)' },
      { name: 'tenant:k8s_cpu_throttle:ratio', expr: 'rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m]) * 100', desc: 'CPU throttle ratio (%)' },
    ],
    alerts: [
      { name: 'KubernetesPodCrashLoop', severity: 'warning', expr: 'tenant:k8s_pod_restarts:rate1h > tenant:k8s_pod_restart_threshold:value', desc: 'Pod restart rate above threshold', action: 'Check pod logs, review resource limits' },
      { name: 'KubernetesCPUThrottle', severity: 'warning', expr: 'tenant:k8s_cpu_throttle:ratio > tenant:k8s_cpu_throttle_threshold:value', desc: 'CPU throttle ratio above threshold', action: 'Increase CPU limits or optimize workload' },
      { name: 'KubernetesOOMKill', severity: 'critical', expr: 'increase(kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}[1h]) > 0', desc: 'OOMKill detected', action: 'Increase memory limits, investigate memory leaks' },
    ]
  },
  operational: {
    label: 'Operational',
    category: 'Platform',
    description: 'Cross-cutting operational rules: config drift detection, exporter health, cardinality monitoring.',
    exporter: 'threshold-exporter',
    recording: [
      { name: 'tenant:config_reload:status', expr: 'threshold_exporter_config_reload_success', desc: 'Config reload success status' },
      { name: 'tenant:cardinality:count', expr: 'count by(tenant) ({__name__=~"user_threshold_.*"})', desc: 'Per-tenant series cardinality' },
    ],
    alerts: [
      { name: 'ConfigReloadFailed', severity: 'warning', expr: 'tenant:config_reload:status == 0', desc: 'Config reload failed', action: 'Check config syntax, review YAML validation errors' },
      { name: 'HighCardinality', severity: 'warning', expr: 'tenant:cardinality:count > 500', desc: 'Per-tenant series count above 500', action: 'Review tenant config, reduce metric dimensions' },
    ]
  },
  platform: {
    label: 'Platform Health',
    category: 'Platform',
    description: 'Platform self-monitoring: threshold-exporter health, Prometheus/Alertmanager status, rule evaluation errors.',
    exporter: 'threshold-exporter + prometheus',
    recording: [
      { name: 'platform:exporter_up:count', expr: 'sum(up{job=~".*threshold.*"})', desc: 'Healthy exporter instance count' },
      { name: 'platform:rule_evaluation_failures:rate5m', expr: 'rate(prometheus_rule_evaluation_failures_total[5m])', desc: 'Rule evaluation failure rate' },
    ],
    alerts: [
      { name: 'ThresholdExporterDown', severity: 'critical', expr: 'platform:exporter_up:count < 1', desc: 'No healthy threshold-exporter instances', action: 'Check deployment, pod status, readiness probes' },
      { name: 'RuleEvaluationErrors', severity: 'warning', expr: 'platform:rule_evaluation_failures:rate5m > 0', desc: 'Rule evaluation failures detected', action: 'Check Prometheus logs, fix PromQL syntax errors' },
    ]
  },
};

// Read pack from URL hash
function getPackFromHash() {
  try {
    const params = new URLSearchParams(window.location.hash.slice(1));
    return params.get('pack') || null;
  } catch { return null; }
}

const SeverityBadge = ({ severity }) => {
  const colors = {
    warning: 'bg-amber-100 text-amber-800 border-amber-200',
    critical: 'bg-red-100 text-red-800 border-red-200',
  };
  return (
    <span className={`inline-block px-2 py-0.5 text-xs font-semibold rounded border ${colors[severity] || 'bg-gray-100 text-gray-700 border-gray-200'}`}>
      {severity}
    </span>
  );
};

export default function RulePackDetail() {
  const initialPack = getPackFromHash();
  const [selectedPack, setSelectedPack] = useState(initialPack && RULE_PACKS[initialPack] ? initialPack : 'mariadb');
  const [expandedAlerts, setExpandedAlerts] = useState(new Set());

  const pack = RULE_PACKS[selectedPack];

  const toggleAlert = (name) => {
    setExpandedAlerts(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  };

  const handlePackChange = (key) => {
    setSelectedPack(key);
    setExpandedAlerts(new Set());
    window.history.replaceState(null, '', '#pack=' + key);
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-5xl mx-auto">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('Rule Pack 詳情', 'Rule Pack Details')}</h1>
          <p className="text-slate-600">{t('深入查看每個 Rule Pack 的規則與 PromQL 表達式', 'Explore recording rules, alert rules, and PromQL expressions for each pack')}</p>
        </div>

        <div className="flex gap-3 flex-wrap mb-8">
          {Object.entries(RULE_PACKS).map(([key, p]) => (
            <button
              key={key}
              onClick={() => handlePackChange(key)}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                selectedPack === key
                  ? 'bg-blue-600 text-white shadow-md'
                  : 'bg-white text-slate-700 border border-slate-200 hover:border-blue-300'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>

        {/* Pack Header */}
        <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 mb-6">
          <div className="flex items-start justify-between flex-wrap gap-4">
            <div>
              <h2 className="text-2xl font-bold text-slate-900">{pack.label}</h2>
              <p className="text-slate-600 mt-1">{pack.description}</p>
            </div>
            <div className="flex gap-4 text-center">
              <div className="bg-blue-50 px-4 py-2 rounded-lg">
                <div className="text-xl font-bold text-blue-600">{pack.recording.length}</div>
                <div className="text-xs text-slate-500">{t('記錄規則', 'Recording')}</div>
              </div>
              <div className="bg-red-50 px-4 py-2 rounded-lg">
                <div className="text-xl font-bold text-red-600">{pack.alerts.length}</div>
                <div className="text-xs text-slate-500">{t('告警規則', 'Alerts')}</div>
              </div>
            </div>
          </div>
          <div className="mt-3 text-xs text-slate-500">
            {t('分類', 'Category')}: <span className="font-medium">{pack.category}</span>
            {' · '}{t('匯出器', 'Exporter')}: <code className="bg-slate-100 px-2 py-0.5 rounded">{pack.exporter}</code>
          </div>
        </div>

        {/* Recording Rules */}
        <div className="mb-6">
          <h3 className="text-lg font-semibold text-slate-900 mb-3">{t('記錄規則', 'Recording Rules')}</h3>
          <div className="space-y-2">
            {pack.recording.map(rule => (
              <div key={rule.name} className="bg-white rounded-lg border border-slate-200 p-4">
                <div className="font-mono text-sm font-semibold text-blue-700 mb-1">{rule.name}</div>
                <div className="text-xs text-slate-600 mb-2">{rule.desc}</div>
                <pre className="bg-slate-900 text-green-400 text-xs p-3 rounded overflow-x-auto font-mono">{rule.expr}</pre>
              </div>
            ))}
          </div>
        </div>

        {/* Alert Rules */}
        <div>
          <h3 className="text-lg font-semibold text-slate-900 mb-3">{t('告警規則', 'Alert Rules')}</h3>
          <div className="space-y-2">
            {pack.alerts.map(alert => (
              <div
                key={alert.name}
                className="bg-white rounded-lg border border-slate-200 overflow-hidden"
              >
                <button
                  onClick={() => toggleAlert(alert.name)}
                  className="w-full text-left p-4 flex items-center justify-between hover:bg-slate-50 transition-colors"
                >
                  <div className="flex items-center gap-3">
                    <SeverityBadge severity={alert.severity} />
                    <span className="font-mono text-sm font-semibold text-slate-900">{alert.name}</span>
                  </div>
                  <span className="text-slate-400 text-sm">{expandedAlerts.has(alert.name) ? '▲' : '▼'}</span>
                </button>
                {expandedAlerts.has(alert.name) && (
                  <div className="px-4 pb-4 border-t border-slate-100 pt-3 space-y-3">
                    <div className="text-sm text-slate-600">{alert.desc}</div>
                    <div>
                      <div className="text-xs font-semibold text-slate-500 mb-1">PromQL</div>
                      <pre className="bg-slate-900 text-green-400 text-xs p-3 rounded overflow-x-auto font-mono">{alert.expr}</pre>
                    </div>
                    <div>
                      <div className="text-xs font-semibold text-slate-500 mb-1">{t('建議操作', 'Suggested Action')}</div>
                      <div className="text-sm text-slate-700 bg-blue-50 p-3 rounded">{alert.action}</div>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
