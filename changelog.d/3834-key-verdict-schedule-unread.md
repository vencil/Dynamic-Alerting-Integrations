---
section: Changed
topic: exporter
issues: [3834]
created: 2026-11-14T04:54:37+00:00
---
- **⚠️ Breaking：保留鍵寫成排程，共用鍵判決改報 `schedule_unread`（da-guard；[#3834](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3834)）**：`_silent_mode`、`_severity_dedup`、`_state_<filter>`、`_routing_profile`、`_namespaces` 等保留鍵寫了 `overrides:` 時窗或 `expires:`／`reason:` 時，讀取端只讀 `default`，其餘不生效。`da-guard key-verdict` 原本對它沒有 finding，現在報 `schedule_unread`，判在寫它的檔與層：租戶檔是 error，根平台檔的 `tenants:` 與 profile 是 notice。因此既有樹在租戶檔寫了這種時窗的，`generate-routes --validate` 會失敗（finding kind `key_schedule_unread`；`_severity_dedup`、`_routing_profile` 原本就由產生器自己擋），`da-guard write` 會擋新寫出或挪動過的時窗（`worsened` 的 kind `schedule_unread`）。exporter 行為不變；tenant-api 不因它多拒收，寫入回應可能多出其他租戶或 profile 的提示行。修法：拿掉時窗或到期時間。需要一次性結束時間的，`_silent_mode` 改用 `{target: …, expires: …}`，`_state_maintenance` 改用 `{target: enable, expires: …}`；週期性維護窗改用 `_state_maintenance` 的 `recurring:`。
