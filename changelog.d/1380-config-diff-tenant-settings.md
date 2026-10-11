---
section: Fixed
topic: da-tools
issues: [1380, 1516]
created: 2026-09-27T10:23:34+00:00
---
- **`config-diff` 報出租戶 `_` 開頭設定的變更（da-tools；[#1380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1380)）**：過去這類 key 一律略過，拿掉 `_state_maintenance`（該租戶告警全部恢復）、改 `_silent_mode`、`_routing`、`_severity_dedup`、`_state_*` 過濾器等，報告都寫 `No changes detected`、rc=0。現在「Tenant Setting Changes」一段列出 exporter 讀到的設定前後值（巢狀設定只列有變的那一層，例如 `_routing.group_wait`），也含 state filter 的 severity 與靜默、維護時段的期限，有變更就 rc=1；JSON 在 `setting_changes`。receiver 的憑證欄位改了照樣報，但前後值一律以 `<redacted>` 取代：欄位名單與 tenant-api 的遮罩（#1560）同一份，`http_config`、`tls_config` 與 header map 整段遮，因為報告會貼成 PR comment。`_custom_alerts` 在它自己的段落。
