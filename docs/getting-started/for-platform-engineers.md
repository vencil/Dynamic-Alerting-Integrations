---
title: "Platform Engineer 快速入門指南"
tags: [getting-started, platform-setup]
audience: [platform-engineer]
version: v2.9.0
lang: zh
---
# Platform Engineer 快速入門指南

> **Language / 語言：** **中文 (Current)** | [English](./for-platform-engineers.en.md)

> **v2.9.0** | 適用對象：Platform Engineers、SRE、基礎設施管理員
>
> 相關文件：[Architecture](../architecture-and-design.md) · [Benchmarks](../architecture-and-design.md) · [GitOps Deployment](../integration/gitops-deployment.md) · [Rule Packs](../rule-packs/README.md) · [Prometheus Operator 整合](../integration/prometheus-operator-integration.md) · [Deployment Decision Matrix](decision-matrix.md)

> 💡 **建議第一步：先在本機把整套跑起來。** [`try-local/`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md) 一鍵 docker compose（不需 K8s），~1 分鐘看到 threshold-exporter → Prometheus → 真實告警的完整鏈路，再規劃怎麼上 K8s。

## 你的上手路徑

1. **先把整套跑起來**（看大圖像）→ [try-local](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md)。
2. **深入你的核心元件** → [threshold-exporter QUICKSTART](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/QUICKSTART.md) · [tenant-api QUICKSTART](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/tenant-api/QUICKSTART.md)。
3. **上 production** → 本指南下方的部署路徑 + [helm/](https://github.com/vencil/Dynamic-Alerting-Integrations/tree/main/helm)。

## 你需要知道的三件事

**1. threshold-exporter 是核心。** 它讀取 YAML 設定、產生 Prometheus Metrics、支援 SHA-256 hot-reload。兩個副本以 HA 方式運行在 port 8080。

**2. Rule Pack 是自成一體的單位。** 每個 Rule Pack 透過 Projected Volume 掛載到 Prometheus，每個涵蓋一個資料庫或服務類型（MariaDB、PostgreSQL、Redis 等）。用 `optional: true` 機制安全卸載不需要的 Rule Pack。

**3. 一切都由配置驅動。** `_defaults.yaml` 控制平台全局行為，tenant YAML 覆蓋預設值，`_profiles.yaml` 提供繼承鏈。沒有硬編碼，沒有秘密。

## 部署路徑選擇

平台支援兩種部署路徑，不確定該選哪種？參考 [Deployment Decision Matrix](decision-matrix.md)。

| 路徑 | 適用場景 | 指南 |
|------|---------|------|
| **Path A: ConfigMap** | 任何 Prometheus 環境、非 K8s 環境 | 本文（下方） |
| **Path B: Operator CRD** | 已安裝 kube-prometheus-stack | [Operator 整合指南](../integration/prometheus-operator-integration.md) |

## 部署到 Kubernetes（Helm）

> **前置**：一個 K8s 叢集 + `helm` + `kubectl`（下例 namespace 用 `monitoring`）。
>
> 3 個 component chart 的 image 預設都自動解析為 `ghcr.io/vencil/<chart>:v<appVersion>`（published、可拉取——與 release pipeline 的 digest 驗證 [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) 同一不變式）。直接 `helm install ./helm/<chart>/` 即可；要釘特定版本才加 `--set image.tag=...`。

最小可用平台配置：

```yaml
# conf.d/_defaults.yaml
defaults:
  # ⚠️ 值是數字、不加引號 —— Go 端型別為 map[string]float64
  mysql_connections: 80
  mysql_threads_running: 30  # threads_running 飽和（併發執行緒數，NOT host CPU%；#1231 由 mysql_cpu 改名）
  mysql_replication_lag: 30  # 秒 — 非同步複製延遲
  # 其他預設閾值...（key 必須是 threshold-registry 認得的名字，
  # 否則會產出沒有任何 rule pack 消費的死 series，且不會有人告訴你）
```

### 部署 threshold-exporter ×2 HA

```bash
# threshold-exporter 透過 Helm chart 部署（Chart 在 helm/threshold-exporter/）。
# image 預設自動解析為 ghcr.io/vencil/threshold-exporter:v<appVersion>（published、可拉取）：
helm install threshold-exporter ./helm/threshold-exporter/ \
  -n monitoring --create-namespace
# 驗證副本運行
kubectl get pod -n monitoring | grep threshold-exporter
```

> 本機 kind 開發要用 `kind load` 載入的本地 image，改帶 `-f environments/local/threshold-exporter.yaml`（pin `:dev` + `pullPolicy: Never`）。

> 客戶自帶 GitOps（Argo CD / Flux）時，把 chart path 寫入 Application/Kustomization 即可 — 不需要 raw manifests。Chart 同時是 OCI artifact (`ghcr.io/vencil/dynamic-alerting`)，airgapped 環境可走 [migration-toolkit-installation.md](../migration-toolkit-installation.md) 的 Path C。

### 掛載 Rule Pack

```bash
# 部署監控堆疊（Prometheus + rule-pack ConfigMap）。k8s/03-monitoring/ 內的
# *.json 是 Grafana dashboard、非 k8s manifest，故只 apply *.yaml（直接
# `kubectl apply -f k8s/03-monitoring/` 會在那 3 個 .json 上 validation error）：
for f in k8s/03-monitoring/*.yaml; do kubectl apply -f "$f"; done
# Prometheus Deployment 透過 Projected Volume 掛載 rule-pack ConfigMap
# 確認 k8s/03-monitoring/deployment-prometheus.yaml 的 volume 部分
kubectl get configmap -n monitoring | grep prometheus-rules
```

> 💡 **互動工具** — 不確定需要哪些 Rule Pack？用 [Rule Pack Selector](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-selector.jsx) 互動選取。想估算叢集資源需求？試試 [Capacity Planner](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/capacity-planner.jsx)。不確定該選哪種架構？[Architecture Quiz](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/architecture-quiz.jsx) 幫你做決定。想在瀏覽器中體驗完整的工作流？[Platform Demo](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/platform-demo.jsx) 展示 scaffold → validate → deploy。

### 部署 tenant-api + da-portal（Tenant Manager）

> 這兩個元件**是什麼**（RBAC / PR 寫回模式 / 審計）見下方 [§Self-Service Portal（tenant-api）](#self-service-portaltenant-api)。

**tenant-api**（file-based 設定 API，commit-on-write）：

```bash
# FORGE_TOKEN：可推分支、開 PR/MR 的 GitHub／GitLab token
kubectl create namespace tenant-api
kubectl -n tenant-api create secret generic tenant-api-vcs --from-literal=token="$FORGE_TOKEN"
helm install tenant-api ./helm/tenant-api/ -n tenant-api --create-namespace \
  --set gitRepoUrl=https://github.com/your-org/your-config-repo.git \
  --set writeBack.mode=pr-github \
  --set writeBack.github.repo=your-org/your-config-repo \
  --set writeBack.token.secretName=tenant-api-vcs
```

需要：
- **身分**：chart 預設帶 oauth2-proxy sidecar（`oauth2Proxy.enabled=true`，provider github），須先建好 `oauth2-proxy-secrets`（見 chart 的 `secret-oauth2proxy.yaml` 模板）。production 由 oauth2-proxy 注入 `X-Forwarded-Email`；本機 dev 才用 `--dev-bypass-auth`（[ADR-022](../adr/022-dev-auth-bypass-four-layer-containment.md)，**不在 published image**）。缺 `X-Forwarded-Email` 時 `/api/v1/me` 回 401；有 `_rbac.yaml` 時，沒有任何規則授予讀取權限的人在資料端點回 403（`/me` 只要求身分，回 200 與空的授權摘要）；授權只有單租戶或前綴（如 `["db-a-*"]`）的人也進得來，列表只看到自己的租戶（[#2520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2520)；先前這類人一律 403）。saved views 仍只給規則寫了 `tenants: ["*"]`、且不帶 `org-scope` 的人（[#3160](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3160)）。
- **conf.d 來源**：`gitRepoUrl` 指向放 conf.d 的 git repo（init container clone）；留空則 conf.d 為空、`_rbac.yaml` 由 configmap 掛 `/etc/rbac`。這個 repo 不能依賴 git 的 filter／diff／merge driver（git-lfs、git-crypt 都是）：tenant-api 跑 git 時讀得到的設定裡有任一個就拒絕啟動，啟動後才出現則拒絕寫入與預覽；repo 的 hooks 照常執行（[#3175](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3175)；見 [tenant-api 硬化 §3.11](../api/tenant-api-hardening.md)）。
- **寫回（必填）**：`writeBack.mode` 沒有預設值，不設 chart 就不 render（[#2855](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2855)）。`pr-github` / `pr-gitlab` 每筆寫入開一個 PR/MR，合併後由你的 GitOps 流程組出 exporter 讀的 `threshold-config` ConfigMap（[ADR-011](../adr/011-pr-based-write-back.md)）；需要 `gitRepoUrl` 與放 forge token 的 Secret（`writeBack.token.secretName`），clone 與 push 都用它認證，token 不會出現在 URL 或 ConfigMap。`direct` 只把寫入留在 pod 自己的 `/conf.d`：不 push、送不到 exporter，`confDir` 用預設 emptyDir 時重啟即失，只適合試用，且要另設 `writeBack.directAcknowledgeNotDelivered=true`。single-writer 約束需 `replicaCount=1`。
- **state store 的備份與還原**：群組、saved views、federation 設定與 AccountID 帳本存在 `tenant-api-state` ConfigMap，`kubectl -n tenant-api get configmap tenant-api-state -o yaml > backup.yaml` 即為完整快照。⛔ **不要用 `kubectl apply` 把備份蓋回去**：備份之後配發的 AccountID 會被下一個租戶再拿到一次（跨租戶 log 洩漏）。還原一律經 `tenant-api state restore`，帳本只合併、不覆蓋（[#2884](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2884)；用法見 [tenant-api README §state store 的備份與還原](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/tenant-api/README.md#state-store-的備份與還原)）。
- **產生器容器（必備）**：pod 裡固定有一個 da-tools 容器跑 `da-tools generator-service`（[ADR-036 §3](../adr/036-single-parser-effective-config.md)），以唯讀方式讀同一個 conf.d repo；tenant-api 每筆會改變 conf.d 內容的寫入，commit 前都經只有兩者掛載的 Unix socket 問它「這筆寫入後整棵 conf.d 能否通過 `generate-routes --validate --strict`」，通過才寫入（[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)；回應碼見 [tenant-api 硬化 §3.9](../api/tenant-api-hardening.md)）。需要 git repo（`gitRepoUrl`，或 `confDir.type=pvc`／`hostPath` 裡已有的 repo），否則 chart 不 render；資源 request＝limit（預設 512Mi／1 CPU）。部署端 policy 檔（`allowed_domains`）放在你自己管的 ConfigMap，以 `generator.policy.existingConfigMap`／`generator.policy.key` 指定，chart 不內建內容；同一份會掛進 tenant-api，用來核對產生器判定時用的是同一份。容器不可用或 policy 檔不可用時，寫入一律回 503，讀取不受影響（`tenant_api_policy_available` 為 0，告警 `TenantApiPolicyUnavailable`）。⚠️ chart 預設的 `tools/v2.9.0` 映像沒有這個子指令，要把 `generator.image` 指到含它的 da-tools 映像（下一個 tools 版本發布時預設會一起調升），否則容器反覆重啟、連帶 tenant-api 不 Ready。設了 `generator.image.digest` 時以 digest 為準，只改 tag 不會換映像：要同時更新或清除 digest，兩者都要指向含 `generator-service` 的映像。

**da-portal**（Tenant Manager UI）：

```bash
helm install da-portal ./helm/da-portal/ -n monitoring
```

需要：
- **oauth2-proxy 必備**：portal nginx 把 `:80 → oauth2-proxy:4180`，**關掉 oauth2-proxy 會 CrashLoopBackOff**（它是 auth 前端）。
- **後端**：`config.tenantApiUrl` 指向 tenant-api Service（預設 `http://tenant-api.tenant-api.svc.cluster.local:8080`）。

> 一鍵本機體驗（不需 oauth2-proxy / git repo；dev-bypass + 從源碼 build tenant-api）見 [try-local](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md)。

## 常見操作

### 管理全局預設值

```yaml
# conf.d/_defaults.yaml
defaults:
  # ⚠️ 值是數字、不加引號 —— Go 端型別為 map[string]float64。寫成 "80" 會讓
  #    「整份檔案」unmarshal 失敗，該檔的 defaults 全數落空（exporter 會記一行
  #    ERROR，但 tenant-api 的驗證路徑是靜默吞掉的）。
  mysql_connections: 80
  container_cpu: 70
  container_memory: 80
  # 維度閾值不寫在這裡：維度 key 僅限租戶設定，不繼承 defaults
  # 平台不想給某個 key 預設值 → 整條不寫。defaults 沒有 "disable" 這個狀態；
  # 三態的 "disable" 與 <key>_critical 都只在租戶設定裡有效（見 for-tenants.md）

# ↓ 頂層 key，與 defaults: 同級，不是它的子項
_routing_defaults:
  group_wait: "30s"
  group_interval: "5m"
  repeat_interval: "12h"
```

驗證預設值語法：

```bash
python3 scripts/tools/ops/validate_config.py --config-dir conf.d/
```

> 直接跑 `validate_config.py` 需要 da-guard：先 `make da-guard-build` 並設 `DA_GUARD_BINARY=.build/da-guard`（da-tools 映像已內建）；詳見 [validate-config 的前置需求](../cli-reference.md#validate-config)。

### 管理 Rule Pack

檢視已掛載的 Rule Pack：

```bash
kubectl get configmap -n monitoring | grep prometheus-rules
# 可能輸出：prometheus-rules-mariadb, prometheus-rules-postgresql, prometheus-rules-redis...
```

移除不需要的 Rule Pack（編輯 Prometheus Deployment）：

```bash
kubectl edit deployment prometheus -n monitoring
# 在 volumes.projected.sources 中移除對應的 configMapRef
# 或設定 Projected Volume 的 optional: true 實現安全卸載
```

### 設定平台強制路由 (_routing_enforced)

啟用雙軌通知（NOC + Tenant）：

```yaml
# conf.d/_defaults.yaml — _routing_enforced 是頂層 key，不放在 defaults: 底下
_routing_enforced:
  enabled: true                 # ⚠️ 省略＝false，整段被靜默忽略
  receiver:
    type: "slack"
    api_url: "https://hooks.slack.com/services/T/B/xxx"
    channel: "#noc-alerts"
  # ⚠️ match 必須是「matcher 字串的 list」。省略它會產生一條沒有任何 matcher
  #    的 route，加上固定帶的 continue: true ＝ 所有租戶的所有告警都被雙送到
  #    這個 receiver（match-all 消防水管）。寫成 map 形式（或單一字串）時，
  #    產生器不產出這條 route，`--validate` 回 1（#2989）。
  match:
    - 'severity="critical"'
  group_wait: "10s"
  repeat_interval: "2h"
```

NOC 收到的通知使用 `platform_summary` annotation，內容聚焦容量規劃和升級決策。Tenant 仍收到各自的 `summary`，不受影響。

### 設定路由預設值 (_routing_defaults)

```yaml
# conf.d/_defaults.yaml — _routing_defaults 是頂層 key，不放在 defaults: 底下
_routing_defaults:
  receiver:
    type: "slack"
    api_url: "https://hooks.slack.com/services/T/{{tenant}}-alerts"
    channel: "#{{tenant}}-team"
  group_wait: "30s"
  repeat_interval: "4h"
```

`{{tenant}}` 佔位符自動展開為各 tenant 的名稱。Tenant YAML 的 `_routing` 可覆蓋此預設。

⚠️ 不要把 `_routing` 寫進根目錄 `_defaults.yaml` 的 `defaults:` 底下：路由產生器不讀那裡，而 exporter 會因為解不開而丟掉整個 `defaults:` 區塊（所有平台閾值）。`da-tools validate-config` 的 `root_defaults` 列會擋下這個寫法（#2291）。

### 配置 Tenant Profile

```yaml
# conf.d/_profiles.yaml
profiles:
  standard-db:
    mysql_connections: "80"
    mysql_threads_running: "30"
    container_memory: "85"
  high-load-db:
    mysql_connections: "60"     # 更嚴格
    mysql_threads_running: "45" # 反而放寬：threads_running 是飽和訊號，高負載庫的正常併發本就較高
    container_memory: "80"
```

Tenant 可透過 `_profile` 繼承：

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant:
    _profile: "standard-db"
    mysql_connections: "70"     # 覆蓋 profile 的值
```

### Configuring Routing Profiles & Domain Policies (v2.1.0 ADR-007)

當多個 tenant 共用相同的路由配置時，建立 `_routing_profiles.yaml` 定義命名路由設定檔：

```yaml
# conf.d/_routing_profiles.yaml
routing_profiles:
  team-sre-apac:
    receiver:
      type: slack
      api_url: "https://hooks.slack.com/sre-apac"
    group_wait: 30s
    repeat_interval: 4h
  team-dba-global:
    receiver:
      type: pagerduty
      service_key: "dba-key-123"
    repeat_interval: 1h
```

Tenant 透過 `_routing_profile` 引用，四層合併順序為 `_routing_defaults` → profile → tenant `_routing` → `_routing_enforced`。

**Domain Policies** 在 `_domain_policy.yaml` 中定義業務域合規約束（如金融域禁止 Slack）：

```yaml
# conf.d/_domain_policy.yaml
domain_policies:
  finance:
    tenants: [db-finance, db-audit]
    constraints:
      forbidden_receiver_types: [slack, webhook]
      max_repeat_interval: 1h
```

驗證指令：`da-tools generate-routes --config-dir conf.d/ --validate`（routing profile 引用 + domain policy 約束都在這支裡跑。domain policy 違規預設只出 WARN，加 `--strict` 轉 ERROR 並 exit 非零；引用不存在的 routing profile 則不論有沒有 `--strict` 都只出 WARN、不擋——CI 的 validate-config job 跑 `--validate --strict`，domain policy 違規為 blocking。⚠️ `--strict` 是 v2.9.0 **之後**才加的，**目前出貨的映像兩支子命令都拒收它**：實測 `generate-routes --validate --strict` 與 `validate-config --strict` 皆為 `rc=2`、`unrecognized arguments: --strict`、stdout 零位元組。在映像更新前，這一段描述的是 CI 上的原始碼行為，不是你在本機跑得到的行為）。偵錯指令：`da-tools explain-route --config-dir conf.d/ --tenant <tenant-id>`。JSON Schema 可在 VS Code 中啟用即時驗證（見 `docs/schemas/`）。

### 設定 Webhook Domain Allowlist

限制 webhook receiver 的目標域名。`--policy` 吃的是**一個政策 YAML 的路徑**，不是域名本身：

```yaml
# policy.yaml
allowed_domains:
  - "*.example.com"
  - "hooks.slack.com"
```

```bash
python3 scripts/tools/ops/generate_alertmanager_routes.py \
  --config-dir conf.d/ \
  --policy policy.yaml \
  --validate
```

`allowed_domains` 的項目是 fnmatch 模式，支援萬用字元。⚠️ 空清單（或省略 `--policy`）表示不限制 — **生產環境強烈建議設定白名單**，避免 tenant 將告警發送到未授權的外部端點。

⛔ **本節的指令在 v2.9.0 之前是錯的**：它把域名直接當成 `--policy` 的值，而該旗標吃路徑。舊寫法會被當成「找不到這個檔案」，**而工具在 #1556 修好之前對此靜默 exit 0**——白名單整條沒跑，輸出仍是 `OK: all configs valid`。修好之後同一個寫法會 **exit 2**。⛔ 不要靠拿掉 `--policy` 轉綠，那等於把這個檢查關掉。

## 驗證工具

### 一站式配置驗證

```bash
python3 scripts/tools/ops/validate_config.py \
  --config-dir conf.d/
```

> 直接跑 `validate_config.py` 需要 da-guard：先 `make da-guard-build` 並設 `DA_GUARD_BINARY=.build/da-guard`（da-tools 映像已內建）；詳見 [validate-config 的前置需求](../cli-reference.md#validate-config)。

schema 驗證預設就跑，不用加旗標；有些檢查要帶旗標才出現（例如 `--policy`、`--rule-packs`、`--version-check`）。每一列檢查什麼、哪些要旗標，見 [CLI 參考 validate-config](../cli-reference.md#validate-config)。

### Alert Quality Scoring (v2.1.0)

```bash
# 掃描所有 tenant 的告警品質（Noise / Stale / Latency / Suppression）
da-tools alert-quality --prometheus http://localhost:9090

# CI gate：低於 60 分 exit 1
da-tools alert-quality --prometheus http://localhost:9090 --ci --min-score 60
```

### Policy-as-Code Validation (v2.1.0)

```bash
# 用 _defaults.yaml 中的 _policies DSL 評估所有 tenant
da-tools evaluate-policy --config-dir conf.d/

# CI gate：有 error 違規時 exit 1
da-tools evaluate-policy --config-dir conf.d/ --ci
```

### Cardinality Forecasting (v2.1.0)

```bash
# 預測 per-tenant 基數成長趨勢、觸頂天數
da-tools cardinality-forecast --prometheus http://localhost:9090

# CI gate：有 critical 風險時 exit 1
da-tools cardinality-forecast --prometheus http://localhost:9090 --ci
```

### 配置差異比對

```bash
python3 scripts/tools/ops/config_diff.py \
  --old-dir conf.d.baseline \
  --new-dir conf.d/ \
  --format json
```

輸出：新增 tenant、移除 tenant、變更的預設值、變更的 profile。用於 GitOps PR review。

### 版號一致性檢查

```bash
make version-check
python3 scripts/tools/dx/bump_docs.py --check
```

確保 CLAUDE.md、README、CHANGELOG 的版號同步。

## 效能監控

### 執行 Benchmark

```bash
make benchmark ARGS="--under-load --routing-bench --alertmanager-bench --reload-bench --json"
```

輸出指標：
- Idle memory footprint
- 延展曲線（QPS vs memory/latency）
- Routing throughput
- Alertmanager 反應時間
- ConfigMap reload 延遲

結果保存為 JSON，供 CI 比較。

### Platform Rule Pack 自監控

Platform 本身提供 Rule Pack alert（如 exporter 離線、Alertmanager delay > 1m）：

```bash
kubectl get alerts -n monitoring | grep platform
```

## 生產環境安全加固

完整的安全合規矩陣（Container 加固、NetworkPolicy、SAST、機密管理）見 [governance-security.md](../governance-security.md)。以下僅列 Platform Engineer 需要操作的關鍵項目。

### Port-forward 安全

本地 `kubectl port-forward` 預設綁定 `127.0.0.1`（僅本機）。**切勿使用 `--address 0.0.0.0`**，這會將 Prometheus/Alertmanager/Grafana 暴露到所有網路介面。

### Grafana 密碼

`secret-grafana.yaml` 提供開發環境 placeholder。生產部署前覆寫：

```bash
kubectl create secret generic grafana-credentials \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$(openssl rand -base64 24)" \
  -n monitoring --dry-run=client -o yaml | kubectl apply -f -
```

### Lifecycle 端點保護

Prometheus 的 `/-/quit` 端點可關閉服務（需 `--web.enable-lifecycle`）。**Alertmanager 沒有 `/-/quit`**（v0.33.1 實測 404），但它的 `/-/reload` 無條件啟用且關不掉。兩者都應透過 NetworkPolicy 擋掉**叢集內其他 Pod** 對管理埠的存取（同 Pod 內的 `config-reloader` sidecar 走 loopback，不經 NetworkPolicy，也不需要放行）。

### Secrets Management — Migrating from ConfigMap to K8s Secret

Alertmanager receiver 的敏感資訊（Slack token、webhook URL 等）必須存放在 K8s Secret，不可用 ConfigMap。基本做法是 `secretKeyRef`，進階做法整合 External Secrets Operator + HashiCorp Vault（自動輪換 + 審計日誌）。

**基本做法 — K8s Secret + secretKeyRef：**

```bash
# 1. 建立 Secret（一次性或由 CI 管理）
kubectl create secret generic alertmanager-secrets \
  --from-literal=slack-api-url='https://hooks.slack.com/services/T.../B.../xxx' \
  --from-literal=pagerduty-key='your-service-key' \
  -n monitoring
```

```yaml
# 2. 在 Alertmanager Deployment 中參照
env:
  - name: SLACK_API_URL
    valueFrom:
      secretKeyRef:
        name: alertmanager-secrets
        key: slack-api-url
  - name: PAGERDUTY_KEY
    valueFrom:
      secretKeyRef:
        name: alertmanager-secrets
        key: pagerduty-key
```

在 `generate_alertmanager_routes.py` 產生的 receiver 配置中，使用 `<secret>` 或環境變數參照而非明文值。

**進階做法 — External Secrets Operator + HashiCorp Vault：**

生產環境若需中央化 secrets 管理、自動輪換和審計日誌，整合 External Secrets Operator (ESO)：

```bash
# 1. 安裝 External Secrets Operator
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace
```

```yaml
# 2. 設定 SecretStore（連接 Vault）
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: vault-backend
  namespace: monitoring
spec:
  provider:
    vault:
      server: "https://vault.internal:8200"
      path: "secret"
      version: "v2"
```

### Webhook Domain Allowlist

`generate_alertmanager_routes.py --policy <policy.yaml>` 所指政策檔裡的 `allowed_domains:` 為空清單（或不給 `--policy`）表示不限制。**生產環境強烈建議設定白名單**。

### TLS 加密通訊指南

元件間通訊應啟用 TLS。推薦用 cert-manager 簽發憑證，threshold-exporter 支援 `--tls-cert-file` / `--tls-key-file` 參數，Prometheus 用 `scheme: https` + `tls_config` 抓取。

### Config Reload 端點安全

Prometheus 的 `/-/reload` 和 Alertmanager 的 `/-/reload` 是用於觸發設定重新載入的 HTTP POST 端點。本專案在**兩邊各有一顆** `config-reloader` sidecar 自動呼叫該端點：Alertmanager 側守路由設定（#1243），Prometheus 側守 Rule Pack 目錄 `/etc/prometheus/rules`（#1246 補上）。Prometheus 側**不涵蓋 `prometheus.yml`**——它以 `subPath` 掛載，K8s 不把 ConfigMap 更新傳播進 subPath 掛載，改它仍須重啟 Pod。

**安全影響：** 這些端點不需認證。如果攻擊者可以到達 Prometheus/Alertmanager 埠，他們可以重複觸發重新載入造成效能衝擊；Prometheus 若啟用了 `--web.enable-lifecycle`，還可以透過 `/-/quit` 關閉服務（Alertmanager 無此端點）。

**生產環境建議：** 使用上面「Lifecycle Endpoint Protection」章節的 NetworkPolicy 限制存取。確保 Prometheus 和 Alertmanager 使用 ClusterIP Services（不是 NodePort/LoadBalancer），僅在叢集內可達。

## Self-Service Portal（tenant-api）

> **部署方式**見上方 [§部署 tenant-api + da-portal](#部署-tenant-api--da-portaltenant-manager)。

平台提供 Web UI（tenant-manager）讓 Domain Expert 自助管理部分 tenant 配置：自訂告警直接在 UI 改，維護／靜默模式由 UI 產生 YAML 片段貼進租戶檔；閾值與路由仍改 YAML（或由自動化呼叫 tenant-api 寫入）。

- **tenant-api**：Go HTTP server，透過 oauth2-proxy 認證，commit-on-write 保留完整 Git 審計軌跡
- **RBAC**：`_rbac.yaml` 控制哪些 team 可管理哪些 tenant
- **群組管理**：自定義群組（存於 tenant-api 的 state store，不在 conf.d）+ 批量操作
- **PR 模式**：高安全環境以 Helm 值 `writeBack.mode: pr-github` 或 `pr-gitlab` 啟用（對應 tenant-api 的 `--write-mode` 旗標／`TA_WRITE_MODE` 環境變數；不是 conf.d 裡的設定），UI 操作產生 GitHub PR / GitLab MR，經 reviewer 審核後才生效

詳見 [ADR-009](../adr/009-tenant-manager-crud-api.md)（API 架構）、[ADR-011](../adr/011-pr-based-write-back.md)（PR 寫回模式）。

## 常見問題

**Q: 我要如何新增一個 Rule Pack？**
A: 新 Rule Pack 需在 `rule-packs/` 目錄新增 YAML 檔案，並在 Prometheus Projected Volume 配置中掛載對應的 ConfigMap。請參考 Rule Pack README 的模板。

**Q: 如何強制 NOC 接收所有通知？**
A: 在 `_defaults.yaml` 中設定 `_routing_enforced`。通知會發送給 NOC 的 channel 和各 tenant 的 receiver，獨立進行。

**Q: Webhook allowlist 為何拒絕我的 domain？**
A: 檢查你的 webhook URL 是否符合 `--policy` 所指政策檔裡 `allowed_domains:` 的 fnmatch 模式。`*` 會跨過點號，所以 `*.example.com` 也匹配 `webhook.internal.example.com`；但它不匹配 `example.com` 本身（要放行得另列一條），也不匹配結尾帶點的 `a.example.com.`。不符的 receiver 會印 WARN 並被略過，只有 `--validate` 會以 rc 1 結束。

**Q: 如何驗證新 tenant 的配置不會造成 alert noise？**
A: 先用 `validate_config.py` 檢查語法和 schema，再用 `config_diff.py` 看 blast radius，最後在 shadow monitoring 環境中測試（參考 shadow-monitoring-sop.md）。

**Q: Rule Pack 的 optional: true 是什麼？**
A: Kubernetes Projected Volume 的特性。設定 `optional: true` 後，如果該 ConfigMap 不存在，Prometheus 仍可啟動（卷掛載為空）。用於安全卸載 Rule Pack。

**Q: 我需要自定義某個 Rule Pack 中的規則嗎？**
A: 不直接修改 Rule Pack。在 tenant YAML 中用 `_routing.overrides[]` 覆蓋單個規則的路由，或用 custom rule governance（lint_custom_rules.py）新增自訂規則。

> 💡 **互動工具** — 驗證配置可用 [Config Lint](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/config-lint.jsx)。比較配置變更用 [Config Diff](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/config-diff.jsx)。查看 Rule Pack 依賴用 [Dependency Graph](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/dependency-graph.jsx)。完整 [Onboarding Checklist](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/onboarding-checklist.jsx) 追蹤上線進度。所有工具見 [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/)。需要在企業內網部署？用 `da-portal` Docker image：`docker run -p 8080:80 ghcr.io/vencil/da-portal`（[部署說明](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)）。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Platform Engineer 快速入門指南"](for-platform-engineers.md) | ⭐⭐⭐ |
| ["Domain Expert (DBA) 快速入門指南"](for-domain-experts.md) | ⭐⭐ |
| ["Tenant 快速入門指南"](for-tenants.md) | ⭐⭐ |
| ["Migration Guide — 遷移指南"](../migration-guide.md) | ⭐⭐ |
