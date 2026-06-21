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

---

## ClickHouse Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| ClickHouseDown | critical |  | Immediately check server status and network connectivity; review system logs | up |
| ClickHouseHighQueryRate | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse query rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:clickhouse_queries:rate5m |
| ClickHouseHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse connections exceeded  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:clickhouse_active_connections:max |
| ClickHouseHighPartCount | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse partition merge pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:clickhouse_max_part_count:max |
| ClickHouseReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse replication queue high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:clickhouse_replication_queue:max |
| ClickHouseHighMemoryUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse memory usage high  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | tenant:clickhouse_memory_tracking:max |
| ClickHouseHighFailedQueryRate | warning | Failed query rate: {{ $value \| printf "%.1f" }} queries/sec | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:clickhouse_failed_queries:rate5m |

---

## DB2 Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| DB2DatabaseDown | critical | db2_up=0 for 15s on {{ $labels.instance }} | Immediately check server status and network connectivity; review system logs | db2_up |
| DB2HighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 connection usage high  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:db2_connections_active:max |
| DB2LowBufferpoolHitRatio | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 bufferpool hit ratio low  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_bufferpool_hit_ratio:min |
| DB2HighLogUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 transaction log usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_log_usage_percent:max |
| DB2HighDeadlockRate | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 deadlock rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_deadlocks:rate5m |
| DB2TablespaceAlmostFull | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 tablespace nearing capacity  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_tablespace_used_percent:max |
| DB2HighSortOverflow | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 sort overflow ratio high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:db2_sort_overflow_ratio:avg |

---

## Elasticsearch Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| ElasticsearchClusterRed | critical | Cluster health is RED  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_cluster_health:status |
| ElasticsearchClusterYellow | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES cluster YELLOW  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_cluster_health:status |
| ElasticsearchHighHeapUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES heap usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_heap_usage_percent:max |
| ElasticsearchHighDiskUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES disk usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_disk_usage_percent:max |
| ElasticsearchHighSearchLatency | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES search latency elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_search_latency_ms:avg |
| ElasticsearchUnassignedShards | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES unassigned shards  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_unassigned_shards:count |
| ElasticsearchPendingTasks | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES pending cluster tasks  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:es_pending_tasks:max |

---

## JVM Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| JVMHighGCPause | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: GC pause elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_gc_pause:rate5m |
| JVMHighGCPauseCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical GC pause  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_gc_pause:rate5m |
| JVMMemoryPressure | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: heap memory pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_memory_used:percent |
| JVMMemoryPressureCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical heap pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_memory_used:percent |
| JVMThreadPoolExhaustion | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: thread pool saturation  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_threads:current |
| JVMThreadPoolExhaustionCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical thread exhaustion  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_threads:current |
| JVMPerformanceDegraded | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: multi-signal JVM degradation  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:jvm_gc_pause:rate5m |

---

## Kafka Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| KafkaExporterAbsent | critical | No kafka_brokers metric found for 30s | Verify component is running and configured correctly; check component logs | kafka_brokers |
| KafkaHighConsumerLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: consumer lag elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_consumer_lag:max |
| KafkaHighConsumerLagCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical consumer lag  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_consumer_lag:max |
| KafkaUnderReplicatedPartitions | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: under-replicated partitions  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_under_replicated_partitions:max |
| KafkaUnderReplicatedPartitionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical under-replication  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_under_replicated_partitions:max |
| KafkaNoActiveController | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: no active Kafka controller  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_active_controllers:max |
| KafkaLowBrokerCount | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: broker count below minimum  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_broker_count:max |
| KafkaHighRequestRate | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: message rate threshold exceeded  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_request_rate:sum |
| KafkaHighRequestRateCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical message rate  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:kafka_request_rate:sum |

---

