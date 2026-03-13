---
title: "Rule Pack Alert Reference Guide"
tags: [alerts, reference, rule-packs]
audience: [tenant, sre]
version: v1.12.0
lang: en
---
# Rule Pack Alert Reference Guide

This document provides tenants with a unified reference for all alerts across Rule Packs, including alert meanings, trigger conditions, and recommended actions.

**Note**: This guide covers only **user-facing threshold alerts**. Sentinel alerts in the Operational Rule Pack are platform-internal control mechanisms and do not require tenant action.

---

## MariaDB Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| MariaDBDown | critical | Instance connection failure (mysql_up=0 for 15s) | Immediately check DB server status, network connectivity, firewall rules; check MariaDB logs | mysql_up |
| MariaDBExporterAbsent | critical | mysqld_exporter missing (no mysql_up metric for 30s) | Verify exporter container is running and configured correctly; check exporter logs | mysql_up |
| MariaDBHighConnections | warning | Connection count exceeds warning threshold (default 80%) | Check connection pool config, potential connection leaks; consider increasing max_connections | mysql_global_status_threads_connected |
| MariaDBHighConnectionsCritical | critical | Connection count exceeds critical threshold | Immediate intervention required; check active connections, kill idle sessions; consider app-level throttling | mysql_global_status_threads_connected |
| MariaDBSystemBottleneck | critical | Both connections AND CPU exceed warning thresholds simultaneously | Multi-resource bottleneck, escalate immediately; address both connection and CPU pressure sources | mysql_global_status_threads_connected, mysql_global_status_threads_running |
| MariaDBRecentRestart | info | Instance recently restarted (uptime < 5 minutes) | Informational; check for unexpected restarts; review system and MariaDB logs | mysql_global_status_uptime |
| MariaDBHighSlowQueries | warning | Slow query rate high (> 1 query/sec) | Check slow query logs, identify optimization candidates; consider adjusting long_query_time parameter | mysql_global_status_slow_queries |
| MariaDBHighAbortedConnections | warning | Aborted connection rate high (> 5 conn/sec) | Check client connections, verify authentication issues; review application logs | mysql_global_status_aborted_connects |

---

## PostgreSQL Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| PostgreSQLDown | critical | Instance connection failure (pg_up=0 for 15s) | Immediately check PostgreSQL server status and network; review postgresql logs | pg_up |
| PostgreSQLExporterAbsent | critical | postgres_exporter missing (no pg_up metric for 30s) | Verify exporter container is running and configured correctly; check exporter logs | pg_up |
| PostgreSQLHighConnections | warning | Connection count exceeds warning threshold (default 80% of max_connections) | Check active queries, application connection pool settings; consider increasing max_connections or closing idle connections | pg_stat_activity_count |
| PostgreSQLHighConnectionsCritical | critical | Connection count exceeds critical threshold (default 90%) | Immediate intervention required; identify long-running queries; use pg_terminate_backend to close idle connections | pg_stat_activity_count |
| PostgreSQLHighReplicationLag | warning | Replication lag exceeds warning threshold (default 30s) | Check replication status, primary-replica network connectivity; inspect WAL segment buildup | pg_replication_lag |
| PostgreSQLHighReplicationLagCritical | critical | Replication lag exceeds critical threshold (default 60s) | Immediately check replica health, WAL disk space; consider manual catch-up or resync | pg_replication_lag |
| PostgreSQLHighDeadlocks | warning | Deadlock occurrence rate high (> 1/sec over 5m) | Analyze deadlock query logs, adjust application logic to reduce contention; consider increasing lock timeout | pg_stat_database_deadlocks |
| PostgreSQLHighRollbackRatio | warning | Transaction rollback ratio high (exceeds threshold) | Check application error rates, constraint violations; investigate root cause of transaction failures | pg_stat_database_xact_rollback |
| PostgreSQLRecentRestart | info | Instance recently restarted (uptime < 5 minutes) | Informational; check for unexpected restarts; review system logs | pg_postmaster_start_time_seconds |

---

