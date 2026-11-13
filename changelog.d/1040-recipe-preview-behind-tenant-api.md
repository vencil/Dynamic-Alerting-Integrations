---
section: Changed
topic: tenant-api-rbac-identity
issues: [1040, 1011]
created: 2026-11-11T16:29:17+00:00
---
- **⚠️ breaking：would-fire 預覽改由 tenant-api 授權後經 pod 內 socket 呼叫（tenant-api、recipe-preview、portal；[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §5；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：新增 `POST /api/v1/tenants/{id}/recipe-preview`（讀取權限，body `{recipe, scenario}`），portal 改呼叫它；`GET /api/v1/tenants/{id}/access` 刪除。recipe-preview 只聽 `PREVIEW_SOCKET`（`0600`、只回應同 uid）、`POST /v1/preview`，不再讀身分、不再向 tenant-api 查授權，刪除網路埠與 `PREVIEW_TENANT_API_URL`、`PREVIEW_DEV_BYPASS_*`、`PREVIEW_AUTH_TOKEN_FILE`、`PREVIEW_CLAIM_HEADERS`、`PREVIEW_LISTEN_*`。tenant-api 新增 `--recipe-preview-socket`（`TA_RECIPE_PREVIEW_SOCKET`），把請求剩下的時間減 1 秒傳給它；排隊與評估都以此為限，過期回 `429 PREVIEW_BUSY`（`reason: deadline`）。新錯誤碼 `PREVIEW_BUSY`、`PREVIEW_UNAVAILABLE`、`PREVIEW_NOT_CONFIGURED`。try-local 與 tenant-api chart 走同一拓撲：chart 新增 `recipePreview` 容器（預設開啟，image tag 跟隨 `generator.image.tag`），獨立的 `helm/recipe-preview` chart 刪除。
