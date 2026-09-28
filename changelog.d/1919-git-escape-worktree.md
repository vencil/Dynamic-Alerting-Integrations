---
section: Fixed
topic: dev-workflow
issues: [1919]
created: 2026-09-28T13:40:00+08:00
---
- **Windows 逃生門 `win_git_escape.bat` 在 linked worktree 裡也清得到 lock，且一律操作腳本所在的那棵樹（internal、dx；[#1919](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1919)）**：自動清理的 `index.lock`／`refs/heads/*.lock`，以及 `preflight` 列出的 lock，路徑改由 `git rev-parse` 推導。以前寫死 `.git\`，在 worktree 裡（`.git` 是檔案）什麼也沒清到。操作哪一棵樹改由腳本自己的位置決定，不再看呼叫端的工作目錄；以前從別的樹呼叫，會把 add／commit／push 落在那棵樹上。`win_git_escape.bat`、`win_gh.bat` 與 playbook 的 MCP 呼叫樣板不再寫死主 repo 的路徑。
