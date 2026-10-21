---
section: Changed
topic: tenant-api
issues: [3202]
created: 2026-10-21T19:40:00+00:00
---
- **⚠️ breaking：一個錯誤碼只有一種處理方式，舊碼拆出新碼（tenant-api；[#3202](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3202)）**：status 與文字不變，`code` 改為：409 `CONFLICT` → 兩檔宣告同一租戶 `TENANT_FILE_AMBIGUOUS`、批次已 commit 未收尾 `BATCH_COMMITTED`、token 達上限 `TOKEN_LIMIT_REACHED`；429 `GENERATOR_REJECTED` → `GENERATOR_BUSY`；503 `UPSTREAM_ERROR` → 寫入結果未知 `WRITE_OUTCOME_UNKNOWN`、federation 未設定 `FEDERATION_NOT_CONFIGURED`；forge 拒絕 tenant-api 自己的 token 403 `FORBIDDEN` → `FORGE_FORBIDDEN`。`CONFLICT` 只剩「重整後重送」。OpenAPI `ErrorResponse.code` 改為列舉 `ErrorCode`；portal 契約檔 `tenant-api-errors.json` 由 `make error-contract` 產生（每碼的 `remedy`／`retryable`），CI 擋漂移。見 [hardening §2.6](docs/api/tenant-api-hardening.md)。
