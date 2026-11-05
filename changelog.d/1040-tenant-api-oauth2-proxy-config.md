---
section: Changed
topic: tenant-api-rbac-identity
issues: [1040]
created: 2026-11-05T11:07:01+00:00
---
- **⚠️ Breaking：tenant-api 信任的身分 header 由 oauth2-proxy 設定檔決定（tenant-api、helm；[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §2、§10；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：chart 的 oauth2-proxy 改用結構化設定檔（`--alpha-config`，ConfigMap），provider、header 注入與 listener 都在檔裡，同一份檔掛進 tenant-api（`--oauth2-proxy-config`／`TA_OAUTH2_PROXY_CONFIG`）；tenant-api 讀其中的 `injectRequestHeaders` 決定 email、group 與各 claim 的 header，形狀不含糊才啟動（`config check --kind oauth2-proxy` 可先檢查）。升級要改：OAuth client ID 改由 `oauth2Proxy.clientID` 給（Secret 的 `OAUTH2_PROXY_CLIENT_ID` 不再讀）；額外 claim 由 `oauth2Proxy.claimHeaders`（claim 名稱 → header）宣告；`oauth2Proxy.cookieSameSite` 只收小寫（舊預設 `Lax` 讓 oauth2-proxy v7.15.5 拒絕啟動）；oauth2-proxy 不再注入 `X-Forwarded-User`／`X-Forwarded-Preferred-Username` 與 `X-Auth-Request-*` 回應 header。
