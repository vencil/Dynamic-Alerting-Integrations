---
section: Fixed
topic: confd-family
issues: [2371]
created: 2026-09-28T17:00:00+00:00
---
- **`da_assembler --render-cr` 遇到形狀不對的 CR 改回 rc 2（da-tools；[#2371](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2371)）**：以下三種 CR 現在都印一行錯誤、rc 2、不寫任何檔案，與 `kind` 不符的處理相同。
  - 頂層是 list 或純量：先前丟 traceback、rc 1。
  - `metadata` 缺少或不是 mapping，或 `metadata.name` 不是非空字串：先前丟 traceback、rc 1，或拿它當檔名寫出 `.yaml`、`42.yaml`。型別以 YAML 1.1（PyYAML）的隱式型別判定：沒加引號時會被讀成整數／浮點數／布林的 name（`8`、`010`、`0x1F`、`1:30`、`1.5`、`yes`、`true`）視為呼叫端錯誤，改為 rc 2。這與 Kubernetes 經 YAML→JSON 後的型別大致相同，但在 `0o17`、`1e3`、`08`、`y`、`n`、`1:30` 等形狀上有差異。沒加引號的日期（`2024-01-01`）照原文接受，檔名也用原文；加了引號的值是字串，但仍須通過 [#2396](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2396) 的名稱格式（DNS-1123）檢查。
  - `spec` 存在但不是 mapping：先前 rc 0，卻什麼都沒寫。

  沒有 `spec` 的 CR 行為不變，與 `spec: {}` 相同。
