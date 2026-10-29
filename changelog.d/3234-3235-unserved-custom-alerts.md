---
section: Changed
topic: da-tools
issues: [3234, 3235]
created: 2026-10-24T06:00:00+00:00
---
- **被拒收、重複或解不開的自訂告警都擋（[#3234](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3234)、[#3235](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3235)）**：da-guard 新增 `custom_alert_rejected`，涵蓋因 `threshold:` 以外欄位被拒收的條目與整段解不開的 `_custom_alerts`。哪一筆報哪個 kind 由 Go 的 `guard.CustomAlertFindingKind` 決定，`served-values` 印在 `custom_alerts[].finding` 與 `custom_alerts_finding`（沒有時為 `null`）；`validate-config` 的 `guard_errors` 列照列這些 finding，重複項也在內；刻意寫成 `threshold: disable` 的條目不送也不報。`da-guard write` 判斷變壞時，自訂告警的 finding 以序數、不以索引比對。⚠️ 行為變更：這類樹 da-guard 與 `validate-config` 結束碼 `0` → `1`；只讓既有壞條目換索引的 write `1` → `0`；讀到沒有這兩個欄位的舊 da-guard 時結束碼 `2`。da-tools 映像沒有 jsonschema，這類條目先前在映像裡沒有任何一列擋。
