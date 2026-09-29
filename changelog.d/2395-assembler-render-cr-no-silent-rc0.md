---
section: Fixed
topic: confd-family
issues: [2395]
created: 2026-09-29T14:02:27+00:00
---
- **`da_assembler --render-cr` 沒有寫出可用的檔案時不再回 rc 0（da-tools；[#2395](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2395)）**：以下情況現在都印一行錯誤、rc 2、不寫任何檔案。
  - render 途中發生寫出錯誤以外的例外：先前只記一行 `Failed to reconcile`，rc 0 卻什麼都沒寫。
  - `spec.tenants` 底下某個租戶的值不是 mapping（`t2: 0`、`false`、`[x]`、`x`）：先前照原樣寫出，exporter 會整份跳過該檔，同檔其他租戶也一併失效。
  - `spec.tenants`、`spec.defaults`、`spec.stateFilters` 存在但不是 mapping（`defaults: [x]`、`stateFilters: foo`）：先前被靜默丟掉。

  值為 null 的租戶或區塊行為不變；區塊值被 YAML 讀成 falsy（`defaults: 0`、`stateFilters: false`、`[]`）時仍當成空區塊丟掉。controller 模式（`--once` 與 watch）不受影響，仍是記 log、把 CR status 設為 Error，然後繼續處理其他 CR。
