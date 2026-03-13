---
title: "Rule Pack 告警參考指南 (Alert Reference Guide)"
tags: [alerts, reference, rule-packs]
audience: [tenant, sre]
version: v1.12.0
lang: zh
---
# Rule Pack 告警參考指南 (Alert Reference Guide)

本文件為租戶提供各 Rule Pack 中所有告警的統一參考，包括告警含義、觸發條件和建議動作。

**注意**: 本指南僅涵蓋**使用者導向的閾值告警** (threshold alerts)。Operational Rule Pack 的 sentinel 告警為平台內部控制機制，不需要租戶操作。

---

## MariaDB Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| MariaDBDown | critical | 實例連接故障 (mysql_up=0 持續 15 秒) | 立即檢查資料庫伺服器狀態、網路連線、防火牆規則；查看 mariadb 日誌 | mysql_up |
| MariaDBExporterAbsent | critical | mysqld_exporter 缺失 (無 mysql_up 指標 30 秒) | 確認 exporter 容器已啟動、配置正確；檢查 exporter 日誌 | mysql_up |
| MariaDBHighConnections | warning | 連線數超過警告閾值 (預設 80%) | 檢查連線池配置、應用連線是否有洩漏；考慮增加 max_connections | mysql_global_status_threads_connected |
| MariaDBHighConnectionsCritical | critical | 連線數超過 critical 閾值 | 立即介入，檢查活躍連線、殺掉閒置連線；考慮應用端限流 | mysql_global_status_threads_connected |
| MariaDBSystemBottleneck | critical | 連線數**且** CPU 同時超過警告閾值 | 多資源瓶頸，立即升級；同時檢查連線和 CPU 壓力源 | mysql_global_status_threads_connected, mysql_global_status_threads_running |
| MariaDBRecentRestart | info | 實例最近重啟 (uptime < 5 分鐘) | 通知訊息，檢查是否有異常重啟；查看系統日誌和 mariadb 日誌 | mysql_global_status_uptime |
| MariaDBHighSlowQueries | warning | 慢查詢速率高 (> 1 query/sec) | 檢查慢查詢日誌，找出優化候選；考慮調整 long_query_time 參數 | mysql_global_status_slow_queries |
| MariaDBHighAbortedConnections | warning | 已中止連線率高 (> 5 conn/sec) | 檢查用戶端連線、驗證授權問題；查看應用端日誌 | mysql_global_status_aborted_connects |

---

## PostgreSQL Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| PostgreSQLDown | critical | 實例連接故障 (pg_up=0 持續 15 秒) | 立即檢查 PostgreSQL 伺服器狀態、網路連線；查看 postgresql 日誌 | pg_up |
| PostgreSQLExporterAbsent | critical | postgres_exporter 缺失 (無 pg_up 指標 30 秒) | 確認 exporter 容器已啟動、配置正確；檢查 exporter 日誌 | pg_up |
| PostgreSQLHighConnections | warning | 連線數超過警告閾值 (預設 80% of max_connections) | 檢查活躍查詢、應用連線池設定；考慮增加 max_connections 或關閉閒置連線 | pg_stat_activity_count |
| PostgreSQLHighConnectionsCritical | critical | 連線數超過 critical 閾值 (預設 90%) | 立即介入，檢查長時間運行查詢；使用 pg_terminate_backend 終止閒置連線 | pg_stat_activity_count |
| PostgreSQLHighReplicationLag | warning | 複寫延遲超過警告閾值 (預設 30 秒) | 檢查複寫狀態、主副本網路連線；檢查 WAL 段堆積 | pg_replication_lag |
| PostgreSQLHighReplicationLagCritical | critical | 複寫延遲超過 critical 閾值 (預設 60 秒) | 立即檢查副本健康狀態、WAL 磁碟空間；考慮手動追趕或重新同步 | pg_replication_lag |
| PostgreSQLHighDeadlocks | warning | 死鎖發生頻率高 (> 1/sec over 5m) | 分析死鎖查詢日誌、調整應用邏輯減少衝突；考慮增加鎖定超時時間 | pg_stat_database_deadlocks |
| PostgreSQLHighRollbackRatio | warning | 事務回滾比例高 (> 某閾值) | 檢查應用錯誤率、約束違反；調查交易失敗根本原因 | pg_stat_database_xact_rollback |
| PostgreSQLRecentRestart | info | 實例最近重啟 (uptime < 5 分鐘) | 通知訊息，檢查是否有異常重啟；查看系統日誌 | pg_postmaster_start_time_seconds |

---

