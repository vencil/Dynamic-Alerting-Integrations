---
title: "Rule Pack Alert Reference Guide"
tags: [alerts, reference, rule-packs]
audience: [tenant, sre]
version: v1.12.0
lang: en
---
# Rule Pack Alert Reference Guide

> **Language / 語言：** **English (Current)** | [中文](./ALERT-REFERENCE.md)

This document provides tenants with a unified reference for all alerts across Rule Packs, including alert meanings, trigger conditions, and recommended actions.

**Note**: This guide covers only **user-facing threshold alerts**. Sentinel alerts in the Operational Rule Pack are platform-internal control mechanisms and do not require tenant action.

The *In maintenance* column is each alert's `maintenance` label: `muted` means the rule does not fire while the tenant has `_state_maintenance` on (a `recurring:` window included); `fires` means it still notifies during maintenance.

---

## ClickHouse Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| ClickHouseHighQueryRate | warning | muted | [tier] {{ $labels.tenant }}: ClickHouse query rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:clickhouse_queries:rate5m |
| ClickHouseHighConnections | warning | muted | [tier] {{ $labels.tenant }}: ClickHouse connections exceeded  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:clickhouse_active_connections:max |
| ClickHouseHighConnectionsCritical | critical | muted | [tier] {{ $labels.tenant }}: ClickHouse connections at critical level  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:clickhouse_active_connections:max |
| ClickHouseHighPartCount | warning | muted | [tier] {{ $labels.tenant }}: ClickHouse partition merge pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:clickhouse_max_part_count:max |
| ClickHouseReplicationLag | warning | muted | [tier] {{ $labels.tenant }}: ClickHouse replication queue high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:clickhouse_replication_queue:max |
| ClickHouseHighMemoryUsage | warning | muted | [tier] {{ $labels.tenant }}: ClickHouse memory usage high  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | tenant:clickhouse_memory_tracking:max |
| ClickHouseHighFailedQueryRate | warning | muted | Failed query rate: {{ $value \| printf "%.1f" }} queries/sec | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:clickhouse_failed_queries:rate5m |

---

## DB2 Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| DB2DatabaseDown | critical | fires | db2_up=0 for 15s on {{ $labels.instance }} | Immediately check server status and network connectivity; review system logs | db2_up |
| DB2HighConnections | warning | muted | [tier] {{ $labels.tenant }}: DB2 connection usage high  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:db2_connections_active:max |
| DB2HighConnectionsCritical | critical | muted | [tier] {{ $labels.tenant }}: DB2 connections at critical level  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:db2_connections_active:max |
| DB2LowBufferpoolHitRatio | warning | muted | [tier] {{ $labels.tenant }}: DB2 bufferpool hit ratio low  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_bufferpool_hit_ratio:min |
| DB2HighLogUsage | warning | muted | [tier] {{ $labels.tenant }}: DB2 transaction log usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_log_usage_percent:max |
| DB2HighDeadlockRate | warning | muted | [tier] {{ $labels.tenant }}: DB2 deadlock rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_deadlocks:rate5m |
| DB2TablespaceAlmostFull | warning | muted | [tier] {{ $labels.tenant }}: DB2 tablespace nearing capacity  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_tablespace_used_percent:max |
| DB2HighLockWaitTime | warning | muted | [tier] {{ $labels.tenant }}: DB2 lock wait time high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_lock_wait_time:rate5m |
| DB2HighLockWaitTimeCritical | critical | muted | [tier] {{ $labels.tenant }}: DB2 lock waits at critical level  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_lock_wait_time:rate5m |
| DB2HighSortOverflow | warning | muted | [tier] {{ $labels.tenant }}: DB2 sort overflow ratio high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_sort_overflow_ratio:avg |

---

