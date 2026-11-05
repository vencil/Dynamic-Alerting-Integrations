---
section: Added
topic: tenant-api-rbac-identity
issues: [657, 962, 1040]
created: 2026-09-26T17:00:00+00:00
---
- **RBAC 可依身分裡的 claim 與組織授權（opt-in）**：oauth2-proxy 設定檔注入的額外 claim（helm `oauth2Proxy.claimHeaders`，見 ADR-027 §2 那份片段）載入身分，`GET /api/v1/me` 會回傳 `claims`。`_rbac.yaml` 規則新增選配 `match:`（`groups`／`claims`）與 `org-scope:`，後者以平台管理的 `_tenant_orgs.yaml` 把規則範圍限縮到使用者所屬組織的租戶。org 軸作用於清單、寫入與 read-by-id，一律強制（見 ADR-027 §7 那份片段）。⚠️ `_rbac.yaml` 改為嚴格解析：未知欄位、空 `match:`、引用沒有 header 注入的 claim 都會載入失敗（初次載入 fatal，hot-reload 保留 last-good）；合法的既有設定不受影響（[#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962)、[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）。
