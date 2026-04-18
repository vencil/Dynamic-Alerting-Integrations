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
| ClickHouseDown | critical |  | 立即檢查伺服器狀態、網路連線；查看系統日誌 |  |
| ClickHouseHighQueryRate | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse query rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 |  |
| ClickHouseHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse connections exceeded  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 |  |
| ClickHouseHighPartCount | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse partition merge pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 |  |
| ClickHouseReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse replication queue high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 |  |
| ClickHouseHighMemoryUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ClickHouse memory usage high  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 |  |
| ClickHouseHighFailedQueryRate | warning |  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 |  |

---

## DB2 Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| DB2DatabaseDown | critical |  | 立即檢查伺服器狀態、網路連線；查看系統日誌 |  |
| DB2HighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 connection usage high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | value |
| DB2LowBufferpoolHitRatio | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 bufferpool hit ratio low  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | ratio |
| DB2HighLogUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 transaction log usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | usage |
| DB2HighDeadlockRate | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 deadlock rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |
| DB2TablespaceAlmostFull | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 tablespace nearing capacity  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | usage |
| DB2HighSortOverflow | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: DB2 sort overflow ratio high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | overflow |

---

## Elasticsearch Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| ElasticsearchClusterRed | critical | Cluster health is RED  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | health |
| ElasticsearchClusterYellow | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES cluster YELLOW  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | replica |
| ElasticsearchHighHeapUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES heap usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | heap |
| ElasticsearchHighDiskUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES disk usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | usage |
| ElasticsearchHighSearchLatency | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES search latency elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | search |
| ElasticsearchUnassignedShards | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES unassigned shards  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |
| ElasticsearchPendingTasks | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: ES pending cluster tasks  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |

---

## JVM Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| JVMHighGCPause | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: GC pause elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | pause |
| JVMHighGCPauseCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical GC pause  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | pause |
| JVMMemoryPressure | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: heap memory pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | usage |
| JVMMemoryPressureCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical heap pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | usage |
| JVMThreadPoolExhaustion | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: thread pool saturation  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | threads |
| JVMThreadPoolExhaustionCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical thread exhaustion  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | threads |
| JVMPerformanceDegraded | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: multi-signal JVM degradation  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | pause |

---

## Kafka Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| KafkaExporterAbsent | critical | No kafka_brokers metric found for 30s | 確認相關元件已啟動、配置正確；檢查元件日誌 | kafka_brokers |
| KafkaHighConsumerLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: consumer lag elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | lag |
| KafkaHighConsumerLagCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical consumer lag  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | lag |
| KafkaUnderReplicatedPartitions | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: under-replicated partitions  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |
| KafkaUnderReplicatedPartitionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical under-replication  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |
| KafkaNoActiveController | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: no active Kafka controller  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | controllers |
| KafkaLowBrokerCount | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: broker count below minimum  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | brokers |
| KafkaHighRequestRate | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: message rate threshold exceeded  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rate |
| KafkaHighRequestRateCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical message rate  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rate |

---

## Kubernetes Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| PodContainerHighCPU | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: container CPU pressure  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | container |
| PodContainerHighMemory | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: container memory pressure  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | container |
| ContainerCrashLoop | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: crash loop detected  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |
| ContainerImagePullFailure | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: image pull failing  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |

---

## MariaDB Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| MariaDBDown | critical | mysql_up=0 for 15s on {{ $labels.instance }} | 立即檢查伺服器狀態、網路連線；查看系統日誌 | mysql_up |
| MariaDBExporterAbsent | critical | No mysql_up metric found for 30s | 確認相關元件已啟動、配置正確；檢查元件日誌 | mysql_up |
| MariaDBHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: connection threshold breached  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | value |
| MariaDBHighConnectionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical connection saturation  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | value |
| MariaDBSystemBottleneck | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: CPU + connections both exceeded  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | connections |
| MariaDBRecentRestart | info | Uptime is only {{ $value }}s (< 5 min) | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | is |
| MariaDBHighSlowQueries | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: slow query rate elevated  | 檢查慢查詢日誌，找出優化候選；考慮調整相關參數 | value |
| MariaDBHighAbortedConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: aborted connection rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |

---

## MongoDB Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| MongoDBDown | critical |  | 立即檢查伺服器狀態、網路連線；查看系統日誌 |  |
| MongoDBHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB connection threshold breached  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | value |
| MongoDBReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB replication lag  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | lag |
| MongoDBHighOperations | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB operation rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |
| MongoDBHighPageFaults | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB page fault rate high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |
| MongoDBConnectionSaturation | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: MongoDB connection pool near saturation  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |

