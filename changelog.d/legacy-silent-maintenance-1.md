---
section: Fixed
topic: silent-maintenance
issues: [1203, 2000, 2003]
created: 2026-09-26T17:00:00+00:00
---
- **靜音／維護的失效方向修正（⚠️ 行為變更；exporter、k8s）**：結構化 `_silent_mode`／`_state_maintenance` 的 `expires` 不是 RFC3339 時，改為忽略整筆設定（fail-closed），不再當成永不到期——已用錯誤格式靜音的租戶升級後會開始收到通知（[#2000](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2000)）。過期的 `_silent_mode: {target: all, reason: …}` 不再讓整個 `/metrics` 回 HTTP 500；`da_config_event` 因此新增 `target_severity` label，升級當下已過期 silence 的 alert 會 resolve 一次再重新 fire（[#2003](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2003)）。maintenance-scheduler 在強制 NetworkPolicy 的叢集裡從未成功建立 silence（CronJob 的 egress 與 Alertmanager 的 ingress 都沒放行），維護窗期間告警照常開火；現已補齊兩端規則（[#1203](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1203)）。
