---
section: Fixed
topic: dev-workflow
issues: [1919]
created: 2026-09-28T13:40:00+08:00
---
- **Windows 逃生門 `win_git_escape.bat` 不再靜默操作到別棵樹，也不再刪 lock（internal、dx；[#1919](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1919)）**：呼叫端目前所在的樹若不是這份腳本所在的樹，就印 `FAILED` 回 1。以前一律操作呼叫端所在的樹，從別棵樹呼叫時，add／commit／push 會落在那棵樹上。命令在呼叫端的目錄執行，相對路徑引數照 git 本身的規則解析。繼承來的 repo 區域環境變數（`git rev-parse --local-env-vars` 列出的那些，例如 git hook 設的 `GIT_DIR`、`GIT_INDEX_FILE`）會先清掉；呼叫端用 `git -c` 或 `GIT_CONFIG_COUNT` 帶進來的設定也在其中，會一併失效。腳本路徑含 `!` 時直接拒絕：cmd 的 delayed expansion 會改寫這種路徑。以前每個子命令執行前都會刪掉 `.git\index.lock`，分不出殘留的鎖和正被另一個 git 行程持有的鎖；現在不刪任何 lock，改由 `preflight` 遞迴列出共用 git dir 底下的所有 lock（linked worktree 的也在內），由操作者判斷後自行刪除。`win_git_escape.bat`、`win_gh.bat` 與 playbook 的 MCP 呼叫樣板不再寫死主 repo 的路徑。