## Elasticsearch Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| ElasticsearchClusterRed | critical | fires | Cluster health is RED  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_cluster_health:status |
| ElasticsearchClusterYellow | warning | muted | [tier] {{ $labels.tenant }}: ES cluster YELLOW  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_cluster_health:status |
| ElasticsearchHighHeapUsage | warning | muted | [tier] {{ $labels.tenant }}: ES heap usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_heap_usage_percent:max |
| ElasticsearchHighHeapUsageCritical | critical | muted | [tier] {{ $labels.tenant }}: ES heap at critical level  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_heap_usage_percent:max |
| ElasticsearchHighDiskUsage | warning | muted | [tier] {{ $labels.tenant }}: ES disk usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_disk_usage_percent:max |
| ElasticsearchHighDiskUsageCritical | critical | muted | [tier] {{ $labels.tenant }}: ES disk at critical level  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_disk_usage_percent:max |
| ElasticsearchHighSearchLatency | warning | muted | [tier] {{ $labels.tenant }}: ES search latency elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_search_latency_ms:avg |
| ElasticsearchHighSearchLatencyCritical | critical | muted | [tier] {{ $labels.tenant }}: ES search latency at critical level  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_search_latency_ms:avg |
| ElasticsearchUnassignedShards | warning | muted | [tier] {{ $labels.tenant }}: ES unassigned shards  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_unassigned_shards:count |
| ElasticsearchPendingTasks | warning | muted | [tier] {{ $labels.tenant }}: ES pending cluster tasks  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_pending_tasks:max |
| ElasticsearchPendingTasksCritical | critical | muted | [tier] {{ $labels.tenant }}: ES master task backlog at critical level  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_pending_tasks:max |

---

## JVM Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| JVMHighGCPause | warning | muted | [tier] {{ $labels.tenant }}: GC pause elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_gc_pause:rate5m |
| JVMHighGCPauseCritical | critical | muted | [tier] {{ $labels.tenant }}: critical GC pause  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_gc_pause:rate5m |
| JVMMemoryPressure | warning | muted | [tier] {{ $labels.tenant }}: heap memory pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_memory_used:percent |
| JVMMemoryPressureCritical | critical | muted | [tier] {{ $labels.tenant }}: critical heap pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_memory_used:percent |
| JVMThreadPoolExhaustion | warning | muted | [tier] {{ $labels.tenant }}: thread pool saturation  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_threads:current |
| JVMThreadPoolExhaustionCritical | critical | muted | [tier] {{ $labels.tenant }}: critical thread exhaustion  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_threads:current |
| JVMPerformanceDegraded | critical | muted | [tier] {{ $labels.tenant }}: multi-signal JVM degradation  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_gc_pause:rate5m |

---

## Kafka Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| KafkaHighConsumerLag | warning | muted | [tier] {{ $labels.tenant }}: consumer lag elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_consumer_lag:max |
| KafkaHighConsumerLagCritical | critical | muted | [tier] {{ $labels.tenant }}: critical consumer lag  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_consumer_lag:max |
| KafkaUnderReplicatedPartitions | warning | muted | [tier] {{ $labels.tenant }}: under-replicated partitions  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_under_replicated_partitions:sum |
| KafkaUnderReplicatedPartitionsCritical | critical | muted | [tier] {{ $labels.tenant }}: critical under-replication  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_under_replicated_partitions:sum |
| KafkaLowBrokerCount | warning | muted | [tier] {{ $labels.tenant }}: broker count below minimum  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_broker_count:max |

---

