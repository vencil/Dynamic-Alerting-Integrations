# da-tools (v2.9.0)

<!-- 標題版號 = 最後 released tag（目前 v2.8.0）；下一版 in-flight feature 在內文以 inline 版號標記。
     Release wrap 切六線 tag 時，本標題 + VERSION 檔 + 下方版號表跟著批次同步 bump。 -->

> 💡 **想快速把這個元件跑起來？** → **[app/QUICKSTART.md](app/QUICKSTART.md)**（`docker run` 對 fixture 跑 da-guard、≤2 分鐘看到 CI 紅字攔截）。本篇 README 是完整子命令 / 客戶旅程 **參考（Reference）**。

> **核心 component** — 一顆 ~60 MB Alpine Python image，把 Dynamic Alerting 平台所需 50 個驗證 / 遷移 / 治理 CLI 工具裝在一起，**無需 clone repo、無需安裝 Python 依賴**，docker pull 即用。Platform Engineer / SRE、Domain Expert、Tenant 三種角色共用入口（各角色上手指南見 §2）。
>
> **Companion 文件：** [interactive-tools.md](../../docs/interactive-tools.md) · [cli-reference.md](../../docs/cli-reference.md) · [cheat-sheet.md](../../docs/cheat-sheet.md) · [BYOP integration guide](../../docs/integration/byo-prometheus-integration.md) · [Shadow Monitoring SOP](../../docs/shadow-monitoring-sop.md) · [architecture-and-design](../../docs/architecture-and-design.md)

---

## 1. What & Why

- **Input** — Prometheus HTTP API（可攜式工具）、`conf.d/` YAML（檔案系統工具）、CSV/JSON spec（規則 / mapping 等）；透過 `docker run -v` 掛載
- **Output** — stdout 表格 / Markdown 報告 / JSON（`--json` 結構化）；部分工具寫檔（route fragment、ConfigMap、scaffold 結果）
- **Why container-bundled** — 1) 客戶現場常無 Python ≥3.12，**避開 dependency hell**；2) 整合 Go binary（`da-guard` / `da-batchpr` / `da-parser`），air-gapped 環境一顆 image 就夠；3) `tools/v*` tag 與平台版號脫鉤，工具迭代不影響 helm chart
- **Why one CLI, 50 subcommands** — 客戶旅程是連續的（discover → onboard → validate → cutover → operate → tune → migrate），分散成 50 個獨立 binary 反而難記；統一 `da-tools <cmd>` 入口配 `--help` 自我描述
- **不做的事** — 不執行 PromQL 查詢以外的 alerting reconcile（交給 `threshold-exporter`）；不 hot-reload（短命 CLI，每次 fresh state）；不持久化（除非 `-o` 明確指定輸出檔）；不 silent fail（CI 模式預設 exit 1 on any issue）

---

## 2. 角色導引（從哪開始）

本 README 是**角色中立的元件 reference**（完整 50 子命令 + 客戶旅程）。第一次接觸，先依角色挑任務導向的上手指南：

| 你是誰？ | 上手指南 |
|---------|---------|
| **Platform Engineer / SRE** — 部署、Rule Pack、路由、CI 護欄、遷移 | [Platform Engineer 入門](../../docs/getting-started/for-platform-engineers.md) |
| **Domain Expert** — 定義閾值、設計告警規則、Rule Pack 治理 | [Domain Expert 入門](../../docs/getting-started/for-domain-experts.md) |
| **Tenant** — 調自己租戶閾值、看告警、自助驗證 | [Tenant 入門](../../docs/getting-started/for-tenants.md) |

> 想直接查「我這個角色該用哪些命令」？看下方 §4.0 Journey Map 的 **典型負責角色** 欄。

---

## 3. Quick Start

### 3.0 選擇安裝路徑（Docker / static binary / 本機 checkout）

三種交付形態，**重點：不是每條路徑都含全部工具**。完整步驟（hash 驗證 / cosign 驗章 / air-gapped tar import）見 [Migration Toolkit 安裝指南](../../docs/migration-toolkit-installation.md)。