---

## Nginx Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| NginxHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Nginx connection threshold exceeded  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | connections |
| NginxHighConnectionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical Nginx connection saturation  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | connections |
| NginxRequestRateSpike | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: request rate spike  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rate |
| NginxRequestRateSpikeCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical request rate  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | rate |
| NginxConnectionBacklog | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: connection backlog building  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | connections |
| NginxConnectionBacklogCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical connection backlog  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | connections |

---

## Operational Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| TenantSilentWarning | none | Warning alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppressed | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | alerts |
| TenantSilentCritical | none | Critical alerts for tenant {{ $labels.tenant }} will be recorded in TSDB but notifications suppresse | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | alerts |
| TenantSeverityDedupEnabled | none | Warning notifications for {{ $labels.tenant }} will be suppressed when critical fires for the same m | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | notifications |
| TenantConfigEvent | warning | Timed config for tenant {{ $labels.tenant }} has expired and auto-deactivated. Event: {{ $labels.eve | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | config |

---

## Oracle Database Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| OracleDatabaseDown | critical |  | 立即檢查伺服器狀態、網路連線；查看系統日誌 |  |
| OracleHighActiveSessions | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle active sessions elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |
| OracleTablespaceAlmostFull | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle tablespace nearing capacity  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | usage |
| OracleHighWaitTime | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle wait time elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | time |
| OracleHighProcessCount | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle process count high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |
| OracleHighPGAUsage | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle PGA usage high  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | allocated |
| OracleHighSessionUtilization | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Oracle session limit approaching  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | utilization |

---

## PostgreSQL Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| PostgreSQLDown | critical | pg_up=0 for 15s on {{ $labels.instance }} | 立即檢查伺服器狀態、網路連線；查看系統日誌 | pg_up |
| PostgreSQLExporterAbsent | critical | No pg_up metric found for 30s | 確認相關元件已啟動、配置正確；檢查元件日誌 | pg_up |
| PostgreSQLHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: connection usage high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | value |
| PostgreSQLHighConnectionsCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical connection saturation  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | value |
| PostgreSQLHighReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: replication lag elevated  | 檢查複寫狀態、網路連線；檢查複寫隊列堆積情況 | value |
| PostgreSQLHighReplicationLagCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical replication lag  | 檢查複寫狀態、網路連線；檢查複寫隊列堆積情況 | value |
| PostgreSQLHighDeadlocks | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: deadlocks detected  | 分析死鎖查詢日誌、調整應用邏輯減少衝突；考慮增加鎖定超時時間 | value |
| PostgreSQLHighRollbackRatio | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: high rollback ratio  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |
| PostgreSQLRecentRestart | info | PostgreSQL uptime is only {{ $value | printf "%.0f" }}s (< 5 min) | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | uptime |

---

## RabbitMQ Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| RabbitMQExporterAbsent | critical | No rabbitmq_identity_info metric found for 30s | 確認相關元件已啟動、配置正確；檢查元件日誌 | rabbitmq_identity_info |
| RabbitMQHighQueueDepth | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: queue depth threshold exceeded  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | ready |
| RabbitMQHighQueueDepthCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical queue depth  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | ready |
| RabbitMQHighMemory | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: RabbitMQ memory usage high  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | used |
| RabbitMQHighMemoryCritical | critical | [{{ $labels.tier }}] {{ $labels.tenant }}: critical RabbitMQ memory  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | used |
| RabbitMQHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: RabbitMQ connection count high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | connections |
| RabbitMQLowConsumers | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: RabbitMQ consumer count low  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | consumers |
| RabbitMQHighUnackedMessages | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: unacked messages piling up  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |

---

## Redis Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| RedisDown | critical |  | 立即檢查伺服器狀態、網路連線；查看系統日誌 |  |
| RedisHighMemory | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Redis memory threshold breached  | 檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮 | usage |
| RedisHighConnections | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Redis connection count high  | 檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數 | value |
| RedisHighKeyEvictions | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: key eviction rate elevated  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | value |
| RedisReplicationLag | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: Redis replication lag  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | lag |
| RedisLowHitRatio | warning | [{{ $labels.tier }}] {{ $labels.tenant }}: low cache hit ratio  | 檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊 | ratio |

---
