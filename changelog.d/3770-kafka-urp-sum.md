---
section: Fixed
topic: rule-packs
issues: [3770]
created: 2026-11-12T15:51:49+00:00
---
- **Kafka 的 under-replicated partitions 告警改成依 partition 數觸發（rule-packs；[#3770](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3770)）**：kafka_exporter 的 `kafka_topic_partition_under_replicated_partition` 是逐 partition 的 0/1 旗標，舊的 `max by(tenant)` 只會得到 0 或 1，所以門檻 ≥ 1 的 `KafkaUnderReplicatedPartitions`／`…Critical` 永遠不響。改為先依 `topic`、`partition` 去重（多個 exporter 副本回報同一 partition 只算一次）再 `sum by(tenant)`。⚠️ 升級後這兩條告警會開始依門檻觸發，叢集裡原本就存在的 under-replication 可能在升級當下發出告警。critical 告警（`for: 30s`）在 broker 滾動重啟期間也可能依 under-replicated partition 數觸發；設了 `kafka_under_replicated_partitions_critical` 的租戶，必要時調高門檻或在重啟時開維護窗。recording rule 改名見同號的 Removed 條目。threshold registry 同步拿掉這個 key 的聚合錯誤 blocker，它改列為驗證候選；chart 仍不出貨這個 key 的預設值。