| 路徑 | 內容 | 適用 |
|---|---|---|
| **Docker image**（`ghcr.io/vencil/da-tools:v*`） | **全部 50 個 Python 子命令 + 3 顆 bundled Go binary**（da-guard / da-batchpr / da-parser） | self-hosted production、air-gapped（一顆 image 就夠）— **首選** |
| **Static binary 下載**（`tools/v*` release assets） | **僅** 3 顆 Go CLI（da-guard / da-batchpr / da-parser）；**不含** Python 子命令 | 只需 guard / parser / batch-pr，且不想跑 container 的 CI |
| **本機 repo checkout**（`python3 components/da-tools/app/entrypoint.py <cmd>`） | 全部 Python 子命令（依當前 source） | 開發 / 貢獻者 dev path |

> 一句話決策：要完整工具集 → **Docker image**；只要那 3 顆 Go gate 工具 → **static binary**；改 code 跑測試 → **本機 checkout**。Python 子命令**沒有** standalone binary，只能走 Docker 或本機 checkout。

```bash
# 抓 image（quick-start 用 :latest 即可；production 請釘特定版號如 :v2.8.0）
docker pull ghcr.io/vencil/da-tools:latest

# Help / Version
docker run --rm ghcr.io/vencil/da-tools --help
docker run --rm ghcr.io/vencil/da-tools --version

# 中文 CLI（預設依 $LANG / $LC_ALL，可顯式指定）
docker run --rm -e DA_LANG=zh ghcr.io/vencil/da-tools --help
```

最常見的兩種使用形態：

```bash
# 形態 A：純 Prometheus API（無檔案 IO，只需網路）
docker run --rm --network=host \
  -e PROMETHEUS_URL=http://prometheus.monitoring.svc:9090 \
  ghcr.io/vencil/da-tools \
  diagnose db-a

# 形態 B：檔案系統工具（mount conf.d/，可選 --network=host 走 Prometheus）
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d \
  ghcr.io/vencil/da-tools \
  validate-config --config-dir /data/conf.d
```

---

## 4. Command Reference

`da-tools` 共 50 個子命令。按 **客戶旅程** 分類；每個命令一行用途，完整 flag 用 `da-tools <cmd> --help`。

> **讀表須知**：`✨vX.Y.Z` = 該版新增；範例中的 `db-a` 等是**佔位 tenant id**（換成你的）；`validate`（Shadow Monitoring 雙軌比對，§4.4）與 `validate-config`（離線一站式配置驗證，§4.3）是**兩個不同命令**。

### 4.0 Journey Map（一目了然）

| 階段 | 典型負責角色 | 你想做什麼 | 用哪些命令 |
|------|------------|------------|------------|
| **Discover** | Platform | 認識客戶現有環境，盤點待遷移範圍 | `onboard`、`discover-mappings`、`blind-spot`、`parser` |
| **Onboard** | Platform | 建立新 tenant、bootstrap CI/CD | `init`、`scaffold`、`migrate`、`grafana-import` |
| **Validate** | Platform（custom rules 含 Domain） | Commit / PR 前驗 schema、routing、cardinality、policy | `validate-config`、`guard`、`evaluate-policy`、`opa-evaluate`、`lint`、`analyze-gaps`、`config-diff`、`drift-detect` |
| **Cutover** | Platform | Shadow Monitoring → 正式切換 | `validate`、`shadow-verify`、`backtest`、`cutover` |
| **Operate** | Platform（自己租戶含 Tenant） | 日常健康檢查、噪音治理、容量預測 | `diagnose`、`batch-diagnose`、`check-alert`、`alert-quality`、`alert-correlate`、`cardinality-forecast`、`maintenance-scheduler` |
| **Tune** | Domain（含 Tenant） | 觀測 baseline、推薦閾值、改 ConfigMap | `baseline`、`threshold-recommend`、`patch-config`、`explain-route` |
| **GitOps** | Platform | 產 Alertmanager fragment、批次 PR、快照比對 | `generate-routes`、`batch-pr`、`config-history`、`gitops-check`、`tenant-verify`、`state-reconcile` |
| **Migrate** | Platform | ConfigMap → Operator CRD、edge/central 拆分 | `migrate-to-operator`、`operator-generate`、`operator-check`、`runtime-audit`、`rule-pack-split`、`rule-pack-diff`、`silencer-drift-check` |
| **Decommission** | Platform | 下架 tenant、棄用 metric | `offboard`、`deprecate` |
| **Bridge** | Platform | 整合外掛 AM / Federation / 通知 | `byo-check`、`test-notification`、`federation-check`、`fed-key` |

