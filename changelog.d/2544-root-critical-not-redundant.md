---
section: Fixed
topic: confd-family
issues: [2544]
created: 2026-10-02T15:00:00+00:00
---
- **da-guard 不再以根 `_defaults.yaml` 的 `<key>_critical` 判定租戶的同名鍵多餘，並新增 `root_defaults_critical_key` 警告（[#2544](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2544)）**：根 `defaults:` 的 `_critical` 鍵送成一條獨立的 series（severity=warning），不會變成 `<key>` 的 critical row；先前根層與租戶寫同值時，da-guard 判租戶那行 `redundant_override`，照建議刪掉後 `/metrics` 就少了 critical row。新舊兩種拼法都已修正。子目錄 `_defaults.yaml`、根平台檔 `tenants:` 與 profile 寫的 `_critical` 仍算繼承值。根層 `_critical` 的送出方式不變，da-guard 另對每個這種鍵報一條 warn（不擋 merge；`_state_`、`_silent_` 開頭的鍵不報）。
