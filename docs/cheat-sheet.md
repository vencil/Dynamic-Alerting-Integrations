---
title: "da-tools Quick Reference"
tags: [reference, cli, cheat-sheet]
audience: [all]
version: v2.1.0
lang: zh
---

# da-tools 快速參考

> **Language / 語言：** | **中文（當前）**

da-tools 命令速查表。完整文件見 [cli-reference.md](cli-reference.md)。

## 命令速查

| 命令 | 說明 | 常用 Flag | 範例 |
|------|------|----------|------|
| `check-alert` | 查詢特定 alert 在某個 tenant 上的狀態 | - | `da-tools check-alert --help` |
| `diagnose` | 對單一 tenant 執行全面健康檢查 | --config-dir <PATH>, --namespace <NS> | `da-tools diagnose --help` |
| `batch-diagnose` | 對所有 tenant 執行並行健康檢查 | --tenants <LIST>, --workers <N>, --timeout <SEC> | `da-tools batch-diagnose --help` |
| `baseline` | 觀測指標時間序列，計算統計摘要（p50/p90/p95/p99/max），產出閾值建議 | --tenant <NAME>, --duration <SEC>, --interval <SEC> | `da-tools baseline --help` |
| `validate` | Shadow Monitoring 驗證工具：比對新舊 Recording Rule 數值，偵測自動... | --watch, --interval <SEC>, --rounds <N> | `da-tools validate --help` |
| `cutover` | Shadow Monitoring 一鍵切換：停止舊規則、啟用新規則、驗證健康 | --tenant <NAME>, --readiness-json <FILE>, --dry-run | `da-tools cutover --help` |
| `blind-spot` | 掃描 Prometheus 叢集的活躍 targets，與 tenant 配置交叉比對，找出盲區（有... | --config-dir <PATH>, --exclude-jobs <LIST>, --json-output | `da-tools blind-spot --help` |
| `maintenance-scheduler` | 評估排程式維護窗口，自動建立 Alertmanager silence | --config-dir <PATH>, --output <FILE>, --timezone <TZ> | `da-tools maintenance-scheduler --help` |
| `backtest` | 執行 PR 中 threshold 變更的歷史回測 | --lookback <DAYS>, --output <FILE> | `da-tools backtest --help` |
| `shadow-verify` | Shadow Monitoring 就緒度與收斂性三階段驗證 | --mapping <FILE>, --report-csv <FILE>, --readiness-json <FILE> | `da-tools shadow-verify --help` |
| `byo-check` | 自動化 BYO Prometheus & Alertmanager 整合驗證（取代手動 curl +... | --prometheus <URL>, --alertmanager <URL>, --json | `da-tools byo-check --help` |
| `federation-check` | 多叢集 Federation 整合驗證（自動化 federation-integration | --prometheus <URL>, --edge-urls <URLS>, --json | `da-tools federation-check --help` |
| `grafana-import` | Grafana Dashboard 匯入工具（透過 ConfigMap sidecar 自動掛載） | --dashboard <FILE>, --dashboard-dir <DIR>, --name <NAME> | `da-tools grafana-import --help` |
| `generate-routes` | 從 tenant YAML 產出 Alertmanager route + receiver + i... | --config-dir <PATH>, --output <FILE>, --output-configmap | `da-tools generate-routes --help` |
| `patch-config` | ConfigMap 局部更新工具，支援 preview（--diff）和直接應用 | --namespace <NS>, --configmap <CM>, --dry-run | `da-tools patch-config --help` |
| `scaffold` | 產生新 tenant 配置（互動式或非互動式） | --non-interactive, --tenant <NAME>, --db <LIST> | `da-tools scaffold --help` |
| `migrate` | 將傳統 Prometheus 規則轉換為動態格式（AST 引擎） | --output <DIR>, --dry-run, --triage | `da-tools migrate --help` |
| `validate-config` | 一站式配置驗證：YAML 格式、schema、routing、policy、版本一致性 | --config-dir <PATH>, --policy <DOMAINS>, --ci | `da-tools validate-config --help` |
| `offboard` | 下架 tenant 配置與相關資源 | --config-dir <PATH>, --backup <DIR>, --cleanup-rules | `da-tools offboard --help` |
| `deprecate` | 標記指標為 disabled，防止誤用 | --config-dir <PATH>, --reason <TEXT>, --dry-run | `da-tools deprecate --help` |
| `lint` | 檢查 Custom Rule 的治理合規性（根據 `custom_` 前綴規則） | --strict, --json-output | `da-tools lint --help` |
| `onboard` | 分析既有 Alertmanager 或 Prometheus 配置，產出遷移提示 | --alertmanager-config <FILE>, --output <FILE> | `da-tools onboard --help` |
| `analyze-gaps` | 比對 custom rule 與 Rule Pack，找出重複/缺口 | --config <PATH>, --output <FILE>, --json-output | `da-tools analyze-gaps --help` |
| `config-diff` | 比較兩個配置目錄（GitOps PR review） | --old-dir <PATH>, --new-dir <PATH>, --json-output | `da-tools config-diff --help` |
| `alert-quality` | 警報品質評估：4 指標、三級評分、CI gate | --prometheus <URL>, --tenant <NAME>, --ci --min-score <N> | `da-tools alert-quality --help` |
| `alert-correlate` | 告警關聯分析：時間窗口聚類 + 根因推斷 | --prometheus <URL>, --input <FILE>, --window <MIN>, --min-score <N> | `da-tools alert-correlate --help` |
| `drift-detect` | 跨叢集配置漂移偵測：目錄級 SHA-256 比對 | --dirs <LIST>, --labels <LIST>, --ci | `da-tools drift-detect --help` |
| `cardinality-forecast` | Per-tenant 基數趨勢預測與觸頂預警 | --prometheus <URL>, --limit <N>, --warn-days <N>, --ci | `da-tools cardinality-forecast --help` |
| `evaluate-policy` | Policy-as-Code 評估引擎：宣告式 DSL 策略檢查 | --config-dir <PATH>, --policy <FILE>, --ci | `da-tools evaluate-policy --help` |
| `test-notification` | 多通道通知連通性測試：驗證 receiver 可達性 | --config-dir <PATH>, --tenant <NAME>, --dry-run, --ci | `da-tools test-notification --help` |
| `threshold-recommend` | 閾值推薦引擎：基於歷史 P50/P95/P99 數據 | --config-dir <PATH>, --prometheus <URL>, --lookback, --json | `da-tools threshold-recommend --help` |
| `explain-route` | 路由合併管線除錯器：四層展開 + 設定檔擴展 (ADR-007) | --config-dir <PATH>, --tenant <NAME>, --show-profile-expansion, --json | `da-tools explain-route --help` |
| `discover-mappings` | 自動發現 1:N 實例-租戶映射 (ADR-006) | --endpoint <URL> 或 --prometheus <URL> --instance <INST>, --job, -o, --json | `da-tools discover-mappings --help` |

