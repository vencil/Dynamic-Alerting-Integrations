---
section: Security
topic: tenant-api-rbac-identity
issues: [962]
created: 2026-09-26T17:00:00+00:00
---
- **tenant-api 信任邊界硬化：RBAC fail-closed**：⚠️ 沒有規則的 `_rbac.yaml` 拒絕存取（見 ADR-027 §7 那份片段）；畸形 `tenants` 樣式（如 `["**"]`）在載入時拒絕；rate-limit 與 audit 改用真實 TCP peer，不再信任 client 送的 `X-Real-IP`；⚠️ `GET /api/v1/groups/{id}` 改為依權限過濾成員，看不到任何成員的 group 回 404（[#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962)）。
