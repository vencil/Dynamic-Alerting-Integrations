---
section: Changed
topic: tenant-api
issues: [3226]
created: 2026-10-21T18:30:48+00:00
---
- **⚠️ 錯誤回應一律是 JSON 信封、batch 逐筆結果一律帶 code（tenant-api；[#3226](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3226)）**：沒有路由回 404 `NOT_FOUND`、方法不對回 405 `BAD_REQUEST`（`Allow` 照舊）、超過請求期限而 handler 沒回應回 504 `UPSTREAM_ERROR`、panic 回 500 `INTERNAL_ERROR`，原本是純文字或空 body；SSE 在關機中回 503＋`Retry-After: 1`；logs federation 未設定的 503 不帶 `Retry-After`。batch 每筆 `status: error` 的結果都帶 `code`，與同一失敗的整體回應相同（原本多數為空，例如過載、conflict、取消、驗證、權限）；base 同步逾時改 `FORGE_UNAVAILABLE`、已 commit 未收尾的不再說 conflict。見 [hardening §2.5](docs/api/tenant-api-hardening.md)。
