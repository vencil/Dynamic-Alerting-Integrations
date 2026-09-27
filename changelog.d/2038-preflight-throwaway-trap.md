---
section: Fixed
topic: dev-workflow
issues: [2038]
created: 2026-09-27T18:10:00+08:00
---
- **pre-push 橫幅給的暫存 worktree 指令，preflight 跑到一半按 Ctrl-C 也會清掉暫存樹（internal、dx；[#2038](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2038)）**：以前清理步驟寫在 preflight 之後，中斷就跳過，暫存樹會留在磁碟上，也仍登記在共用 `.git` 的 `git worktree list` 裡。現在清理改用 `trap … EXIT`，Ctrl-C 或 SIGTERM 時也會執行；結束碼仍是 preflight 的。直接關掉終端機（SIGHUP）時仍可能留下暫存樹，要自己用 `git worktree remove` 清。