## Kubernetes Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| PodContainerHighCPU | warning | muted | [tier] {{ $labels.tenant }}: container CPU pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:pod_container_high_cpu_warning:core |
| PodContainerHighCPUCritical | critical | muted | [tier] {{ $labels.tenant }}: container CPU CRITICAL  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:pod_container_high_cpu_critical:core |
| PodContainerHighMemory | warning | muted | [tier] {{ $labels.tenant }}: container memory pressure  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | rule_pack_kubernetes:pod_container_high_memory_warning:core |
| PodContainerHighMemoryCritical | critical | muted | [tier] {{ $labels.tenant }}: container memory CRITICAL  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | rule_pack_kubernetes:pod_container_high_memory_critical:core |
| PodContainerCPUThrottled | warning | muted | [tier] {{ $labels.tenant }}: sustained CFS throttling  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:pod_cpu_throttled_warning:core |
| PodContainerCPUThrottledCritical | critical | muted | [tier] {{ $labels.tenant }}: CFS throttling CRITICAL  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:pod_cpu_throttled_critical:core |
| ContainerOOMKilled | warning | muted | [tier] {{ $labels.tenant }}: kernel OOM-killed a container in the last hour  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:container_oom_killed_warning:core |
| ContainerOOMKilledCritical | critical | muted | [tier] {{ $labels.tenant }}: >=3 OOM kills on one container within 1h  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:container_oom_killed_critical:core |
| ContainerCrashLoop | critical | muted | [tier] {{ $labels.tenant }}: crash loop detected  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:container_waiting_reason:count |
| ContainerImagePullFailure | warning | muted | [tier] {{ $labels.tenant }}: image pull failing  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:container_waiting_reason:count |
| VersionAwareThresholdInert | warning | fires | {{ $value \| printf "%.0f" }} version-specific container CPU threshold(s) declared and tenant pods ar | Check alert metrics and review related logs; contact platform team for assistance if needed | user_threshold |
| CustomRecipeDiskInert | warning | muted | [SRE] {{ $labels.tenant }} disk recipe inert  | Check alert metrics and review related logs; contact platform team for assistance if needed | user_threshold |
| NodeNotReady | warning | muted | [tier] {{ $labels.tenant }}: node {{ $labels.node }} NotReady  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:node_not_ready:core |
| TenantHAReplicasDegraded | warning | muted | [tier] {{ $labels.tenant }}: ready replicas < desired on an HA set (≥2)  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:ha_replicas_degraded:core |

---

