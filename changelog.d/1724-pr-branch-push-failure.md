---
section: Fixed
topic: tenant-api
issues: [1724]
created: 2026-10-20T14:06:11+00:00
---
- **⚠️ breaking：PR 模式 push 失敗改回 503 `GIT_PUSH_FAILED`、不開 PR；分支名加亂數後綴（tenant-api；[#1724](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1724)）**：分支名原本只到秒，同一秒的兩次寫入撞名，第二次 push 被拒卻只記 WARN、照樣回 200 `pending_review`。現在分支名是 `tenant-api/<租戶>/<UTC 秒>-<12 位十六進位亂數>`（批次 `tenant-api/batch/…` 同）。push 任何失敗（含逾時）回 503 `GIT_PUSH_FAILED`＋`Retry-After`，訊息固定，細節只進 log；不開 PR/MR，批次整批同一錯誤；本機分支刪掉（重試會重建 commit）。PR 路徑其他 git 逾時或請求取消改回 503（原為 500）。PR 模式必須有可推送的 origin：沒有的部署由 200 變 503。push 逾時時 origin 可能留下無 PR 的分支；比對分支名格式的腳本要更新。
