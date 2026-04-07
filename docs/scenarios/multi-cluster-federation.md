---
title: "場景：多叢集聯邦架構 — 中央閾值 + 邊緣指標"
tags: [scenario, federation, multi-cluster]
audience: [platform-engineer]
version: v2.6.0
lang: zh
---
# 場景：多叢集聯邦架構 — 中央閾值 + 邊緣指標

> **快速指引** — 完整架構設計與原理見 [Federation Integration Guide](../federation-integration.md)。本文件聚焦部署步驟。

## 問題

組織運營多個 Kubernetes 叢集，面臨閾值離散、監控孤島、通知路由混亂等挑戰。需要統一的多叢集告警管理。

## 架構選擇

| 面向 | 中央評估 | 邊緣評估 |
|------|---------|---------|
| 適用規模 | < 20 邊緣叢集 | 20+ 邊緣或跨區高延遲 |
| 延遲 | ~60–90s（federation）/ ~30s（remote-write） | ~5–15s |
| 複雜度 | 低（單點部署） | 高（Rule Pack 需拆分） |

詳細比較見 [Federation Guide §1.2](../federation-integration.md#12-架構選擇中央評估-vs-邊緣評估)。

## 部署步驟（中央評估架構）

### Step 1：邊緣叢集配置

**1.1 設定 external_labels**

```yaml
# prometheus.yml (edge)
global:
  scrape_interval: 15s
  external_labels:
    cluster: "edge-asia-prod"    # 唯一識別
```

**1.2 Tenant 標籤注入**（選擇一種模式）

```yaml
# Namespace-to-Tenant 1:1
relabel_configs:
  - source_labels: [__meta_kubernetes_namespace]
    target_label: tenant

# 或使用 scaffold 工具自動產出
da-tools scaffold --tenant db-a --db postgresql --namespaces ns-prod,ns-staging
```

**1.3 驗證邊緣**

```bash
da-tools federation-check edge --prometheus http://edge-prometheus:9090
```

### Step 2：中央叢集配置

**2.1 選擇傳輸方案**

Federation（< 10 邊緣）或 Remote Write（10+ 邊緣）。配置範例見 [Federation Guide §4](../federation-integration.md#4-中央叢集配置)。

**2.2 部署 threshold-exporter HA**

```bash
helm upgrade --install threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.6.0 \
  -n monitoring --create-namespace -f values-override.yaml
```

**2.3 配置租戶閾值**

在 `conf.d/` 中建立租戶 YAML（與單叢集相同格式）。

**2.4 部署 Rule Pack + 驗證**

```bash
da-tools federation-check central --prometheus http://central-prometheus:9090
```

### Step 3：端對端驗證

```bash
da-tools federation-check e2e \
  --prometheus http://central-prometheus:9090 \
  --edge-urls http://edge-asia:9090,http://edge-europe:9090

da-tools diagnose db-a --prometheus http://central-prometheus:9090
```

## 檢查清單

**邊緣叢集**：external_labels 已設置 · Tenant relabel 正確 · DB exporter 運行 · Federation/remote-write 已啟用

**中央叢集**：threshold-exporter ×2 HA · Rule Pack 完整部署 · Alertmanager 路由配置 · 所有邊緣指標可見

**端對端**：跨叢集指標查詢 · 告警路由到正確通知通道 · Grafana 全域視圖

## 故障排查

| 症狀 | 診斷方向 | 常見原因 |
|------|----------|----------|
| 邊緣指標未到達中央 | `federation-check edge` | tenant label 未注入、match[] 過嚴、網路不可達 |
| 告警未觸發 | `federation-check central` | tenant 處於 silent/maintenance、路由缺少 matcher |
| Recording rule 無輸出 | `federation-check central` | Rule Pack 未掛載、指標命名不匹配 |

## 互動工具

- [Capacity Planner](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/capacity-planner.jsx) — 估算多叢集資源需求
- [Dependency Graph](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/dependency-graph.jsx) — Rule Pack 依賴關係視覺化

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [Federation Integration Guide](../federation-integration.md) | ⭐⭐⭐ |
| [ADR-004 Federation 中央 Exporter 優先](../adr/004-federation-central-exporter-first.md) | ⭐⭐ |
| [進階場景與測試覆蓋](../internal/test-coverage-matrix.md) | ⭐⭐ |
| [Shadow Monitoring 切換工作流](shadow-monitoring-cutover.md) | ⭐⭐ |
