---
section: Changed
topic: exporter
issues: [3881]
created: 2026-11-15T01:30:00+00:00
---
- **⚠️ Breaking：子目錄 `_defaults.yaml` 寫了保留鍵，共用鍵判決改報 `unread`（da-guard；[#3881](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3881)）**：子目錄 `_defaults.yaml` 的 defaults 寫了 `_silent_mode`、`_severity_dedup`、`_state_<filter>` 這類保留鍵，或寫了做不成閾值列的 `_` 鍵時，exporter 不論值為何都不套用（排程寫法也一樣）。da-guard 主檢查本來就報 `subtree_default_reserved_key`，`da-guard write` 也因此會擋；但 `da-guard key-verdict` 原本沒有 finding，所以 `generate-routes --validate` 會放行。現在判決對每個檔與鍵報一筆 `unread`（error，原因 `subtree_default_reserved_key`），掛在子目錄檔，列出繼承它的租戶。這份集合與主檢查共用同一個函式，一邊報、另一邊一定也報。因此既有樹若有這種鍵，`generate-routes --validate` 會失敗（finding kind `key_unread`）；不過這種樹本來就過不了 da-guard 主檢查。`da-guard write` 對它仍只列主檢查那一筆。tenant-api 只擋它自己寫到的檔，不會因此拒收子目錄下的新租戶。修法：把鍵從子目錄 `_defaults.yaml` 刪掉；要讓它生效，就寫在各租戶自己的那一筆。
