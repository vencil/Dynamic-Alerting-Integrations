---
section: Changed
topic: tenant-api-rbac-identity
issues: [3880]
created: 2026-11-14T07:09:45+00:00
---
- **GitLab CE SSO 指南與 try-local 的身分敘述改成 ADR-027 之後的模型（docs、try-local；[#3880](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3880)）**：指南不再寫「後端盲信 `X-Forwarded-Groups`、production cutover 待身份硬化」，改為 production 需部署含 ADR-027 的 tenant-api，並附一行 curl 確認叢集裡的版本（8080 帶身分 header 打 `/health`：400 為新版、200 為舊版）；撤權延遲受 `cookieRefresh` 約束改列為已接受的限制。try-local 的 `_rbac.yaml` 與 compose 的 `TA_DEV_BYPASS_GROUPS` 改由測試綁定：每個開發旁路注入的群組都必須是平台管理員（`tenants: ["*"]`、`admin`、無範圍軸）。
