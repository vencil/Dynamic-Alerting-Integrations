---
section: Changed
topic: platform-self-monitoring
issues: [1595]
created: 2026-11-03T15:20:00+00:00
---
- **平台自監控告警在 Prometheus Operator 模式下也會跑（rule-packs／helm／tools；[#1595](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1595) D1b）⚠️ BREAKING**：`tenant-api`、`vector`、`federation-gateway`、`federation-reconciler` chart 新增 `serviceMonitor.enabled`（預設關、不設 `jobLabel`）；tenant-api 的 8080 白名單多放行 kube-prometheus-stack 的 Prometheus（`internalPortAllow.prometheusOperator`）。`operator-generate` 也輸出 `da-rule-pack-platform`，去掉 kube-prometheus-stack 沒有指標來源的 rule（由 metric family 表推導，stderr 與 annotation `dynamic-alerting.io/operator-excluded-rules` 點名）。⚠️ 告警變動：`TenantApiSingleWriterBreach`、`ThresholdExporterHighRestarts` 不再釘 namespace；`Config*`、`TenantMetricsOverLimit`、`DefaultsTruncationStorm`、`MassExporterOutage` 多副本只響一次、不再帶 `instance`／`pod`，以它們寫的 route、silence、inhibit 要改；`FederationRevocationReconcileStale`（absent 那半）與 `FederationAuditPipelineSilent` 改以 kube-state-metrics 判斷 federation 是否部署：沒部署不再永遠 firing；部署了卻沒開 ServiceMonitor 會響，請開。