### 4.1 Discover

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `onboard` | 分析既有 Alertmanager / Prometheus 配置產出遷移計畫 | `<config_file>` 或 `--alertmanager-config <file>` |
| `discover-mappings` | 自動發現 1:N 實例-租戶映射（掃 exporter `/metrics`） | `--prometheus <url>` |
| `blind-spot` | 掃 cluster targets vs `conf.d/` 找出未涵蓋的實例 | `--config-dir <dir>` |
| `parser` ✨v2.8.0 | 解析 PrometheusRule YAML → ParseResult JSON（子命令：`import` / `allowlist`） | `import <file>` |

### 4.2 Onboard

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `init` | 在客戶 repo bootstrap Dynamic Alerting 整合骨架（CI/CD + conf.d + Kustomize） | `--ci both --tenants db-a --rule-packs mariadb` |
| `scaffold` | 產 tenant 配置（互動 / 非互動，支援 1:1 / N:1 / 1:N 拓撲） | `--tenant <name> --db <types>` |
| `migrate` | 傳統 Prometheus 規則 → 動態格式（AST + regex 雙引擎，含 `--triage` 報告） | `<input_file>` |
| `grafana-import` | Grafana Dashboard JSON 匯入轉 conf.d 結構 | `<dashboard.json>` |

### 4.3 Validate（pre-flight gate）

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `validate-config` | 一站式配置驗證（YAML + schema + routes + policy） | `--config-dir <dir>` |
| `guard` ✨v2.8.0 | 子命令 `defaults-impact`：schema + routing + cardinality 三層檢查 | `defaults-impact --config-dir <dir>` |
| `evaluate-policy` | Policy-as-Code 宣告式 DSL（10 運算子） | `--config-dir <dir>` |
| `opa-evaluate` | OPA Rego 政策評估橋接 | `--config-dir <dir> --policy <file>` |
| `lint` | Custom Rule 治理合規檢查 | `<path...>` |
| `analyze-gaps` | Custom Rule vs Rule Pack 缺口分析 | `--config <path>` |
| `config-diff` | 兩目錄配置差異比對（GitOps PR review） | `--old-dir <dir> --new-dir <dir>` |
| `drift-detect` | 跨叢集 SHA-256 配置漂移偵測 | `--clusters <a,b>` |

### 4.4 Cutover

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `validate` | Shadow Monitoring 雙軌比對（含 auto-convergence） | `--mapping <file>` 或 `--old <q> --new <q>` |
| `shadow-verify` | Shadow Monitoring 雙軌驗證（精簡版，CI 用） | `--mapping <file>` |
| `backtest` | PR threshold 變更歷史回測 | `--git-diff` 或 `--config-dir <dir> --baseline <dir>` |
| `cutover` | 一鍵切換（停 shadow → 移 recording rules → 移 shadow route → 健康檢驗） | `--readiness-json <file> --tenant <name>` |

