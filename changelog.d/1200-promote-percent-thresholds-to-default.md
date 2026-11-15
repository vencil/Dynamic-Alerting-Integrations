---
section: Changed
topic: rule-packs
issues: [1200]
created: 2026-11-14T17:12:34+00:00
---
- **⚠️ breaking：三個百分比閾值改為預設出貨（Helm chart；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：`es_disk_usage_percent`（85）、`es_heap_usage_percent`（85）、`oracle_tablespace_used_percent`（85）現在寫在 chart 的 `thresholdConfig.defaults`，依據登記在 threshold registry 的 `validation`。升級後，部署了對應 rule pack、有 exporter 指標的租戶，數值超過 85 時會收到 `ElasticsearchHighDiskUsage`、`ElasticsearchHighHeapUsage`、`OracleTablespaceAlmostFull` 的 warning；它們的 `_critical` 也變成租戶可以直接設定。不想啟用：在 Helm values 把該 key 設成 `null`，或由租戶寫 `<key>: disable`。升級前可對自己的 Prometheus 重播，看多少樣本點超過這個值：`da-tools backtest --tenant <id> --metric <key> --new-value <值> --prometheus <url>`（近似值：不模擬 `for:` 與維護期）。自備 conf.d 的部署不會自動拿到這三個 key；根平台檔已給同一個 pack 其他 key 值的，`validate-config` 的 `unsupplied_defaults` 列會點名。`jvm_memory` 沒有升：它的規則逐記憶體池相除（[#3928](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3928)）。`rabbitmq_node_mem_percent` 另見 #3929 的條目。
