---
section: Fixed
topic: dev-workflow
issues: [2236]
created: 2026-09-29T23:30:00+08:00
---
- **Windows 的 gh 逃生門 `win_gh.bat`、`win_git_escape.ps1` 不再把 gh 的失敗回報成成功（internal、dx；[#2236](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2236)）**：以前 `win_gh.bat` 每個子命令都以 rc 0 結束，`win_git_escape.ps1` 只有 `pr-preflight` 會傳出 rc，所以 `pr-create`、`pr-merge`、`release-create` 失敗時呼叫端看到的是成功。現在兩支都原樣傳出 gh 的 rc，包括 `gh pr checks` 表示「checks 還在跑」的 8：pending 不是通過，呼叫端可以分辨 8 與 1。`win_git_escape.ps1 pr-preflight` 改用 `py -3`，不會叫到回 0 卻什麼都沒跑的 Microsoft Store stub。`win_gh.bat` 的 `raw`、`pr-create` 不再吃掉參數裡的 `!` 與 `^`。
