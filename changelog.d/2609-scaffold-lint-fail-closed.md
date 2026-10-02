---
section: Fixed
topic: dx
issues: [2609, 2601]
created: 2026-10-01T23:35:18+00:00
---
- **`scaffold_lint.py` 產生的 lint 不再把讀不懂的檔當成乾淨（dx；[#2609](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2609)）**：`ast` 樣板改用 `_lint_helpers.parse_python_file`，與直譯器同樣處理 UTF-8 BOM 與 PEP 263 coding cookie，行號也與 AST 一致，所以帶 BOM 或 `# -*- coding: latin-1 -*-` 的檔照樣掃得出違規。無法解析的檔不再回傳空結果，而是在 stderr 指名並以 exit 2 結束；所有 kind 共用的主迴圈遇到讀不到的檔（例如懸空 symlink）也一樣，不再印警告後略過。只影響今後新產生的 lint，既有 lint 不變。
