---
section: Fixed
topic: portal
issues: [3999]
created: 2026-11-15T16:45:51+00:00
---
- **用 alias 寫成的 `default` key 不再被說成「exporter 拒收整個檔」（portal；[#3999](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3999)）**：`{*d : [1]}`（旁邊寫 `_d: &d default`）這種寫法，exporter 看到的是 alias 而不是 `default` 這個字，所以不當成排程，檔案照常服務；Alert Preview、YAML Validator、Routing Trace 與 Config Lint 卻報錯、不模擬（[#3974](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3974) 的檢查造成）。現在 `parseTenantYaml` 和 exporter 一樣依 key 節點自身的值判斷排程：alias 看的是錨點名稱，所以 `{*d : [1]}` 不算排程；反過來，錨點名稱是 `default` 的 alias（例如 `&default foo` 的 `*default`）算排程，型別不符時會照 exporter 回報整檔拒收。另新增由 Go 依 exporter 讀檔函式寫出的 `tests/shared/tenant_file_matrix.json`（大多由排程 key 的寫法 × 欄位形狀 × 持有排程的 key 的寫法組合產生），portal 必須與它逐列一致。