## Redis Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| RedisDown | critical | 實例連接故障 (redis_up=0 持續 15 秒) | 立即檢查 Redis 伺服器狀態、網路連線；查看 redis 日誌 | redis_up |
| RedisHighMemory | warning | 記憶體使用超過警告閾值 | 檢查鍵值對數量、淘汰策略；考慮增加記憶體或啟用數據壓縮 | redis_memory_used_bytes |
| RedisHighConnections | warning | 連線數超過警告閾值 (預設 500) | 檢查應用連線池、是否有連線洩漏；增加 maxclients 設定 | redis_connected_clients |
| RedisHighKeyEvictions | warning | 鍵值逐出速率高 (> 100 keys/sec) | 記憶體壓力，檢查淘汰策略 (LRU/LFU)；考慮增加記憶體或優化數據結構 | redis_evicted_keys_total |
| RedisReplicationLag | warning | 複寫副本延遲高 | 檢查副本伺服器健康、網路延遲；查看複寫隊列大小 | redis_connected_slave_lag_seconds |
| RedisLowHitRatio | warning | 鍵空間命中率低 (< 某閾值) | 應用查詢效率低下，檢查熱數據訪問模式；可能需要優化應用邏輯或增加記憶體 | redis_keyspace_hits_total, redis_keyspace_misses_total |

---

## MongoDB Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| MongoDBDown | critical | 實例連接故障 (mongodb_up=0 持續 15 秒) | 立即檢查 MongoDB 伺服器狀態、複寫集狀態；查看 mongodb 日誌 | mongodb_up |
| MongoDBHighConnections | warning | 活躍連線超過警告閾值 (預設 500) | 檢查應用連線池、連線是否有洩漏；考慮增加 maxPoolSize | mongodb_connections{state="current"} |
| MongoDBReplicationLag | warning | 複寫延遲超過警告閾值 (預設 10 秒) | 檢查複寫成員健康、網路延遲；查看 oplog 大小和追趕進度 | mongodb_mongod_replset_member_replication_lag |
| MongoDBHighOperations | warning | 操作速率高 (超過閾值) | 工作負載繁重，檢查是否需要拆分或優化查詢；考慮增加 CPU/記憶體 | mongodb_opcounters_total |
| MongoDBHighPageFaults | warning | 頁面故障率高 (> 100/sec) | 記憶體不足，工作集大於可用記憶體；增加記憶體或優化查詢索引 | mongodb_extra_info_page_faults_total |
| MongoDBConnectionSaturation | warning | 連線池飽和 (> 80% usage) | 接近連線限制，檢查應用行為；考慮增加連線池大小或實施限流 | mongodb_connections |

---

## Elasticsearch Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| ElasticsearchClusterRed | critical | 叢集狀態為 RED (數據不可用) | 立即升級，可能有主分片遺失；檢查節點可用性、磁碟空間 | elasticsearch_cluster_health_status |
| ElasticsearchClusterYellow | warning | 叢集狀態為 YELLOW (副本分片未分配) | 檢查叢集健康狀態、節點可用性；考慮重新分配副本或增加節點 | elasticsearch_cluster_health_status |
| ElasticsearchHighHeapUsage | warning | JVM heap 使用超過警告閾值 (預設 85%) | 檢查大型查詢、聚合操作；調整 JVM 堆大小或優化查詢 | elasticsearch_jvm_memory_used_bytes{area="heap"} |
| ElasticsearchHighDiskUsage | warning | 磁碟空間使用超過警告閾值 (預設 80%) | 清理舊索引、增加磁碟容量；檢查索引大小和分片分佈 | elasticsearch_filesystem_data_size_bytes |
| ElasticsearchHighSearchLatency | warning | 搜尋延遲超過警告閾值 (預設 500ms) | 檢查查詢複雜度、索引大小；調整 refresh_interval 或優化映射 | elasticsearch_indices_search_query_time_seconds |
| ElasticsearchUnassignedShards | warning | 未分配的分片 | 叢集無法恢復分片，檢查節點可用性；手動分配或重新啟動節點 | elasticsearch_cluster_health_unassigned_shards |
| ElasticsearchPendingTasks | warning | 待處理叢集任務堆積 | 主節點負載高，檢查是否有故障節點；考慮優化或分割大型操作 | elasticsearch_cluster_health_number_of_pending_tasks |

---

