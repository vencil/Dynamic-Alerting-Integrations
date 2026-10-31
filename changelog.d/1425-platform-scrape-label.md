---
section: Changed
topic: exporter
issues: [1425]
created: 2026-10-31T02:55:58+00:00
---
- **平台元件改依 Service label 被 Prometheus 探索；threshold-exporter 的 NetworkPolicy 改由 chart 渲染（breaking，[#1425](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1425)）**：threshold-exporter、Vector、tenant-api 的 Service 帶 `dynamic-alerting.io/scrape: platform`，新 job `platform-components` 在所有 namespace 依此收錄；`monitoring-components` 只剩 `monitoring` 的原始 manifest，`tenant-exporters` 不再收帶此 label 的 Service。exporter 的 NetworkPolicy（`threshold-exporter-ingress`／`-egress`）入站預設只限 metrics port、不限來源（`networkPolicy.allowExternal: true`）；要隔離就設 `false` 並填你 scraper 的 `prometheusNamespace` 與 `prometheusPodLabels`（kube-prometheus-stack 是 `app.kubernetes.io/name: prometheus`）。⚠️ 遷移順序：先升級三個 chart 讓 Service 帶上 label，再照 `k8s/03-monitoring/configmap-prometheus.yaml` 改 Prometheus job——反過來做，`tenant_api_*`、`vector_*` 告警沒有 absent 守衛，會靜默失效。舊的 `allow-threshold-exporter-*` 請刪除。
