---
section: Changed
topic: confd-family
issues: [2906]
created: 2026-10-17T16:40:00+00:00
---
- **⚠️ Breaking：寫 conf.d 的工具改依 `da-guard parse-tree` 判定，寫入前再以 parse-tree 讀回確認（tools；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：`patch-config`、`deprecate`、`init`、`offboard`、`threshold-govern`、`scaffold`、`migrate_conf_d.py`、`da_assembler.py` 改什麼、改不改都依 parse-tree（yaml.v3 的讀法），PyYAML 只產生文字；寫入前讀回的鍵、結構、每個純量的原文／型別／值必須正是原檔套上這次修改，原檔讀不了、或路徑沒有讀取端也不寫，一個檔都不動（各命令的結束碼見 cli-reference）。沒有 da-guard 結束碼 2。PyYAML 會換掉寫法的值（`!!float 1` → `1.0`、字串 `1e3` 未加引號）讓整份檔拒寫，請先手動改寫；`deprecate` 不再改寫目錄沒選上的 `_defaults` 拼法（還有 key 即殘留、rc 1）；`patch-config` 不再因 `<<` 拒絕；`offboard` 只刪恰好只宣告該租戶的檔；`init` 改用 parse-tree 判斷別的檔宣告了誰。
