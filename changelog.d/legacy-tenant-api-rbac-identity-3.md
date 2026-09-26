---
section: Security
topic: tenant-api-rbac-identity
issues: [962]
created: 2026-09-26T17:00:00+00:00
---
- **tenant-api 信任邊界硬化：機器身分稽核與 RBAC fail-closed**：新增 opt-in、只做稽核的機器身分驗證（`machineIdentity.enabled`），以 TokenReview 驗 audience 為 `tenant-api` 的 projected SA token，結果只寫 log 與 `tenant_api_identity_audit_total{result}`，不影響授權；threshold-govern、recipe-preview 與 da-portal relay 可掛上這顆 token，人流可改走 pod 內 Unix socket（`--human-socket`）。⚠️ 有設 `--rbac` 但檔案裡零 group 時改為拒絕存取（逃生旗標 `--rbac-empty-open`）；畸形 `tenants` 樣式（如 `["**"]`）在載入時拒絕；rate-limit 與 audit 改用真實 TCP peer，不再信任 client 送的 `X-Real-IP`；⚠️ `GET /api/v1/groups/{id}` 改為依權限過濾成員，看不到任何成員的 group 回 404（[#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962)）。