## Redis Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| RedisDown | critical | Instance connection failure (redis_up=0 for 15s) | Immediately check Redis server status and network; check redis logs | redis_up |
| RedisHighMemory | warning | Memory usage exceeds warning threshold | Check key count and eviction policy; consider increasing memory or enabling compression | redis_memory_used_bytes |
| RedisHighConnections | warning | Connection count exceeds warning threshold (default 500) | Check application connection pool, potential connection leaks; increase maxclients setting | redis_connected_clients |
| RedisHighKeyEvictions | warning | Key eviction rate high (> 100 keys/sec) | Memory pressure, check eviction policy (LRU/LFU); consider increasing memory or optimizing data structures | redis_evicted_keys_total |
| RedisReplicationLag | warning | Replica replication lag high | Check replica server health and network latency; inspect replication queue size | redis_connected_slave_lag_seconds |
| RedisLowHitRatio | warning | Keyspace hit ratio low (below threshold) | Low application query efficiency, check hot data access patterns; may need to optimize app logic or increase memory | redis_keyspace_hits_total, redis_keyspace_misses_total |

---

## MongoDB Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| MongoDBDown | critical | Instance connection failure (mongodb_up=0 for 15s) | Immediately check MongoDB server and replica set status; check mongodb logs | mongodb_up |
| MongoDBHighConnections | warning | Active connections exceed warning threshold (default 500) | Check application connection pool for leaks; consider increasing maxPoolSize | mongodb_connections{state="current"} |
| MongoDBReplicationLag | warning | Replication lag exceeds warning threshold (default 10s) | Check replica member health and network latency; inspect oplog size and catch-up progress | mongodb_mongod_replset_member_replication_lag |
| MongoDBHighOperations | warning | Operation rate exceeds threshold | Heavy workload, check if sharding or query optimization needed; consider adding CPU/memory | mongodb_opcounters_total |
| MongoDBHighPageFaults | warning | Page fault rate high (> 100/sec) | Memory insufficient, working set exceeds available memory; increase memory or optimize query indexes | mongodb_extra_info_page_faults_total |
| MongoDBConnectionSaturation | warning | Connection pool saturation (> 80% usage) | Approaching connection limit, check application behavior; consider increasing connection pool size or implementing throttling | mongodb_connections |

---

## Elasticsearch Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| ElasticsearchClusterRed | critical | Cluster health RED (data unavailable) | Immediate escalation, possible primary shard loss; check node availability and disk space | elasticsearch_cluster_health_status |
| ElasticsearchClusterYellow | warning | Cluster health YELLOW (replica shards unassigned) | Check cluster health status and node availability; consider reallocating replicas or adding nodes | elasticsearch_cluster_health_status |
| ElasticsearchHighHeapUsage | warning | JVM heap usage exceeds warning threshold (default 85%) | Check for large queries and aggregations; adjust JVM heap size or optimize queries | elasticsearch_jvm_memory_used_bytes{area="heap"} |
| ElasticsearchHighDiskUsage | warning | Disk usage exceeds warning threshold (default 80%) | Clean up old indices, add disk capacity; check index sizing and shard distribution | elasticsearch_filesystem_data_size_bytes |
| ElasticsearchHighSearchLatency | warning | Search latency exceeds warning threshold (default 500ms) | Check query complexity and index size; adjust refresh_interval or optimize mappings | elasticsearch_indices_search_query_time_seconds |
| ElasticsearchUnassignedShards | warning | Unassigned shards present | Cluster cannot recover shards, check node availability; manually allocate or restart nodes | elasticsearch_cluster_health_unassigned_shards |
| ElasticsearchPendingTasks | warning | Pending cluster tasks accumulated | Master node high load, check for failed nodes; consider optimizing or splitting large operations | elasticsearch_cluster_health_number_of_pending_tasks |

---

