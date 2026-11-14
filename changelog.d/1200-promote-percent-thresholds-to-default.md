---
section: Changed
topic: rule-packs
issues: [1200]
created: 2026-11-14T16:48:28+00:00
---
- **⚠️ breaking：四個百分比閾值改為預設出貨（Helm chart；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：`es_disk_usage_percent`（85）、`es_heap_usage_percent`（85）、`jvm_memory`（80）、`oracle_tablespace_used_percent`（85）現在寫在 chart 的 `thresholdConfig.defaults`。升級後，有 Elasticsearch／JVM／Oracle exporter 的租戶會開始收到 `ElasticsearchHighDiskUsage`、`ElasticsearchHighHeapUsage`、`JVMMemoryPressure`、`OracleTablespaceAlmostFull` 的 warning；它們的 `_critical` 也變成租戶可以直接設定。不想啟用：在 Helm values 把該 key 設成 `null`，或由租戶寫 `<key>: disable`。升級前想知道會多響多少，可以對自己的 Prometheus 重播：`da-tools backtest --tenant <id> --metric <key> --new-value <值> --prometheus <url>`。用 GitOps 或自備 conf.d 的部署不會自動拿到這四個 key，`validate-config` 的 `unsupplied_defaults` 列會點名。每個值的依據（廠商與上游來源）登記在 threshold registry 的 `validation`。`rabbitmq_node_mem_percent` 沒有升：查到的來源都用 90，不是 80。