## liveness

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| TenantExporterAbsent | critical | fires | No healthy up{job="tenant-exporters"}==1 target for tenant {{ $labels.tenant }} (db_type={{ $labels. | Verify component is running and configured correctly; check component logs | tenant_expected_exporter |
| TenantExporterReplicaDown | warning | muted | Target {{ $labels.instance }}{{ with $labels.pod }} (pod {{ . }}){{ end }} of tenant {{ $labels.tena | Immediately check server status and network connectivity; review system logs | up |

---

## MariaDB Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| MariaDBDown | warning | muted | mysql_up=0 on {{ $labels.instance }}  | Immediately check server status and network connectivity; review system logs | mysql_up |
| MariaDBClusterDown | critical | fires | No mysqld reports up=1 for tenant {{ $labels.tenant }}  | Immediately check server status and network connectivity; review system logs | mysql_up |
| MariaDBNoPrimary | critical | fires | All instances are read_only for tenant {{ $labels.tenant }}  | Check alert metrics and review related logs; contact platform team for assistance if needed | mysql_global_variables_read_only |
| MariaDBSemiSyncDegraded | warning | muted | Tenant {{ $labels.tenant }} has semi-sync enabled but it fell back to ASYNC (rpl_semi_sync_master_st | Check alert metrics and review related logs; contact platform team for assistance if needed | mysql_global_variables_rpl_semi_sync_master_enabled |
| MariaDBSemiSyncReplicasGone | critical | fires | Tenant {{ $labels.tenant }} has semi-sync enabled but ZERO semi-sync replicas connected (rpl_semi_sy | Check alert metrics and review related logs; contact platform team for assistance if needed | mysql_global_variables_rpl_semi_sync_master_enabled |
| MariaDBReplicaIOThreadDown | critical | fires | Replica IO thread is stopped (Slave_IO_Running=No) for tenant {{ $labels.tenant }}  | Immediately check server status and network connectivity; review system logs | mysql_slave_status_slave_io_running |
| MariaDBReplicaSQLThreadDown | critical | fires | Replica SQL thread is stopped (Slave_SQL_Running=No) for tenant {{ $labels.tenant }}  | Immediately check server status and network connectivity; review system logs | mysql_slave_status_slave_sql_running |
| MariaDBReplicationLag | warning | muted | [tier] {{ $labels.tenant }}: replication lag threshold breached  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mysql_replication_lag:max |
| MariaDBReplicationLagCritical | critical | muted | [tier] {{ $labels.tenant }}: CRITICAL replication lag  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mysql_replication_lag:max |
| MariaDBHighConnections | warning | muted | [tier] {{ $labels.tenant }}: connection threshold breached  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:mysql_connection_usage:percent |
| MariaDBHighConnectionsCritical | critical | muted | [tier] {{ $labels.tenant }}: critical connection saturation  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:mysql_connection_usage:percent |
| MariaDBHighThreadsRunning | warning | muted | [tier] {{ $labels.tenant }}: running-thread saturation (threads_running, NOT host CPU%) breached  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mysql_threads_running:avg1m |
| MariaDBHighThreadsRunningCritical | critical | muted | [tier] {{ $labels.tenant }}: CRITICAL running-thread saturation (threads_running, NOT host CPU%)  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mysql_threads_running:avg1m |
| MariaDBSystemBottleneck | critical | muted | [tier] {{ $labels.tenant }}: running-thread saturation + connections both exceeded  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mysql_connection_usage:percent |
| MariaDBRecentRestart | info | muted | Uptime is only {{ $value }}s (< 5 min) | Check alert metrics and review related logs; contact platform team for assistance if needed | mysql_global_status_uptime |
| MariaDBHighSlowQueries | warning | muted | [tier] {{ $labels.tenant }}: slow query rate elevated  | Check slow query logs, identify optimization candidates; consider parameter tuning | tenant:mysql_slow_queries:rate5m |
| MariaDBHighAbortedConnections | warning | muted | [tier] {{ $labels.tenant }}: aborted connection rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mysql_aborted_connections:rate5m |

---

## MongoDB Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| MongoDBDown | warning | muted | mongodb_up=0 on {{ $labels.instance }}  | Immediately check server status and network connectivity; review system logs | mongodb_up |
| MongoDBClusterDown | critical | fires | No mongod reports up=1 for tenant {{ $labels.tenant }}  | Immediately check server status and network connectivity; review system logs | mongodb_up |
| MongoDBNoPrimary | critical | fires | Replica-set members are reachable but none is PRIMARY for tenant {{ $labels.tenant }}  | Check alert metrics and review related logs; contact platform team for assistance if needed | mongodb_mongod_replset_member_state |
| MongoDBHighConnections | warning | muted | [tier] {{ $labels.tenant }}: MongoDB connection threshold breached  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:mongodb_connections_current:max |
| MongoDBHighConnectionsCritical | critical | muted | [tier] {{ $labels.tenant }}: MongoDB connections at critical level  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:mongodb_connections_current:max |
| MongoDBReplicationLag | warning | muted | [tier] {{ $labels.tenant }}: MongoDB replication lag  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mongodb_replication_lag:max |
| MongoDBReplicationLagCritical | critical | muted | [tier] {{ $labels.tenant }}: MongoDB secondary far behind primary  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mongodb_replication_lag:max |
| MongoDBHighOperations | warning | muted | [tier] {{ $labels.tenant }}: MongoDB operation rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mongodb_opcounters:rate5m |
| MongoDBHighPageFaults | warning | muted | [tier] {{ $labels.tenant }}: MongoDB page fault rate high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mongodb_page_faults:rate5m |
| MongoDBConnectionSaturation | warning | muted | [tier] {{ $labels.tenant }}: MongoDB connection pool near saturation  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mongodb_connection_usage:ratio |

---

## Nginx Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| NginxHighConnections | warning | muted | [tier] {{ $labels.tenant }}: Nginx connection threshold exceeded  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:nginx_connections_active:max |
| NginxHighConnectionsCritical | critical | muted | [tier] {{ $labels.tenant }}: critical Nginx connection saturation  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:nginx_connections_active:max |
| NginxRequestRateSpike | warning | muted | [tier] {{ $labels.tenant }}: request rate spike  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:nginx_requests:rate5m |
| NginxRequestRateSpikeCritical | critical | muted | [tier] {{ $labels.tenant }}: critical request rate  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:nginx_requests:rate5m |
| NginxConnectionBacklog | warning | muted | [tier] {{ $labels.tenant }}: connection backlog building  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:nginx_connections_waiting:max |
| NginxConnectionBacklogCritical | critical | muted | [tier] {{ $labels.tenant }}: critical connection backlog  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:nginx_connections_waiting:max |

---

## Operational Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| TenantSilentWarning | none | fires | Warning alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppressed | Check alert metrics and review related logs; contact platform team for assistance if needed | user_silent_mode |
| TenantSilentCritical | none | fires | Critical alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppresse | Check alert metrics and review related logs; contact platform team for assistance if needed | user_silent_mode |
| TenantSeverityDedupEnabled | none | fires | Warning notifications for {{ $labels.tenant }} will be suppressed when critical fires for the same m | Check alert metrics and review related logs; contact platform team for assistance if needed | user_severity_dedup |
| TenantConfigEvent | warning | fires | Timed config for tenant {{ $labels.tenant }} has expired and auto-deactivated. Event: {{ $labels.eve | Check alert metrics and review related logs; contact platform team for assistance if needed | da_config_event |

---

## Oracle Database Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| OracleDatabaseDown | critical | fires | oracledb_up=0 for 15s on {{ $labels.instance }} | Immediately check server status and network connectivity; review system logs | oracledb_up |
| OracleHighActiveSessions | warning | muted | [tier] {{ $labels.tenant }}: Oracle active sessions elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_sessions_active:max |
| OracleHighActiveSessionsCritical | critical | muted | [tier] {{ $labels.tenant }}: Oracle active sessions at critical level  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_sessions_active:max |
| OracleTablespaceAlmostFull | warning | muted | [tier] {{ $labels.tenant }}: Oracle tablespace nearing capacity  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_tablespace_used_percent:max |
| OracleTablespaceAlmostFullCritical | critical | muted | [tier] {{ $labels.tenant }}: Oracle tablespace almost out of space  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_tablespace_used_percent:max |
| OracleHighWaitTime | warning | muted | [tier] {{ $labels.tenant }}: Oracle wait time elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_wait_time:rate5m |
| OracleHighProcessCount | warning | muted | [tier] {{ $labels.tenant }}: Oracle process count high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_process_count:max |
| OracleHighPGAUsage | warning | muted | [tier] {{ $labels.tenant }}: Oracle PGA usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_pga_allocated_bytes:max |
| OracleHighSessionUtilization | warning | muted | [tier] {{ $labels.tenant }}: Oracle session limit approaching  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_session_utilization:ratio |

---

## PostgreSQL Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| PostgreSQLDown | critical | fires | pg_up=0 for 15s on {{ $labels.instance }} | Immediately check server status and network connectivity; review system logs | pg_up |
| PostgreSQLHighConnections | warning | muted | [tier] {{ $labels.tenant }}: connection usage high  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:pg_connection_usage:ratio |
| PostgreSQLHighConnectionsCritical | critical | muted | [tier] {{ $labels.tenant }}: critical connection saturation  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:pg_connection_usage:ratio |
| PostgreSQLHighReplicationLag | warning | muted | [tier] {{ $labels.tenant }}: replication lag elevated  | Check replication status and network connectivity; inspect queue buildup | tenant:pg_replication_lag:max |
| PostgreSQLHighReplicationLagCritical | critical | muted | [tier] {{ $labels.tenant }}: critical replication lag  | Check replication status and network connectivity; inspect queue buildup | tenant:pg_replication_lag:max |
| PostgreSQLHighDeadlocks | warning | muted | [tier] {{ $labels.tenant }}: deadlocks detected  | Analyze deadlock query logs, adjust application logic to reduce contention; consider increasing lock timeout | tenant:pg_deadlocks:rate5m |
| PostgreSQLHighRollbackRatio | warning | muted | [tier] {{ $labels.tenant }}: high rollback ratio  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:pg_rollback_ratio:rate5m |
| PostgreSQLRecentRestart | info | muted | PostgreSQL uptime is only {{ $value \| printf "%.0f" }}s (< 5 min) | Check alert metrics and review related logs; contact platform team for assistance if needed | pg_postmaster_start_time_seconds |

---

## RabbitMQ Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| RabbitMQHighQueueDepth | warning | muted | [tier] {{ $labels.tenant }}: queue depth threshold exceeded  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:rabbitmq_queue_messages:max |
| RabbitMQHighQueueDepthCritical | critical | muted | [tier] {{ $labels.tenant }}: critical queue depth  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:rabbitmq_queue_messages:max |
| RabbitMQHighMemory | warning | muted | [tier] {{ $labels.tenant }}: RabbitMQ memory usage high  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | tenant:rabbitmq_node_mem_percent:ratio |
| RabbitMQHighMemoryCritical | critical | muted | [tier] {{ $labels.tenant }}: critical RabbitMQ memory  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | tenant:rabbitmq_node_mem_percent:ratio |
| RabbitMQHighConnections | warning | muted | [tier] {{ $labels.tenant }}: RabbitMQ connection count high  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:rabbitmq_connections:max |
| RabbitMQHighConnectionsCritical | critical | muted | [tier] {{ $labels.tenant }}: critical connection count  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:rabbitmq_connections:max |
| RabbitMQLowConsumers | warning | muted | [tier] {{ $labels.tenant }}: RabbitMQ consumer count low  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:rabbitmq_consumers:max |
| RabbitMQHighUnackedMessages | warning | muted | [tier] {{ $labels.tenant }}: unacked messages piling up  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:rabbitmq_unacked_messages:max |
| RabbitMQHighUnackedMessagesCritical | critical | muted | [tier] {{ $labels.tenant }}: critical unacked backlog  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:rabbitmq_unacked_messages:max |

---

## Redis Rule Pack

| Alert Name | Severity | In maintenance | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|---|
| RedisDown | critical | fires |  | Immediately check server status and network connectivity; review system logs | redis_up |
| RedisHighMemory | warning | muted | [tier] {{ $labels.tenant }}: Redis memory threshold breached  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | tenant:redis_memory_used_bytes:max |
| RedisHighMemoryCritical | critical | muted | [tier] {{ $labels.tenant }}: Redis memory at critical level  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | tenant:redis_memory_used_bytes:max |
| RedisHighConnections | warning | muted | [tier] {{ $labels.tenant }}: Redis connection count high  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:redis_connected_clients:max |
| RedisHighConnectionsCritical | critical | muted | [tier] {{ $labels.tenant }}: Redis connections at critical level  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:redis_connected_clients:max |
| RedisHighKeyEvictions | warning | muted | [tier] {{ $labels.tenant }}: key eviction rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:redis_evicted_keys:rate5m |
| RedisReplicationLag | warning | muted | [tier] {{ $labels.tenant }}: Redis replication lag  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:redis_replication_lag:max |
| RedisReplicationLagCritical | critical | muted | [tier] {{ $labels.tenant }}: Redis replica far behind  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:redis_replication_lag:max |
| RedisLowHitRatio | warning | muted | [tier] {{ $labels.tenant }}: low cache hit ratio  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:redis_keyspace_hit_ratio:avg |

---
