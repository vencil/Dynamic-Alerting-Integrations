---
section: Added
topic: tenant-api-rbac-identity
issues: [962, 3160]
created: 2026-09-26T17:00:00+00:00
---
- **存取稽核與身分自見性工具（tenant-api、portal）**：新增平台管理員專用的逆向存取報告 `GET /api/v1/audit/tenants/{id}/access-report`（誰、經哪條規則、在什麼組織條件下能對租戶做什麼）與 what-if 試算 `POST .../access-report/dry-run`（貼上候選 `_rbac.yaml`，比較生效前後的差異），兩者都有 redacted 視圖，非管理員一律 403。⚠️ 這裡的平台管理員是 `tenants: ["*"]`、帶 `admin` 且**不帶 `org-scope`** 的規則；同一門檻也套用在改平台聯邦白名單（`PUT /federation/policy`）與 AccountID backfill——帶 org-scope 的 `"*"` admin 在這幾支一律 403，不論 org-scope 是否 enforce（[#3160](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3160)）。**BREAKING**：saved views 的讀寫也改要求不帶 org-scope 的 `"*"` 規則（GET 要 read、PUT／DELETE 要 write，open mode 照舊可讀），帶 org-scope 的 `"*"` 規則一律 403，portal 改為隱藏 saved views 面板。Portal 新增對應的「存取報告 What-if 試算」工具，Tenant Manager 顯示 org 徽章與存取範圍面板（`/me` 新增 `org_claim_keys`）。RBAC 設定精靈的輸出原本無法通過 tenant-api 的嚴格解析，現已修正並支援 claims／org-scope。`_rbac.yaml`、`_domain_policy.yaml`、`_tenant_orgs.yaml` 新增 merge 前的 schema 驗證（[#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962)）。
