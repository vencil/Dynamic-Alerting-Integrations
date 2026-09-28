---
section: Fixed
topic: dev-workflow
issues: [1918]
created: 2026-09-28T09:30:00+08:00
---
- **Windows 逃生門 `win_git_escape.bat` 的 git 失敗會回非 0（internal、dx；[#1918](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1918)）**：`status`／`add`／`commit`／`commit-file`／`tag`／`branch`／`log`／`diff`／`preflight` 在 git 失敗時印出 git 的錯誤並回 1；以前印完 `FAILED:` 仍回 0，`make win-commit` 的 commit 失敗後會照樣 push。`status`／`log`／`diff` 在非 repo 目錄不再無聲回 0。`branch <已存在的分支>` 切換成功時不再印 `FAILED:`。`pr-preflight` 改用與 `commit` 相同的 Python 解析，不再被「什麼都不執行就回 0」的 `python` 判成通過。
