---
section: Fixed
topic: dev-workflow
issues: [2248]
created: 2026-09-30T00:10:00+08:00
---
- **`make win-commit` 在 Git Bash 上不再什麼都沒 commit 卻印出 `✅ Done`（internal、dx；[#2248](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2248)）**：它的 `Windows_NT` 分支從 Git Bash 執行 `cmd.exe /c`，MSYS 把 `/c` 當成路徑轉換，`cmd.exe` 以互動模式啟動、讀到 EOF 就回 0，bat 根本沒跑。Windows host 預設沒有 make，所以移除這個分支：只有看得到 `/mnt/c/Windows/System32/cmd.exe` 的 WSL 會真的執行 commit 與 push，其他環境只印出三行 `win_git_escape.bat` 指令。Windows host 請直接用 `scripts/ops/win_git_escape.bat`。