## Oracle Database Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| OracleDatabaseDown | critical | 實例連接故障 (oracledb_up=0) | 立即檢查 Oracle 實例狀態、監聽器、網路；查看 alert.log | oracledb_up |
| OracleHighActiveSessions | warning | 活躍 session 超過警告閾值 (預設 200) | 檢查長時間運行查詢、鎖定情況；使用 v$session 分析會話 | oracledb_sessions_active |
| OracleTablespaceAlmostFull | warning | 表空間使用接近上限 (預設 85%) | 增加表空間大小、清理垃圾數據；檢查自動擴展設定 | oracledb_tablespace_used_percent |
| OracleHighWaitTime | warning | 等待時間率高 | 性能瓶頸，使用 v$session_wait 分析等待類型；調整參數或優化查詢 | oracledb_wait_time_seconds_total |
| OracleHighProcessCount | warning | 進程數超過警告閾值 | 檢查進程使用情況；優化應用邏輯減少後台進程數 | oracledb_process_count |
| OracleHighPGAUsage | warning | PGA 記憶體超過警告閾值 | 檢查大型排序、雜湊操作；調整 pga_aggregate_target 參數 | oracledb_pga_allocated_bytes |
| OracleHighSessionUtilization | warning | Session 限制接近上限 (> 85%) | 檢查並行會話數、清理閒置會話；考慮增加 processes 參數 | oracledb_sessions_active |

---

## DB2 Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| DB2DatabaseDown | critical | 實例連接故障 (db2_up=0) | 立即檢查 DB2 實例狀態、網路連線；查看診斷日誌 | db2_up |
| DB2HighConnections | warning | 活躍連線超過警告閾值 | 檢查應用連線池、連線洩漏；調整 maxagents 參數 | db2_connections_active |
| DB2LowBufferpoolHitRatio | warning | 緩衝池命中率低於警告閾值 (預設 0.95) | 缺衝池記憶體不足，調整緩衝池大小；檢查索引使用情況 | db2_bufferpool_hit_ratio |
| DB2HighLogUsage | warning | 交易日誌使用超過警告閾值 (預設 70%) | 大量活躍交易，檢查長時間運行的 DDL；調整日誌檔案大小 | db2_log_usage_percent |
| DB2HighDeadlockRate | warning | 死鎖發生頻率高 | 分析死鎖查詢、調整應用邏輯；增加鎖定超時時間 | db2_deadlocks_total |
| DB2TablespaceAlmostFull | warning | 表空間使用接近上限 | 增加表空間容量、清理數據；檢查自動擴展設定 | db2_tablespace_used_percent |
| DB2HighSortOverflow | warning | 排序溢出比例高 (> 5%) | SORTHEAP 參數不足，調整 SORTHEAP；考慮增加可用記憶體 | db2_sort_overflows |

---

## ClickHouse Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| ClickHouseDown | critical | 實例連接故障 (up=0 持續 2 分鐘) | 立即檢查 ClickHouse 伺服器狀態、網路連線；查看系統日誌 | up |
| ClickHouseHighQueryRate | warning | 查詢速率超過警告閾值 (預設 500 query/sec) | 高工作負載，檢查查詢複雜度；考慮新增節點或優化查詢 | ClickHouseProfileEvents_Query |
| ClickHouseHighConnections | warning | 活躍連線超過警告閾值 (預設 200) | 檢查應用連線池、連線是否洩漏；調整 max_concurrent_queries 參數 | ClickHouseMetrics_TCPConnection |
| ClickHouseHighPartCount | warning | 分片部分計數高 (合併壓力) | 寫入速率高，導致部分堆積；檢查合併進度或調整寫入策略 | ClickHouseAsyncMetrics_MaxPartCountForPartition |
| ClickHouseReplicationLag | warning | 複寫隊列大小超過警告閾值 | 複寫副本追趕不上主節點；檢查網路延遲或副本資源 | ClickHouseMetrics_ReplicatedSendQueueSize |
| ClickHouseHighMemoryUsage | warning | 記憶體使用超過警告閾值 (預設 8 GB) | 檢查大型查詢、聚合操作；調整記憶體限制或優化查詢 | ClickHouseMetrics_MemoryTracking |
| ClickHouseHighFailedQueryRate | warning | 查詢失敗速率高 (> 10/sec) | 檢查查詢日誌找出失敗原因；檢查磁碟空間、網路連線 | ClickHouseProfileEvents_FailedQuery |

---

