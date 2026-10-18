---
section: Fixed
topic: tenant-api
issues: [1535]
created: 2026-10-18T01:11:25+00:00
---
- **tenant-api：直寫模式不再蓋掉 tenant-api 以外的 commit，也不再把工作樹上沒 commit 的狀態當成 HEAD（[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)）**：`PUT /tenants/{id}`、`PUT /tenants/{id}/custom-alerts` 與 `WriteMerged`，改在讀租戶檔之前就取樣要落在其上的 commit，讀檔之後才落地的外部 commit 一律回 `409 CONFLICT`、什麼都不寫，不再被覆蓋後回 200。直寫 batch（`POST /tenants/batch`、group batch）與 `WriteMerged` 判斷「沒有變更」時，改以 HEAD 會存成的 blob 比對（套用 `.gitattributes` 正規化），不再比工作樹的原始位元組；合併結果在工作樹上沒有變更、但工作樹與 HEAD 不同時，改以 HEAD 的內容重算合併：仍無變更就不寫，有變更就寫入以 HEAD 算出的結果，工作樹上沒 commit 的外部編輯不會掛在 API 使用者名下 commit。錯誤優先序也變了：conf.d 不是 git repo 或 HEAD 讀不到時，寫入在取樣這一步就回 `500`，先於 body 驗證的 4xx；啟動時會記一行 WARN。