## 快速提示

- **Prometheus API 工具**：需要能連到 Prometheus HTTP API
  - `check-alert` — 查詢 alert 狀態
  - `diagnose` / `batch-diagnose` — Tenant 健康檢查
  - `baseline` — 觀測指標，產出閾值建議
  - `validate` — Shadow Monitoring 雙軌比對
  - `cutover` — 一鍵切換（遷移最後一步）
  - 其他：`blind-spot`、`maintenance-scheduler`、`backtest`
  - `alert-quality` — 警報品質評估（noise, stale, latency, suppression）
  - `alert-correlate` — 告警關聯分析（時間窗口聚類 + 根因推斷）
  - `cardinality-forecast` — Per-tenant 基數趨勢預測
  - `threshold-recommend` — 閾值推薦（P50/P95/P99）

- **配置生成工具**
  - `generate-routes` — Tenant YAML → Alertmanager fragment
  - `patch-config` — ConfigMap 快速更新

- **檔案系統工具**（離線可用）
  - `scaffold` — 產生 tenant 配置
  - `migrate` — 規則格式轉換
  - `validate-config` — 配置驗證
  - `offboard` / `deprecate` — Tenant 下架／指標棄用
  - `lint` / `onboard` / `analyze-gaps` / `config-diff` — 治理工具
  - `evaluate-policy` — Policy-as-Code 評估（宣告式 DSL）
  - `test-notification` — 多通道通知連通性測試
  - `explain-route` — 路由合併管線除錯器（四層展開）
  - `discover-mappings` — 自動發現 1:N 實例-租戶映射

## 網路配置

```bash
# K8s internal
export PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090

# Docker Desktop
export PROMETHEUS_URL=http://host.docker.internal:9090

# Linux Docker (--network=host)
export PROMETHEUS_URL=http://localhost:9090
```

## 常用樣板

```bash
# Basic command
docker run --rm --network=host \
  -e PROMETHEUS_URL=$PROMETHEUS_URL \
  ghcr.io/vencil/da-tools:v2.1.0 \
  <command> [arguments]

# With local files
docker run --rm --network=host \
  -v $(pwd)/conf.d:/etc/config:ro \
  -e PROMETHEUS_URL=$PROMETHEUS_URL \
  ghcr.io/vencil/da-tools:v2.1.0 \
  <command> --config-dir /etc/config
```

---

完整參考見 [cli-reference.md](cli-reference.md)。