## Kafka Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| KafkaExporterAbsent | critical | kafka_exporter 缺失 (無 kafka_brokers 30 秒) | 確認 exporter 容器已啟動、配置正確；檢查 exporter 日誌 | kafka_brokers |
| KafkaHighConsumerLag | warning | 消費者延遲超過警告閾值 (預設 1000) | 消費者追趕不上，檢查消費者應用狀態；增加消費者實例或優化消費邏輯 | kafka_consumergroup_lag_sum |
| KafkaHighConsumerLagCritical | critical | 消費者延遲超過 critical 閾值 | 立即升級，消費者嚴重落後；檢查消費者應用故障、網路問題 | kafka_consumergroup_lag_sum |
| KafkaUnderReplicatedPartitions | warning | 副本不足的分片 (未達到 in-sync 副本數) | 分片副本故障，檢查代理節點健康；檢查磁碟、網路問題 | kafka_topic_partition_under_replicated_partition |
| KafkaUnderReplicatedPartitionsCritical | critical | 副本不足分片數超過 critical 閾值 | 立即檢查代理故障、修復副本；數據可用性受威脅 | kafka_topic_partition_under_replicated_partition |
| KafkaNoActiveController | critical | 無活躍控制器 | 集群無主控制器，立即檢查代理狀態；重啟故障的主控制器 | kafka_controller_active_controller_count |
| KafkaLowBrokerCount | warning | 代理數低於預期最小值 (預設 3) | 代理故障，檢查代理健康狀態；考慮重新啟動或替換故障代理 | kafka_brokers |
| KafkaHighRequestRate | warning | 請求速率高 (超過警告閾值) | 高吞吐量工作負載，檢查是否需要擴展；監控代理 CPU 和磁碟使用 | kafka_server_brokertopicmetrics_messagesin_total |
| KafkaHighRequestRateCritical | critical | 請求速率超過 critical 閾值 | 立即升級，代理接近飽和；考慮增加代理或分片 | kafka_server_brokertopicmetrics_messagesin_total |

---

## RabbitMQ Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| RabbitMQExporterAbsent | critical | rabbitmq_exporter 缺失 (無 rabbitmq_identity_info 30 秒) | 確認 exporter 容器已啟動、配置正確；檢查 exporter 日誌 | rabbitmq_identity_info |
| RabbitMQHighQueueDepth | warning | 佇列深度超過警告閾值 (預設 100000 msg) | 消費者追趕不上，檢查消費者應用狀態；增加消費者或優化消費邏輯 | rabbitmq_queue_messages_ready |
| RabbitMQHighQueueDepthCritical | critical | 佇列深度超過 critical 閾值 | 立即升級，佇列堆積嚴重；立即增加消費者或檢查消費者故障 | rabbitmq_queue_messages_ready |
| RabbitMQHighMemory | warning | 記憶體使用超過警告閾值 (預設 80%) | 檢查佇列大小、消費速率；調整記憶體限制或增加代理 | rabbitmq_node_mem_used |
| RabbitMQHighMemoryCritical | critical | 記憶體使用超過 critical 閾值 (預設 95%) | 立即升級，代理接近故障；清除舊訊息或增加記憶體 | rabbitmq_node_mem_used |
| RabbitMQHighConnections | warning | 連線數超過警告閾值 (預設 1000) | 檢查應用連線池、是否有連線洩漏；調整連線限制或優化應用 | rabbitmq_connections |
| RabbitMQLowConsumers | warning | 消費者數低於預期最小值 | 消費者故障或不足，檢查消費者應用健康狀態；啟動更多消費者實例 | rabbitmq_queue_consumers |
| RabbitMQHighUnackedMessages | warning | 未確認訊息數高 | 消費者處理過慢，檢查消費者應用邏輯；增加消費者或優化業務邏輯 | rabbitmq_queue_messages_unacked |

---

## Kubernetes Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| PodContainerHighCPU | warning | 容器 CPU 使用超過警告閾值 (預設 80%) | 檢查容器應用負載；增加 CPU 限制或優化應用效能 | container_cpu_usage_seconds_total |
| PodContainerHighMemory | warning | 容器記憶體使用超過警告閾值 (預設 85%) | 檢查容器應用記憶體洩漏；增加記憶體限制或優化應用 | container_memory_working_set_bytes |
| ContainerCrashLoop | critical | 容器反覆崩潰 (CrashLoopBackOff 狀態) | 檢查容器日誌找出崩潰原因；檢查應用配置、依賴項、磁碟空間 | kube_pod_container_status_waiting_reason |
| ContainerImagePullFailure | warning | 容器鏡像拉取失敗 (ImagePullBackOff/InvalidImageName) | 檢查鏡像名稱、registry 可用性；驗證鏡像存在和認證配置 | kube_pod_container_status_waiting_reason |

---

