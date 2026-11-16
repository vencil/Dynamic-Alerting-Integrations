---
section: Removed
topic: rule-packs
issues: [1320]
created: 2026-11-16T23:45:00+00:00
---
- **⚠️ ClickHouse pack 移除 `ClickHouseHighMemoryUsage` 與閾值 key `clickhouse_memory_tracking_bytes`（rule-packs；[#1320](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1320)）**：8 GiB 的絕對位元組門檻在不同部署之間沒有意義，而 kubernetes pack 預設出貨的 `container_memory`（容器記憶體 limit 的 85%）已涵蓋同一件事：ClickHouse 21.12 起以 cgroup 上限計、預設用其 0.9，85% 約為它允許用量的 94%。前提是啟用 kubernetes pack、pod 在租戶 namespace 且設了 memory limit；沒設 limit 的 ClickHouse pod 不再有記憶體告警。這個 key 已列入 registry 的 `retired_keys`：寫了它的 conf.d 會被判為已退役，提示改用 `container_memory`。注意單位由位元組改成百分比，數值不能直接照抄。遷移：Alertmanager 中 `alertname="ClickHouseHighMemoryUsage"` 的 matcher 改為 `alertname=~"PodContainerHighMemory(Critical)?"`。不符合前提（含非 Kubernetes 部署）而需要時，請用 custom alert 自行定義。
