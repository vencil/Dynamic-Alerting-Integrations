---
section: Changed
topic: exporter
issues: [1425]
created: 2026-10-31T02:55:58+00:00
---
- **平台元件改依 Service label 被 Prometheus 探索，不再列 namespace；threshold-exporter 的 NetworkPolicy 改由 chart 渲染（breaking，[#1425](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1425)）**：threshold-exporter、Vector、tenant-api 的 Service 帶 `dynamic-alerting.io/scrape: platform`，新 job `platform-components` 在所有 namespace 依此 label 收錄；`monitoring-components` 只剩 `monitoring` 裡的原始 manifest（KSM、Alertmanager），`tenant-exporters` 不再收帶這個 label 的 Service。chart 裝在哪個 namespace 都會被抓。⚠️ 遷移：自帶 Prometheus 設定的要照 `k8s/03-monitoring/configmap-prometheus.yaml` 改 job；exporter 的 NetworkPolicy 改由 chart 建（`networkPolicy.*`，名稱 `threshold-exporter-ingress`／`-egress`），舊的 `allow-threshold-exporter-*` 請自行刪除。
