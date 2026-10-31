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

建立 Deployment (2 replicas + PDB) · Service (含 Prometheus scrape annotations) · ConfigMap (`threshold-config`；設 `thresholdConfig.existingConfigMap` 時不建) · ServiceMonitor (optional)。所有資源都落在 release 的 namespace（`-n`）；平台的 Prometheus SD 清單預設涵蓋 `monitoring`，裝到別的 namespace 要一併把它加進 SD 清單（#1425）。

⚠️ chart 帶 `values.schema.json`：這個 chart 沒有的 key（拼錯、或已移除的舊 key）會讓 `helm install`／`upgrade`／`template` 直接失敗，不再被靜默忽略（#1425）。`thresholdConfig.tenants`／`defaults`／`state_filters`、`service.annotations`、`resources`、各 probe、`rules.operator.serviceMonitor.labels` 是開放欄位，不受此限。

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
| `rules.operator.serviceMonitor.labels` | `{release: kube-prometheus-stack}` | `operator` 模式下加在 ServiceMonitor 上的 label，讓 Prometheus 的 `serviceMonitorSelector` 對得上（kube-prometheus-stack 預設比對 `release: <Helm release 名稱>`）；預設與 `da-tools operator-generate` 一致，stack 的 release 名稱不同就改 `release`，設 `release: null` 可拿掉；不可覆寫 `app`／`app.kubernetes.io/part-of`（#2075） |
| `thresholdConfig.existingConfigMap` | `""` | 設了名字時 chart 不建 `threshold-config`，改掛這個 ConfigMap；其餘 `thresholdConfig.*` 不再渲染。`da-tools init --deploy kustomize` 的樹自建 `threshold-config`，搭配它安裝時設 `threshold-config`、`-n` 與樹相同，否則兩份同名 ConfigMap 互相覆蓋（#1425） |
| `networkPolicy.enabled` / `.allowExternal` / `.prometheusNamespace` / `.prometheusPodLabels` | `true` / `true` / `""` / `{}` | 在 release namespace 建 exporter 的 NetworkPolicy：入站只開 metrics port、出站只有 DNS。`allowExternal: true`（預設）只限 port、不限來源——chart 無從得知你的 scraper 帶什麼 pod label（kube-prometheus-stack 是 `app.kubernetes.io/name: prometheus`，vmagent、blackbox probe 又各不同），猜錯會讓 scrape 靜默斷掉。要隔離就設 `allowExternal: false` 並填你 scraper 的 namespace 與 pod label；此時兩者都不可為空（空的 selector 會放行該 namespace 所有 pod，chart 直接拒絕）（#1425） |
| `thresholdConfig.max_metrics_per_tenant` | `null`（＝內建 500） | 每租戶 threshold series 上限；只寫進根目錄 `_defaults.yaml`，負值＝不截斷（#2028） |
| `startupProbe.failureThreshold` / `.periodSeconds` | `60` / `10`（＝最多等 10 分鐘） | 冷載入的等待上限。exporter 把 conf.d 載入完才啟動 HTTP server，載入期間 `/health` 連不上；startupProbe 成功前 liveness 不會執行，所以冷載入不會被重啟。載入超過 10 分鐘就調高 `failureThreshold`（實際載入秒數見 `da_config_initial_load_duration_seconds`）；設 `startupProbe: null` 可拿掉（#2153） |
