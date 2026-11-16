---
section: Removed
topic: rule-packs
issues: [1320]
created: 2026-11-16T23:45:00+00:00
---
- **⚠️ ClickHouse pack 移除 `ClickHouseHighMemoryUsage` 與閾值 key `clickhouse_memory_tracking_bytes`（rule-packs；[#1320](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1320)）**：8 GiB 的絕對位元組門檻在不同部署之間沒有意義，而在 Kubernetes 上，kubernetes pack 預設出貨的 `container_memory`（容器記憶體 limit 的百分比，85）已經涵蓋同一件事。ClickHouse 預設把自己的伺服器上限設在 cgroup 上限的 0.9，所以容器的 85% 約等於 ClickHouse 允許用量的 94%。這個 key 已列入 registry 的 `retired_keys`：寫了它的 conf.d 會被判為已退役，提示改用 `container_memory`。注意單位由位元組改成百分比，數值不能直接照抄。遷移：Alertmanager 中 `alertname="ClickHouseHighMemoryUsage"` 的 matcher 改為 `alertname=~"PodContainerHighMemory(Critical)?"`。非 Kubernetes 部署從此沒有 ClickHouse 記憶體告警，需要的話請用 custom alert 自行定義。
