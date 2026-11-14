---
section: Changed
topic: rule-packs
issues: [3929]
created: 2026-11-14T19:03:23+00:00
---
- **⚠️ breaking：RabbitMQ 節點記憶體告警改成逐節點量，預設值 80 → 90 並預設出貨（rule pack、Helm chart；[#3929](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3929)）**：`tenant:rabbitmq_node_mem_percent:ratio` 原本拿「用量最大的節點」除以「上限最大的節點」，節點上限不一致時會低報（57/60 與 10/100 兩台讀成 57%）；現在每台自己相除、取最滿的一台（95%）。上限讀到 0 的節點不再算進來。除了 kbudde exporter 的 `rabbitmq_node_mem_used`／`rabbitmq_node_mem_limit`，也讀內建 `rabbitmq_prometheus` plugin 的 `rabbitmq_process_resident_memory_bytes`／`rabbitmq_resident_memory_limit_bytes`，只裝 plugin 的叢集原本沒有這條告警。`RabbitMQHighMemory` 與 `RabbitMQHighMemoryCritical` 的 `for:` 由 30s 改為 2m。`rabbitmq_node_mem_percent` 的預設值改為 90 並寫進 chart 的 `thresholdConfig.defaults`：升級後有 RabbitMQ exporter 的租戶會開始收到 warning，`rabbitmq_node_mem_percent_critical` 也變成租戶可以直接設定。不想啟用：在 Helm values 把該 key 設成 `null`，或由租戶寫 `rabbitmq_node_mem_percent: disable`。自備 conf.d 而且沿用 80 的部署不受預設值影響，但量法與 `for:` 會跟著 rule pack 改變。
