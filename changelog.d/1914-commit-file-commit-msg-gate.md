---
section: Fixed
topic: windows-escape
issues: [1914]
created: 2026-09-28T22:19:46+00:00
---
- **Windows 逃生門的 `commit-file` 現在會先驗 commit 訊息（ops；[#1914](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1914)）**：`win_git_escape.bat commit-file`（含 `make win-commit`）內部帶 `--no-verify`，連 commit-msg hook 一起跳過，訊息錯誤要到 CI 的 commitlint 才被擋。`commit_helper.py` 現在於 commit 前自己跑同一個驗證器（`pr_preflight.py --check-commit-msg`），不過就不 commit。
