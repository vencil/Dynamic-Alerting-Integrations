---
section: Fixed
topic: docs
issues: [2235]
created: 2026-09-28T01:28:29+00:00
---
- **平台自監控告警的說明不再寫總數（docs；[#2235](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2235)）**：BYO Alertmanager 整合指南 §11 與 troubleshooting 中英版寫平台告警共 41 條、帶 `alert_source` 的 40 條、沒有 `tenant` 的 37 條、只選 critical 時涵蓋 18 條；平台之後又新增一條告警，這些數字都少了一條，也沒有機制維持。改為「除 `Watchdog` 外全部」「除那 3 條以外的全部」這類說法。帶 `tenant` 的仍然是點名的那 3 條（`TenantMetricsOverLimit`、`FederationRejectionRateAnomaly`、`FederationGatewayBackendErrors`），路由建議不變。
