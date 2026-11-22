---
section: Fixed
topic: tenant-api
issues: [2778]
created: 2026-10-10T11:30:48+00:00
---
- **⚠️ breaking：tenant-api 超過 `TA_MAX_BODY_BYTES` 的寫入 body 改回 413，不再截斷後照寫（[#2778](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2778)）**：`PUT /tenants/{id}`、`PUT /tenants/{id}/custom-alerts`、`POST /tenants/{id}/validate`、`POST /tenants/{id}/diff`、`PUT /groups/{id}`、`PUT /views/{id}`、access-report dry-run（diff 只限未遮罩憑證的呼叫者）與 federation 的 `POST /federation/tokens`、`PUT /federation/policy`、`PUT /tenants/{id}/federation` 過去只讀上限內的前段、不報錯；截斷後仍可解析的 body 會被當成完整內容處理。現在一律回 413（code `PAYLOAD_TOO_LARGE`，訊息附上限與 `TA_MAX_BODY_BYTES`），不寫入任何東西。原本因截斷而回 400「invalid JSON」的請求（federation 三個端點的超量 body 都是）改回 413，依 400 判斷的 client 要一併處理 413。OpenAPI 文件已為上述端點補上 413。