## Oracle Database Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| OracleDatabaseDown | critical | Instance connection failure (oracledb_up=0) | Immediately check Oracle instance status, listener, network; review alert.log | oracledb_up |
| OracleHighActiveSessions | warning | Active sessions exceed warning threshold (default 200) | Check for long-running queries and locks; analyze sessions using v$session | oracledb_sessions_active |
| OracleTablespaceAlmostFull | warning | Tablespace usage nearing capacity (default 85%) | Increase tablespace size, clean up data; check autoextend settings | oracledb_tablespace_used_percent |
| OracleHighWaitTime | warning | Wait time rate high | Performance bottleneck, analyze wait types using v$session_wait; adjust parameters or optimize queries | oracledb_wait_time_seconds_total |
| OracleHighProcessCount | warning | Process count exceeds warning threshold | Check process usage; optimize application logic to reduce background processes | oracledb_process_count |
| OracleHighPGAUsage | warning | PGA memory exceeds warning threshold | Check for large sorts and hash operations; adjust pga_aggregate_target parameter | oracledb_pga_allocated_bytes |
| OracleHighSessionUtilization | warning | Session limit approaching (> 85% usage) | Check parallel session usage, clean idle sessions; consider increasing processes parameter | oracledb_sessions_active |

---

## DB2 Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| DB2DatabaseDown | critical | Instance connection failure (db2_up=0) | Immediately check DB2 instance status and network; review diagnostic logs | db2_up |
| DB2HighConnections | warning | Active connections exceed warning threshold | Check application connection pool and leaks; adjust maxagents parameter | db2_connections_active |
| DB2LowBufferpoolHitRatio | warning | Bufferpool hit ratio below warning threshold (default 0.95) | Memory insufficient for bufferpool, increase bufferpool size; check index usage | db2_bufferpool_hit_ratio |
| DB2HighLogUsage | warning | Transaction log usage exceeds warning threshold (default 70%) | Heavy active transactions, check for long-running DDL; adjust log file size | db2_log_usage_percent |
| DB2HighDeadlockRate | warning | Deadlock occurrence rate high | Analyze deadlock queries, adjust application logic; increase lock timeout | db2_deadlocks_total |
| DB2TablespaceAlmostFull | warning | Tablespace usage nearing capacity | Add tablespace capacity, clean up data; check autoextend settings | db2_tablespace_used_percent |
| DB2HighSortOverflow | warning | Sort overflow ratio high (> 5%) | SORTHEAP parameter insufficient, adjust SORTHEAP; consider increasing available memory | db2_sort_overflows |

---

## ClickHouse Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| ClickHouseDown | critical | Instance connection failure (up=0 for 2 minutes) | Immediately check ClickHouse server status and network; review system logs | up |
| ClickHouseHighQueryRate | warning | Query rate exceeds warning threshold (default 500 query/sec) | High workload, check query complexity; consider adding nodes or optimizing queries | ClickHouseProfileEvents_Query |
| ClickHouseHighConnections | warning | Active connections exceed warning threshold (default 200) | Check application connection pool and leaks; adjust max_concurrent_queries parameter | ClickHouseMetrics_TCPConnection |
| ClickHouseHighPartCount | warning | Shard part count high (merge pressure) | High write rate causing part accumulation; check merge progress or adjust write strategy | ClickHouseAsyncMetrics_MaxPartCountForPartition |
| ClickHouseReplicationLag | warning | Replication queue size exceeds threshold | Replica cannot keep up with primary; check network latency or replica resources | ClickHouseMetrics_ReplicatedSendQueueSize |
| ClickHouseHighMemoryUsage | warning | Memory usage exceeds warning threshold (default 8 GB) | Check large queries and aggregations; adjust memory limit or optimize queries | ClickHouseMetrics_MemoryTracking |
| ClickHouseHighFailedQueryRate | warning | Query failure rate high (> 10/sec) | Check query logs for failure reasons; check disk space and network connectivity | ClickHouseProfileEvents_FailedQuery |

---

