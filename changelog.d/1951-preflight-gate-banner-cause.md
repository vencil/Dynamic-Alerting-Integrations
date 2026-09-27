---
section: Fixed
topic: dev-workflow
issues: [1951]
created: 2026-09-27T16:20:00+08:00
---
- **pre-push 的 preflight 閘門擋下時，不再一律說「preflight 沒跑」（internal、dx；[#1951](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1951)）**：marker 不見有三種可能：這顆 commit 沒跑過 preflight、跑了沒過，或之後同一顆 commit 上有一次 FAIL 把它撤銷了。marker 是所有 worktree 共用的，所以撤銷可能來自任何一棵停在這顆 commit 上的樹。橫幅現在把三種情況都列出來。撤銷的行為不變：同一顆 commit 以最後一次結果為準，寧可擋下，代價是重跑一次 preflight。