## Kubernetes Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| PodContainerHighCPU | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: container CPU pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:pod_container_high_cpu_warning:core |
| PodContainerHighCPUCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: container CPU CRITICAL  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:pod_container_high_cpu_critical:core |
| PodContainerHighMemory | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: container memory pressure  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | rule_pack_kubernetes:pod_container_high_memory_warning:core |
| PodContainerHighMemoryCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: container memory CRITICAL  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | rule_pack_kubernetes:pod_container_high_memory_critical:core |
| ContainerCrashLoop | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: crash loop detected  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:container_waiting_reason:count |
| ContainerImagePullFailure | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: image pull failing  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:container_waiting_reason:count |
| VersionAwareThresholdInert | warning | {{ $value \| printf "%.0f" }} version-specific container CPU threshold(s) declared and tenant pods ar | Check alert metrics and review related logs; contact platform team for assistance if needed | user_threshold |
| CustomRecipeDiskInert | warning | [SRE] {{ $labels.tenant }} disk recipe inert  | Check alert metrics and review related logs; contact platform team for assistance if needed | user_threshold |
| NodeNotReady | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: node {{ $labels.node }} NotReady  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:node_not_ready:core |
| TenantHAReplicasDegraded | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ready replicas < desired on an HA set (≥2)  | Check alert metrics and review related logs; contact platform team for assistance if needed | rule_pack_kubernetes:ha_replicas_degraded:core |

---

