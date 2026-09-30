---
section: Fixed
topic: confd-family
issues: [2476, 2481]
created: 2026-09-30T12:10:20+00:00
---
- **`da_assembler --render-cr` 讀不了 CR 檔時回 rc 2 的一行錯誤，不再印 traceback；null 的 mapping key 改為拒收（da-tools；[#2476](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2476)）**：CR 檔不是合法 UTF-8、帶明確標籤但內容建構不出值（`!!int team`、`!!bool team`、`!!timestamp "2024-13-01"`、加引號且超過 4300 位的 `!!int "…"`），或巢狀過深、anchor 引用自己時，先前 rc 1 加 traceback，現在印一行點名檔案的錯誤、rc 2、不寫檔。巢狀過深時，不論卡在讀檔或 render 階段，訊息都註明這是本工具的上限，Kubernetes 本身可能收得下。
  Kubernetes decode 完之後仍留著的 null mapping key（`null:`、`~:`、空鍵，含經 alias 或 `<<` merge 帶入的）會讓整份被拒收；先前 rc 0 並寫出檔案，現在 rc 2、不寫檔。被後面同名鍵蓋掉的值不算，照常 rc 0；加引號的 `"null":` 是字串鍵，行為不變。