## Kafka Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| KafkaExporterAbsent | critical | kafka_exporter missing (no kafka_brokers for 30s) | Verify exporter container is running and configured correctly; check exporter logs | kafka_brokers |
| KafkaHighConsumerLag | warning | Consumer lag exceeds warning threshold (default 1000) | Consumer cannot keep up, check consumer application status; add consumer instances or optimize consumer logic | kafka_consumergroup_lag_sum |
| KafkaHighConsumerLagCritical | critical | Consumer lag exceeds critical threshold | Immediate escalation, consumer severely behind; check consumer app failure or network issues | kafka_consumergroup_lag_sum |
| KafkaUnderReplicatedPartitions | warning | Partitions without all in-sync replicas | Replica failure, check broker node health; inspect disk and network issues | kafka_topic_partition_under_replicated_partition |
| KafkaUnderReplicatedPartitionsCritical | critical | Under-replicated partition count exceeds critical threshold | Immediately check broker failures and repair replicas; data availability at risk | kafka_topic_partition_under_replicated_partition |
| KafkaNoActiveController | critical | No active controller | Cluster without master controller, immediately check broker status; restart failed master controller | kafka_controller_active_controller_count |
| KafkaLowBrokerCount | warning | Broker count below expected minimum (default 3) | Broker failure, check broker health status; consider restarting or replacing failed brokers | kafka_brokers |
| KafkaHighRequestRate | warning | Request rate exceeds warning threshold | High throughput workload, check if scaling needed; monitor broker CPU and disk usage | kafka_server_brokertopicmetrics_messagesin_total |
| KafkaHighRequestRateCritical | critical | Request rate exceeds critical threshold | Immediate escalation, broker approaching saturation; consider adding brokers or partitions | kafka_server_brokertopicmetrics_messagesin_total |

---

## RabbitMQ Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| RabbitMQExporterAbsent | critical | rabbitmq_exporter missing (no rabbitmq_identity_info for 30s) | Verify exporter container is running and configured correctly; check exporter logs | rabbitmq_identity_info |
| RabbitMQHighQueueDepth | warning | Queue depth exceeds warning threshold (default 100000 msg) | Consumer cannot keep up, check consumer app status; add consumers or optimize consumer logic | rabbitmq_queue_messages_ready |
| RabbitMQHighQueueDepthCritical | critical | Queue depth exceeds critical threshold | Immediate escalation, queue accumulation severe; immediately add consumers or check consumer failure | rabbitmq_queue_messages_ready |
| RabbitMQHighMemory | warning | Memory usage exceeds warning threshold (default 80%) | Check queue size and consumption rate; adjust memory limit or add brokers | rabbitmq_node_mem_used |
| RabbitMQHighMemoryCritical | critical | Memory usage exceeds critical threshold (default 95%) | Immediate escalation, broker near failure; purge old messages or increase memory | rabbitmq_node_mem_used |
| RabbitMQHighConnections | warning | Connection count exceeds warning threshold (default 1000) | Check application connection pool and leaks; adjust connection limits or optimize app | rabbitmq_connections |
| RabbitMQLowConsumers | warning | Consumer count below expected minimum | Consumer failure or insufficient consumers, check consumer app health; start more consumer instances | rabbitmq_queue_consumers |
| RabbitMQHighUnackedMessages | warning | Unacknowledged message count high | Consumer processing too slow, check consumer app logic; add consumers or optimize business logic | rabbitmq_queue_messages_unacked |

---

## Kubernetes Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| PodContainerHighCPU | warning | Container CPU usage exceeds warning threshold (default 80%) | Check container application load; increase CPU limits or optimize application performance | container_cpu_usage_seconds_total |
| PodContainerHighMemory | warning | Container memory usage exceeds warning threshold (default 85%) | Check container application for memory leaks; increase memory limits or optimize app | container_memory_working_set_bytes |
| ContainerCrashLoop | critical | Container repeatedly crashing (CrashLoopBackOff state) | Check container logs for crash reasons; verify application config, dependencies, disk space | kube_pod_container_status_waiting_reason |
| ContainerImagePullFailure | warning | Container image pull failure (ImagePullBackOff/InvalidImageName) | Check image name and registry availability; verify image exists and authentication is configured | kube_pod_container_status_waiting_reason |

---

