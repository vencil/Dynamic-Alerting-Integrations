---
section: Added
topic: alertmanager-routing
issues: [1211]
created: 2026-11-01T09:35:00+08:00
---
- **`generate-routes` 報出送不出去的 page-mode custom alert（[#1211](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1211)）**：租戶有 exporter 實際 serve 的 page-mode custom alert（da-guard served-values；`mode` 未寫即 `page`）、在 `component="custom"` route 下卻沒有投遞子路由（沒有有效 `_routing`）時，每個租戶報一行 WARN，`--findings-json` 裡是 `custom_alert_undelivered`。`--output-configmap`／`--output-alertmanager`／`--apply` 會檢查要寫出／套用的 `custom-alerts-firehose` 有沒有 integration：有就不報，沒有時 `--strict` 升為 ERROR、結束碼 1、不寫檔／不 apply。租戶自己寫的 `_routing: disable` 照報但不升級；`--validate` 看不到 firehose，只報 WARN 並註明沒檢查到。以前這種告警靜靜地永遠不送出，結束碼 0。
