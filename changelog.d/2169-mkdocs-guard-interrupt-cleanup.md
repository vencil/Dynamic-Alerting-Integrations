---
section: Fixed
topic: dev-workflow
issues: [2169]
created: 2026-09-27T16:49:57+00:00
---
- **mkdocs strict 的 pre-push 守衛建站途中被中斷，也會清掉暫存 worktree（internal、dx；[#2169](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2169)）**：以前清理步驟寫在建站之後，推送時按 Ctrl-C 或收到 SIGTERM 就會跳過，暫存樹留在 `.git` 裡，也仍登記在 `git worktree list`。現在由腳本頂層的 `trap … EXIT` 清掉正在建的那一棵，一次推多個 ref 時也一樣；守衛的結束碼不變。中斷時的清理依賴 bash 的 trap 行為。
