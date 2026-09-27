---
section: Fixed
topic: da-tools
issues: [1380]
created: 2026-09-27T10:23:34+00:00
---
- **`config-diff` 報出租戶 `_` 開頭設定的變更（da-tools；[#1380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1380)）**：過去這類 key 一律略過，拿掉 `_state_maintenance`（該租戶告警全部恢復）、改 `_silent_mode`、`_routing`、`_profile`、`_severity_dedup`、`_state_*` 過濾器等，報告都寫 `No changes detected`、rc=0。現在新增「Tenant Setting Changes」一段，列出每個變更的前後值（巢狀設定只列有變的那一層，例如 `_routing.receiver.url`），有變更就 rc=1；JSON 輸出多一個 `setting_diffs`。讀的是所有 `_` key，日後新增的保留 key 也會報出；`_custom_alerts` 仍在它自己的段落。
