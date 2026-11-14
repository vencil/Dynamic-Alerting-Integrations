---
section: Removed
topic: rule-packs
issues: [3770]
created: 2026-11-12T16:40:00+00:00
---
- **⚠️ BREAKING：移除 recording rule `tenant:kafka_under_replicated_partitions:max`，改為 `tenant:kafka_under_replicated_partitions:sum`（[#3770](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3770)）**：值的語意也跟著變：舊名只會是 0 或 1（「有沒有任何 partition 複製不足」），新名是複製不足的 partition 數（跨 exporter 副本去重）。遷移：外部 dashboard、federation 查詢或自建告警引用舊名的，改成 `:sum`；原本拿它當布林用的（`== 1`），改成 `> 0`。平台內的告警、observed map 已一併改好；租戶設定的閾值 key `kafka_under_replicated_partitions` 不變。
