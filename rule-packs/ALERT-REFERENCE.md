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

「維護期間」一欄取自每條告警的 `maintenance` label：`靜音` 表示租戶開啟 `_state_maintenance`（含 `recurring:` 週期窗）時這條不觸發，`照常觸發` 表示維護期間仍會通知。

---

## ClickHouse Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| ClickHouseHighQueryRate | warning | 靜音 | [tier] {{ $labels.tenant }}: ClickHouse query rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:clickhouse_queries:rate5m |
| ClickHouseHighConnections | warning | 靜音 | [tier] {{ $labels.tenant }}: ClickHouse connections exceeded  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:clickhouse_active_connections:max |
| ClickHouseHighConnectionsCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: ClickHouse connections at critical level  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:clickhouse_active_connections:max |
| ClickHouseHighPartCount | warning | 靜音 | [tier] {{ $labels.tenant }}: ClickHouse partition merge pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:clickhouse_max_part_count:max |
| ClickHouseReplicationLag | warning | 靜音 | [tier] {{ $labels.tenant }}: ClickHouse replication queue high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:clickhouse_replication_queue:max |
| ClickHouseHighMemoryUsage | warning | 靜音 | [tier] {{ $labels.tenant }}: ClickHouse memory usage high  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | tenant:clickhouse_memory_tracking:max |
| ClickHouseHighFailedQueryRate | warning | 靜音 | Failed query rate: {{ $value \| printf "%.1f" }} queries/sec | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:clickhouse_failed_queries:rate5m |

---

## DB2 Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| DB2DatabaseDown | critical | 照常觸發 | db2_up=0 for 15s on {{ $labels.instance }} | 立即檢查伺服器狀態、網路連線；查看系統日誌 | db2_up |
| DB2HighConnections | warning | 靜音 | [tier] {{ $labels.tenant }}: DB2 connection usage high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:db2_connections_active:max |
| DB2HighConnectionsCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: DB2 connections at critical level  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:db2_connections_active:max |
| DB2LowBufferpoolHitRatio | warning | 靜音 | [tier] {{ $labels.tenant }}: DB2 bufferpool hit ratio low  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_bufferpool_hit_ratio:min |
| DB2HighLogUsage | warning | 靜音 | [tier] {{ $labels.tenant }}: DB2 transaction log usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_log_usage_percent:max |
| DB2HighDeadlockRate | warning | 靜音 | [tier] {{ $labels.tenant }}: DB2 deadlock rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_deadlocks:rate5m |
| DB2TablespaceAlmostFull | warning | 靜音 | [tier] {{ $labels.tenant }}: DB2 tablespace nearing capacity  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_tablespace_used_percent:max |
| DB2HighLockWaitTime | warning | 靜音 | [tier] {{ $labels.tenant }}: DB2 lock wait time high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_lock_wait_time:rate5m |
| DB2HighLockWaitTimeCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: DB2 lock waits at critical level  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_lock_wait_time:rate5m |
| DB2HighSortOverflow | warning | 靜音 | [tier] {{ $labels.tenant }}: DB2 sort overflow ratio high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:db2_sort_overflow_ratio:avg |

---

## Elasticsearch Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| ElasticsearchClusterRed | critical | 照常觸發 | Cluster health is RED  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_cluster_health:status |
| ElasticsearchClusterYellow | warning | 靜音 | [tier] {{ $labels.tenant }}: ES cluster YELLOW  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_cluster_health:status |
| ElasticsearchHighHeapUsage | warning | 靜音 | [tier] {{ $labels.tenant }}: ES heap usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_heap_usage_percent:max |
| ElasticsearchHighHeapUsageCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: ES heap at critical level  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_heap_usage_percent:max |
| ElasticsearchHighDiskUsage | warning | 靜音 | [tier] {{ $labels.tenant }}: ES disk usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_disk_usage_percent:max |
| ElasticsearchHighDiskUsageCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: ES disk at critical level  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_disk_usage_percent:max |
| ElasticsearchHighSearchLatency | warning | 靜音 | [tier] {{ $labels.tenant }}: ES search latency elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_search_latency_ms:avg |
| ElasticsearchHighSearchLatencyCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: ES search latency at critical level  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_search_latency_ms:avg |
| ElasticsearchUnassignedShards | warning | 靜音 | [tier] {{ $labels.tenant }}: ES unassigned shards  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_unassigned_shards:count |
| ElasticsearchPendingTasks | warning | 靜音 | [tier] {{ $labels.tenant }}: ES pending cluster tasks  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_pending_tasks:max |
| ElasticsearchPendingTasksCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: ES master task backlog at critical level  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:es_pending_tasks:max |

