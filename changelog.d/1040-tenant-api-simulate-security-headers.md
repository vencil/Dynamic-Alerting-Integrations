---
section: Added
topic: tenant-api-rbac-identity
issues: [1040]
created: 2026-11-07T14:04:55+00:00
---
- **tenant-api 回 simulate 的 501 與 API 安全 header（tenant-api；[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §5；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：portal 改由 tenant-api 的 pod 提供後，`/api/` 一律到 tenant-api，原本由獨立 portal 的 nginx 回的兩件事改由 tenant-api 做：`POST /api/v1/tenants/simulate` 回 501 `SIMULATE_NOT_PROVIDED`（同一個 code 與文字；只有 POST，其他方法仍是名為 `simulate` 的租戶），以及每個回應（含錯誤）帶 `X-Content-Type-Options`、`X-Frame-Options`、`Content-Security-Policy`、`Strict-Transport-Security`、`Referrer-Policy`。