### 4.5 Operate（Day-2）

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `diagnose` | 單 tenant 健康檢查（config + metric + alert） | `<tenant>` |
| `batch-diagnose` | 多 tenant 並行健康檢查（auto-discover） | （自動探索） |
| `check-alert` | 查 alert 觸發狀態 | `<alert_name> <tenant>` |
| `alert-quality` | 警報品質四指標評估（噪音 / 陳腐 / 延遲 / 壓制） | `--tenant <name>` 或 `--all` |
| `alert-correlate` | 告警關聯分析（時間窗聚類 + 根因推斷） | `--tenant <name>` |
| `cardinality-forecast` | 基數線性回歸預測 + 觸頂天數 | `--tenant <name>` 或 `--all` |
| `maintenance-scheduler` | 評估排程式維護窗、自動建 Alertmanager silence | `--config-dir <dir>` |

### 4.6 Tune

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `baseline` | 觀測指標 + 閾值建議 | `--tenant <name>` |
| `threshold-recommend` | 閾值推薦引擎（歷史 P50/P95/P99） | `--tenant <name> --metric <key>` |
| `threshold-govern` | 閾值治理迴路（#656）：推薦→過濾→經 tenant-api 開 per-tenant proposed-PR | `--config-dir <dir> --apply --tenant-api-url <url>` |
| `patch-config` | ConfigMap 局部更新（`--diff` 模式預覽） | `<tenant> <metric> <value>` 或 `--diff` |
| `explain-route` | 路由 merge pipeline 除錯器（四層展開 + profile） | `--tenant <name>` |

### 4.7 GitOps

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `generate-routes` | tenant YAML → AM route + receiver + inhibit fragment 或完整 ConfigMap | `--config-dir <dir>` |
| `batch-pr` ✨v2.8.0 | Hierarchy-aware Batch PR（子命令：`apply` / `refresh` / `refresh-source`） | `apply --plan <p> --emit-dir <d> --repo <r> --workdir <w>` |
| `config-history` | 配置快照與歷史追蹤（子命令：`snapshot` / `log` / `diff` / `show`） | `snapshot --config-dir <dir>` |
| `gitops-check` | GitOps Native Mode 就緒度驗證（repo / local / sidecar 三模式） | `--mode <m> --config-dir <dir>` |
| `tenant-verify` ✨v2.8.0 | 印 tenant effective config + merged_hash；`--expect-merged-hash` 比對快照（rollback 驗證） | `<tenant> --conf-d <dir>` |
| `state-reconcile` ✨v2.8.0 | 遷移狀態目錄聲明式一致化（`.da/state/*.json` schema 驗證 + `.da/manifest.json` 重建），取代手動 jq 校正流程 | `--state-dir <dir>`（預設 `.da/state`） |

### 4.8 Migrate（Operator-Native / Federation）

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `migrate-to-operator` | ConfigMap 格式 → Operator 原生 CRD（含遷移清單與預檢） | `--source-dir <d> --config-dir <d>` |
| `operator-generate` | 產出 PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML | `--config-dir <dir>` |
| `operator-check` | 驗證 Operator CRD 部署狀態（5 項檢查 + 診斷） | `--namespace <ns>` |
| `runtime-audit` | Git rule-packs ↔ Prometheus runtime 唯讀對帳（MISSING / UNHEALTHY / ORPHAN；偵測-only，不自癒） | `--prometheus <url>` 或 `--runtime-json <file>` |
| `rule-pack-split` | Rule Pack 分層拆分（edge Part 1 + central Parts 2+3） | `--rule-pack <file>` |
| `rule-pack-diff` ✨v2.8.0 | Rule Pack 兩版本機械比對（added / removed / breaking label schema），供 upgrade audit | `--from <v1.yaml> --to <v2.yaml>` |
| `silencer-drift-check` ✨v2.8.0 | AM silence 對 v2 rule pack 漂移偵測（offline，吃 amtool silence query -o json dump），cutover 必跑 | `--silences-file <json> --rule-source <path>` |

### 4.9 Decommission

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `offboard` | 下架 tenant 配置（含預檢） | `<tenant>` |
| `deprecate` | 標記 metric 為 disabled | `<metric_keys...>` |

