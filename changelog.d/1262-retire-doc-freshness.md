---
section: Removed
topic: dx
issues: [1262, 2023]
created: 2026-09-27T10:15:40+00:00
---
- **退役文件新鮮度檢查（lint；[#1262](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1262)）**：刪除 `scripts/tools/lint/check_doc_freshness.py`、manual-stage hook `check-doc-freshness`、`validate_all` 的 freshness 列與 `.doc-freshness-ignore`。這支工具只量「距上次 commit 幾天」，文件放滿 90 天不代表內容過時；檔內檢查懸空路徑、da-tools 指令與 image 版號的函式從未被呼叫。
