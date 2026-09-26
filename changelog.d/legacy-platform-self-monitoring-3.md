---
section: Fixed
topic: platform-self-monitoring
issues: [1203, 1207, 1252, 2102]
created: 2026-09-26T17:00:00+00:00
---
- **平台告警可被路由、不再能被租戶消音，五條原本不可能觸發的告警復活（platform、docs；[#1203](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1203)、[#1207](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1207)）**：平台自監控告警（Watchdog 除外）加上 `alert_source: platform` label，operator 可用 `_routing_enforced` 搭配該 label 接上投遞（出貨預設仍不接）；文件補上路由方式、噪音預期與診斷步驟，並修正 BYO 指南中會產生 match-all route 的 `match` 範例。⚠️ 已接上投遞的環境套用時 label set 改變，會出現一輪假 resolved 再重新 page，`for` 計時也會重置，建議在維護窗口套用。⚠️ 租戶的 `_silent_mode` inhibit 不再能壓掉帶 `tenant` label 的平台告警（維護排程建立的 silence 仍會）。Prometheus 補掃 tenant-api 與 Alertmanager，使 `TenantApiConfigReloadFailing` 等四條 tenant-api 告警與 `AlertmanagerWebhookNotificationsFailing` 真正可觸發。平台告警的 `runbook_url` 覆蓋與連結可解析性改由測試強制。
