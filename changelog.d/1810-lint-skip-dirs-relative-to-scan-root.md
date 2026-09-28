---
section: Fixed
topic: lint
issues: [1810]
created: 2026-09-28T08:05:35+00:00
---
- **lint 的排除目錄改以掃描根為準，不再被 checkout 路徑左右（lint；[#1810](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1810)）**：`check_hardcode_tenant` / `check_bilingual_content` / `check_orphan_lint` / `sync_schema` 原本拿絕對路徑的每一段比對排除清單（`examples`、`includes`、`venv`、`vendor` 等），repo 放在同名目錄底下時會整批排除、甚至掃 0 檔仍回報通過。現在一律用相對於掃描根的路徑判斷；`check_hardcode_tenant` 預設掃描、`check_bilingual_content` 的 docs 樹若一個檔都沒掃到，改為 exit 2 並在 stderr 說明，不再印出空洞的通過。
