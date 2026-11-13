---
section: Added
topic: rule-packs
issues: [3658]
created: 2026-11-10T15:24:09+00:00
---
- **Redis、MongoDB、Elasticsearch、Oracle、DB2、ClickHouse、RabbitMQ 補上 16 組 critical 層（rule-packs；[#3658](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3658)）**：這些包的連線數、記憶體／heap、磁碟、複製延遲、搜尋延遲、待處理工作、鎖等待等門檻多了 `<base>_critical` 鍵與對應的 `*Critical` 告警，互動式 scaffold 會寫出它們。出貨值（例如 `redis_connected_clients_critical` 300、`es_disk_usage_percent_critical` 90）只是參考起點，請依自己的容量調整。這 16 組的 warning 告警都帶 `metric_group` label（值為門檻鍵名；其中 14 條是新加的）。嚴重度去重預設開啟，所以設了 `<base>_critical` 的租戶，critical 觸發後同組的 warning 預設會被抑制；不要的話設 `_severity_dedup: "disable"`。`mongodb_opcounters_rate`、`clickhouse_queries_rate`、`redis_evicted_keys_rate` 這三個吞吐量鍵刻意不補；租戶自行寫這三個鍵的 `_critical` 時，`validate-config` 的 `no_alert` 會提出建議。
