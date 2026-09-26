---
section: Fixed
topic: release-versioning
issues: [1407, 1479, 1480, 1483, 1484, 1506, 1508, 1534, 1540, 1765, 1836, 1843]
created: 2026-09-26T17:00:00+00:00
---
- **版號與 CHANGELOG 閘門改為 fail-closed（internal、dx、lint）**：`version-consistency` 過去因讀不到平台版號而長期靜默跳過檢查，規則指向搬走的檔案也只印 SKIP；現在讀不到來源、檔案不見或 pattern 撈不到都會轉紅，`make version-check` 並加跑計數對帳（計數漂移會擋 tag）（[#1407](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1407)、[#1480](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1480)）。`bump_docs` 重指 da-tools pin 時會以即將出貨的工作樹驗證文件與 portal 教的子命令確實存在；CHANGELOG 檔案結構 lint 接進 pre-commit 與 CI，並新增 `changelog_rebase_check.py` 檢查 rebase 是否吃掉或重複條目。