## JVM Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| JVMHighGCPause | warning | 垃圾回收暫停時間率高 (預設 0.5 sec/5m) | 檢查應用記憶體洩漏；調整 JVM 堆大小或 GC 參數 | jvm_gc_pause_seconds_sum |
| JVMHighGCPauseCritical | critical | GC 暫停時間超過 critical 閾值 | 立即升級，應用性能嚴重下降；增加堆大小或使用低延遲 GC 演算法 | jvm_gc_pause_seconds_sum |
| JVMMemoryPressure | warning | 堆記憶體使用超過警告閾值 (預設 80%) | 檢查應用記憶體洩漏；增加堆大小或優化應用邏輯 | jvm_memory_used_bytes{area="heap"} |
| JVMMemoryPressureCritical | critical | 堆記憶體使用超過 critical 閾值 (預設 95%) | 立即升級，OOM 風險；立即增加堆大小或重啟應用 | jvm_memory_used_bytes{area="heap"} |
| JVMThreadPoolExhaustion | warning | 活躍線程超過警告閾值 (預設 500) | 線程池飽和，檢查請求隊列；增加線程池大小或優化應用 | jvm_threads_current |
| JVMThreadPoolExhaustionCritical | critical | 活躍線程超過 critical 閾值 | 立即升級，服務降級風險；立即增加線程池或實施限流 | jvm_threads_current |
| JVMPerformanceDegraded | critical | GC 暫停**且**堆記憶體同時超過警告閾值 | 應用性能嚴重下降，同時有記憶體和 GC 壓力；立即增加資源或優化應用 | jvm_gc_pause_seconds_sum, jvm_memory_used_bytes{area="heap"} |

---

## Nginx Rule Pack

| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |
|---|---|---|---|---|
| NginxHighConnections | warning | 活躍連線超過警告閾值 (預設 1000) | 流量高，檢查上游應用；增加 worker 進程或上游容量 | nginx_connections_active |
| NginxHighConnectionsCritical | critical | 活躍連線超過 critical 閾值 (預設 1500) | 立即升級，服務接近飽和；立即增加容量或實施限流 | nginx_connections_active |
| NginxRequestRateSpike | warning | 請求速率超過警告閾值 (預設 5000 req/s) | 流量激增，檢查是否為正常業務流量；監控上游應用負載 | nginx_http_requests_total |
| NginxRequestRateSpikeCritical | critical | 請求速率超過 critical 閾值 | 立即升級，可能 DDoS 攻擊或流量激增；檢查日誌、啟用限流或 DDoS 保護 | nginx_http_requests_total |
| NginxConnectionBacklog | warning | 等待連線 (backlog) 超過警告閾值 (預設 200) | 上游應用響應慢，連線堆積；檢查上游應用狀態、優化回應時間 | nginx_connections_waiting |
| NginxConnectionBacklogCritical | critical | 等待連線超過 critical 閾值 | 立即升級，服務降級；檢查上游應用故障、增加上游容量 | nginx_connections_waiting |

---

## 操作指南

### 告警分類

- **critical (紅色)**: 需要立即人工介入，可能影響服務可用性
- **warning (黃色)**: 需要注意但不緊急，為未來問題的預兆
- **info (藍色)**: 參考訊息，通常不需要動作

### 快速決策樹

1. **是否為計劃維護?** 使用 `_state_maintenance` 或 `_silent_mode` 暫時抑制告警
2. **是否需要立即動作?** 檢查嚴重度
   - critical → 立即升級給 on-call 工程師
   - warning → 在下一個工作時段內調查
3. **告警是否持續?** 確認不是瞬間尖峰
4. **根本原因是什麼?** 使用相關指標深入診斷

### 常見原因和快速修復

| 根本原因 | 適用告警 | 快速修復 |
|---|---|---|
| 記憶體洩漏 | High Memory, GC Pause | 重啟應用或增加記憶體；檢查代碼記憶體洩漏 |
| 連線池耗盡 | High Connections | 增加 max_connections；檢查連線洩漏 |
| 消費者故障 | High Queue Depth, Consumer Lag | 重啟消費者；檢查消費者應用日誌 |
| 磁碟滿 | High Disk Usage, Tablespace Full | 清理舊數據；增加磁碟容量 |
| 網路延遲 | Replication Lag | 檢查網路連線；增加帶寬或優化傳輸 |
| 應用故障 | High Error Rate, CrashLoop | 檢查應用日誌；回滾有問題的發布 |

---

## 進一步資源

- **詳細配置**: 參考 `_defaults.yaml` 和租戶 YAML 配置
- **Runbook**: 每個告警的 `runbook_url` 標籤提供詳細解決步驟
- **指標查詢**: 使用 Prometheus UI 查詢相關指標進行深入診斷
- **平台文件**: 參考 `docs/` 目錄下的詳細架構和運維指南
