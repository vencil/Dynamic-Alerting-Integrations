---
section: Fixed
topic: tenant-api
issues: [1073]
created: 2026-11-21T12:41:14+00:00
---
- **寫入已存、但伺服器重讀快照失敗時不再回 200（tenant-api；[#1073](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1073)）**：groups、views 的 `PUT`／`DELETE` 與 `PUT /api/v1/federation/policy` 寫入 state store 後若重讀失敗，原本照回 200、`GET` 卻繼續回舊內容直到下一次週期性重讀。現在回 503 `STATE_RELOAD_FAILED`（`committed: true`，處理方式 `refresh`，不帶 `Retry-After`）：寫入已存，下一次週期性重讀後就會生效。client 不要盲目重送，先 `GET` 重讀確認（重送 `PUT` 會蓋掉期間別人的修改、重送 `DELETE` 在重讀後回 404）。portal 的群組建立／刪除收到它時保留畫面上的變更、顯示伺服器的說明，不再當成失敗撤回。新碼已進 OpenAPI 的 `ErrorCode` 與 portal 的錯誤契約；說明見 [tenant-api-hardening §2.8](docs/api/tenant-api-hardening.md)。
