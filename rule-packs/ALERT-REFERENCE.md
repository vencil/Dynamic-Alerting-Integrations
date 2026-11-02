---
title: "Rule Pack 告警參考指南 (Alert Reference Guide)"
tags: [alerts, reference, rule-packs]
audience: [tenant, sre]
version: v1.12.0
lang: zh
---
# Rule Pack 告警參考指南 (Alert Reference Guide)

> **Language / 語言：** **中文 (Current)** | [English](./ALERT-REFERENCE.en.md)

本文件為租戶提供各 Rule Pack 中所有告警的統一參考，包括告警含義、觸發條件和建議動作。

**注意**: 本指南僅涵蓋**使用者導向的閾值告警** (threshold alerts)。Operational Rule Pack 的 sentinel 告警為平台內部控制機制，不需要租戶操作。

---

## ClickHouse Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| ClickHouseDown | critical |  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | up |
| ClickHouseHighQueryRate | warning | [tier] {{ $labels.tenant }}: ClickHouse query rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:clickhouse_queries:rate5m |
| ClickHouseHighConnections | warning | [tier] {{ $labels.tenant }}: ClickHouse connections exceeded  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:clickhouse_active_connections:max |
| ClickHouseHighPartCount | warning | [tier] {{ $labels.tenant }}: ClickHouse partition merge pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:clickhouse_max_part_count:max |
| ClickHouseReplicationLag | warning | [tier] {{ $labels.tenant }}: ClickHouse replication queue high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:clickhouse_replication_queue:max |
| ClickHouseHighMemoryUsage | warning | [tier] {{ $labels.tenant }}: ClickHouse memory usage high  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | tenant:clickhouse_memory_tracking:max |
| ClickHouseHighFailedQueryRate | warning | Failed query rate: {{ $value \| printf "%.1f" }} queries/sec | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:clickhouse_failed_queries:rate5m |

---

## DB2 Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| DB2DatabaseDown | critical | db2_up=0 for 15s on {{ $labels.instance }} | 立即檢查伺服器狀態、網路連線；查看系統日誌 | db2_up |
| DB2HighConnections | warning | [tier] {{ $labels.tenant }}: DB2 connection usage high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:db2_connections_active:max |
| DB2LowBufferpoolHitRatio | warning | [tier] {{ $labels.tenant }}: DB2 bufferpool hit ratio low  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_bufferpool_hit_ratio:min |
| DB2HighLogUsage | warning | [tier] {{ $labels.tenant }}: DB2 transaction log usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_log_usage_percent:max |
| DB2HighDeadlockRate | warning | [tier] {{ $labels.tenant }}: DB2 deadlock rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_deadlocks:rate5m |
| DB2TablespaceAlmostFull | warning | [tier] {{ $labels.tenant }}: DB2 tablespace nearing capacity  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_tablespace_used_percent:max |
| DB2HighLockWaitTime | warning | [tier] {{ $labels.tenant }}: DB2 lock wait time high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_lock_wait_time:rate5m |
| DB2HighSortOverflow | warning | [tier] {{ $labels.tenant }}: DB2 sort overflow ratio high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_sort_overflow_ratio:avg |

---

## Elasticsearch Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| ElasticsearchClusterRed | critical | Cluster health is RED  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_cluster_health:status |
| ElasticsearchClusterYellow | warning | [tier] {{ $labels.tenant }}: ES cluster YELLOW  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_cluster_health:status |
| ElasticsearchHighHeapUsage | warning | [tier] {{ $labels.tenant }}: ES heap usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_heap_usage_percent:max |
| ElasticsearchHighDiskUsage | warning | [tier] {{ $labels.tenant }}: ES disk usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_disk_usage_percent:max |
| ElasticsearchHighSearchLatency | warning | [tier] {{ $labels.tenant }}: ES search latency elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_search_latency_ms:avg |
| ElasticsearchUnassignedShards | warning | [tier] {{ $labels.tenant }}: ES unassigned shards  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_unassigned_shards:count |
| ElasticsearchPendingTasks | warning | [tier] {{ $labels.tenant }}: ES pending cluster tasks  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_pending_tasks:max |

