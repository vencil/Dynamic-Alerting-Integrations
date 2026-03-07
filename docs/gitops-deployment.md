# GitOps 部署指南

> **版本**：v1.10.0
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
| **配置變更 blast radius** | `config-diff --old-dir <base> --new-dir <pr>` | PR comment 顯示受影響 tenant/metric（v1.10.0） |
| **閾值歷史回測** | `backtest --git-diff --prometheus <url>` | 風險等級報告貼 PR comment（v1.9.0） |

所有檢查通過 + CODEOWNERS 指定的 reviewer approve → 允許 merge。

### PR Review 變更影響分析（v1.10.0）

當 PR 修改 `conf.d/` 下的 tenant 配置時，CI 可自動執行 `config-diff` 產出 blast radius 報告，讓 reviewer 一眼看出變更影響範圍：

```yaml
# .github/workflows/config-review.yaml (摘要)
- name: Config diff
  run: |
    # 從 base branch checkout 舊配置
    git show origin/${{ github.base_ref }}:conf.d/ > /tmp/old-conf.d/ || true

    docker run --rm \
      -v /tmp/old-conf.d:/data/old \
      -v $(pwd)/conf.d:/data/new \
      ghcr.io/vencil/da-tools:1.10.0 \
      config-diff --old-dir /data/old --new-dir /data/new \
    > /tmp/config-diff.md

- name: Post PR comment
  uses: actions/github-script@v7
  with:
    script: |
      const diff = fs.readFileSync('/tmp/config-diff.md', 'utf8');
      github.rest.issues.createComment({
        issue_number: context.issue.number,
        body: `## Config Diff Report\n${diff}`
      });
```

報告內容包括：每個受影響 tenant 的變更清單、變更分類（tighter / looser / added / removed / toggled）、推斷受影響的 alert name。詳見 [da-tools README 場景八](../components/da-tools/README.md#場景八配置目錄級差異比對v1110)。

## 3. ConfigMap 組裝

GitOps sync 需要將 `conf.d/` 目錄轉為 K8s ConfigMap。

### 方式 A：Makefile target（threshold-config）

```bash
make configmap-assemble
# 產出: .build/threshold-config.yaml（threshold-exporter 用的 tenant 配置）
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
  oci://ghcr.io/vencil/charts/threshold-exporter --version 1.8.0 \
  -n monitoring \
  -f values-override.yaml
```

### 方式 C：`--output-configmap`（Alertmanager ConfigMap，v1.10.0）

如果 Alertmanager 的路由配置也走 GitOps，可用 `generate-routes --output-configmap` 產出完整 Alertmanager ConfigMap YAML：

```bash
# CI 中自動產出 Alertmanager ConfigMap
python3 scripts/tools/generate_alertmanager_routes.py \
  --config-dir config/conf.d/ --output-configmap \
  --base-config deploy/base-alertmanager.yaml \
  -o deploy/alertmanager-configmap.yaml
```

產出的 YAML 可直接 `kubectl apply` 或由 ArgoCD/Flux 自動 sync。與方式 A（threshold-config）搭配使用，實現 threshold-exporter 和 Alertmanager 配置的完整 GitOps 閉環。

不提供 `--base-config` 時使用內建預設值。需要自訂 `global`（如 SMTP 設定）、default receiver、或 inhibit_rules 基礎規則時，建議維護一份 `base-alertmanager.yaml` 作為輸入。詳見 [BYO Alertmanager 整合指南 Step 5](byo-alertmanager-integration.md#step-5-合併至-alertmanager-configmap)。

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

```
                    ┌─────────────────────────────────────────┐
                    │          ① Standard Pathway              │
                    │                                          │
  Tenant/Platform   │   conf.d/*.yaml                          │
  修改 YAML ───────►│── Git PR ──► CI validate ──► merge ─┐   │
                    │                                      │   │
                    └──────────────────────────────────────┼───┘
                                                           │
                            ArgoCD / Flux sync (自動)      │
                                                           ▼
               ┌──────────────┐                   ┌────────────────┐
               │ ② Break-Glass│   patch_config.py │   ConfigMap    │
  P0 事故 ────►│  SRE 直接     ├──────────────────►│   (K8s)        │
  緊急 bypass  │  runtime patch│                   └───────┬────────┘
               └──────┬───────┘                            │
                      │                          SHA-256 hot-reload
                      │                                    ▼
               ┌──────▼───────┐                   ┌────────────────┐
               │ ③ Drift      │                   │ threshold-     │
               │  Reconcile   │                   │ exporter       │
               │  事後補 PR    │                   │ 套用新配置      │
               │  同步回 Git   │                   └────────────────┘
               └──────────────┘
```

**① 常規流程 (Standard Pathway)** — Tenant 修改 YAML → PR → CI → merge → GitOps sync → ConfigMap → hot-reload。平均落地時間：PR merge 後 < 2 分鐘。

**② 緊急破窗 (Break-Glass)** — P0 事故期間，SRE 可跳過 Git 直接 runtime patch：

```bash
python3 scripts/tools/patch_config.py <tenant> <key> <value>
```

ConfigMap 立即更新，threshold-exporter 在下一個 reload 週期（30-60s）自動套用。

**③ 飄移收斂 (Drift Reconciliation)** — 破窗修改後，SRE **必須**事後補 PR 同步回 Git。否則下一次 GitOps sync 會將 K8s 配置覆蓋回 Git 版本——這正是 GitOps 的自癒特性，天然防止「急救後忘記改程式碼」造成永久技術債。

## 7. Tenant 自助設定範圍

GitOps 工作流下，Tenant 可在自己的 YAML 中自行管理以下設定（無需 Platform Team 介入）：

| 設定 | 說明 | 範例 |
|------|------|------|
| 閾值三態 | 自訂值 / 省略用預設 / `"disable"` | `mysql_connections: "70"` |
| `_critical` 後綴 | 多層嚴重度 | `mysql_connections_critical: "95"` |
| `_routing` | 通知路由（6 種 receiver type） | `receiver: {type: "webhook", url: "..."}` |
| `_routing.overrides[]` | 特定 alert 使用不同 receiver | `alertname: "..."`，`receiver: {type: "email", ...}` |
| `_silent_mode` | 靜默模式（TSDB 有紀錄但不通知） | `{target: "all", expires: "2026-04-01T00:00:00Z"}` |
| `_state_maintenance` | 維護模式（完全不觸發） | 同上，支援 `expires` 自動失效 |
| `_severity_dedup` | 嚴重度去重 | `enabled: true` |

Platform Team 控制的設定（`_defaults.yaml`）包括全域預設、`_routing_defaults`、`_routing_enforced`（雙軌通知）。

## 8. 新增租戶 Checklist

1. `da-tools scaffold --tenant <name> --db <type>` 產生 YAML（多 namespace 加 `--namespaces ns1,ns2`）
2. 將產出放入 `conf.d/<tenant>.yaml`
3. 更新 `.github/CODEOWNERS` 加入 `@team-<tenant>`
4. 發 PR → CI 驗證（`validate-config` 一站式檢查） → merge → 自動部署
