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

## ClickHouse Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| ClickHouseDown | critical |  | Immediately check server status and network connectivity; review system logs |  |
| ClickHouseHighQueryRate | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse query rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed |  |
| ClickHouseHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse connections exceeded  | Check connection pool configuration and potential leaks; consider increasing max connections |  |
| ClickHouseHighPartCount | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse partition merge pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed |  |
| ClickHouseReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse replication queue high  | Check alert metrics and review related logs; contact platform team for assistance if needed |  |
| ClickHouseHighMemoryUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse memory usage high  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression |  |
| ClickHouseHighFailedQueryRate | warning |  | Check alert metrics and review related logs; contact platform team for assistance if needed |  |

---

## DB2 Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| DB2DatabaseDown | critical |  | Immediately check server status and network connectivity; review system logs |  |
| DB2HighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 connection usage high  | Check connection pool configuration and potential leaks; consider increasing max connections | value |
| DB2LowBufferpoolHitRatio | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 bufferpool hit ratio low  | Check alert metrics and review related logs; contact platform team for assistance if needed | ratio |
| DB2HighLogUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 transaction log usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | usage |
| DB2HighDeadlockRate | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 deadlock rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |
| DB2TablespaceAlmostFull | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 tablespace nearing capacity  | Check alert metrics and review related logs; contact platform team for assistance if needed | usage |
| DB2HighSortOverflow | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 sort overflow ratio high  | Check alert metrics and review related logs; contact platform team for assistance if needed | overflow |

---

## Elasticsearch Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| ElasticsearchClusterRed | critical | Cluster health is RED  | Check alert metrics and review related logs; contact platform team for assistance if needed | health |
| ElasticsearchClusterYellow | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES cluster YELLOW  | Check alert metrics and review related logs; contact platform team for assistance if needed | replica |
| ElasticsearchHighHeapUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES heap usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | heap |
| ElasticsearchHighDiskUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES disk usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | usage |
| ElasticsearchHighSearchLatency | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES search latency elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | search |
| ElasticsearchUnassignedShards | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES unassigned shards  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |
| ElasticsearchPendingTasks | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES pending cluster tasks  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |

---

## JVM Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| JVMHighGCPause | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: GC pause elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | pause |
| JVMHighGCPauseCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical GC pause  | Check alert metrics and review related logs; contact platform team for assistance if needed | pause |
| JVMMemoryPressure | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: heap memory pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed | usage |
| JVMMemoryPressureCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical heap pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed | usage |
| JVMThreadPoolExhaustion | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: thread pool saturation  | Check alert metrics and review related logs; contact platform team for assistance if needed | threads |
| JVMThreadPoolExhaustionCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical thread exhaustion  | Check alert metrics and review related logs; contact platform team for assistance if needed | threads |
| JVMPerformanceDegraded | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: multi-signal JVM degradation  | Check alert metrics and review related logs; contact platform team for assistance if needed | pause |

---

## Kafka Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| KafkaExporterAbsent | critical | No kafka_brokers metric found for 30s | Verify component is running and configured correctly; check component logs | kafka_brokers |
| KafkaHighConsumerLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: consumer lag elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | lag |
| KafkaHighConsumerLagCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical consumer lag  | Check alert metrics and review related logs; contact platform team for assistance if needed | lag |
| KafkaUnderReplicatedPartitions | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: under-replicated partitions  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |
| KafkaUnderReplicatedPartitionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical under-replication  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |
| KafkaNoActiveController | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: no active Kafka controller  | Check alert metrics and review related logs; contact platform team for assistance if needed | controllers |
| KafkaLowBrokerCount | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: broker count below minimum  | Check alert metrics and review related logs; contact platform team for assistance if needed | brokers |
| KafkaHighRequestRate | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: message rate threshold exceeded  | Check alert metrics and review related logs; contact platform team for assistance if needed | rate |
| KafkaHighRequestRateCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical message rate  | Check alert metrics and review related logs; contact platform team for assistance if needed | rate |

---

## Kubernetes Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| PodContainerHighCPU | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: container CPU pressure  | Check alert metrics and review related logs; contact platform team for assistance if needed | container |
| PodContainerHighMemory | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: container memory pressure  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | container |
| ContainerCrashLoop | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: crash loop detected  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |
| ContainerImagePullFailure | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: image pull failing  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |

---

## MariaDB Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| MariaDBDown | critical | mysql_up=0 for 15s on {{ $labels.instance }} | Immediately check server status and network connectivity; review system logs | mysql_up |
| MariaDBExporterAbsent | critical | No mysql_up metric found for 30s | Verify component is running and configured correctly; check component logs | mysql_up |
| MariaDBHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: connection threshold breached  | Check connection pool configuration and potential leaks; consider increasing max connections | value |
| MariaDBHighConnectionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical connection saturation  | Check connection pool configuration and potential leaks; consider increasing max connections | value |
| MariaDBSystemBottleneck | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: CPU + connections both exceeded  | Check alert metrics and review related logs; contact platform team for assistance if needed | connections |
| MariaDBRecentRestart | info | Uptime is only {{ $value }}s (< 5 min) | Check alert metrics and review related logs; contact platform team for assistance if needed | is |
| MariaDBHighSlowQueries | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: slow query rate elevated  | Check slow query logs, identify optimization candidates; consider parameter tuning | value |
| MariaDBHighAbortedConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: aborted connection rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |

---