---

## JVM Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| JVMHighGCPause | warning | [tier] {{ $labels.tenant }}: GC pause elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_gc_pause:rate5m |
| JVMHighGCPauseCritical | critical | [tier] {{ $labels.tenant }}: critical GC pause  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_gc_pause:rate5m |
| JVMMemoryPressure | warning | [tier] {{ $labels.tenant }}: heap memory pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_memory_used:percent |
| JVMMemoryPressureCritical | critical | [tier] {{ $labels.tenant }}: critical heap pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_memory_used:percent |
| JVMThreadPoolExhaustion | warning | [tier] {{ $labels.tenant }}: thread pool saturation  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_threads:current |
| JVMThreadPoolExhaustionCritical | critical | [tier] {{ $labels.tenant }}: critical thread exhaustion  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_threads:current |
| JVMPerformanceDegraded | critical | [tier] {{ $labels.tenant }}: multi-signal JVM degradation  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_gc_pause:rate5m |

---

## Kafka Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| KafkaExporterAbsent | critical | No kafka_brokers metric found for 30s | 確認相關元件已啟動、配置正確；檢查元件日誌 | kafka_brokers |
| KafkaHighConsumerLag | warning | [tier] {{ $labels.tenant }}: consumer lag elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_consumer_lag:max |
| KafkaHighConsumerLagCritical | critical | [tier] {{ $labels.tenant }}: critical consumer lag  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_consumer_lag:max |
| KafkaUnderReplicatedPartitions | warning | [tier] {{ $labels.tenant }}: under-replicated partitions  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_under_replicated_partitions:max |
| KafkaUnderReplicatedPartitionsCritical | critical | [tier] {{ $labels.tenant }}: critical under-replication  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_under_replicated_partitions:max |
| KafkaNoActiveController | critical | [tier] {{ $labels.tenant }}: no active Kafka controller  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_active_controllers:max |
| KafkaLowBrokerCount | warning | [tier] {{ $labels.tenant }}: broker count below minimum  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_broker_count:max |
| KafkaHighRequestRate | warning | [tier] {{ $labels.tenant }}: message rate threshold exceeded  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_request_rate:sum |
| KafkaHighRequestRateCritical | critical | [tier] {{ $labels.tenant }}: critical message rate  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_request_rate:sum |

---

## Kubernetes Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| PodContainerHighCPU | warning | [tier] {{ $labels.tenant }}: container CPU pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:pod_container_high_cpu_warning:core |
| PodContainerHighCPUCritical | critical | [tier] {{ $labels.tenant }}: container CPU CRITICAL  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:pod_container_high_cpu_critical:core |
| PodContainerHighMemory | warning | [tier] {{ $labels.tenant }}: container memory pressure  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | rule_pack_kubernetes:pod_container_high_memory_warning:core |
| PodContainerHighMemoryCritical | critical | [tier] {{ $labels.tenant }}: container memory CRITICAL  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | rule_pack_kubernetes:pod_container_high_memory_critical:core |
| PodContainerCPUThrottled | warning | [tier] {{ $labels.tenant }}: sustained CFS throttling  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:pod_cpu_throttled_warning:core |
| PodContainerCPUThrottledCritical | critical | [tier] {{ $labels.tenant }}: CFS throttling CRITICAL  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:pod_cpu_throttled_critical:core |
| ContainerOOMKilled | warning | [tier] {{ $labels.tenant }}: kernel OOM-killed a container in the last hour  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:container_oom_killed_warning:core |
| ContainerOOMKilledCritical | critical | [tier] {{ $labels.tenant }}: >=3 OOM kills on one container within 1h  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:container_oom_killed_critical:core |
| ContainerCrashLoop | critical | [tier] {{ $labels.tenant }}: crash loop detected  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:container_waiting_reason:count |
| ContainerImagePullFailure | warning | [tier] {{ $labels.tenant }}: image pull failing  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:container_waiting_reason:count |
| VersionAwareThresholdInert | warning | {{ $value \| printf "%.0f" }} version-specific container CPU threshold(s) declared and tenant pods ar | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | user_threshold |
| CustomRecipeDiskInert | warning | [SRE] {{ $labels.tenant }} disk recipe inert  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | user_threshold |
| NodeNotReady | warning | [tier] {{ $labels.tenant }}: node {{ $labels.node }} NotReady  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:node_not_ready:core |
| TenantHAReplicasDegraded | warning | [tier] {{ $labels.tenant }}: ready replicas < desired on an HA set (≥2)  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:ha_replicas_degraded:core |

