---
section: Fixed
topic: dev-workflow
issues: [1919]
created: 2026-09-28T13:40:00+08:00
---
- **Windows 逃生門 `win_git_escape.bat` 不再靜默操作到別棵樹，並清得到 linked worktree 的 `index.lock`（internal、dx；[#1919](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1919)）**：呼叫端目前所在的樹，若不是這份腳本所在的樹，就印 `FAILED` 回 1。以前一律操作呼叫端所在的樹，從別棵樹呼叫時，add／commit／push 會落在那棵樹上。命令在呼叫端的目錄執行，所以相對路徑引數照 git 本身的規則解析。繼承來的 `GIT_DIR` 會被清掉。自動清理只刪這棵樹自己的 `index.lock`（路徑由 git 推導，linked worktree 也清得到），不再刪 `refs/heads/*.lock`：那可能是另一棵樹正在使用的鎖。`preflight` 會遞迴列出共用 git dir 底下的所有 lock，不代刪。`win_git_escape.bat`、`win_gh.bat` 與 playbook 的 MCP 呼叫樣板不再寫死主 repo 的路徑。
