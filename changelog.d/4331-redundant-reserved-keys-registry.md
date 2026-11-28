---
section: Changed
topic: da-guard
issues: [4331]
created: 2026-11-28T06:00:00+00:00
---
- **⚠️ Breaking：保留鍵的 `redundant_override` 改由保留鍵登錄檔決定判不判（da-guard；[#4331](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4331)）**：`reserved_keys.json` 寫明 exporter 是唯一讀者、且在租戶檔、平台檔 `tenants:` 與 profile 三層都讀的鍵，租戶寫與繼承值同值同型別的純量時才報（目前是 `_silent_mode` 與 `_state_` 開頭的鍵）。`_severity_dedup`、`_routing_profile` 這類 route generator 也讀的鍵不再報；`_profile`、登錄檔沒列的 `_` 鍵（例如 `_silent_mode_critical`）與 mapping 底下的值也不再報。帶 `expires:` 的靜默、依星期的維護時段與 `_metadata` 的欄位，比值判不準刪了會不會改變行為，所以一律不判。
