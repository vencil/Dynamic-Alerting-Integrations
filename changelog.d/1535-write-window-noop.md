---
section: Fixed
topic: tenant-api
issues: [1535]
created: 2026-10-18T01:11:25+00:00
---
- **tenant-api：直寫模式不再蓋掉外部 commit，也不再把工作樹上沒 commit 的狀態當成 HEAD；新增 409 `TENANT_FILE_DIRTY`（breaking，[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)）**：`PUT /tenants/{id}`、`PUT /tenants/{id}/custom-alerts` 與 `WriteMerged` 改在讀租戶檔之前就取樣要落在其上的 commit，讀檔之後才落地的外部 commit 回 `409 CONFLICT`、什麼都不寫。直寫模式下，租戶檔在 tenant-api 那份工作樹（含 index）與 HEAD 不同時（外部未 commit 的修改、staged、刪除、未追蹤的同名檔，或 symlink 指向 repo 內的目標被改；目標在 repo 外不判），寫入改回 `409 TENANT_FILE_DIRTY`（固定訊息、不含路徑），`/validate`、`/diff` 同判；直寫 batch 整批 422、該筆帶此 code。先在 repo 裡 commit 或捨棄再重試。原本這類寫入會把外部修改掛在 API 使用者名下 commit，或回成功卻沒 commit。錯誤優先序也變了：conf.d 不是 git repo 或 HEAD 讀不到時，寫入在取樣處就回 `500`，先於 body 驗證的 4xx；啟動時記一行 WARN。
