---
section: Changed
topic: rule-packs
issues: [1200]
created: 2026-11-14T17:12:34+00:00
---
- **⚠️ breaking：三個百分比閾值改為預設出貨（Helm chart；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：`es_disk_usage_percent`（85）、`es_heap_usage_percent`（85）、`oracle_tablespace_used_percent`（85）現在寫在 chart 的 `thresholdConfig.defaults`。升級後，有 Elasticsearch／Oracle exporter 的租戶會開始收到 `ElasticsearchHighDiskUsage`、`ElasticsearchHighHeapUsage`、`OracleTablespaceAlmostFull` 的 warning；它們的 `_critical` 也變成租戶可以直接設定。不想啟用：在 Helm values 把該 key 設成 `null`，或由租戶寫 `<key>: disable`。升級前想知道會多響多少，可以對自己的 Prometheus 重播：`da-tools backtest --tenant <id> --metric <key> --new-value <值> --prometheus <url>`。用 GitOps 或自備 conf.d 的部署不會自動拿到這三個 key；根平台檔已經給同一個 pack 其他 key 值的，`validate-config` 的 `unsupplied_defaults` 列會點名。每個值的依據登記在 threshold registry 的 `validation`。另外兩個候選沒有升：`jvm_memory` 的規則逐記憶體池相除（[#3928](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3928)），`rabbitmq_node_mem_percent` 查到的來源都用 90 而不是 80（[#3929](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3929)）。