## liveness

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| TenantExporterAbsent | critical | No healthy up{job="tenant-exporters"}==1 target for tenant {{ $labels.tenant }} (db_type={{ $labels. | Verify component is running and configured correctly; check component logs | tenant_expected_exporter |

---

## MariaDB Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| MariaDBDown | warning | mysql_up=0 on {{ $labels.instance }}  | Immediately check server status and network connectivity; review system logs | mysql_up |
| MariaDBClusterDown | critical | No mysqld reports up=1 for tenant {{ $labels.tenant }}  | Immediately check server status and network connectivity; review system logs | mysql_up |
| MariaDBNoPrimary | critical | All instances are read_only for tenant {{ $labels.tenant }}  | Check alert metrics and review related logs; contact platform team for assistance if needed | mysql_global_variables_read_only |
| MariaDBExporterAbsent | critical | No mysql_up metric found for 30s | Verify component is running and configured correctly; check component logs | mysql_up |
| MariaDBHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: connection threshold breached  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:mysql_threads_connected:max |
| MariaDBHighConnectionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical connection saturation  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:mysql_threads_connected:max |
| MariaDBHighCPU | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: CPU threshold breached  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mysql_cpu_usage:rate5m |
| MariaDBHighCPUCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical CPU saturation  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mysql_cpu_usage:rate5m |
| MariaDBSystemBottleneck | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: CPU + connections both exceeded  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mysql_threads_connected:max |
| MariaDBRecentRestart | info | Uptime is only {{ $value }}s (< 5 min) | Check alert metrics and review related logs; contact platform team for assistance if needed | mysql_global_status_uptime |
| MariaDBHighSlowQueries | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: slow query rate elevated  | Check slow query logs, identify optimization candidates; consider parameter tuning | tenant:mysql_slow_queries:rate5m |
| MariaDBHighAbortedConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: aborted connection rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mysql_aborted_connections:rate5m |

---

## MongoDB Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| MongoDBDown | warning | mongodb_up=0 on {{ $labels.instance }}  | Immediately check server status and network connectivity; review system logs | mongodb_up |
| MongoDBClusterDown | critical | No mongod reports up=1 for tenant {{ $labels.tenant }}  | Immediately check server status and network connectivity; review system logs | mongodb_up |
| MongoDBNoPrimary | critical | Replica-set members are reachable but none is PRIMARY for tenant {{ $labels.tenant }}  | Check alert metrics and review related logs; contact platform team for assistance if needed | mongodb_mongod_replset_member_state |
| MongoDBHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB connection threshold breached  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:mongodb_connections_current:max |
| MongoDBReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB replication lag  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mongodb_replication_lag:max |
| MongoDBHighOperations | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB operation rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mongodb_opcounters:rate5m |
| MongoDBHighPageFaults | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB page fault rate high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mongodb_page_faults:rate5m |
| MongoDBConnectionSaturation | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB connection pool near saturation  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:mongodb_connection_usage:ratio |

---

## Nginx Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| NginxHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Nginx connection threshold exceeded  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:nginx_connections_active:max |
| NginxHighConnectionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical Nginx connection saturation  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:nginx_connections_active:max |
| NginxRequestRateSpike | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: request rate spike  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:nginx_requests:rate5m |
| NginxRequestRateSpikeCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical request rate  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:nginx_requests:rate5m |
| NginxConnectionBacklog | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: connection backlog building  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:nginx_connections_waiting:max |
| NginxConnectionBacklogCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical connection backlog  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:nginx_connections_waiting:max |

---

## Operational Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| TenantSilentWarning | none | Warning alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppressed | Check alert metrics and review related logs; contact platform team for assistance if needed | user_silent_mode |
| TenantSilentCritical | none | Critical alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppresse | Check alert metrics and review related logs; contact platform team for assistance if needed | user_silent_mode |
| TenantSeverityDedupEnabled | none | Warning notifications for {{ $labels.tenant }} will be suppressed when critical fires for the same m | Check alert metrics and review related logs; contact platform team for assistance if needed | user_severity_dedup |
| TenantConfigEvent | warning | Timed config for tenant {{ $labels.tenant }} has expired and auto-deactivated. Event: {{ $labels.eve | Check alert metrics and review related logs; contact platform team for assistance if needed | da_config_event |

---

## Oracle Database Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| OracleDatabaseDown | critical | oracledb_up=0 for 15s on {{ $labels.instance }} | Immediately check server status and network connectivity; review system logs | oracledb_up |
| OracleHighActiveSessions | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle active sessions elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_sessions_active:max |
| OracleTablespaceAlmostFull | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle tablespace nearing capacity  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_tablespace_used_percent:max |
| OracleHighWaitTime | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle wait time elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_wait_time:rate5m |
| OracleHighProcessCount | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle process count high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_process_count:max |
| OracleHighPGAUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle PGA usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_pga_allocated_bytes:max |
| OracleHighSessionUtilization | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle session limit approaching  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:oracle_session_utilization:ratio |

---

## PostgreSQL Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| PostgreSQLDown | critical | pg_up=0 for 15s on {{ $labels.instance }} | Immediately check server status and network connectivity; review system logs | pg_up |
| PostgreSQLExporterAbsent | critical | No pg_up metric found for 30s | Verify component is running and configured correctly; check component logs | pg_up |
| PostgreSQLHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: connection usage high  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:pg_connection_usage:ratio |
| PostgreSQLHighConnectionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical connection saturation  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:pg_connection_usage:ratio |
| PostgreSQLHighReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: replication lag elevated  | Check replication status and network connectivity; inspect queue buildup | tenant:pg_replication_lag:max |
| PostgreSQLHighReplicationLagCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical replication lag  | Check replication status and network connectivity; inspect queue buildup | tenant:pg_replication_lag:max |
| PostgreSQLHighDeadlocks | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: deadlocks detected  | Analyze deadlock query logs, adjust application logic to reduce contention; consider increasing lock timeout | tenant:pg_deadlocks:rate5m |
| PostgreSQLHighRollbackRatio | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: high rollback ratio  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:pg_rollback_ratio:rate5m |
| PostgreSQLRecentRestart | info | PostgreSQL uptime is only {{ $value \| printf "%.0f" }}s (< 5 min) | Check alert metrics and review related logs; contact platform team for assistance if needed | pg_postmaster_start_time_seconds |

---

## RabbitMQ Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| RabbitMQExporterAbsent | critical | No rabbitmq_identity_info metric found for 30s | Verify component is running and configured correctly; check component logs | rabbitmq_identity_info |
| RabbitMQHighQueueDepth | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: queue depth threshold exceeded  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:rabbitmq_queue_messages:max |
| RabbitMQHighQueueDepthCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical queue depth  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:rabbitmq_queue_messages:max |
| RabbitMQHighMemory | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: RabbitMQ memory usage high  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | tenant:rabbitmq_node_mem_percent:ratio |
| RabbitMQHighMemoryCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical RabbitMQ memory  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | tenant:rabbitmq_node_mem_percent:ratio |
| RabbitMQHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: RabbitMQ connection count high  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:rabbitmq_connections:max |
| RabbitMQLowConsumers | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: RabbitMQ consumer count low  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:rabbitmq_consumers:max |
| RabbitMQHighUnackedMessages | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: unacked messages piling up  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:rabbitmq_unacked_messages:max |

---

## Redis Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| RedisDown | critical |  | Immediately check server status and network connectivity; review system logs | redis_up |
| RedisHighMemory | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Redis memory threshold breached  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | tenant:redis_memory_used_bytes:max |
| RedisHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Redis connection count high  | Check connection pool configuration and potential leaks; consider increasing max connections | tenant:redis_connected_clients:max |
| RedisHighKeyEvictions | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: key eviction rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:redis_evicted_keys:rate5m |
| RedisReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Redis replication lag  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:redis_replication_lag:max |
| RedisLowHitRatio | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: low cache hit ratio  | Check alert metrics and review related logs; contact platform team for assistance if needed | tenant:redis_keyspace_hit_ratio:avg |

---
