---
section: Changed
topic: platform-self-monitoring
issues: [1595]
created: 2026-11-03T15:20:00+00:00
---
- **平台自監控告警在 Prometheus Operator 模式下也會跑（rule-packs／helm／tools；[#1595](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1595) D1b）⚠️ BREAKING**：`tenant-api`、`vector`、`federation-gateway`、`federation-reconciler` chart 新增 `serviceMonitor.enabled`（預設關、不設 `jobLabel`）；`operator-generate` 也輸出 `da-rule-pack-platform`，去掉 kube-prometheus-stack 沒有指標來源的 rule（由 metric family 表推導，stderr 與 annotation `dynamic-alerting.io/operator-excluded-rules` 點名），`operator-check` 的期望數隨之加一。federation 兩個 chart 的 Service 帶上 `dynamic-alerting.io/scrape: platform`，raw 模式在任何 namespace 都被抓，不必裝進 `monitoring`；`allow-prometheus-egress` 加開 3903／9099，gateway NetworkPolicy 補上 metrics port（`networkPolicy.scrapeNamespaceLabel`）。⚠️ 告警 label 變動：`TenantApiSingleWriterBreach`、`ThresholdExporterHighRestarts` 不再釘 namespace；`Config*`、`TenantMetricsOverLimit`、`DefaultsTruncationStorm`、`MassExporterOutage` 多副本只響一次、不再帶 `instance`／`pod`，以它們寫的 route、silence、inhibit 要改。
