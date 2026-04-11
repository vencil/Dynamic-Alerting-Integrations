---
title: "threshold-exporter Helm Chart"
tags: [helm, chart, threshold-exporter]
audience: [platform-engineer, operator]
version: v2.6.0
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
| `replicaCount` | `2` | HA 副本數（搭配 PDB） |
| `image.tag` | `v2.6.0` | threshold-exporter 映像版本 |
| `rules.mode` | `configmap` | Rule Pack 供應方式（`configmap` / `operator` / `disabled`） |
| `config.directory` | `/etc/threshold-exporter/conf.d` | Tenant config 掛載路徑 |
| `podDisruptionBudget.enabled` | `true` | 滾動升級期間保留最少可用副本 |
