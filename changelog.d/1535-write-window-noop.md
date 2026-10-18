---
section: Fixed
topic: tenant-api
issues: [1535]
created: 2026-10-18T01:11:25+00:00
---
- **tenant-api：直寫不再蓋掉外部 commit，也不再把沒 commit 的狀態當成 HEAD；新增 409 `TENANT_FILE_DIRTY`（breaking，[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)）**：`PUT /tenants/{id}`、`PUT /tenants/{id}/custom-alerts` 改在讀租戶檔前取樣要落在其上的 commit，讀檔後才落地的外部 commit 回 `409 CONFLICT`。直寫模式下租戶檔在 tenant-api 那份 repo 與 HEAD 不一致時（未 commit 的修改、staged、刪除、未追蹤的同名檔、`skip-worktree`／`assume-unchanged` 標記，或經 symlink 抵達時沿路連結或 repo 內目標被改；repo 外、巢狀 repo 內的目標不判）回 `409 TENANT_FILE_DIRTY`（固定訊息），`/validate`、`/diff` 同判，直寫 batch 整批 422、該筆帶此 code；以前會把外部修改掛在 API 使用者名下 commit，或回成功卻沒 commit。只擋進寫入鎖時已存在的不一致。要經 API 修復的懸空 symlink 租戶檔須先 commit。conf.d 不是 git repo 或 HEAD 讀不到時，寫入在取樣處回 `500`（先於 body 驗證的 4xx），啟動時記 WARN。portal 對此 code 顯示後端訊息、不提示重整。
