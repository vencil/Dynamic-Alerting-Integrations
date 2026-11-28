---
section: Removed
topic: rule-packs
issues: [4116]
created: 2026-11-28T02:00:01+00:00
---
- **⚠️ breaking：移除複合告警 `JVMPerformanceDegraded`（rule-packs；[#4116](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4116)）**：這條告警要求 GC 與 heap 同時超過 warning 閾值。GC 改取最忙的 JVM 之後，它會拿一個 JVM 的 GC 去配另一個 JVM 的 heap；它依賴的 `jvm_memory` 讀數在部分 GC 下也不可靠（[#3976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3976)）。沒有替代告警，也不留過渡期的 shim。外部 Alertmanager 設定裡以 `alertname="JVMPerformanceDegraded"` 寫的 route、inhibit rule 或 silence matcher 不會再配到任何告警：請刪除，或改配 `JVMHighGCPauseCritical`／`JVMMemoryPressureCritical`。
