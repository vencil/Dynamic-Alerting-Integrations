---
title: "Operator GitOps 部署指南"
tags: [operator, gitops, argocd, flux]
audience: [platform-engineer]
version: v2.7.0
lang: zh
---
# Operator GitOps 部署指南

> **受眾**：Platform Engineers、SREs
> **版本**：v2.6.0
> **對應文件**：[GitOps 部署指南](gitops-deployment.md)（ConfigMap 路徑）
> **前置閱讀**：[Operator Prometheus 整合](operator-prometheus-integration.md)

---

## Overview

本指南涵蓋如何在 GitOps（ArgoCD / Flux）工作流中，使用 `operator-generate --gitops` 產出的 CRD YAML 實現全自動化的 Operator 路徑部署。

---

## 目錄結構

```
monitoring-config/
├── prometheus-operator-crds/
│   ├── prometheusrule/
│   │   ├── da-rule-pack-mariadb.yaml
│   │   ├── da-rule-pack-postgresql.yaml
│   │   ├── da-rule-pack-kubernetes.yaml
│   │   └── ...（15 個 Rule Pack）
│   ├── alertmanagerconfig/
│   │   ├── da-tenant-db-a.yaml
│   │   ├── da-tenant-db-b.yaml
│   │   └── ...（per-tenant）
│   ├── servicemonitor/
│   │   └── da-threshold-exporter.yaml
│   └── kustomization.yaml
├── secrets/                          # Sealed Secrets / External Secrets
│   ├── da-db-a-slack.yaml
│   └── da-db-b-pagerduty.yaml
└── README.md
```

---

## 使用 da-tools 產出 GitOps-friendly YAML

```bash
da-tools operator-generate \
  --config-dir ./conf.d \
  --output-dir ./prometheus-operator-crds \
  --receiver-template slack \
  --secret-name da-alerts \
  --gitops

# --gitops 效果：
#   - sorted keys（deterministic output）
#   - 無 timestamps、resourceVersion、status
#   - ArgoCD / Flux 不會誤報 OutOfSync
```

---

## ArgoCD 整合

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: dynamic-alerting-operator
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/your-org/monitoring-config
    targetRevision: main
    path: prometheus-operator-crds/
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

### CI Pipeline 範例

```yaml
# .github/workflows/update-crds.yml
name: Update Operator CRDs
on:
  push:
    paths: ['conf.d/**', 'rule-packs/**']

jobs:
  generate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Generate CRDs
        run: |
          docker run --rm \
            -v ${{ github.workspace }}/conf.d:/conf.d:ro \
            -v ${{ github.workspace }}/rule-packs:/rule-packs:ro \
            -v ${{ github.workspace }}/prometheus-operator-crds:/output \
            ghcr.io/vencil/da-tools:latest \
            operator-generate --gitops --output-dir /output
      - name: Commit & Push
        run: |
          git add prometheus-operator-crds/
          git diff --cached --quiet || git commit -m "chore: update operator CRDs"
          git push
```

---

## Flux 整合

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: dynamic-alerting-operator
  namespace: flux-system
spec:
  targetNamespace: monitoring
  sourceRef:
    kind: GitRepository
    name: monitoring-config
  path: ./prometheus-operator-crds/
  prune: true
  interval: 5m
```

---

## Secret 管理

Receiver 的機密資訊（Webhook URL、API Key 等）**禁止明文進入 Git repo**。推薦方案：

### Sealed Secrets

```bash
kubeseal --controller-name=sealed-secrets \
  --controller-namespace=kube-system \
  --format yaml < secret-slack.yaml > sealed-secret-slack.yaml
```

### External Secrets Operator

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: da-db-a-slack
  namespace: monitoring
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: da-db-a-slack
  data:
    - secretKey: webhook-url
      remoteRef:
        key: dynamic-alerting/db-a/slack
        property: webhook-url
```

---

## Migration Path: ConfigMap → Operator CRD

### Phase 1: Parallel Run（1 週）

同時部署 ConfigMap 與 CRD，比對告警產出：

```bash
# 保留舊 ConfigMap
kubectl get configmap -n monitoring | grep prometheus-rules

# 新增 PrometheusRule CRD
kubectl apply -f prometheusrule/

# 檢查衝突
kubectl logs -n monitoring prometheus-kube-prometheus-stack-prometheus-0 | grep -i "duplicate"
```

### Phase 2: Switch Alert Routes（1 天）

```bash
kubectl apply -f alertmanagerconfig/
curl -s http://localhost:9093/api/v1/alerts | jq '.alerts | length'
```

### Phase 3: Clean Up ConfigMap（1 天後）

```bash
# 備份
kubectl get configmap -n monitoring -o yaml > backup-configmaps.yaml

# 刪除舊 ConfigMap
kubectl delete configmap prometheus-rules-mariadb -n monitoring
```

### Rollback

```bash
kubectl apply -f backup-configmaps.yaml
kubectl delete -f prometheusrule/
```

---

## 相關文件

| 文件 | 說明 |
|------|------|
| [Operator Prometheus 整合](operator-prometheus-integration.md) | ServiceMonitor + PrometheusRule |
| [Operator Alertmanager 整合](operator-alertmanager-integration.md) | AlertmanagerConfig + Receiver |
| [GitOps 部署指南](gitops-deployment.md) | ConfigMap 路徑 GitOps |
| [ADR-008](../adr/008-operator-native-integration-path.md) | 架構決策記錄 |
