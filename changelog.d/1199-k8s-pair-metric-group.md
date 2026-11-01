---
section: Fixed
topic: rule-packs
issues: [1199]
created: 2026-11-01T01:25:26+00:00
---
- **Kubernetes Rule Pack 的 4 對容器告警恢復 severity 去重（rule-packs；[#1199](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1199)）**：`PodContainerHighCPU`／`PodContainerHighMemory`／`PodContainerCPUThrottled`／`ContainerOOMKilled` 與各自的 `…Critical` 先前都沒有 `metric_group`，per-tenant 去重抑制規則對不上，critical 觸發時 warning 也照樣通知。現在每對帶一個固定值（`container_cpu`、`container_memory`、`container_cpu_throttle`、`container_oom_killed`），critical 觸發時只送 critical。已知殘餘：同一租戶同時跑兩個 `version` 時，一個版本的 critical 會抑制另一個版本的 warning。`check_metric_group_pairs` 的 `KNOWN_UNGROUPED` 已清空。