---

## JVM Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| JVMHighGCPause | warning | 靜音 | [tier] {{ $labels.tenant }}: GC pause elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_gc_pause:rate5m |
| JVMHighGCPauseCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical GC pause  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_gc_pause:rate5m |
| JVMMemoryPressure | warning | 靜音 | [tier] {{ $labels.tenant }}: heap memory pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_memory_used:percent |
| JVMMemoryPressureCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical heap pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_memory_used:percent |
| JVMThreadPoolExhaustion | warning | 靜音 | [tier] {{ $labels.tenant }}: thread pool saturation  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_threads:current |
| JVMThreadPoolExhaustionCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical thread exhaustion  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_threads:current |
| JVMPerformanceDegraded | critical | 靜音 | [tier] {{ $labels.tenant }}: multi-signal JVM degradation  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:jvm_gc_pause:rate5m |

---

## Kafka Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| KafkaHighConsumerLag | warning | 靜音 | [tier] {{ $labels.tenant }}: consumer lag elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_consumer_lag:max |
| KafkaHighConsumerLagCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical consumer lag  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_consumer_lag:max |
| KafkaUnderReplicatedPartitions | warning | 靜音 | [tier] {{ $labels.tenant }}: under-replicated partitions  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_under_replicated_partitions:sum |
| KafkaUnderReplicatedPartitionsCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical under-replication  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_under_replicated_partitions:sum |
| KafkaLowBrokerCount | warning | 靜音 | [tier] {{ $labels.tenant }}: broker count below minimum  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:kafka_broker_count:max |

---

## Kubernetes Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| PodContainerHighCPU | warning | 靜音 | [tier] {{ $labels.tenant }}: container CPU pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:pod_container_high_cpu_warning:core |
| PodContainerHighCPUCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: container CPU CRITICAL  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:pod_container_high_cpu_critical:core |
| PodContainerHighMemory | warning | 靜音 | [tier] {{ $labels.tenant }}: container memory pressure  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | rule_pack_kubernetes:pod_container_high_memory_warning:core |
| PodContainerHighMemoryCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: container memory CRITICAL  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | rule_pack_kubernetes:pod_container_high_memory_critical:core |
| PodContainerCPUThrottled | warning | 靜音 | [tier] {{ $labels.tenant }}: sustained CFS throttling  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:pod_cpu_throttled_warning:core |
| PodContainerCPUThrottledCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: CFS throttling CRITICAL  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:pod_cpu_throttled_critical:core |
| ContainerOOMKilled | warning | 靜音 | [tier] {{ $labels.tenant }}: kernel OOM-killed a container in the last hour  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:container_oom_killed_warning:core |
| ContainerOOMKilledCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: >=3 OOM kills on one container within 1h  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:container_oom_killed_critical:core |
| ContainerCrashLoop | critical | 靜音 | [tier] {{ $labels.tenant }}: crash loop detected  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:container_waiting_reason:count |
| ContainerImagePullFailure | warning | 靜音 | [tier] {{ $labels.tenant }}: image pull failing  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:container_waiting_reason:count |
| VersionAwareThresholdInert | warning | 照常觸發 | {{ $value \| printf "%.0f" }} version-specific container CPU threshold(s) declared and tenant pods ar | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | user_threshold |
| CustomRecipeDiskInert | warning | 靜音 | [SRE] {{ $labels.tenant }} disk recipe inert  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | user_threshold |
| NodeNotReady | warning | 靜音 | [tier] {{ $labels.tenant }}: node {{ $labels.node }} NotReady  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:node_not_ready:core |
| TenantHAReplicasDegraded | warning | 靜音 | [tier] {{ $labels.tenant }}: ready replicas < desired on an HA set (≥2)  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rule_pack_kubernetes:ha_replicas_degraded:core |

