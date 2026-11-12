---
section: Changed
topic: exporter
issues: [3638]
created: 2026-11-12T02:50:00+00:00
---
- **⚠️ Breaking：`_custom_alerts` 依寫下的結構讀，純量不再被當成清單（exporter、da-guard、tenant-api、custom-alert 編譯器、patch-config；[#3638](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3638)）**：以前讀取端把值的文字重新解析，所以字串 `_custom_alerts: "- {recipe: …}"`（或區塊純量 `|`）會像清單一樣送出告警。現在只有寫成 YAML 清單的值才會被讀；非空純量整塊不送出，計入 `da_custom_alert_parse_errors`，訊息為 `_custom_alerts is written as a scalar, not a list`。空字串仍等於沒有宣告。寫入關卡（`ValidateTenantCustomAlerts`）判整份租戶段落，所以檔案裡留著這種寫法的租戶，tenant-api 對它**任何鍵**的寫入都會被拒，直到改成清單；`patch-config` 的值一律取自命令列字串，所以不能再用它寫 `_custom_alerts`。JSON Schema 本來就要求清單，`validate-config` 的 `json_schema` 列早已標紅這種寫法。請改寫成清單。
