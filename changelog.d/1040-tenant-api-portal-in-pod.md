---
section: Added
topic: tenant-api-rbac-identity
issues: [1040]
created: 2026-11-07T13:51:31+00:00
---
- **tenant-api 的 pod 內提供 portal，oauth2-proxy 依路徑分流（helm/tenant-api；[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §5、§10；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：pod 多一個 `portal` 容器（`ghcr.io/vencil/da-portal`，`portal.image` 釘在 portal 的 release 版本，由 `bump_docs.py --portal` 隨 release 改寫），只綁 pod 內 loopback、執行 chart 自帶的 nginx 設定，只提供靜態檔、不轉送任何請求。oauth2-proxy 的 `/api/` 送往 tenant-api 的 Unix socket，其餘送往 portal；`/api/` 下未登入回 401（`--api-route`），不轉址到 IdP。原本免登入的 `/health`、`/ready` 分流後會落到 portal，改為不再免登入；負載平衡器的健康檢查用 oauth2-proxy 的 `/ping`。