---

## liveness

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| TenantExporterAbsent | critical | No healthy up{job="tenant-exporters"}==1 target for tenant {{ $labels.tenant }} (db_type={{ $labels. | 確認相關元件已啟動、配置正確；檢查元件日誌 | tenant_expected_exporter |

---

## MariaDB Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| MariaDBDown | warning | mysql_up=0 on {{ $labels.instance }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mysql_up |
| MariaDBClusterDown | critical | No mysqld reports up=1 for tenant {{ $labels.tenant }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mysql_up |
| MariaDBNoPrimary | critical | All instances are read_only for tenant {{ $labels.tenant }}  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | mysql_global_variables_read_only |
| MariaDBSemiSyncDegraded | warning | Tenant {{ $labels.tenant }} has semi-sync enabled but it fell back to ASYNC (rpl_semi_sync_master_st | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | mysql_global_variables_rpl_semi_sync_master_enabled |
| MariaDBSemiSyncReplicasGone | critical | Tenant {{ $labels.tenant }} has semi-sync enabled but ZERO semi-sync replicas connected (rpl_semi_sy | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | mysql_global_variables_rpl_semi_sync_master_enabled |
| MariaDBReplicaIOThreadDown | critical | Replica IO thread is stopped (Slave_IO_Running=No) for tenant {{ $labels.tenant }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mysql_slave_status_slave_io_running |
| MariaDBReplicaSQLThreadDown | critical | Replica SQL thread is stopped (Slave_SQL_Running=No) for tenant {{ $labels.tenant }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mysql_slave_status_slave_sql_running |
| MariaDBReplicationLag | warning | [tier] {{ $labels.tenant }}: replication lag threshold breached  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_replication_lag:max |
| MariaDBReplicationLagCritical | critical | [tier] {{ $labels.tenant }}: CRITICAL replication lag  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_replication_lag:max |
| MariaDBExporterAbsent | critical | No mysql_up metric found for 30s | 確認相關元件已啟動、配置正確；檢查元件日誌 | mysql_up |
| MariaDBHighConnections | warning | [tier] {{ $labels.tenant }}: connection threshold breached  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:mysql_threads_connected:max |
| MariaDBHighConnectionsCritical | critical | [tier] {{ $labels.tenant }}: critical connection saturation  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:mysql_threads_connected:max |
| MariaDBHighThreadsRunning | warning | [tier] {{ $labels.tenant }}: running-thread saturation (threads_running, NOT host CPU%) breached  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_threads_running:avg1m |
| MariaDBHighThreadsRunningCritical | critical | [tier] {{ $labels.tenant }}: CRITICAL running-thread saturation (threads_running, NOT host CPU%)  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_threads_running:avg1m |
| MariaDBSystemBottleneck | critical | [tier] {{ $labels.tenant }}: running-thread saturation + connections both exceeded  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_threads_connected:max |
| MariaDBRecentRestart | info | Uptime is only {{ $value }}s (< 5 min) | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | mysql_global_status_uptime |
| MariaDBHighSlowQueries | warning | [tier] {{ $labels.tenant }}: slow query rate elevated  | 檢查慢查詢日誌，找出優化候選；考慮調整相關參數 | tenant:mysql_slow_queries:rate5m |
| MariaDBHighAbortedConnections | warning | [tier] {{ $labels.tenant }}: aborted connection rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_aborted_connections:rate5m |

---

## MongoDB Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| MongoDBDown | warning | mongodb_up=0 on {{ $labels.instance }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mongodb_up |
| MongoDBClusterDown | critical | No mongod reports up=1 for tenant {{ $labels.tenant }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mongodb_up |
| MongoDBNoPrimary | critical | Replica-set members are reachable but none is PRIMARY for tenant {{ $labels.tenant }}  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | mongodb_mongod_replset_member_state |
| MongoDBHighConnections | warning | [tier] {{ $labels.tenant }}: MongoDB connection threshold breached  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:mongodb_connections_current:max |
| MongoDBReplicationLag | warning | [tier] {{ $labels.tenant }}: MongoDB replication lag  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mongodb_replication_lag:max |
| MongoDBHighOperations | warning | [tier] {{ $labels.tenant }}: MongoDB operation rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mongodb_opcounters:rate5m |
| MongoDBHighPageFaults | warning | [tier] {{ $labels.tenant }}: MongoDB page fault rate high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mongodb_page_faults:rate5m |
| MongoDBConnectionSaturation | warning | [tier] {{ $labels.tenant }}: MongoDB connection pool near saturation  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mongodb_connection_usage:ratio |

---

## Nginx Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| NginxHighConnections | warning | [tier] {{ $labels.tenant }}: Nginx connection threshold exceeded  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:nginx_connections_active:max |
| NginxHighConnectionsCritical | critical | [tier] {{ $labels.tenant }}: critical Nginx connection saturation  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:nginx_connections_active:max |
| NginxRequestRateSpike | warning | [tier] {{ $labels.tenant }}: request rate spike  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:nginx_requests:rate5m |
| NginxRequestRateSpikeCritical | critical | [tier] {{ $labels.tenant }}: critical request rate  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:nginx_requests:rate5m |
| NginxConnectionBacklog | warning | [tier] {{ $labels.tenant }}: connection backlog building  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:nginx_connections_waiting:max |
| NginxConnectionBacklogCritical | critical | [tier] {{ $labels.tenant }}: critical connection backlog  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:nginx_connections_waiting:max |

---

## Operational Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| TenantSilentWarning | none | Warning alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppressed | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | user_silent_mode |
| TenantSilentCritical | none | Critical alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppresse | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | user_silent_mode |
| TenantSeverityDedupEnabled | none | Warning notifications for {{ $labels.tenant }} will be suppressed when critical fires for the same m | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | user_severity_dedup |
| TenantConfigEvent | warning | Timed config for tenant {{ $labels.tenant }} has expired and auto-deactivated. Event: {{ $labels.eve | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | da_config_event |

---

## Oracle Database Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| OracleDatabaseDown | critical | oracledb_up=0 for 15s on {{ $labels.instance }} | 立即檢查伺服器狀態、網路連線；查看系統日誌 | oracledb_up |
| OracleHighActiveSessions | warning | [tier] {{ $labels.tenant }}: Oracle active sessions elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_sessions_active:max |
| OracleTablespaceAlmostFull | warning | [tier] {{ $labels.tenant }}: Oracle tablespace nearing capacity  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_tablespace_used_percent:max |
| OracleHighWaitTime | warning | [tier] {{ $labels.tenant }}: Oracle wait time elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_wait_time:rate5m |
| OracleHighProcessCount | warning | [tier] {{ $labels.tenant }}: Oracle process count high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_process_count:max |
| OracleHighPGAUsage | warning | [tier] {{ $labels.tenant }}: Oracle PGA usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_pga_allocated_bytes:max |
| OracleHighSessionUtilization | warning | [tier] {{ $labels.tenant }}: Oracle session limit approaching  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_session_utilization:ratio |

---

## PostgreSQL Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| PostgreSQLDown | critical | pg_up=0 for 15s on {{ $labels.instance }} | 立即檢查伺服器狀態、網路連線；查看系統日誌 | pg_up |
| PostgreSQLExporterAbsent | critical | No pg_up metric found for 30s | 確認相關元件已啟動、配置正確；檢查元件日誌 | pg_up |
| PostgreSQLHighConnections | warning | [tier] {{ $labels.tenant }}: connection usage high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:pg_connection_usage:ratio |
| PostgreSQLHighConnectionsCritical | critical | [tier] {{ $labels.tenant }}: critical connection saturation  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:pg_connection_usage:ratio |
| PostgreSQLHighReplicationLag | warning | [tier] {{ $labels.tenant }}: replication lag elevated  | 檢查複寫狀態、網路連線；檢查複寫隊列堆積情況 | tenant:pg_replication_lag:max |
| PostgreSQLHighReplicationLagCritical | critical | [tier] {{ $labels.tenant }}: critical replication lag  | 檢查複寫狀態、網路連線；檢查複寫隊列堆積情況 | tenant:pg_replication_lag:max |
| PostgreSQLHighDeadlocks | warning | [tier] {{ $labels.tenant }}: deadlocks detected  | 分析死鎖查詢日誌、調整應用邏輯減少衝突；考慮增加鎖定超時時間 | tenant:pg_deadlocks:rate5m |
| PostgreSQLHighRollbackRatio | warning | [tier] {{ $labels.tenant }}: high rollback ratio  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:pg_rollback_ratio:rate5m |
| PostgreSQLRecentRestart | info | PostgreSQL uptime is only {{ $value \| printf "%.0f" }}s (< 5 min) | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | pg_postmaster_start_time_seconds |

---

## RabbitMQ Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| RabbitMQExporterAbsent | critical | No rabbitmq_identity_info metric found for 30s | 確認相關元件已啟動、配置正確；檢查元件日誌 | rabbitmq_identity_info |
| RabbitMQHighQueueDepth | warning | [tier] {{ $labels.tenant }}: queue depth threshold exceeded  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:rabbitmq_queue_messages:max |
| RabbitMQHighQueueDepthCritical | critical | [tier] {{ $labels.tenant }}: critical queue depth  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:rabbitmq_queue_messages:max |
| RabbitMQHighMemory | warning | [tier] {{ $labels.tenant }}: RabbitMQ memory usage high  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | tenant:rabbitmq_node_mem_percent:ratio |
| RabbitMQHighMemoryCritical | critical | [tier] {{ $labels.tenant }}: critical RabbitMQ memory  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | tenant:rabbitmq_node_mem_percent:ratio |
| RabbitMQHighConnections | warning | [tier] {{ $labels.tenant }}: RabbitMQ connection count high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:rabbitmq_connections:max |
| RabbitMQLowConsumers | warning | [tier] {{ $labels.tenant }}: RabbitMQ consumer count low  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:rabbitmq_consumers:max |
| RabbitMQHighUnackedMessages | warning | [tier] {{ $labels.tenant }}: unacked messages piling up  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:rabbitmq_unacked_messages:max |

---

## Redis Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| RedisDown | critical |  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | redis_up |
| RedisHighMemory | warning | [tier] {{ $labels.tenant }}: Redis memory threshold breached  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | tenant:redis_memory_used_bytes:max |
| RedisHighConnections | warning | [tier] {{ $labels.tenant }}: Redis connection count high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:redis_connected_clients:max |
| RedisHighKeyEvictions | warning | [tier] {{ $labels.tenant }}: key eviction rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:redis_evicted_keys:rate5m |
| RedisReplicationLag | warning | [tier] {{ $labels.tenant }}: Redis replication lag  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:redis_replication_lag:max |
| RedisLowHitRatio | warning | [tier] {{ $labels.tenant }}: low cache hit ratio  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:redis_keyspace_hit_ratio:avg |

---
