---
section: Fixed
topic: tenant-api
issues: [2597]
created: 2026-10-17T19:19:07+00:00
---
- **tenant-api 的 OpenAPI 規格不再組出 `/api/v1/api/v1/...`（tenant-api；[#2597](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2597)）**：`components/tenant-api/docs/swagger.json`／`swagger.yaml` 先前同時宣告 `basePath: /api/v1` 與完整路徑的 `paths`（`/api/v1/views`），依 OpenAPI 2.0 規則兩者相接，照規格產 client 或設 gateway 的人會打到不存在的路徑。現在拿掉 `basePath`，`paths` 維持完整路徑，實際路由不變。用 `--url` 指定伺服器的 schemathesis 契約測試原本就不讀 `basePath`，行為不受影響。
