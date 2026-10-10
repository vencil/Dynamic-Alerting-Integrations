---
section: Fixed
topic: tenant-api
issues: [2778]
created: 2026-10-10T11:30:48+00:00
---
- **tenant-api 超過 `TA_MAX_BODY_BYTES` 的寫入 body 改回 413，不再截斷後照寫（[#2778](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2778)）**：`PUT /tenants/{id}`、`PUT /tenants/{id}/custom-alerts`、`POST /tenants/{id}/validate`、`POST /tenants/{id}/diff`、`PUT /groups/{id}`、`PUT /views/{id}` 與 access-report dry-run（diff 只限未遮罩憑證的呼叫者，遮罩預覽本來就回 413）過去只讀上限內的前段、不報錯；截斷後仍是合法 YAML 的 body 會被當成完整內容寫入，尾段靜默消失。現在一律回 413（code `PAYLOAD_TOO_LARGE`，訊息附上限與 `TA_MAX_BODY_BYTES`），不寫入任何東西。原本因截斷而回 400「invalid JSON」的請求，現在也改回 413。OpenAPI 文件已為上述端點補上 413。
