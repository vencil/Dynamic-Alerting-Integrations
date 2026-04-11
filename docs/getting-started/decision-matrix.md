---
title: "Deployment Decision Matrix"
tags: [getting-started, decision, operator, configmap]
audience: [platform-engineer]
version: v2.6.0
lang: zh
---
# Deployment Decision Matrix

> **受眾**：Platform Engineers
> **版本**：v2.6.0
> **用途**：幫助你在 5 分鐘內決定使用 ConfigMap（Path A）或 Operator CRD（Path B）部署 Dynamic Alerting

---

## 快速決策樹

```
你的叢集已安裝 kube-prometheus-stack？
  ├─ 是 → 你使用 ArgoCD / Flux 做 GitOps？
  │        ├─ 是 → ★ Operator 路徑（Path B）
  │        └─ 否 → 你有多個 Prometheus 實例？
  │                  ├─ 是 → ★ Operator 路徑（Path B）
  │                  └─ 否 → 皆可，看團隊偏好
  └─ 否 → 你計劃安裝 kube-prometheus-stack？
           ├─ 是 → ★ Operator 路徑（Path B）
           └─ 否 → ★ ConfigMap 路徑（Path A）
```

---

## 詳細比較表

| 維度 | ConfigMap（Path A） | Operator CRD（Path B） |
|------|---------------------|------------------------|
| **前置條件** | 任何 Prometheus 環境 | kube-prometheus-stack 已安裝 |
| **Rule Pack 載入** | projected volume / configMapGenerator | PrometheusRule CRD |
| **路由配置** | `generate_alertmanager_routes.py` → ConfigMap | `operator-generate` → AlertmanagerConfig CRD |
| **配置重載** | configmap-reload sidecar | Operator 自動 reconcile |
| **GitOps 支援** | 手動管理 ConfigMap YAML | `--gitops` 產出 deterministic YAML |
| **多 Prometheus** | 複雜（需手動分配 ConfigMap） | 原生支援（namespace-scoped CRD） |
| **遷移複雜度** | 低（直接掛載） | 中（需轉換 CRD 格式） |
| **Receiver 模板** | 5 種（YAML 模板） | 6 種（secretKeyRef 安全引用） |
| **驗證工具** | `validate_config.py` | `operator-check` |
| **學習曲線** | 低 | 中（需理解 CRD + Operator 概念） |

---

## 推薦方案

### 適合 ConfigMap 路徑的場景

- 沒有使用 kube-prometheus-stack
- 單一 Prometheus 實例的簡單環境
- 團隊不熟悉 Kubernetes CRD
- 需要支援非 K8s 環境（VM、Docker Compose）

→ 開始：[BYO Prometheus 整合指南](../integration/byo-prometheus-integration.md)

### 適合 Operator 路徑的場景

- 已安裝 kube-prometheus-stack
- 使用 ArgoCD / Flux 做 GitOps
- 多個 Prometheus 實例或多叢集 Federation
- 需要 CRD-level 的 RBAC 控制
- Enterprise 環境要求 Secret 不以明文存在 YAML

→ 開始：[Operator Prometheus 整合指南](../integration/operator-prometheus-integration.md)

---

## 混合注意事項

**同一叢集的 Alertmanager 不可同時使用兩種路徑管理路由**。Prometheus 端（Rule Pack 載入）可以混合使用，但 Alertmanager 路由必須擇一。詳見 [ADR-008](../adr/008-operator-native-integration-path.md)。

---

## 相關文件

| 文件 | 說明 |
|------|------|
| [Platform Engineer 入門](for-platform-engineers.md) | 完整入門指南 |
| [ADR-008](../adr/008-operator-native-integration-path.md) | 雙路徑架構決策 |
| [Operator 整合手冊（Hub）](../integration/prometheus-operator-integration.md) | Operator 路徑導航 |
