---
title: "GitOps 部署指南"
tags: [gitops, deployment, ci-cd]
audience: [platform-engineer, devops]
version: v2.9.0
lang: zh
---
# GitOps 部署指南

> **Language / 語言：** **中文 (Current)** | [English](./gitops-deployment.en.md)

> **版本**：v2.9.0
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
components/threshold-exporter/config/conf.d/_defaults.yaml  @<org>/platform-team

# Tenant-level（各團隊自行 approve）
components/threshold-exporter/config/conf.d/db-a.yaml       @<org>/team-db-a
components/threshold-exporter/config/conf.d/db-b.yaml       @<org>/team-db-b
```

> ⚠️ 上面是**有 organization 的環境**下的樣子。本 repo 的 owner 是個人帳號，`@org/team` 語法不適用、裸寫的 `@xxx-team` 會被當成使用者帳號解析——實際 `.github/CODEOWNERS` 的 active 指派全部是 `@vencil`，團隊切分只留在註解（#1277）。導入時把 `@<org>/team-<tenant>` 換成你的真實 team，並確認它對 repo 有 **write** 權限；沒有 write 的對象不會被指派，可用 `GET /repos/{owner}/{repo}/codeowners/errors` 逐行查出。

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
| **配置變更 blast radius** | `config-diff --old-dir <base> --new-dir <pr>` | PR comment 顯示受影響 tenant/metric |
| **閾值歷史回測** | `backtest --git-diff --prometheus <url>` | 風險等級報告貼 PR comment |

所有檢查通過 + CODEOWNERS 指定的 reviewer approve → 允許 merge。

### PR Review 變更影響分析

當 PR 修改 `conf.d/` 下的 tenant 配置時，**GitHub Actions** 會自動執行 `config-diff` 產出 blast radius 報告，讓 reviewer 一眼看出變更影響範圍。

**CI 範本**：

| 平台 | 取得方式 | blast radius | 觸發條件 |
|------|---------|-------------|---------|
| GitHub Actions | 本 repo 內建 `.github/workflows/config-diff.yaml`，可直接複製套用 | ✅ 自動貼成 PR comment | PR 修改 `conf.d/**` |
| GitLab CI | 由 `da-tools init --ci gitlab` 產生 `.gitlab-ci.d/dynamic-alerting.yml`，並以根目錄 `.gitlab-ci.yml` 的 `include: local` 接進 pipeline | ❌ 尚未提供（見下） | MR 修改 `conf.d/**` |

GitHub Actions 範本會自動將 blast radius 報告貼為 PR comment（冪等更新，不重複建立）。

⚠️ **GitLab 那一份目前不含 blast radius**：`da-tools init --ci gitlab` 產出的 pipeline 只有 validate 與 apply 兩個 stage。原因在映像而不在你的 repo —— GitLab 是在 `$DA_TOOLS_IMAGE` 內跑 `script:`，而該映像沒有 `git`，所以比較基準取不到；取不到的基準會表現成「每一個租戶都是新增的」，因此寧可不出貨。詳見 [#1358](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1358)，補回作法見 [#1444](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1444)。

**Exit codes**（供 CI pipeline 判斷）：

| Exit Code | 含義 | CI 行為 |
|-----------|------|---------|
| 0 | 無配置變更 | 報告內容為「無變更」，留言仍然更新 |
| 1 | 偵測到變更 | 張貼 blast radius 報告 |
| 2 | 錯誤（目錄不存在等） | pipeline 報錯，不張貼 |

> ℹ️ 第 0 列以前寫的是「跳過 comment」，**但這個 repo 裡沒有任何實作者**——實測
> `config-diff` 在無變更時仍輸出一份**非空**的報告（含 `No changes detected.`），
> 所以任何以「報告檔非空」當閘門的寫法在 `0` 時也會貼。這樣其實比較好：留言是
> sticky 的，跳過只會讓**上一輪**的報告留在原地、看起來像是這一輪的結論。
>
> ⚠️ 由此得到的規則：**判斷有沒有變更要看結束碼，不要看報告檔空不空。** 後者在
> `0` 與 `1` 都非空，只有在工具根本沒跑完時才是空的——那是「失敗」不是「無變更」。

**Data-Driven Threshold Review 雙引擎**：`config-diff`（blast radius 靜態分析）搭配 `backtest`（Prometheus 歷史回測），形成變更前預覽 + 歷史驗證的完整審查流程。

報告內容包括：每個受影響 tenant 的變更清單、變更分類（tighter / looser / added / removed / toggled）、推斷受影響的 alert name。詳見 [da-tools README 場景八](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-tools/README.md#場景八配置目錄級差異比對v1110)。

## 3. ConfigMap Assembly

GitOps sync 需要將 `conf.d/` 目錄轉為 K8s ConfigMap。

### 方式 A：Makefile target（threshold-config）

```bash
make configmap-assemble CONFDIR=/path/to/your/conf.d
# 產出: .build/threshold-config.yaml（threshold-exporter 用的 tenant 配置）
```

⛔ **`CONFDIR` 一定要明示。** 它的內建預設值是 `components/threshold-exporter/config/conf.d`——**本 repo 自帶的開發範例樹**，裡面的 `db-a` / `db-b` 是參考範本，既不包含在發布的 chart 也不在 image 裡，幾乎不可能是你的租戶。不帶 `CONFDIR=` 直接跑會被**硬擋**，因為照著組出來的產物 `kubectl apply` 上去會把線上的 `threshold-config` **換成示範內容**。那棵樹**底下的子目錄**（`examples/` 之類）一樣擋——同一批示範租戶，換個路徑不會變成你的。

真的要組那棵範例樹時（寫文件、跑 demo、本 repo 自己的測試），明示放行：

```bash
ALLOW_SAMPLE_CONFDIR=1 make configmap-assemble
```

⚠️ **這道擋的射程是 per-checkout，不是 per-repo**——寫在這裡是因為拒絕訊息給的補救是「指向你自己的樹」，而照字面去指**另一個 checkout／worktree 裡同一棵範例樹**就是綠燈。範例樹的位置由 script 自己的 `__file__` 推導，所以它守的是「跑這一份 script 的這一棵樹」。⛔ 這是**已知邊界不是疏漏**：它要擋的缺陷是「照內建預設值跑」，而預設值一定落在本 checkout；要碰到另一個 checkout 的那棵樹，得自己把完整路徑打出來。改用 `git` 收斂 repo identity 的代價更壞——客戶樹上未必有 `git`，而缺 `git` 時那道判定會**靜默放行**。

在 CI pipeline 中使用：

```yaml
# ArgoCD pre-sync hook 或 Flux Kustomization postBuild
steps:
  # ⛔ CONFDIR 指向你自己 config repo 的 conf.d/，不是預設那棵範例樹
  - run: make configmap-assemble CONFDIR=tenants/conf.d
  - run: kubectl apply -f .build/threshold-config.yaml -n monitoring
```

這一步會在寫出產物**之前**擋下下列問題，並盡可能具名到檔：

| 擋什麼 | 為什麼不是警告 |
|---|---|
| 同一個租戶 id 出現在兩個檔 | exporter 對整棵 dir 是 hard reject，**每一個**租戶都會失去告警 |
| 檔名不能當 ConfigMap key | key 必須匹配 `[-._a-zA-Z0-9]+` 且不是 `.` / `..`（k8s `IsConfigMapKey`）。`db b.yaml`、`db-a (copy).yaml` 這種**永遠**不可能成為合法 key，只能改名 |
| 產出的 ConfigMap 與你的樹對不上 | 組完之後這一步會**讀回它即將寫出的那份 manifest**（還沒落地）：裡面的 key 集合與每個值的長度，必須等於選中的那批載體。⚠️ 落到這一列的成因有**兩類**：⑴ **引數層**——路徑裡的 `,`、`"`、`=` 會讓 kubectl 在讀檔之前就把 `--from-file` 的引數切壞（先過 CSV 再過 `key=path` 兩層切割）⇒ 少一個租戶、或多一個沒人宣告的 key，解法是換一條不含那些字元的路徑；⑵ **內容層**——key 集合正確、只有**長度**對不上，那與路徑無關：值沒能逐位元組通過 kubectl 的 YAML emitter 與這一步的 loader（**唯一實測到的字元是 U+0085 NEL**，回程被正規化成一般換行而少一個位元組；⚠️ 同一次也量了 U+2028 / U+2029，兩者原樣往返——這不是「奇怪字元都會」），此時搬樹重跑會得到一模一樣的錯，要比對的是 manifest 裡那個 key 的值與檔案內容。⚠️ 本列抓的是**引數被弄壞而 kubectl 仍然成功**的那一半；被弄壞到 kubectl 自己拒絕時，它的訊息會說「key names or file paths」而**誰也不點名**，這一步只能原樣轉述 |
| 載體總位元組超過 1 MiB | k8s `ValidateConfigMap` 對 `data` 的總和設上限，超過時 `kubectl apply` 是對**整個物件**失敗、不提任何檔。量的是**產出的 manifest 裡的值**，不是檔案大小的預測 |
| 目錄裡沒有任何載體 | 「組出零個租戶」與「平台真的沒有租戶」無法區分，通常是 `CONFDIR` 指錯。⚠️ 這一列**沒有檔可以點名**——它就是「一個都沒有」 |

⛔ **退出碼要看你呼叫的是哪一層。** script 本身依 repo 慣例回 `1 = 設定違規` / `2 = 呼叫端或工具錯誤`（例如 `kubectl` 不在 PATH）。但 **`make` 對任何 recipe 失敗一律 exit 2**，所以經由 `make configmap-assemble` 跑時這個區分**在 make 這一層整個塌掉**——CI **不能**靠 `make` 的 rc 分辨這兩類。要那個區分就直接呼叫 script（與本頁方式 C 同一個慣例）：

```bash
python3 scripts/ops/configmap_assemble.py \
  --config-dir tenants/conf.d --output .build/threshold-config.yaml
# rc 1 = 設定違規（重複租戶 / 檔名 / 產物與樹對不上 / 超過 1 MiB / 沒有載體）
# rc 2 = 呼叫端或工具錯誤（--config-dir 不存在、kubectl 缺席或逾時）
```

⚠️ 幾件文件以前沒說的事：

- **組裝是扁平的**：只有 `CONFDIR` **頂層**的檔會進 ConfigMap（ConfigMap 的 key 平面表達不出子目錄）。`examples/` 與任何階層式子目錄（`region-eu/` 之類）底下的租戶**不會**進去——stderr 的 `WARN` 會**具名前 5 個、其餘以 `(+N more)` 計數**（完整清單在 `validate_config --json` 平面讀取列的 `skipped_nested_files`），而 exporter 在叢集上是遞迴讀的（ADR-016/017），兩邊會不一致。
- **讀不到的載體會具名但不擋**：`CONFDIR` 頂層若有**斷鏈 symlink** 或**取了 config 名字的目錄**（`db-x.yaml/`），它們進不了 ConfigMap，stderr 會逐個 `WARN` 點名。⛔ 那不是警告性的雜訊——那個租戶在叢集上沒有告警。
- **副檔名大小寫與 exporter 一致**：`DB-A.YAML`、`db-b.YML` 這類載體現在**會**進 ConfigMap。⚠️ 連帶效果：如果你同時有 `db-a.yaml` 與 `DB-A.YAML` 且兩者宣告同一個租戶，這一步會擋下來（以前是靜默丟掉大寫那個、印綠燈）。
- **這一步失敗時不會刪掉舊產物**：`.build/threshold-config.yaml` 若是前一次跑出來的，它會**原封不動留著**（留半個檔比留舊檔更糟）。⛔ 所以 `kubectl apply` 那一步一定要接在 assemble **成功**之後——非 fail-fast 的 pipeline 會把**舊**設定推上去。
- **⚠️ 上面那個 1 MiB 不是你先撞到的天花板**：`kubectl apply -f`（client-side，也就是本節教的那條命令）會把**整個物件**寫進 `kubectl.kubernetes.io/last-applied-configuration` 這個 annotation，而 k8s 對一個物件的 annotation 總量上限是 **256 KiB**——`data` 上限的四分之一，而且驗證順序在前。超過時 `kubectl apply` 回的是 `metadata.annotations: Too long`，**不點名任何檔**。產物超過那個大小時 assemble 會印一則 `WARN`（不擋），兩條出路：改用 `kubectl apply --server-side -f`（不存那個 annotation），或把租戶拆開（`make sharded-assemble`）。

### Method B: Helm values overlay

```bash
helm upgrade threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.9.0 \
  -n monitoring \
  -f values-override.yaml
```

### Method C: `--output-configmap` (Alertmanager ConfigMap, v1.10.0)

如果 Alertmanager 的路由配置也走 GitOps，可用 `generate-routes --output-configmap` 產出完整 Alertmanager ConfigMap YAML：

```bash
# CI 中自動產出 Alertmanager ConfigMap
python3 scripts/tools/ops/generate_alertmanager_routes.py \
  --config-dir config/conf.d/ --output-configmap \
  --base-config deploy/base-alertmanager.yaml \
  -o deploy/alertmanager-configmap.yaml
```

產出的 YAML 可直接 `kubectl apply` 或由 ArgoCD/Flux 自動 sync。與方式 A（threshold-config）搭配使用，實現 threshold-exporter 和 Alertmanager 配置的完整 GitOps 閉環。

不提供 `--base-config` 時使用內建預設值。需要自訂 `global`（如 SMTP 設定）、default receiver、或 inhibit_rules 基礎規則時，建議維護一份 `base-alertmanager.yaml` 作為輸入。詳見 [BYO Alertmanager 整合指南 Step 5](byo-alertmanager-integration.md#step-5-merge-into-alertmanager-configmap)。

⚠️ **v2.10.0 起這一格的失敗模式改了（BREAKING，[#1616](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1616) / [#1617](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1617)）**：上面範例裡的 `--base-config` 與 `-o` 都是相對路徑，在 CI 的 workdir 打錯或目錄不存在時——

- **舊行為**：`--base-config` 路徑錯 ⇒ 結束碼 **0**，而產出的 ConfigMap 內容與「完全沒提供 `--base-config`」**逐位元組相同**。你的 `global:`（SMTP smarthost、Slack webhook）被換成平台內建的佔位值，然後被 `kubectl apply` / ArgoCD sync 進叢集——**告警送不出去，而流水線是綠的**。
- **新行為**：結束碼 **2** 並指名是哪一個旗標。`-o` 的父目錄不存在或不可寫同樣是結束碼 2（舊行為是未攔的 Python traceback + 結束碼 1，而 1 在本工具的語意是「你的設定有違規」）。

⛔ **正確的處置是修路徑，不是拿掉 `--base-config`**——拿掉之後的結果與舊的錯誤行為相同（改用平台內建 `global:`）。

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
python3 scripts/tools/ops/patch_config.py <tenant> <key> <value>
```

ConfigMap 立即更新，threshold-exporter 在下一個 reload 週期（30-60s）自動套用。

**③ 飄移收斂 (Drift Reconciliation)** — 破窗修改後，SRE **必須**事後補 PR 同步回 Git。否則下一次 GitOps sync 會將 K8s 配置覆蓋回 Git 版本——這正是 GitOps 的自癒特性，天然防止「急救後忘記改程式碼」造成永久技術債。

## 7. Tenant 自助設定範圍

GitOps 工作流下，Tenant 可在自己的 YAML 中自行管理以下設定（無需 Platform Team 介入）：

| 設定 | 說明 | 範例 |
|------|------|------|
| 閾值三態 | 自訂值 / 省略用預設 / `"disable"`（⚠️ 「省略用預設」僅限 `_defaults.yaml` 的 `defaults:` 有值的 key） | `mysql_connections: "70"` |
| 宣告 key（`optional_overrides:`） | 平台認得 key 名但不主張值：租戶填了才生效，**省略＝沒有值＝靜默**（無預設可繼承，只有兩態） | `oracle_wait_time_rate: "<你校準出的值>"` |
| `_critical` 後綴 | 多層嚴重度 | `mysql_connections_critical: "95"` |
| `_routing` | 通知路由（6 種 receiver type） | `receiver: {type: "webhook", url: "..."}` |
| `_routing.overrides[]` | 特定 alert 使用不同 receiver | `alertname: "..."`，`receiver: {type: "email", ...}` |
| `_silent_mode` | 靜默模式（TSDB 有紀錄但不通知） | `{target: "all", expires: "2026-04-01T00:00:00Z"}` |
| `_state_maintenance` | 維護模式（完全不觸發） | 同上，支援 `expires` 自動失效 |
| `_severity_dedup` | 嚴重度去重 | `"enable"`（預設）/ `"disable"` |

Platform Team 控制的設定（`_defaults.yaml`）包括全域預設、`_routing_defaults`、`_routing_enforced`（雙軌通知）。

## 8. 新增租戶 Checklist

1. `da-tools scaffold --tenant <name> --db <type>` 產生 YAML（多 namespace 加 `--namespaces ns1,ns2`）
2. 將產出放入 `conf.d/<tenant>.yaml`
3. 更新 `.github/CODEOWNERS` 加入該租戶的 owner（org 環境寫 `@<org>/team-<tenant>`；⛔ **不要裸寫 `@team-<tenant>`**——那會被當成使用者帳號，且無 write 權者不會被指派）
4. 發 PR → CI 驗證（`validate-config` 一站式檢查） → merge → 自動部署

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["GitOps Deployment Guide"] | ⭐⭐⭐ |
| ["da-tools CLI Reference"](../cli-reference.md) | ⭐⭐ |
| ["Grafana Dashboard 導覽"](../grafana-dashboards.md) | ⭐⭐ |
| ["AST 遷移引擎架構"](../migration-engine.md) | ⭐⭐ |
