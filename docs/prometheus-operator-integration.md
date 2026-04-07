---
title: "Prometheus Operator 整合手冊（Hub）"
tags: [operator, integration, kube-prometheus-stack]
audience: [platform-engineer]
version: v2.6.0
lang: zh
---
# Prometheus Operator 整合手冊

> **受眾**：Platform Engineers、SREs
> **版本**：v2.6.0
> **相關 ADR**：[ADR-008 — Operator CRD 路徑](adr/008-operator-native-integration-path.md)

---

## Overview

本手冊是 Prometheus Operator（kube-prometheus-stack）環境下，Dynamic Alerting 整合的導航頁面。

v2.6.0 起，本手冊拆分為四份對稱文件，與 ConfigMap 路徑（BYO）的文件結構一一對應：

| Operator 路徑（Path B） | 對應 ConfigMap 路徑（Path A） | 說明 |
|--------------------------|-------------------------------|------|
| [Operator Prometheus 整合](operator-prometheus-integration.md) | [BYO Prometheus 整合](byo-prometheus-integration.md) | ServiceMonitor + PrometheusRule |
| [Operator Alertmanager 整合](operator-alertmanager-integration.md) | [BYO Alertmanager 整合](byo-alertmanager-integration.md) | AlertmanagerConfig + 6 種 Receiver 模板 + 三態抑制規則 |
| [Operator GitOps 部署](operator-gitops-deployment.md) | [GitOps 部署指南](gitops-deployment.md) | ArgoCD / Flux 整合 + CI Pipeline |
| [Operator Shadow Monitoring](operator-shadow-monitoring.md) | [Shadow Monitoring SOP](shadow-monitoring-sop.md) | 雙軌觀察策略 |

---

## 快速選路

### 還不確定要用哪條路徑？

→ 參考 [Deployment Decision Matrix](getting-started/decision-matrix.md)

### 第一次部署 Operator 路徑？

1. [Operator Prometheus 整合](operator-prometheus-integration.md) — 設定 ServiceMonitor + PrometheusRule
2. [Operator Alertmanager 整合](operator-alertmanager-integration.md) — 設定 AlertmanagerConfig + Receiver
3. [Operator GitOps 部署](operator-gitops-deployment.md) — 接入 CI/CD pipeline

### 從 ConfigMap 路徑遷移？

→ [Operator GitOps 部署](operator-gitops-deployment.md) § Migration Path

### Helm Chart `rules.mode` 切換

v2.6.0 的 threshold-exporter Helm chart 新增 `rules.mode: operator` toggle：

```yaml
rules:
  mode: operator
  operator:
    ruleLabels:
      prometheus: kube-prometheus
    serviceMonitor:
      enabled: true
    receiverTemplate: slack
    secretRef:
      name: da-alerts-secret
      key: webhook-url
```

---

## 相關資源

| 資源 | 說明 |
|------|------|
| [ADR-008 — Operator CRD 路徑](adr/008-operator-native-integration-path.md) | 架構決策記錄（含 v2.6.0 邊界宣言） |
| [da-tools CLI Reference — operator-generate](cli-reference.md#operator-generate) | CLI 使用手冊 |
| [架構與設計](architecture-and-design.md) | 平台整體架構 |
| [Prometheus Operator 官方文檔](https://prometheus-operator.dev/) | 上游文件 |