---

## liveness

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| TenantExporterAbsent | critical | 照常觸發 | No healthy up{job="tenant-exporters"}==1 target for tenant {{ $labels.tenant }} (db_type={{ $labels. | 確認相關元件已啟動、配置正確；檢查元件日誌 | tenant_expected_exporter |
| TenantExporterReplicaDown | warning | 靜音 | Target {{ $labels.instance }}{{ with $labels.pod }} (pod {{ . }}){{ end }} of tenant {{ $labels.tena | 立即檢查伺服器狀態、網路連線；查看系統日誌 | up |

---

## MariaDB Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| MariaDBDown | warning | 靜音 | mysql_up=0 on {{ $labels.instance }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mysql_up |
| MariaDBClusterDown | critical | 照常觸發 | No mysqld reports up=1 for tenant {{ $labels.tenant }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mysql_up |
| MariaDBNoPrimary | critical | 照常觸發 | All instances are read_only for tenant {{ $labels.tenant }}  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | mysql_global_variables_read_only |
| MariaDBSemiSyncDegraded | warning | 靜音 | Tenant {{ $labels.tenant }} has semi-sync enabled but it fell back to ASYNC (rpl_semi_sync_master_st | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | mysql_global_variables_rpl_semi_sync_master_enabled |
| MariaDBSemiSyncReplicasGone | critical | 照常觸發 | Tenant {{ $labels.tenant }} has semi-sync enabled but ZERO semi-sync replicas connected (rpl_semi_sy | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | mysql_global_variables_rpl_semi_sync_master_enabled |
| MariaDBReplicaIOThreadDown | critical | 照常觸發 | Replica IO thread is stopped (Slave_IO_Running=No) for tenant {{ $labels.tenant }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mysql_slave_status_slave_io_running |
| MariaDBReplicaSQLThreadDown | critical | 照常觸發 | Replica SQL thread is stopped (Slave_SQL_Running=No) for tenant {{ $labels.tenant }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mysql_slave_status_slave_sql_running |
| MariaDBReplicationLag | warning | 靜音 | [tier] {{ $labels.tenant }}: replication lag threshold breached  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_replication_lag:max |
| MariaDBReplicationLagCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: CRITICAL replication lag  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_replication_lag:max |
| MariaDBHighConnections | warning | 靜音 | [tier] {{ $labels.tenant }}: connection threshold breached  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:mysql_connection_usage:percent |
| MariaDBHighConnectionsCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical connection saturation  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:mysql_connection_usage:percent |
| MariaDBHighThreadsRunning | warning | 靜音 | [tier] {{ $labels.tenant }}: running-thread saturation (threads_running, NOT host CPU%) breached  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_threads_running:avg1m |
| MariaDBHighThreadsRunningCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: CRITICAL running-thread saturation (threads_running, NOT host CPU%)  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_threads_running:avg1m |
| MariaDBSystemBottleneck | critical | 靜音 | [tier] {{ $labels.tenant }}: running-thread saturation + connections both exceeded  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_connection_usage:percent |
| MariaDBRecentRestart | info | 靜音 | Uptime is only {{ $value }}s (< 5 min) | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | mysql_global_status_uptime |
| MariaDBHighSlowQueries | warning | 靜音 | [tier] {{ $labels.tenant }}: slow query rate elevated  | 檢查慢查詢日誌，找出優化候選；考慮調整相關參數 | tenant:mysql_slow_queries:rate5m |
| MariaDBHighAbortedConnections | warning | 靜音 | [tier] {{ $labels.tenant }}: aborted connection rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mysql_aborted_connections:rate5m |

---

## MongoDB Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| MongoDBDown | warning | 靜音 | mongodb_up=0 on {{ $labels.instance }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mongodb_up |
| MongoDBClusterDown | critical | 照常觸發 | No mongod reports up=1 for tenant {{ $labels.tenant }}  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mongodb_up |
| MongoDBNoPrimary | critical | 照常觸發 | Replica-set members are reachable but none is PRIMARY for tenant {{ $labels.tenant }}  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | mongodb_mongod_replset_member_state |
| MongoDBHighConnections | warning | 靜音 | [tier] {{ $labels.tenant }}: MongoDB connection threshold breached  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:mongodb_connections_current:max |
| MongoDBHighConnectionsCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: MongoDB connections at critical level  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:mongodb_connections_current:max |
| MongoDBReplicationLag | warning | 靜音 | [tier] {{ $labels.tenant }}: MongoDB replication lag  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mongodb_replication_lag:max |
| MongoDBReplicationLagCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: MongoDB secondary far behind primary  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mongodb_replication_lag:max |
| MongoDBHighOperations | warning | 靜音 | [tier] {{ $labels.tenant }}: MongoDB operation rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mongodb_opcounters:rate5m |
| MongoDBHighPageFaults | warning | 靜音 | [tier] {{ $labels.tenant }}: MongoDB page fault rate high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mongodb_page_faults:rate5m |
| MongoDBConnectionSaturation | warning | 靜音 | [tier] {{ $labels.tenant }}: MongoDB connection pool near saturation  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:mongodb_connection_usage:ratio |

---

## Nginx Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| NginxHighConnections | warning | 靜音 | [tier] {{ $labels.tenant }}: Nginx connection threshold exceeded  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:nginx_connections_active:max |
| NginxHighConnectionsCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical Nginx connection saturation  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:nginx_connections_active:max |
| NginxRequestRateSpike | warning | 靜音 | [tier] {{ $labels.tenant }}: request rate spike  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:nginx_requests:rate5m |
| NginxRequestRateSpikeCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical request rate  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:nginx_requests:rate5m |
| NginxConnectionBacklog | warning | 靜音 | [tier] {{ $labels.tenant }}: connection backlog building  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:nginx_connections_waiting:max |
| NginxConnectionBacklogCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical connection backlog  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:nginx_connections_waiting:max |

---

## Operational Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| TenantSilentWarning | none | 照常觸發 | Warning alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppressed | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | user_silent_mode |
| TenantSilentCritical | none | 照常觸發 | Critical alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppresse | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | user_silent_mode |
| TenantSeverityDedupEnabled | none | 照常觸發 | Warning notifications for {{ $labels.tenant }} will be suppressed when critical fires for the same m | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | user_severity_dedup |
| TenantConfigEvent | warning | 照常觸發 | Timed config for tenant {{ $labels.tenant }} has expired and auto-deactivated. Event: {{ $labels.eve | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | da_config_event |

---

## Oracle Database Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| OracleDatabaseDown | critical | 照常觸發 | oracledb_up=0 for 15s on {{ $labels.instance }} | 立即檢查伺服器狀態、網路連線；查看系統日誌 | oracledb_up |
| OracleHighActiveSessions | warning | 靜音 | [tier] {{ $labels.tenant }}: Oracle active sessions elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_sessions_active:max |
| OracleHighActiveSessionsCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: Oracle active sessions at critical level  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_sessions_active:max |
| OracleTablespaceAlmostFull | warning | 靜音 | [tier] {{ $labels.tenant }}: Oracle tablespace nearing capacity  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_tablespace_used_percent:max |
| OracleTablespaceAlmostFullCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: Oracle tablespace almost out of space  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_tablespace_used_percent:max |
| OracleHighWaitTime | warning | 靜音 | [tier] {{ $labels.tenant }}: Oracle wait time elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_wait_time:rate5m |
| OracleHighProcessCount | warning | 靜音 | [tier] {{ $labels.tenant }}: Oracle process count high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_process_count:max |
| OracleHighPGAUsage | warning | 靜音 | [tier] {{ $labels.tenant }}: Oracle PGA usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_pga_allocated_bytes:max |
| OracleHighSessionUtilization | warning | 靜音 | [tier] {{ $labels.tenant }}: Oracle session limit approaching  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:oracle_session_utilization:ratio |

---

## PostgreSQL Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| PostgreSQLDown | critical | 照常觸發 | pg_up=0 for 15s on {{ $labels.instance }} | 立即檢查伺服器狀態、網路連線；查看系統日誌 | pg_up |
| PostgreSQLHighConnections | warning | 靜音 | [tier] {{ $labels.tenant }}: connection usage high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:pg_connection_usage:ratio |
| PostgreSQLHighConnectionsCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical connection saturation  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:pg_connection_usage:ratio |
| PostgreSQLHighReplicationLag | warning | 靜音 | [tier] {{ $labels.tenant }}: replication lag elevated  | 檢查複寫狀態、網路連線；檢查複寫隊列堆積情況 | tenant:pg_replication_lag:max |
| PostgreSQLHighReplicationLagCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical replication lag  | 檢查複寫狀態、網路連線；檢查複寫隊列堆積情況 | tenant:pg_replication_lag:max |
| PostgreSQLHighDeadlocks | warning | 靜音 | [tier] {{ $labels.tenant }}: deadlocks detected  | 分析死鎖查詢日誌、調整應用邏輯減少衝突；考慮增加鎖定超時時間 | tenant:pg_deadlocks:rate5m |
| PostgreSQLHighRollbackRatio | warning | 靜音 | [tier] {{ $labels.tenant }}: high rollback ratio  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:pg_rollback_ratio:rate5m |
| PostgreSQLRecentRestart | info | 靜音 | PostgreSQL uptime is only {{ $value \| printf "%.0f" }}s (< 5 min) | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | pg_postmaster_start_time_seconds |

---

## RabbitMQ Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| RabbitMQHighQueueDepth | warning | 靜音 | [tier] {{ $labels.tenant }}: queue depth threshold exceeded  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:rabbitmq_queue_messages:max |
| RabbitMQHighQueueDepthCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical queue depth  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:rabbitmq_queue_messages:max |
| RabbitMQHighMemory | warning | 靜音 | [tier] {{ $labels.tenant }}: RabbitMQ memory usage high  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | tenant:rabbitmq_node_mem_percent:ratio |
| RabbitMQHighMemoryCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical RabbitMQ memory  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | tenant:rabbitmq_node_mem_percent:ratio |
| RabbitMQHighConnections | warning | 靜音 | [tier] {{ $labels.tenant }}: RabbitMQ connection count high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:rabbitmq_connections:max |
| RabbitMQHighConnectionsCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical connection count  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:rabbitmq_connections:max |
| RabbitMQLowConsumers | warning | 靜音 | [tier] {{ $labels.tenant }}: RabbitMQ consumer count low  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:rabbitmq_consumers:max |
| RabbitMQHighUnackedMessages | warning | 靜音 | [tier] {{ $labels.tenant }}: unacked messages piling up  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:rabbitmq_unacked_messages:max |
| RabbitMQHighUnackedMessagesCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: critical unacked backlog  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:rabbitmq_unacked_messages:max |

---

## Redis Rule Pack

| 告警名稱 | 嚴重度 | 維護期間 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|---|
| RedisDown | critical | 照常觸發 |  | 立即檢查伺服器狀態、網路連線；查看系統日誌 | redis_up |
| RedisHighMemory | warning | 靜音 | [tier] {{ $labels.tenant }}: Redis memory threshold breached  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | tenant:redis_memory_used_bytes:max |
| RedisHighMemoryCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: Redis memory at critical level  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | tenant:redis_memory_used_bytes:max |
| RedisHighConnections | warning | 靜音 | [tier] {{ $labels.tenant }}: Redis connection count high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:redis_connected_clients:max |
| RedisHighConnectionsCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: Redis connections at critical level  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | tenant:redis_connected_clients:max |
| RedisHighKeyEvictions | warning | 靜音 | [tier] {{ $labels.tenant }}: key eviction rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:redis_evicted_keys:rate5m |
| RedisReplicationLag | warning | 靜音 | [tier] {{ $labels.tenant }}: Redis replication lag  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:redis_replication_lag:max |
| RedisReplicationLagCritical | critical | 靜音 | [tier] {{ $labels.tenant }}: Redis replica far behind  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:redis_replication_lag:max |
| RedisLowHitRatio | warning | 靜音 | [tier] {{ $labels.tenant }}: low cache hit ratio  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | tenant:redis_keyspace_hit_ratio:avg |

---
