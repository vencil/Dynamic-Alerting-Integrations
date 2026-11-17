---
section: Fixed
topic: confd-family
issues: [3903]
created: 2026-11-17T21:45:00+08:00
---
- **`generator-service`：git 讀不了 `--repo` 時拒絕啟動，不再當成「還沒有 commit」（tools；[#3903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3903)）**：啟動時 `HEAD` 解不出 commit 的情形，以前一律印 `has no HEAD commit` 後照常啟動、回 ready，之後每個請求才以 `rejected/git_read` 拒絕。現在只有「分支上還沒有任何 commit」（`git init` 之後、`checkout --orphan`）照常啟動；repo 在啟動判定後消失或讀不了、`HEAD` 指向的 commit 物件不見了，都是結束碼 2，訊息 `--repo <path>: HEAD names no commit git can read (…)`。
