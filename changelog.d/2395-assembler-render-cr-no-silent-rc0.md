---
section: Fixed
topic: confd-family
issues: [2395]
created: 2026-09-29T14:02:27+00:00
---
- **`da_assembler --render-cr` 沒有寫出 exporter 讀得了的檔案時不再回 rc 0（da-tools；[#2395](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2395)）**：rc 0 只留給檔案成功寫出（`--dry-run` 時為「會寫出」）且 render 沒有任何例外的情況。以下情況改為印一行錯誤、rc 2：
  - render 途中發生寫出錯誤以外的例外：先前 rc 0。例外發生在寫檔之前就不留檔；發生在寫檔之後則檔案會留在磁碟上。
  - 租戶的值不是 mapping（`t2: 0`、`x`、`---`），或 `defaults` 的值不是 exporter 讀得了的數字（`true`、`"80"`、`1:30`、日期、list），或 `stateFilters` 的 filter、`reasons`、`severity` 的型別不對：先前照原樣寫出，exporter 整份跳過該檔。數字的判定是寬鬆超集，`0o8`、`1e400` 這類仍是 rc 0。
  - `tenants`、`defaults`、`stateFilters` 不是 mapping（`defaults: [x]`、`tenants: ---`）：先前被靜默丟掉。

  寫檔前就擋下的情況不寫任何檔案。以下行為不變：null 值、`!!set` 租戶、被 YAML 讀成 falsy 的區塊（`defaults: 0`，仍會丟掉），以及 controller 模式（`--once` 與 watch）。
