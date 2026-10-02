---
section: Fixed
topic: dx
issues: [2601]
created: 2026-10-01T22:19:01+00:00
---
- **`open-encoding-audit` 與 `subprocess-timeout-audit` 不再靜默跳過讀不懂的檔（dx；[#2601](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2601)）**：兩支掃描器改用與直譯器相同的方式解碼原始碼（剝掉恰好一個 UTF-8 BOM、依 PEP 263 coding cookie 選編碼），所以帶 BOM 或 `# -*- coding: latin-1 -*-` 的檔照樣會被掃出違規；先前這兩種寫法都能讓違規整檔繞過閘門。仍然讀不到、解不了碼或語法錯誤的檔，會在 stderr 指名檔案與原因並以 exit 2 結束（任何模式皆然），不再被當成乾淨。`open-encoding-audit --write-subprocess-baseline` 遇到這種檔會拒絕寫帳本。
