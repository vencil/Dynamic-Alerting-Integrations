---
section: Changed
topic: exporter
issues: [3638]
created: 2026-11-12T02:50:00+00:00
---
- **⚠️ Breaking：`_custom_alerts` 依寫下的結構讀，字串不再被當成清單（exporter、da-guard、tenant-api、custom-alert 編譯器；[#3638](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3638)）**：以前讀取端把值的文字重新解析，所以字串 `_custom_alerts: "- {recipe: …}"`（或區塊純量 `|`）會像清單一樣送出告警。現在只有寫成 YAML 清單的值才會被讀；非空字串整塊不送出，計入 `da_custom_alert_parse_errors`，寫入關卡（`ValidateTenantCustomAlerts`、tenant-api）也拒絕，訊息為 `_custom_alerts is written as a string, not a list`。空字串仍等於沒有宣告。JSON Schema 本來就要求清單，所以 `validate-config` 的 `json_schema` 列早已標紅這種寫法；變的是 runtime 不再照送。請改寫成清單。
