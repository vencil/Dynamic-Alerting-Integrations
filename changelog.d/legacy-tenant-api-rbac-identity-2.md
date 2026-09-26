---
section: Added
topic: tenant-api-rbac-identity
issues: [962]
created: 2026-09-26T17:00:00+00:00
---
- **存取稽核與身分自見性工具（tenant-api、portal）**：新增平台管理員專用的逆向存取報告 `GET /api/v1/audit/tenants/{id}/access-report`（誰、經哪條規則、在什麼組織條件下能對租戶做什麼）與 what-if 試算 `POST .../access-report/dry-run`（貼上候選 `_rbac.yaml`，比較生效前後的差異），兩者都有 redacted 視圖，非管理員一律 403。Portal 新增對應的「存取報告 What-if 試算」工具，Tenant Manager 顯示 org 徽章與存取範圍面板（`/me` 新增 `org_claim_keys`）。RBAC 設定精靈的輸出原本無法通過 tenant-api 的嚴格解析，現已修正並支援 claims／org-scope。`_rbac.yaml`、`_domain_policy.yaml`、`_tenant_orgs.yaml` 新增 merge 前的 schema 驗證（[#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962)）。
