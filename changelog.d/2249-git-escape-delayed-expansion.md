---
section: Fixed
topic: dev-workflow
issues: [2249]
created: 2026-09-29T22:40:00+08:00
---
- **Windows 逃生門 `win_git_escape.bat` 不再吃掉 `!`、`^`，`add` 不再拆開含空白的檔名，`fix-hooks` 不再假成功（internal、dx；[#2249](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2249)）**：以前整支腳本開著 delayed expansion，`add a!b.txt` 會 stage 成 `ab.txt`，`fix-hooks` 交給 PowerShell 的指令也被吃掉 `#!`，shebang 修不到；`add` 重組檔名時去掉引號，`sp ace.txt` 變成兩個 pathspec。現在 delayed expansion 只在比對路徑那一段開啟，`add` 每個檔名各自加引號。`fix-hooks` 改由 `git rev-parse --git-path hooks` 找 hooks 目錄（linked worktree 與 `core.hooksPath` 都找得到），一個檔都沒修到時回 1。**移除 `commit "訊息"` 子命令**（改用 `commit-file`，它涵蓋所有訊息，含中文），連同只供它使用的 `commit_helper.py check-ascii`。
