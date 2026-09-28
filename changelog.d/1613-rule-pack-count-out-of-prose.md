---
section: Changed
topic: docs
issues: [1613]
created: 2026-09-28T08:29:22+00:00
---
- **文件散文不再寫 Rule Pack 總數（[#1613](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1613)）**：README、入門指南、遷移指南、詞彙表等處的「16 個 Rule Pack／16 Rule Packs」改成不帶數字的說法；總數只留在 README badge 與自動產生的 `rule-packs/README.md`。版號閘門改為：badge 的檢查與 `--fix` 共用同一組樣式與大小寫規則（原本大寫寫法會被報錯卻修不掉）；手寫散文裡只要出現「數字＋Rule Pack」就報錯，請改寫句子。原本英文寫法的計數完全沒有被檢查。
