---
section: Fixed
topic: da-tools
issues: [3073, 3133]
created: 2026-10-20T12:57:47+00:00
---
- **`alert-quality` 給了 `--alertmanager` 而它沒回答時改為結束碼 2（tools；[#3073](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3073)）**：⚠️ 破壞相容——Alertmanager 連不上、回 HTTP 錯誤、或回應不是 alert 物件的 list 時，以前當成 0 筆被抑制，suppression 評 GOOD、rc 0，`--ci --min-score` 下本該 rc 1 的也被放行成 0；現在不出報告、rc 2，stderr 印 `Error: Alertmanager query failed: <原文>`，`--json` 印 `status: caller_error`、`reason: alertmanager_query_error`。
