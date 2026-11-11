---
section: Fixed
topic: ci
issues: [3644, 3694]
created: 2026-11-07T10:44:09+08:00
---
- **Defaults Impact Guard 遇到整棵 conf.d 樹被刪除的 PR 不再轉紅（ci；[#3644](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3644)）**：scope helper 新增 `removed` 列——該樹在變更清單裡的 `_` 開頭檔全是刪除、且根目錄在 HEAD 已不存在，兩者都成立才算。PR 留言把它列為「NOT CHECKED：此 PR 刪除了整棵樹」，不計入 verdict。只有目錄不存在（例如路徑在途中被改寫）時仍是 target，da-guard 照樣 exit 2 轉紅。⚠️ **巢狀 conf.d（`conf.d` 底下還有 `conf.d`）不再支援**：以哪一層當 `--config-dir` 會得到不同的有效設定，Guard 改輸出 `nested` 列並轉紅，請把佈局攤平。只刪掉內層、外層還在時已不巢狀，外層樹列為 target 照驗；兩份變更清單改帶 `--no-renames`，刪除不會因別處新增同內容檔被當成 rename 而漏列（[#3694](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3694)）。
