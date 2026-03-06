# GitOps 部署指南

> **版本**：v1.6.0
> **受眾**：Platform Engineers、DevOps、SREs
> **前置文件**：[BYO Prometheus 整合指南](byo-prometheus-integration.md)

---

## 概述

本指南說明如何透過 GitOps 工作流（ArgoCD / Flux）管理 Dynamic Alerting 平台的租戶配置。核心原則：

- **Git 是唯一的真相來源**——所有配置變更經 PR → review → merge → GitOps sync
- **CODEOWNERS 實現檔案級 RBAC**——租戶只能改自己的 YAML，平台設定需 Platform Team 批准
- **CI 自動驗證**——PR 觸發 schema validation + routing validation + deny-list linting

## 1. 目錄結構

```
conf.d/
├── _defaults.yaml          # Platform Team 擁有（全域預設 + routing defaults）
├── db-a.yaml               # Tenant Team A 擁有
├── db-b.yaml               # Tenant Team B 擁有
└── <new-tenant>.yaml       # 新增租戶：建立檔案 + 更新 CODEOWNERS
```

權限邊界由 `.github/CODEOWNERS` 控制：

```
# Platform-level（需 Platform Team approve）
components/threshold-exporter/config/conf.d/_defaults.yaml  @platform-team

# Tenant-level（各團隊自行 approve）
components/threshold-exporter/config/conf.d/db-a.yaml       @team-db-a
components/threshold-exporter/config/conf.d/db-b.yaml       @team-db-b
```

## 2. CI 自動驗證

每次 PR 觸發 `.github/workflows/validate.yaml`，執行以下檢查：

| 檢查 | 工具 | 失敗時 |
|------|------|--------|
| Python 測試 | `pytest tests/` | 工具鏈回歸 |
| Go 測試 | `go test ./...` | Exporter 回歸 |
| Tenant key 合法性 | `generate_alertmanager_routes.py --validate` | 未知 key / typo 警告 |
| Webhook URL 合規 | `--policy .github/custom-rule-policy.yaml` | URL 不在 allowed_domains |
| Custom rule deny-list | `lint_custom_rules.py --ci` | 禁用函式 / 破壞 tenant 隔離 |
| 版號一致性 | `bump_docs.py --check` | 跨文件版號不一致 |

所有檢查通過 + CODEOWNERS 指定的 reviewer approve → 允許 merge。

## 3. ConfigMap 組裝

GitOps sync 需要將 `conf.d/` 目錄轉為 K8s ConfigMap。兩種方式：

### 方式 A：Makefile target（推薦）

```bash
make configmap-assemble
# 產出: .build/threshold-config.yaml
```

在 CI pipeline 中使用：

```yaml
# ArgoCD pre-sync hook 或 Flux Kustomization postBuild
steps:
  - run: make configmap-assemble
  - run: kubectl apply -f .build/threshold-config.yaml -n monitoring
```

### 方式 B：Helm values overlay

```bash
helm upgrade threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 1.5.0 \
  -n monitoring \
  -f values-override.yaml
```

## 4. ArgoCD 範例

```yaml
# argocd/dynamic-alerting.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: dynamic-alerting
  namespace: argocd
spec:
  project: monitoring
  source:
    repoURL: https://github.com/your-org/dynamic-alerting-config.git
    targetRevision: main
    path: deploy/
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true    # 自動修正 runtime drift
    syncOptions:
      - CreateNamespace=true
```

## 5. Flux 範例

```yaml
# flux/dynamic-alerting.yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: dynamic-alerting
  namespace: flux-system
spec:
  interval: 1m
  url: https://github.com/your-org/dynamic-alerting-config.git
  ref:
    branch: main
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: dynamic-alerting
  namespace: flux-system
spec:
  interval: 5m
  sourceRef:
    kind: GitRepository
    name: dynamic-alerting
  path: ./deploy
  prune: true
  targetNamespace: monitoring
```

## 6. 三層變更流程

### 常規流程 (Standard Pathway)

```
Tenant 修改 conf.d/db-a.yaml
  → Git PR
  → CI validate (自動)
  → CODEOWNERS reviewer approve
  → Merge to main
  → ArgoCD/Flux sync (自動)
  → ConfigMap 更新
  → threshold-exporter SHA-256 hot-reload (自動)
```

平均落地時間：PR merge 後 < 2 分鐘。

### 緊急破窗 (Break-Glass)

P0 事故期間，SRE 可直接 runtime patch：

```bash
python3 scripts/tools/patch_config.py <tenant> <key> <value>
```

ConfigMap 立即更新，threshold-exporter 在下一個 reload 週期（30-60s）自動套用。

### 飄移收斂 (Drift Reconciliation)

破窗修改後，SRE **必須**在事後補發 PR 將變更同步回 Git。否則下一次 GitOps sync 會將 K8s 上的配置覆蓋回 Git 版本——這正是 GitOps 的自癒特性，天然防止「急救後忘記改程式碼」造成永久技術債。

## 7. 新增租戶 Checklist

1. `da-tools scaffold --tenant <name> --db <type>` 產生 YAML
2. 將產出放入 `conf.d/<tenant>.yaml`
3. 更新 `.github/CODEOWNERS` 加入 `@team-<tenant>`
4. 發 PR → CI 驗證 → merge → 自動部署
