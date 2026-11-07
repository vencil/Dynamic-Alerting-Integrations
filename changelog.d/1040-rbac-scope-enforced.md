---
section: Changed
topic: tenant-api-rbac-identity
issues: [1040]
created: 2026-11-05T05:56:57+00:00
---
- **⚠️ Breaking：RBAC 範圍軸一律強制，沒有 open mode（tenant-api；[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §7；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：未設 `--rbac`、或 `_rbac.yaml` 沒有任何規則時，拒絕所有存取（含讀取）；`--rbac-empty-open` 移除。metadata 讀、metadata 寫、org 三條範圍軸不再有 shadow 模式：未在 `_metadata` 標記 environment／domain、或未在 `_tenant_orgs.yaml` 指派組織的租戶，在限制該軸的規則下不可見、不可寫。`--rbac-metadata-scope-enforce`、`--rbac-metadata-write-scope-enforce`、`--rbac-org-scope-enforce` 與對應 `TA_*` 移除（仍帶著它們 tenant-api 拒絕啟動），helm `rbac.*ScopeEnforce` 移除（render 失敗）。計數改為 `tenant_api_scope_denied_total{axis}`；逆向查詢與 dry-run 的 `org_gate` 合併為單一 `outcome`、拿掉 `flags`（schema_version 2）。環境與 domain 標記只有不受任何範圍限制的平台管理員能變更（只限部分環境或 domain 的 admin 不行）。升級前用新版 image 跑 `tenant-api config check` 的範圍檢查，列出未標記租戶並補齊。
