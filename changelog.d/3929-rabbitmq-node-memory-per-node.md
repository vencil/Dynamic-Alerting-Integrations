---
section: Changed
topic: rule-packs
issues: [3929]
created: 2026-11-14T19:03:23+00:00
---
- **⚠️ breaking：RabbitMQ 節點記憶體告警改成逐節點量，預設值 80 → 90 並預設出貨（rule pack、Helm chart；[#3929](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3929)）**：`tenant:rabbitmq_node_mem_percent:ratio` 原本拿用量最大的節點除以上限最大的節點，節點上限不一致時低報；現在每台自己相除、取最滿的一台。上限讀到 0 的節點不再算進來（原本會讀成極大值並送出 critical，現在這條告警對它靜默）。除了 kbudde exporter 的 `rabbitmq_node_mem_used`／`rabbitmq_node_mem_limit`，也讀內建 `rabbitmq_prometheus` plugin 的 `rabbitmq_process_resident_memory_bytes`／`rabbitmq_resident_memory_limit_bytes`，只裝 plugin 的叢集原本沒有這條告警。`RabbitMQHighMemory` 與 `RabbitMQHighMemoryCritical` 的 `for:` 由 30s 改為 2m。`rabbitmq_node_mem_percent` 的預設值改為 90 並寫進 chart 的 `thresholdConfig.defaults`：升級後，部署了 RabbitMQ rule pack 的租戶在節點超過 90% 時會收到 warning；租戶已經寫了 `rabbitmq_node_mem_percent_critical` 的，那一列也開始送出。不想啟用：在 Helm values 把該 key 設成 `null`，或由租戶寫 `rabbitmq_node_mem_percent: disable`。自備 conf.d 的部署沿用自己的值，但量法與 `for:` 跟著 rule pack 改變。
