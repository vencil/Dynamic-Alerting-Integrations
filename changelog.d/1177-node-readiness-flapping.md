---
section: Added
topic: rule-packs
issues: [1177]
created: 2026-11-28T10:40:00+00:00
---
- **Kubernetes pack 新增 `NodeReadinessFlapping`：節點反覆 NotReady（[#1177](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1177)）**：`NodeNotReady` 要連續 NotReady 滿 3 分鐘才響，節點每次只掉 30 秒到 2 分鐘、一再重複時它從不觸發，租戶的 Pod 卻一直在失去連線。新告警採用 kubernetes-mixin `KubeNodeReadinessFlapping` 的定義：節點 Ready 狀態 15 分鐘內變動超過 2 次、持續 15 分鐘；只看承載該租戶運行中 Pod 的節點，warning、每個節點每個租戶一條、維護模式靜音，不需要設定、沒有 critical 級。**什麼算 flapping**：15 分鐘內 NotReady 三次就算，告警約在第二次發生後 15 分鐘響起，長度約等於第二次 NotReady 的長度（三次各 30 秒只會短暫響一次）；持續反覆時約 16–20 分鐘響起、停止後約 10–14 分鐘解除。兩次不算；滾動升級（每個節點 NotReady 一次）不響；超過 3 分鐘的停機由 `NodeNotReady` 負責。與上游不同處：kube-state-metrics 跑多副本時不會重複計數；被 cordon 的節點不排除。
