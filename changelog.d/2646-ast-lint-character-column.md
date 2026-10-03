---
section: Fixed
topic: dx
issues: [2646]
created: 2026-10-03T08:06:55+00:00
---
- **`subprocess-timeout-audit` 回報的欄位改為字元欄位（dx；[#2646](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2646)）**：`ast` 的 `col_offset` 算的是 UTF-8 位元組，行內有中文等多位元組字元時，`path:line:col` 的欄位會偏大，編輯器跳轉會落到錯的位置（例如 `名稱 = 1; subprocess.run([...])` 原本報第 13 欄，實際是第 9 欄）。純 ASCII 的行輸出不變。換算由 `_lint_helpers.char_col_offset` 負責，`scaffold_lint --kind ast` 產生的樣板也改為匯入並示範它。
