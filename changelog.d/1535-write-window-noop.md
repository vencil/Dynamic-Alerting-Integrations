---
section: Fixed
topic: tenant-api
issues: [1535]
created: 2026-10-18T01:11:25+00:00
---
- **tenant-api：直寫模式不再蓋掉 tenant-api 以外的 commit，batch 也不再把沒 commit 的變更回報成功（[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)）**：`PUT /tenants/{id}` 與 `PUT /tenants/{id}/custom-alerts` 改在讀租戶檔之前就取樣要落在其上的 commit，讀檔之後才落地的外部 commit 一律回 `409 CONFLICT`、什麼都不寫，不再被覆蓋後回 200。直寫 batch（`POST /tenants/batch`、group batch）判斷某項「沒有變更」時改比對 HEAD 的內容而非工作樹，工作樹上有未 commit 的外部編輯時照常 commit，不再回成功卻沒有 commit。
