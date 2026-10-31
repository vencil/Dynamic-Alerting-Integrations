---
section: Changed
topic: da-tools
issues: [3406]
created: 2026-10-31T04:38:07+00:00
---
- **路徑旗標給 `''` 時拒絕，不再當成目前目錄（dx 工具；[#3406](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3406)）**：**Breaking**，add-frontmatter `--base-dir ''`、doc-coverage `--repo-root ''`、migrate-conf-d `--conf-d ''`（例如未設定的 shell 變數）由 0 改為 2，訊息把值顯示成 `''`；先前在轉成路徑時變成目前目錄，掃描、報告或改寫了一棵呼叫端沒指定的樹（add-frontmatter 會改寫其中的 markdown）。
