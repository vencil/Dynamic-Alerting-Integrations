---
title: "threshold-exporter Helm Chart"
tags: [helm, chart, threshold-exporter]
audience: [platform-engineer, operator]
version: v2.7.0
lang: zh
---

# threshold-exporter Helm Chart

Dynamic threshold exporter for the Multi-Tenant Alerting platform — ships tenant `user_threshold` metrics via Directory Scanner + SHA-256 hot-reload.

## 快速安裝

```bash
# OCI registry (推薦)
helm install threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter \
  -n monitoring --create-namespace \
  -f values-override.yaml

# 或指向本地 chart
helm install threshold-exporter ./helm/threshold-exporter \
  -n monitoring --create-namespace \
  -f values-override.yaml
```

## Chart 內容

建立 Deployment (2 replicas + PDB) · Service (含 Prometheus scrape annotations) · ConfigMap (`threshold-config`) · ServiceMonitor (optional)。

## 完整文件

- Go 應用 + 三種 config 注入方式（Helm / kubectl / GitOps）：[`components/threshold-exporter/README.md`](../../components/threshold-exporter/README.md)
- 架構與設計：[`docs/architecture-and-design.md`](../../docs/architecture-and-design.md)
- 部署整合：[`docs/byo-prometheus-integration.md`](../../docs/integration/byo-prometheus-integration.md) · [`docs/gitops-deployment.md`](../../docs/integration/gitops-deployment.md)
- 版本歷程：[`CHANGELOG.md`](../../CHANGELOG.md)

## values.yaml 參數

完整參數對照請見本目錄 [`values.yaml`](values.yaml)。常用覆寫：

| 參數 | 預設 | 說明 |
|------|------|------|
| `replicaCount` | `2` | HA 副本數；`> 1` 時自動建立 PDB（`minAvailable: 1`），沒有另外的開關 |
| `image.tag` | `""`（＝`v<Chart appVersion>`） | 留空由 chart appVersion 推導；需要釘版本才設 |
| `rules.mode` | `configmap` | `configmap` 或 `operator`。`operator` 時依 `rules.operator.serviceMonitor.*` 建立 ServiceMonitor；兩種模式都掛同一個 `threshold-config` 到 `/etc/threshold-exporter/conf.d`（固定路徑，不可設定）。⚠️ 本 chart 不出貨 Rule Pack——Rule Pack 是 `k8s/03-monitoring/configmap-rules-*.yaml`，需自行掛進 Prometheus |
| `thresholdConfig.max_metrics_per_tenant` | `null`（＝內建 500） | 每租戶 threshold series 上限；只寫進根目錄 `_defaults.yaml`，負值＝不截斷（#2028） |
