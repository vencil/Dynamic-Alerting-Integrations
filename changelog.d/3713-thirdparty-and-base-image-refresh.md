---
section: Security
topic: security-supply-chain
issues: [3713, 3715]
created: 2026-11-12T16:03:41+00:00
---
- **第三方 image 與 Dockerfile base image 升版（[#3713](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/3713)、[#3715](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/3715)）**：Prometheus → v3.16.0、prometheus-config-reloader → v0.95.1、prom-label-proxy → v0.16.2、oauth2-proxy → v7.15.6、vmauth → v1.156.0；distroless python3 與 alpine/git 只換 digest。tenant-api、audit-sidecar、projection-gate 的 runtime base 升到 alpine 3.24.4，da-portal 升到 nginx 1.31。da-read 的 Prometheus 模組跟著到 v0.316.0（duration expression 已成常駐功能）。
  ⚠️ 需自建 image 的兩個 chart 要先 rebuild 並 push 再升級：federation-gateway 0.7.1 的 `auditLog.image.tag` 改為 `alpine3.24.4-1`，vector 0.12.1（有設 `tenantProjections` 時）的 `projectionGate.image.tag` 改為 `0.1.2`；沒有新 tag 的 image，對應的 container 會 ImagePullBackOff。