## JVM Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| JVMHighGCPause | warning | Garbage collection pause time rate high (default 0.5 sec/5m) | Check application for memory leaks; adjust JVM heap size or GC parameters | jvm_gc_pause_seconds_sum |
| JVMHighGCPauseCritical | critical | GC pause time exceeds critical threshold | Immediate escalation, application performance severely degraded; increase heap or use low-latency GC algorithm | jvm_gc_pause_seconds_sum |
| JVMMemoryPressure | warning | Heap memory usage exceeds warning threshold (default 80%) | Check application for memory leaks; increase heap size or optimize app logic | jvm_memory_used_bytes{area="heap"} |
| JVMMemoryPressureCritical | critical | Heap memory usage exceeds critical threshold (default 95%) | Immediate escalation, OOM risk; immediately increase heap or restart application | jvm_memory_used_bytes{area="heap"} |
| JVMThreadPoolExhaustion | warning | Active threads exceed warning threshold (default 500) | Thread pool saturated, check request queue; increase thread pool size or optimize app | jvm_threads_current |
| JVMThreadPoolExhaustionCritical | critical | Active threads exceed critical threshold | Immediate escalation, service degradation risk; immediately increase thread pool or implement throttling | jvm_threads_current |
| JVMPerformanceDegraded | critical | Both GC pause AND heap memory exceed warning thresholds simultaneously | Application performance severely degraded with concurrent memory and GC pressure; immediately increase resources or optimize app | jvm_gc_pause_seconds_sum, jvm_memory_used_bytes{area="heap"} |

---

## Nginx Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| NginxHighConnections | warning | Active connections exceed warning threshold (default 1000) | High traffic, check upstream applications; increase worker processes or upstream capacity | nginx_connections_active |
| NginxHighConnectionsCritical | critical | Active connections exceed critical threshold (default 1500) | Immediate escalation, service approaching saturation; immediately increase capacity or implement throttling | nginx_connections_active |
| NginxRequestRateSpike | warning | Request rate exceeds warning threshold (default 5000 req/s) | Traffic spike, check if normal business traffic; monitor upstream application load | nginx_http_requests_total |
| NginxRequestRateSpikeCritical | critical | Request rate exceeds critical threshold | Immediate escalation, possible DDoS attack or traffic surge; check logs, enable rate limiting or DDoS protection | nginx_http_requests_total |
| NginxConnectionBacklog | warning | Waiting connections exceed warning threshold (default 200) | Upstream application responding slowly, connections accumulating; check upstream status, optimize response time | nginx_connections_waiting |
| NginxConnectionBacklogCritical | critical | Waiting connections exceed critical threshold | Immediate escalation, likely service degradation; check upstream app failure, increase upstream capacity | nginx_connections_waiting |

---

## Quick Start Guide

### Alert Categories

- **critical (Red)**: Requires immediate human intervention, may impact service availability
- **warning (Yellow)**: Needs attention but not urgent, precursor to future problems
- **info (Blue)**: Reference information, usually no action required

### Quick Decision Tree

1. **Is this planned maintenance?** Use `_state_maintenance` or `_silent_mode` to temporarily suppress the alert
2. **Does this require immediate action?** Check severity level
   - critical → Immediately escalate to on-call engineer
   - warning → Investigate during next work session
3. **Is the alert persistent?** Confirm it's not a momentary spike
4. **What's the root cause?** Use related metrics for deeper diagnosis

### Common Causes and Quick Fixes

| Root Cause | Applicable Alerts | Quick Fix |
|---|---|---|
| Memory leak | High Memory, GC Pause | Restart app or increase memory; check code for leaks |
| Connection pool exhaustion | High Connections | Increase max_connections; check for connection leaks |
| Consumer failure | High Queue Depth, Consumer Lag | Restart consumer; check consumer app logs |
| Disk full | High Disk Usage, Tablespace Full | Clean old data; increase disk capacity |
| Network latency | Replication Lag | Check network connection; increase bandwidth or optimize transfer |
| Application crash | High Error Rate, CrashLoop | Check app logs; rollback problematic deployment |

---

## Further Resources

- **Detailed Configuration**: Refer to `_defaults.yaml` and tenant YAML configurations
- **Runbooks**: Each alert's `runbook_url` label provides detailed troubleshooting steps
- **Metric Queries**: Use Prometheus UI to query related metrics for in-depth diagnosis
- **Platform Documentation**: See detailed guides in `docs/` directory for architecture and operations

---

## Notes for Tenants

- Thresholds are customizable per tenant via YAML configuration
- All alerts support maintenance windows and silent mode for planned downtime
- Use `metric_group` label in Alertmanager routes to group related alerts
- Refer to your runbook URLs in alert annotations for service-specific guidance
