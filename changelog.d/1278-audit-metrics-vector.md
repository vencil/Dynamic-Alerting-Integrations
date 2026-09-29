---
section: Changed
topic: security-supply-chain
issues: [1278]
created: 2026-09-29T16:30:00+00:00
---
- **federation-gateway 的 audit metrics sidecar 由 mtail 改為上游 Vector（helm；chart 0.6.0；[#1278](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1278)）**：mtail 上游已停擺，它內含的 grpc 是 audit-sidecar 映像唯一的 fixable HIGH/CRITICAL（4 筆）。改用與 `helm/vector` 同一個 Vector 映像執行 `files/audit-metrics.vector.yaml`：`tenant_federation_requests_total`、`tenant_log_query_requests_total`、`tenant_log_query_duration_ms` 的名稱、label 與 bucket 都不變，只少了 mtail 自動加的 `prog` label（沒有規則或 dashboard 使用）。logrotate 輪替時，新檔的行 mtail 會漏數，Vector 則完整計入。⚠️ values 變更：`auditLog.mtail` 改為 `auditLog.metrics`（新增 image 與 `dataSizeLimit`，記憶體上限 64Mi → 128Mi）；audit-sidecar 映像只剩 logrotate，tag 改為 `alpine3.23.6-1`，升級前要重新 build 並 push。
