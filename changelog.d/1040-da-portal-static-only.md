---
section: Removed
topic: tenant-api-rbac-identity
issues: [1040, 1014]
created: 2026-11-11T15:27:51+00:00
---
- **⚠️ breaking：da-portal chart 只剩純靜態部署（helm/da-portal；[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §5；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：刪除它的 oauth2-proxy sidecar 與 Secret、Ingress、對 tenant-api／recipe-preview 的反向代理、relay token 與 njs、`values-tier1.yaml`／`values-tier2.yaml`；pod 只剩一個 nginx 容器，NetworkPolicy 不放行任何 egress。image 的 nginx 設定也不再轉送。需要登入的 portal 改用 tenant-api chart（portal 容器在它的 pod 裡）。新增 `values.schema.json`：仍設 `oauth2Proxy`、`ingress`、`portal.tenantApiUrl`、`portal.recipePreviewUrl`、`portal.relayToken`、`service.internalPort`、`networkPolicy.egress` 或 `resources.nginx` 的 values 檔會讓 install／upgrade 失敗。部署精靈的 Tier 2 不再產生 da-portal release。升級前：把登入入口改到 tenant-api 的 Ingress，再刪掉上述 key 或解除安裝 da-portal。