## MongoDB Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| MongoDBDown | critical |  | Immediately check server status and network connectivity; review system logs |  |
| MongoDBHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB connection threshold breached  | Check connection pool configuration and potential leaks; consider increasing max connections | value |
| MongoDBReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB replication lag  | Check alert metrics and review related logs; contact platform team for assistance if needed | lag |
| MongoDBHighOperations | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB operation rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |
| MongoDBHighPageFaults | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB page fault rate high  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |
| MongoDBConnectionSaturation | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB connection pool near saturation  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |

---

## Nginx Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| NginxHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Nginx connection threshold exceeded  | Check connection pool configuration and potential leaks; consider increasing max connections | connections |
| NginxHighConnectionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical Nginx connection saturation  | Check connection pool configuration and potential leaks; consider increasing max connections | connections |
| NginxRequestRateSpike | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: request rate spike  | Check alert metrics and review related logs; contact platform team for assistance if needed | rate |
| NginxRequestRateSpikeCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical request rate  | Check alert metrics and review related logs; contact platform team for assistance if needed | rate |
| NginxConnectionBacklog | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: connection backlog building  | Check alert metrics and review related logs; contact platform team for assistance if needed | connections |
| NginxConnectionBacklogCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical connection backlog  | Check alert metrics and review related logs; contact platform team for assistance if needed | connections |

---

## Operational Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| TenantSilentWarning | none | Warning alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppressed | Check alert metrics and review related logs; contact platform team for assistance if needed | alerts |
| TenantSilentCritical | none | Critical alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppresse | Check alert metrics and review related logs; contact platform team for assistance if needed | alerts |
| TenantSeverityDedupEnabled | none | Warning notifications for {{ $labels.tenant }} will be suppressed when critical fires for the same m | Check alert metrics and review related logs; contact platform team for assistance if needed | notifications |
| TenantConfigEvent | warning | Timed config for tenant {{ $labels.tenant }} has expired and auto-deactivated. Event: {{ $labels.eve | Check alert metrics and review related logs; contact platform team for assistance if needed | config |

---

## Oracle Database Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| OracleDatabaseDown | critical |  | Immediately check server status and network connectivity; review system logs |  |
| OracleHighActiveSessions | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle active sessions elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |
| OracleTablespaceAlmostFull | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle tablespace nearing capacity  | Check alert metrics and review related logs; contact platform team for assistance if needed | usage |
| OracleHighWaitTime | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle wait time elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | time |
| OracleHighProcessCount | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle process count high  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |
| OracleHighPGAUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle PGA usage high  | Check alert metrics and review related logs; contact platform team for assistance if needed | allocated |
| OracleHighSessionUtilization | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle session limit approaching  | Check alert metrics and review related logs; contact platform team for assistance if needed | utilization |

---

## PostgreSQL Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| PostgreSQLDown | critical | pg_up=0 for 15s on {{ $labels.instance }} | Immediately check server status and network connectivity; review system logs | pg_up |
| PostgreSQLExporterAbsent | critical | No pg_up metric found for 30s | Verify component is running and configured correctly; check component logs | pg_up |
| PostgreSQLHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: connection usage high  | Check connection pool configuration and potential leaks; consider increasing max connections | value |
| PostgreSQLHighConnectionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical connection saturation  | Check connection pool configuration and potential leaks; consider increasing max connections | value |
| PostgreSQLHighReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: replication lag elevated  | Check replication status and network connectivity; inspect queue buildup | value |
| PostgreSQLHighReplicationLagCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical replication lag  | Check replication status and network connectivity; inspect queue buildup | value |
| PostgreSQLHighDeadlocks | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: deadlocks detected  | Analyze deadlock query logs, adjust application logic to reduce contention; consider increasing lock timeout | value |
| PostgreSQLHighRollbackRatio | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: high rollback ratio  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |
| PostgreSQLRecentRestart | info | PostgreSQL uptime is only {{ $value | printf "%.0f" }}s (< 5 min) | Check alert metrics and review related logs; contact platform team for assistance if needed | uptime |

---

## RabbitMQ Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| RabbitMQExporterAbsent | critical | No rabbitmq_identity_info metric found for 30s | Verify component is running and configured correctly; check component logs | rabbitmq_identity_info |
| RabbitMQHighQueueDepth | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: queue depth threshold exceeded  | Check alert metrics and review related logs; contact platform team for assistance if needed | ready |
| RabbitMQHighQueueDepthCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical queue depth  | Check alert metrics and review related logs; contact platform team for assistance if needed | ready |
| RabbitMQHighMemory | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: RabbitMQ memory usage high  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | used |
| RabbitMQHighMemoryCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical RabbitMQ memory  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | used |
| RabbitMQHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: RabbitMQ connection count high  | Check connection pool configuration and potential leaks; consider increasing max connections | connections |
| RabbitMQLowConsumers | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: RabbitMQ consumer count low  | Check alert metrics and review related logs; contact platform team for assistance if needed | consumers |
| RabbitMQHighUnackedMessages | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: unacked messages piling up  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |

---

## Redis Rule Pack

| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |
|---|---|---|---|---|
| RedisDown | critical |  | Immediately check server status and network connectivity; review system logs |  |
| RedisHighMemory | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Redis memory threshold breached  | Check resource consumption and optimize configuration; consider increasing memory or enabling compression | usage |
| RedisHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Redis connection count high  | Check connection pool configuration and potential leaks; consider increasing max connections | value |
| RedisHighKeyEvictions | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: key eviction rate elevated  | Check alert metrics and review related logs; contact platform team for assistance if needed | value |
| RedisReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Redis replication lag  | Check alert metrics and review related logs; contact platform team for assistance if needed | lag |
| RedisLowHitRatio | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: low cache hit ratio  | Check alert metrics and review related logs; contact platform team for assistance if needed | ratio |

---