### 4.10 Bridge（外掛整合）

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `byo-check` | BYO Alertmanager 整合前檢（endpoint + 配置驗證） | `--alertmanager <url>` |
| `test-notification` | 多通道通知連通性測試（驗 receiver 可達性） | `--config-dir <dir>` |
| `federation-check` | Prometheus Federation 健康檢查 | `--prometheus <url>` |
| `fed-key` | 產生 / 輪替 federation JWT 簽章金鑰（federation 簽章金鑰：私鑰 Secret + 公鑰 JWKS） | （無，bootstrap）/ `--rotate` |

---

## 5. Cookbook

挑高頻場景示範完整 docker run 指令；其餘場景見 [cli-reference.md](../../docs/cli-reference.md)。

### 5.1 BYOP 整合驗收（Discover → Validate → Operate）

完成 [BYOP 整合指南](../../docs/integration/byo-prometheus-integration.md) 三步驟後：

```bash
export PROM=http://prometheus.monitoring.svc.cluster.local:9090
DA="docker run --rm --network=host -e PROMETHEUS_URL=$PROM ghcr.io/vencil/da-tools"

# 1. Tenant 健康檢查
$DA diagnose db-a

# 2. 觀測指標 + 閾值建議
$DA baseline --tenant db-a --duration 300

# 3. Shadow Monitoring 雙軌比對（auto-convergence）
docker run --rm --network=host \
  -v $(pwd)/mapping.csv:/data/mapping.csv \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools \
  validate --mapping /data/mapping.csv --watch --rounds 5
```

### 5.2 規則遷移（離線、無 Prometheus）

```bash
docker run --rm \
  -v $(pwd)/my-rules.yml:/data/my-rules.yml \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools \
  migrate /data/my-rules.yml -o /data/output --dry-run --triage

# 產出：
#   /data/output/migration_output/  ← 轉換後的規則
#   /data/output/triage.csv         ← 需人工審閱清單
```

### 5.3 GitOps 完整 ConfigMap 產出

`generate-routes --output-configmap` 產出可直接 `kubectl apply` 的完整 Alertmanager ConfigMap YAML，適合 Git PR flow：

```bash
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools \
  generate-routes --config-dir /data/conf.d --output-configmap \
    -o /data/output/alertmanager-configmap.yaml
```

| 場景 | 模式 | 原因 |
|------|------|------|
| P0 緊急修復 | `--apply --yes` | 立即生效，跳過 PR flow |
| GitOps 正常流程 | `--output-configmap` | 產檔進 Git，走 review + CI |
| CI 驗證 | `--validate` | 只驗不寫，exit code 0/1 |

### 5.4 Pre-merge Defaults Guard（v2.8.0）

阻止「改一行 `_defaults.yaml` 影響 500 租戶」這類 dangling defaults 事故：

```bash
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d \
  ghcr.io/vencil/da-tools \
  guard defaults-impact \
    --config-dir /data/conf.d \
    --required-fields cpu,memory \
    --cardinality-limit 500 \
    --warn-as-error
# exit 0 = clean, 1 = guard found errors, 2 = caller error
```

### 5.5 Hierarchy-aware Batch PR（v2.8.0）

把 1000 租戶導入從「手動切 chunk + 手動開 PR」變成一行命令：

```bash
docker run --rm \
  -v $(pwd)/plan.json:/data/plan.json \
  -v $(pwd)/emit:/data/emit \
  -v $(pwd)/customer-repo:/data/repo \
  -e GH_TOKEN=$GH_TOKEN \
  ghcr.io/vencil/da-tools \
  batch-pr apply \
    --plan /data/plan.json \
    --emit-dir /data/emit \
    --repo vencil/customer \
    --workdir /data/repo
```

Refresh / refresh-source 兩個子命令處理 Base PR merge 後的 rebase 與 hot-fix 重 apply：見 `da-tools batch-pr --help`。

### 5.6 Rollback 驗證（v2.8.0）

