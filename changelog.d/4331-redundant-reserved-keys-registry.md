---
section: Changed
topic: da-guard
issues: [4331]
created: 2026-11-28T06:00:00+00:00
---
- **⚠️ Breaking：保留鍵的 `redundant_override` 改由保留鍵登錄檔決定判不判（da-guard；[#4331](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4331)）**：`reserved_keys.json` 寫明 exporter 是唯一讀者、在租戶檔、平台檔 `tenants:` 與 profile 三層都讀、形狀不是 mapping 或 list 的鍵，租戶寫與繼承值同值同型別的純量、而且沒有別的 finding 負責這一行時才報（目前是 `_silent_mode` 與 `_state_` 開頭的鍵）。route generator 讀的鍵（`_routing*`、`_severity_dedup`、`_routing_profile`）、`_profile`、`_expected_exporters` 與登錄檔沒列的 `_` 鍵（例如 `_silent_mode_critical`）不再報；值被拒收或 state filter 沒宣告的行改由各自的 finding 回報，不再同時給出「靠繼承即可」的建議。帶 `expires:` 的靜默與依星期的維護時段，比值判不準刪了會不會改變行為，所以照舊不判。
