---
section: Fixed
topic: confd-family
issues: [2371]
created: 2026-09-28T17:00:00+00:00
---
- **`da_assembler --render-cr` 遇到形狀不對的 CR 改回 rc 2（da-tools；[#2371](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2371)）**：以下三種 CR 現在都印一行錯誤、rc 2、不寫任何檔案，與 `kind` 不符的處理相同。
  - 頂層是 list 或純量：先前丟 traceback、rc 1。
  - `metadata` 缺少或不是 mapping，或 `metadata.name` 不是非空字串：先前丟 traceback、rc 1，或拿它當檔名寫出 `.yaml`、`42.yaml`。判斷比照 Kubernetes API server（CR 經 YAML→JSON）：沒加引號、YAML 會讀成數字或布林的 name（`8`、`010`、`0x1F`、`1.5`、`yes`、`true`）改為 rc 2，訊息請你加引號；沒加引號的日期與日期時間（`2024-01-01`、`2024-01-01T10:20:30Z`）在 JSON 裡是字串，照原文接受，檔名也用原文。加了引號的任何值都照收。
  - `spec` 存在但不是 mapping：先前 rc 0，卻什麼都沒寫。

  沒有 `spec` 的 CR 行為不變，與 `spec: {}` 相同。
