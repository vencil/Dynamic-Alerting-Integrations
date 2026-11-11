---
section: Added
topic: tenant-api-rbac-identity
issues: [1040]
created: 2026-11-11T15:45:00+00:00
---
- **portal 簽發、列出、撤銷 federation token（Tenant Manager；[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §4；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：在登入後的 Tenant Manager 只選一個租戶時多一個 **Federation tokens** 按鈕，開出的對話框列出該租戶未過期的 token、可簽發 `metrics` 或 `logs` token（簽好的 token 只顯示一次並可複製）、可撤銷（需再確認一次）；token 還沒保存前關閉對話框會先詢問。不勾租戶時另有「依租戶 ID」入口，撤銷已從 conf.d 移除的租戶留下的 token。需要對該租戶的 admin 權限；部署沒有設定簽章金鑰時對話框說明未啟用。示範資料下不顯示這個按鈕。[Tenant Federation](docs/integration/tenant-federation.md)、[Tenant Log Query](docs/integration/tenant-log-query.md) 與 offboarding runbook 改為經 portal 操作，不再以 curl 直接打 tenant-api；目前沒有自動換發的方式（機器呼叫端依 ADR-027 §4 延後），持續拉取需在 TTL 內經 portal 換發。
