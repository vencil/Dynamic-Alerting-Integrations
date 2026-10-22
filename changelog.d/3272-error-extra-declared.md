---
section: Changed
topic: tenant-api
issues: [3272]
created: 2026-10-22T16:20:00+00:00
---
- **錯誤回應的附加欄位改由錯誤碼宣告，只送宣告過的欄位（tenant-api；[#3272](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3272)）**：每個碼可帶的欄位（如 `existing_pr_url`、`tenant_id`）與 JSON 型別宣告在 `internal/errcode`；回應只送該碼宣告的欄位，`INTERNAL_ERROR` 不帶任何附加欄位，附加欄位也蓋不掉 `code`、`error`。現有回應不變（零值欄位照送）。`tenant-api-errors.json` 逐碼多列 `extra`；OpenAPI `ErrorResponse` 列出欄位聯集並註明哪些碼帶；設了未宣告欄位或型別不符會讓 `make error-contract` 失敗。`GET /api/v1/tasks/{id}` 補進 OpenAPI。見 [hardening §2.7](docs/api/tenant-api-hardening.md)。
