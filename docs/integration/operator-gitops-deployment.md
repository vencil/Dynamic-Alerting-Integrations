---
title: "Operator GitOps 部署指南"
tags: [operator, gitops, argocd, flux]
audience: [platform-engineer]
version: v2.9.0
lang: zh
---
# Operator GitOps 部署指南

> **Language / 語言：** **中文 (Current)** | [English](./operator-gitops-deployment.en.md)

> **受眾**：Platform Engineers、SREs
> **版本**：v2.6.0
> **對應文件**：[GitOps 部署指南](gitops-deployment.md)（ConfigMap 路徑）
> **前置閱讀**：[Operator Prometheus 整合](operator-prometheus-integration.md)

---

## Overview

本指南涵蓋如何在 GitOps（ArgoCD / Flux）工作流中，使用 `operator-generate --gitops` 產出的 CRD YAML（PrometheusRule、ServiceMonitor）與 `generate-routes` 產出的 Alertmanager 設定 Secret，實現全自動化的 Operator 路徑部署。

---

## 目錄結構

```
monitoring-config/
├── prometheus-operator-crds/
│   ├── prometheusrule/
│   │   ├── da-rule-pack-mariadb.yaml
│   │   ├── da-rule-pack-postgresql.yaml
│   │   ├── da-rule-pack-kubernetes.yaml
│   │   └── ...（每個 Rule Pack 一份）
│   ├── servicemonitor/
│   │   └── da-threshold-exporter.yaml
│   └── kustomization.yaml
├── alertmanager/
│   └── alertmanager.base.yaml        # 你自己的 Alertmanager 設定（來源，不是產物）
├── secrets/                          # Sealed Secrets / External Secrets
│   ├── da-alertmanager-config.yaml   # generate-routes 的輸出，Alertmanager CR 的 configSecret
│   ├── da-db-a.yaml                  # 租戶 secretRef 引用的 Secret
│   └── da-db-b.yaml
└── README.md
```

---

## 使用 da-tools 產出 GitOps-friendly YAML

```bash
da-tools operator-generate \
  --rule-packs-dir ./rule-packs \
  --output-dir ./prometheus-operator-crds \
  --gitops

# --gitops 效果：
#   - sorted keys（deterministic output）
#   - 無 timestamps、resourceVersion、status
#   - ArgoCD / Flux 不會誤報 OutOfSync
```

告警路由不是 CRD：`generate-routes` 把 conf.d 合併進你的來源，輸出包成 Secret（流程與 Alertmanager CR 的設定見 [Operator Alertmanager 整合](operator-alertmanager-integration.md)）：

```bash
da-tools generate-routes --config-dir ./conf.d --output-alertmanager \
  --base-config ./alertmanager/alertmanager.base.yaml -o alertmanager.yaml
kubectl create secret generic da-alertmanager-config -n monitoring \
  --from-file=alertmanager.yaml --dry-run=client -o yaml \
  | kubeseal --format yaml > secrets/da-alertmanager-config.yaml
rm alertmanager.yaml                  # 明文產物不進 Git
```

conf.d 改了就拿同一份 `alertmanager.base.yaml` 重跑；不要把產出的 `alertmanager.yaml` 當成下一次的 base。

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
      - uses: actions/checkout@v6
      - name: Generate CRDs
        run: |
          docker run --rm \
            --user $(id -u):$(id -g) \
            -v ${{ github.workspace }}/conf.d:/conf.d:ro \
            -v ${{ github.workspace }}/rule-packs:/rule-packs:ro \
            -v ${{ github.workspace }}/prometheus-operator-crds:/output \
            ghcr.io/vencil/da-tools:latest \
            operator-generate --gitops --rule-packs-dir /rule-packs --output-dir /output
      - name: Render Alertmanager config
        run: |
          docker run --rm \
            --user $(id -u):$(id -g) \
            -v ${{ github.workspace }}/conf.d:/conf.d:ro \
            -v ${{ github.workspace }}/alertmanager:/alertmanager \
            ghcr.io/vencil/da-tools:latest \
            generate-routes --config-dir /conf.d --output-alertmanager \
              --base-config /alertmanager/alertmanager.base.yaml -o /alertmanager/alertmanager.yaml
          kubectl create secret generic da-alertmanager-config -n monitoring \
            --from-file=alertmanager/alertmanager.yaml --dry-run=client -o yaml \
            | kubeseal --cert sealed-secrets.pem --format yaml > secrets/da-alertmanager-config.yaml
          rm alertmanager/alertmanager.yaml
      - name: Commit & Push
        run: |
          git add prometheus-operator-crds/ secrets/da-alertmanager-config.yaml
          git diff --cached --quiet || git commit -m "chore: update operator CRDs"
          git push
```

⚠️ v2.9.0 映像沒有 `generate-routes --output-alertmanager`，`operator-generate` 的 `all` 也仍會讀 conf.d、產出 per-tenant AlertmanagerConfig；上面的 pipeline 需要比 v2.9.0 新的 da-tools。<!-- image-caveat: v2.9.0 -->

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
  name: da-db-a
  namespace: monitoring
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: da-db-a
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

ConfigMap 路徑上的來源就是 ConfigMap 的 `alertmanager.base.yml`；拿它當 base，產出 Secret，再讓 Alertmanager CR 的 `spec.configSecret` 指向它（[Operator Alertmanager 整合](operator-alertmanager-integration.md) Step 4）：

```bash
kubectl get configmap alertmanager-config -n monitoring \
  -o jsonpath='{.data.alertmanager\.base\.yml}' > alertmanager/alertmanager.base.yaml
# 照上方「使用 da-tools 產出 GitOps-friendly YAML」產出並套用 secrets/da-alertmanager-config.yaml
kubectl apply -f secrets/da-alertmanager-config.yaml
kubectl port-forward -n monitoring svc/alertmanager-operated 9093 &
curl -s http://localhost:9093/api/v2/status | jq -r '.config.original' | head -40
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
| [Operator Alertmanager 整合](operator-alertmanager-integration.md) | `generate-routes` → Secret → `configSecret` |
| [GitOps 部署指南](gitops-deployment.md) | ConfigMap 路徑 GitOps |
| [ADR-008](../adr/008-operator-native-integration-path.md) | 架構決策記錄 |
