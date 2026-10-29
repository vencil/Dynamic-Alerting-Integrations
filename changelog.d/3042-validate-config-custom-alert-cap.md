---
section: Added
topic: da-tools
issues: [3042]
created: 2026-10-17T08:00:00+00:00
---
- **da-guard 新增 `custom_alert_over_cap`（`validate-config` 的 `guard_errors` 列照列）：租戶自己的自訂告警超過每租戶上限就擋（[#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)）**：租戶檔或根平台檔 `tenants:` 裡該租戶的 `_custom_alerts`，要 exporter 送出超過 20 列時 FAIL：在每租戶指標上限截斷之前數，停用的不計，`slo_burn_rate` 計 2，被當成重複丟掉的不計（tenant-api 寫入檢查則直接拒收重複）；profile 給的 recipe 不計。`da-guard served-values` 每個租戶多一個 `custom_alert_rows`，就是這個數。⚠️ 行為變更：這類樹過去只由編譯器隔離超出的 recipe、結束碼 0，現在 `validate-config` 與 da-guard 主報告都是結束碼 1；編譯器不再判上限，上限只由這兩處與 tenant-api 寫入檢查執行。
