---
section: Added
topic: tenant-api-rbac-identity
issues: [657, 962, 1040]
created: 2026-09-26T17:00:00+00:00
---
- **RBAC 可依身分裡的 claim 與組織授權（opt-in，預設 shadow）**：`--identity-claim-headers` 宣告由 trusted hop 注入的具名 claim，`GET /api/v1/me` 會回傳 `claims`。`_rbac.yaml` 規則新增選配 `match:`（`groups`／`claims`）與 `org-scope:`，後者以平台管理的 `_tenant_orgs.yaml` 把規則範圍限縮到使用者所屬組織的租戶。`--rbac-org-scope-enforce` 一支開關同時支配清單、寫入與 read-by-id；清單面的 env／domain metadata 另有 `--rbac-metadata-scope-enforce`。shadow 模式對未標記的租戶照舊放行，只記 `tenant_api_scope_would_deny_total{axis=...}`，soak 期間增量為 0 再切 enforce；⚠️ 但已在 `_tenant_orgs.yaml` 標記組織的租戶，shadow 下就會拒絕組織不符者，標記前須確認 org claim 經每個入口（含 recipe-preview 的 `PREVIEW_CLAIM_HEADERS`）送達。⚠️ `_rbac.yaml` 改為嚴格解析：未知欄位、空 `match:`、引用未宣告的 claim key 都會載入失敗（初次載入 fatal，hot-reload 保留 last-good）；合法的既有設定不受影響（[#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962)、[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）。
