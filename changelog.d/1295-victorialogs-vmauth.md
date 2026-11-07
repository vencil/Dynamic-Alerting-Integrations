---
section: Added
topic: helm
issues: [1295]
created: 2026-11-06T01:40:00+00:00
---
- **VictoriaLogs 前面加上 vmauth，逐一限制消費端可用的路徑（victorialogs chart 0.4.0；[#1295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1295)）**：VictoriaLogs 改為只監聽 pod 內 loopback，Service 的 :9428 由 vmauth sidecar 接手。`auth.consumers` 是誰能連、能用哪些路徑的單一清單（Vector 只能寫入，其他只能查詢），密碼取自維運方建立的 Secret。預設 `auth.mode: observe`，沒帶帳密的請求照常通過，升級不影響既有消費端；所有消費端接上帳密後切 `enforce`，未認證只剩 `/health`、`/ping`、`/metrics`。五個消費端都已能帶帳密：vector、chargeback-aggregator、federation-gateway、federation-reconciler 四個 chart 以 `victorialogs.auth.existingSecret` 指定 Secret（chart 0.12.0／0.3.0／0.7.0／0.4.0；不指定則行為不變），Grafana 的 datasource 改用 basic auth、密碼取自 Secret `victorialogs-grafana`。步驟見 `docs/internal/platform-log-aggregation-runbook.md` §1.2。