[Emergency Rollback Procedures](../../docs/scenarios/incremental-migration-playbook.md#emergency-rollback-procedures) 的 checklist 第 6 項：

```bash
# Pre-base：拍快照
docker run --rm -v $(pwd)/conf.d:/data/conf.d ghcr.io/vencil/da-tools \
  tenant-verify --all --json --conf-d /data/conf.d > pre-base-snapshot.json

# Rollback 後：比對 merged_hash
docker run --rm -v $(pwd)/conf.d:/data/conf.d ghcr.io/vencil/da-tools \
  tenant-verify db-a --conf-d /data/conf.d \
  --expect-merged-hash $(jq -r '.["db-a"].merged_hash' pre-base-snapshot.json)
# exit 0 = 與快照一致，rollback 成功；非 0 = 不一致，需追查
```

---

## 6. Operator Reference

### 6.1 環境變數

| 變數 | 用途 | 預設 |
|------|------|------|
| `PROMETHEUS_URL` | Prometheus 端點（`--prometheus` fallback） | `http://localhost:9090` |
| `DA_LANG` | CLI 語言（`zh` / `en`，優先於 `LC_ALL` / `LANG`） | 從 `LANG` / `LC_ALL` 偵測，預設 `en` |
| `DA_GUARD_BINARY` | `da-guard` 路徑 override（image 內預設 `/usr/local/bin/da-guard`） | — |
| `DA_BATCHPR_BINARY` | `da-batchpr` 路徑 override | — |
| `DA_PARSER_BINARY` | `da-parser` 路徑 override | — |

> **容器內 `localhost` 是容器自己**：
> - K8s 內部 → `http://prometheus.monitoring.svc.cluster.local:9090`
> - Docker Desktop → `http://host.docker.internal:9090`
> - Linux Docker → `--network=host` 搭配 `http://localhost:9090`

### 6.2 Bundle Artifacts

`da-tools` image 在 build 階段把三顆 Go binary 一起打包進來，air-gapped 環境一顆 image 就齊：

| Binary | Wrap by | 來源 | 子命令 |
|--------|---------|------|--------|
| `/usr/local/bin/da-guard` | `da-tools guard` | [`components/threshold-exporter/app/cmd/da-guard/`](../../components/threshold-exporter/app/cmd/da-guard/) | `defaults-impact` |
| `/usr/local/bin/da-batchpr` | `da-tools batch-pr` | [`components/threshold-exporter/app/cmd/da-batchpr/`](../../components/threshold-exporter/app/cmd/da-batchpr/) | `apply` / `refresh` / `refresh-source` |
| `/usr/local/bin/da-parser` | `da-tools parser` | [`components/threshold-exporter/app/cmd/da-parser/`](../../components/threshold-exporter/app/cmd/da-parser/) | `import` / `allowlist` |

> Binary 解析順序（每個 dispatcher 都遵循）：`--<name>-binary <path>` → `$<NAME>_BINARY` env → `$PATH`。Image 內第三層永遠命中 `/usr/local/bin/`。

### 6.3 Exit Codes

| Code | 含義 |
|------|------|
| `0` | 成功 / 無問題 |
| `1` | 工具邏輯錯誤（lint failure、guard found errors、validate mismatch...） |
| `2` | Caller error（flag 錯、檔不存在、Go binary 缺失...） |

`--ci` mode 將 warning 也視為 exit 1。

### 6.4 Bilingual Help

所有命令支援雙語 help。預設依 `LANG` / `LC_ALL` 偵測（`zh*` → 中文，其他 → 英文），`DA_LANG` 顯式指定可覆蓋：

```bash
DA_LANG=zh docker run --rm ghcr.io/vencil/da-tools migrate --help
DA_LANG=en docker run --rm ghcr.io/vencil/da-tools migrate --help
```

---

## 7. Versioning

`da-tools` 採 **獨立版號**，與平台 / threshold-exporter / portal 版號脫鉤：

| 元件 | 版號 | Git Tag | 內容 |
|------|------|---------|------|
| 平台文件 | v2.9.0 | `v2.9.0` | 整體釋出版本 |
| threshold-exporter | v2.9.0 | `exporter/v2.9.0` | Go binary（含 da-guard / da-batchpr / da-parser） |
| **da-tools** | **v2.9.0** | **`tools/v2.9.0`** | 本 image（50 個 Python CLI + 3 個 bundled Go binary） |
| da-portal | v2.9.0 | `portal/v2.9.0` | Interactive Tools Hub image |
| tenant-api | v2.8.0 | `tenant-api/v2.8.0` | Go HTTP API |

> 命令表的 ✨vX.Y.Z 標記表示該命令於該版引入；下一版 in-flight 命令沿用同款 inline 標記。Release 收尾切六線 tag 時，本 README 標題與 [VERSION](app/VERSION) 跟著批次同步 bump。
>
> **升級相容性** — da-tools 跨版本維持 **additive**：既有命令保留原 flag 介面，客戶 CI 內既有 `docker run ... da-tools <cmd>` 不需改動。

CI/CD 由 `tools/v*` tag 觸發，不受平台文件 / exporter 變更影響。

---

## 8. Local Build

```bash
cd components/da-tools/app

# Build dev image（tag = da-tools:dev）
./build.sh

# Build 指定版號（同步寫進 image label + VERSION 檔內容）
./build.sh 2.9.0

# Assemble-only mode（CI 用，給 Buildx 接 multi-arch build）
./build.sh --assemble-only

# 載入 Kind cluster（K8s Job 場景）
kind load docker-image da-tools:dev --name dynamic-alerting-cluster
```

`build.sh` 的工作流程：
1. 從 [`scripts/tools/`](../../scripts/tools/) 複製 [TOOL_FILES](app/build.sh) 列表內的 Python 工具進 build context
2. Strip repo-layout `sys.path` hack（Docker flat layout 不需要）
3. 從 [`components/threshold-exporter/app/cmd/`](../../components/threshold-exporter/app/cmd/) 編 `da-guard` / `da-batchpr` / `da-parser` 三顆 linux/amd64 binary
4. `docker build` → 清理臨時檔

**前置需求**：本地需有 Go 1.26+ 才能編 Go binary。CI 已有 `actions/setup-go`。

---

## 9. As Kubernetes Job

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: byop-validation
  namespace: monitoring
spec:
  template:
    spec:
      containers:
        - name: da-tools
          image: ghcr.io/vencil/da-tools:v2.9.0
          env:
            - name: PROMETHEUS_URL
              value: "http://prometheus.monitoring.svc.cluster.local:9090"
          args: ["check-alert", "MariaDBHighConnections", "db-a"]
      restartPolicy: Never
  backoffLimit: 0
```

Common patterns:
- **CronJob**：把 `Job` 換 `CronJob` + `schedule: "0 */6 * * *"` 跑定期 `batch-diagnose`
- **InitContainer**：deploy `threshold-exporter` 前用 `da-tools validate-config` 擋壞配置
- **PR check**：GitHub Actions 在 PR review 階段跑 `da-tools guard defaults-impact`

---

## 10. Troubleshooting

| 症狀 | 檢查 |
|------|------|
| `da-guard binary not found` | image 應已 bundle；若自編 image，確認 `build.sh` 的 Go binary build 步驟成功 |
| `Connection refused` 連 Prometheus | 容器內 `localhost` ≠ host；改用 `--network=host` 或 service DNS |
| Help 文字是英文（想看中文） | `-e DA_LANG=zh` 或 `-e LANG=zh_TW.UTF-8` |
| `Unknown command 'xxx'` | `da-tools --help` 看實際支援命令；舊版 README 列名與 entrypoint 偶有 drift（v2.8.0 後 [pr-preflight](../../Makefile) 會擋） |
| 寫檔權限錯 | image 以 `nonroot` (uid 10001) 跑；mount 目標需給 uid 10001 寫入權 |
