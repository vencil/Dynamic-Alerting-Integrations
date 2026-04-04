---
title: "da-tools CLI Reference"
tags: [cli, reference, da-tools, tools]
audience: [platform-engineer, sre, devops, tenant]
version: v2.3.0
lang: zh
---

# da-tools CLI Reference

> **受眾**：Platform Engineers、SREs、DevOps、Tenants
> **容器映像**：`ghcr.io/vencil/da-tools:v2.3.0`
> **版本**：v2.1.0（與平台版本同步）

da-tools 是一個可攜式 CLI 容器，打包了 Dynamic Alerting 平台的驗證、遷移、配置與運維工具。本文件是所有子命令的完整參考。

---

## 目錄

1. [快速開始](#快速開始)
2. [全局選項](#全局選項)
3. [命令分類](#命令分類)
4. [命令詳解](#命令詳解)
   - [Prometheus API 工具](#prometheus-api-工具)
   - [配置生成工具](#配置生成工具)
   - [檔案系統工具](#檔案系統工具)
5. [環境變數](#環境變數)
6. [Docker 快速參考](#docker-快速參考)

---

## 快速開始

### 拉取映像

```bash
# 從 OCI registry 拉取（需要 CI/CD 已推送）
docker pull ghcr.io/vencil/da-tools:v2.3.0

# 本地建構（開發用）
cd components/da-tools/app && ./build.sh v1.11.0
```

### 查看說明

```bash
docker run --rm ghcr.io/vencil/da-tools:v2.3.0 --help
docker run --rm ghcr.io/vencil/da-tools:v2.3.0 --version
da-tools <command> --help
```

---

## Docker 使用模式

--8<-- "docs/includes/docker-usage-pattern.md"

> 後續範例省略此前綴，僅顯示 `da-tools <command>` 形式。

---

## 全局選項

所有命令都支援以下全局選項：

| 選項 | 說明 |
|------|------|
| `--help` | 顯示幫助訊息 |
| `--version` | 顯示版本資訊 |
| `--prometheus <URL>` | Prometheus Query API 端點（預設：`http://localhost:9090`；可用 `PROMETHEUS_URL` env var） |
| `--config-dir <PATH>` | 租戶配置目錄路徑（預設：`./conf.d`；部分命令需要） |

---

## 命令分類

### Prometheus API 工具（需網路存取）

這些工具只需要能連到 Prometheus HTTP API，可從任何位置執行。

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `check-alert` | 查詢特定 tenant 的 alert 狀態 | `<alert_name> <tenant>` |
| `diagnose` | Tenant 健康檢查（config + metric + alert 狀態） | `<tenant>` |
| `batch-diagnose` | 批次租戶健康檢查（auto-discover + 並行診斷） | （自動探索） |
| `baseline` | 觀測指標 + 推薦閾值 | `--tenant <name>` |
| `validate` | Shadow Monitoring 雙軌比對（含 auto-convergence） | `--mapping <file>` 或 `--old <query> --new <query>` |
| `cutover` | Shadow Monitoring 一鍵切換 | `--tenant <name>` |
| `blind-spot` | 掃描 cluster targets 與 tenant config 交叉比對盲區 | `--config-dir <dir>` |
| `maintenance-scheduler` | 評估排程式維護窗口，自動建立 Alertmanager silence | `--config-dir <dir>` |
| `backtest` | PR threshold 變更歷史回測 | `--git-diff` 或 `--config-dir` + `--baseline` |
| `shadow-verify` | Shadow Monitoring 就緒度與收斂性驗證（preflight / runtime / convergence） | `<phase>` |
| `byo-check` | BYO Prometheus & Alertmanager 整合驗證 | `<target>` |
| `federation-check` | 多叢集 Federation 整合驗證（edge / central / e2e） | `<target>` |
| `grafana-import` | Grafana Dashboard ConfigMap 匯入（sidecar 自動掛載） | `--dashboard <file>` 或 `--verify` |
| `alert-quality` | 警報品質評估（4 指標、三級評分、CI gate） | `--prometheus <url>` |
| `alert-correlate` | 告警關聯分析（時間窗口聚類 + 根因推斷） | `--prometheus <url>` 或 `--input <file>` |
| `drift-detect` | 跨叢集配置漂移偵測（目錄級 SHA-256 比對） | `--dirs <list>` |
| `cardinality-forecast` | Per-tenant 基數趨勢預測與觸頂預警 | `--prometheus <url>` |
| `config-history` | 配置快照與歷史追蹤（snapshot / log / show / diff） | `--config-dir <dir> <action>` |

### 採用與初始化

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `init` | 專案骨架產生（CI/CD + conf.d + Kustomize overlays） | `--ci <platform>` 或互動模式 |
| `gitops-check` | GitOps Native Mode 就緒度驗證（repo / local / sidecar） | `<subcommand>` |

### Operator + Federation 工具

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `operator-generate` | Rule Packs + Tenant 配置 → PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML | `--rule-packs-dir <dir>` |
| `operator-check` | Operator CRD 部署狀態驗證（5 項檢查 + 診斷報告） | （自動探索或 `--namespace <ns>`） |
| `rule-pack-split` | Rule Pack 分層拆分（edge Part 1 + central Parts 2+3），Federation Scenario B | `--rule-packs-dir <dir>` |

### 配置生成工具

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `generate-routes` | Tenant YAML → Alertmanager route + receiver + inhibit fragment | `--config-dir <dir>` |
| `patch-config` | ConfigMap 局部更新（含 `--diff` preview） | `<tenant> <metric> <value>` 或 `--diff` |

### 檔案系統工具（離線可用）

這些工具操作本地 YAML 檔案，不需網路。

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `scaffold` | 產生 tenant 配置 | `--tenant <name> --db <types>` |
| `migrate` | 傳統規則 → 動態格式轉換（AST 引擎） | `<input_file>` |
| `validate-config` | 一站式配置驗證（YAML + schema + routes + policy） | `--config-dir <dir>` |
| `offboard` | 下架 tenant 配置 | `<tenant>` |
| `deprecate` | 標記指標為 disabled | `<metric_keys...>` |
| `lint` | 檢查 Custom Rule 治理合規性 | `<path...>` |
| `onboard` | 分析既有 Alertmanager/Prometheus 配置進行遷移 | `<config_file>` 或 `--alertmanager-config <file>` |
| `analyze-gaps` | Custom Rule 對應 Rule Pack 缺口分析 | `--config <path>` |
| `config-diff` | 兩目錄配置差異比對（GitOps PR review） | `--old-dir <dir> --new-dir <dir>` |
| `evaluate-policy` | Policy-as-Code DSL 評估引擎 | `--config-dir <dir>` |
| `opa-evaluate` | OPA Rego 策略評估橋接（OPA 整合） | `--config-dir <dir>` |
| `test-notification` | 多通道通知連通性測試（驗證 receiver 可達性） | `--config-dir <dir>` |
| `threshold-recommend` | 閾值推薦引擎（基於歷史 P50/P95/P99 數據） | `--config-dir <dir>` + `--prometheus <url>` |
| `explain-route` | 路由合併管線除錯器（四層展開 + 設定檔擴展，ADR-007） | `--config-dir <dir>` |
| `discover-mappings` | 自動發現 1:N 實例-租戶映射（掃描 exporter /metrics，ADR-006） | `--endpoint <url>` 或 `--prometheus <url>` |

---

## 命令詳解

### Prometheus API 工具

#### check-alert

查詢特定 alert 在某個 tenant 上的狀態。

**用途**：BYOP 整合驗證、debug alert 狀態。

**語法**

```bash
da-tools check-alert <alert_name> <tenant> [options]
```

**必需參數**

| 參數 | 說明 | 範例 |
|------|------|------|
| `<alert_name>` | Alert 名稱 | `MariaDBHighConnections` |
| `<tenant>` | Tenant ID | `db-a` |

**輸出**

JSON 格式，包含 alert 狀態（firing / pending / inactive）。

```json
{
  "alert": "MariaDBHighConnections",
  "tenant": "db-a",
  "state": "firing",
  "details": [
    {
      "state": "firing",
      "activeAt": "2026-03-12T10:30:00Z"
    }
  ]
}
```

**範例**

```bash
da-tools check-alert MariaDBHighConnections db-a
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功（任何狀態） |
| `1` | Prometheus 連線失敗 |

---

#### diagnose

對單一 tenant 執行全面健康檢查。

**用途**：驗證 tenant 配置、metric 收集、alert 規則完整性。

**語法**

```bash
da-tools diagnose <tenant> [options]
```

**必需參數**

| 參數 | 說明 | 範例 |
|------|------|------|
| `<tenant>` | Tenant ID | `db-a` |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir <PATH>` | 租戶配置目錄（用於查詢 profile 資訊） | `./conf.d` |
| `--namespace <NS>` | K8s namespace（用於查詢 ConfigMap） | `monitoring` |

**輸出**

JSON 格式健康檢查報告。

```json
{
  "status": "healthy",
  "tenant": "db-a",
  "profile": "standard-mariadb",
  "checks": {
    "config": "ok",
    "metrics": "ok",
    "alerts": "ok"
  },
  "details": {
    "config_source": "threshold-config ConfigMap",
    "metric_count": 42,
    "alert_count": 18
  }
}
```

**範例**

```bash
da-tools diagnose db-a --config-dir ./conf.d
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 健康（所有檢查通過） |
| `1` | 一項或多項檢查失敗 |
| `2` | 參數錯誤或連線失敗 |

---

#### batch-diagnose

對所有 tenant 執行並行健康檢查。

**用途**：遷移完成後的定期健檢；快速掃描整個平台狀態。

**語法**

```bash
da-tools batch-diagnose [options]
```

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--tenants <LIST>` | 逗號分隔租戶列表（若不指定則自動探索） | （自動） |
| `--workers <N>` | 並行診斷執行緒數 | `5` |
| `--timeout <SEC>` | 單一 diagnose 超時時間（秒） | `30` |
| `--output <FILE>` | 輸出至檔案（JSON 格式） | stdout |
| `--dry-run` | 僅列出租戶，不執行檢查 | false |
| `--namespace <NS>` | K8s namespace（auto-discover 用） | `monitoring` |

**輸出**

JSON 格式統一報告，包含所有租戶的檢查結果摘要。

**範例**

```bash
da-tools batch-diagnose --workers 10
da-tools batch-diagnose --tenants db-a,db-b,db-c --output /tmp/report.json
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 所有租戶健康 |
| `1` | 一項或多項租戶檢查失敗 |

---

#### baseline

觀測指標時間序列，計算統計摘要（p50/p90/p95/p99/max），產出閾值建議。

**用途**：新增 DB 實例時取得合理初始閾值；負載測試後決定閾值調整。

**語法**

```bash
da-tools baseline --tenant <name> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--tenant <NAME>` | Tenant ID |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--duration <SEC>` | 觀測時長（秒） | `300` |
| `--interval <SEC>` | 採樣間隔（秒） | `30` |
| `--metrics <LIST>` | 逗號分隔指標清單（空=全部） | （全部） |
| `--output <FILE>` | 輸出至 CSV 檔案 | stdout |
| `--dry-run` | 僅顯示要觀測的指標，不實際採樣 | false |

**輸出**

CSV 格式，各行為一個指標的統計摘要（包含 p50、p90、p95、p99、max、建議閾值）。

**範例**

```bash
da-tools baseline --tenant db-a --duration 1800 --interval 30 --output /tmp/baseline.csv
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | Prometheus 連線或查詢失敗 |

---

#### validate

Shadow Monitoring 驗證工具：比對新舊 Recording Rule 數值，偵測自動收斂。

**用途**：遷移階段持續監控新舊規則行為等價性；確認何時可安全切換。

**語法**

```bash
da-tools validate [--mapping <file> | --old <query> --new <query>] [options]
```

**必需參數**

選擇一種模式：

1. **Mapping 模式**：`--mapping <file>`
   mapping.csv 格式：
   ```
   old_rule,new_rule
   mysql_connections,tenant:custom_mysql_connections:max
   mysql_replication_lag,tenant:custom_mysql_replication_lag:max
   ```

2. **Query 模式**：`--old <query> --new <query>`
   直接指定兩組 PromQL

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--watch` | 持續監控模式（每 N 秒比對一次） | false |
| `--interval <SEC>` | 監控間隔（秒） | `60` |
| `--rounds <N>` | 監控輪數（0 = 無限） | `0` |
| `--tolerance <PCT>` | 容許誤差百分比 | `5` |
| `--auto-detect-convergence` | 自動偵測收斂並產出 readiness JSON | false |
| `--output <FILE>` | 輸出至 CSV 或 JSON 檔案 | stdout |

**輸出**

CSV 格式，各行為一個 rule 的比對結果（舊值、新值、差異百分比、收斂狀態）。

若使用 `--auto-detect-convergence`，額外產出 `cutover-readiness.json` 供 `cutover` 命令使用。

**範例**

```bash
da-tools validate --mapping mapping.csv
da-tools validate --mapping mapping.csv --watch --interval 60 --rounds 1440
da-tools validate --mapping mapping.csv --auto-detect-convergence --output validation-report.csv
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功（任何收斂狀態） |
| `1` | Prometheus 連線或查詢失敗 |

---

#### cutover

Shadow Monitoring 一鍵切換：停止舊規則、啟用新規則、驗證健康。

**用途**：遷移最後一步，自動化完整切換流程。

**語法**

```bash
da-tools cutover --tenant <name> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--tenant <NAME>` | Tenant ID |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--readiness-json <FILE>` | validate --auto-detect-convergence 產出的 JSON | （可選） |
| `--dry-run` | 預覽切換步驟，不做任何變更 | false |
| `--force` | 跳過 readiness 檢查，直接執行 | false |
| `--namespace <NS>` | K8s namespace | `monitoring` |

**自動化步驟**

1. 驗證 readiness（若有提供）
2. 停止 Shadow Monitor Job
3. 移除舊 Recording Rules
4. 移除 `migration_status: shadow` label
5. 移除 Alertmanager shadow route
6. 執行 `check-alert` + `diagnose` 驗證

**範例**

```bash
da-tools cutover --tenant db-a --dry-run
da-tools cutover --tenant db-a --readiness-json cutover-readiness.json
da-tools cutover --tenant db-a --force
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 切換成功 |
| `1` | Readiness 檢查失敗 |
| `2` | 切換過程中發生錯誤 |

---

#### blind-spot

掃描 Prometheus 叢集的活躍 targets，與 tenant 配置交叉比對，找出盲區（有 exporter 但無對應 tenant 配置）。

**用途**：遷移完成後的定期健檢；確認新增 exporter 已被納管。

**語法**

```bash
da-tools blind-spot --config-dir <path> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--config-dir <PATH>` | 租戶配置目錄 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--exclude-jobs <LIST>` | 排除的 job 清單（逗號分隔） | （無） |
| `--json-output` | JSON 結構化輸出 | false |

**輸出**

以下列三部分呈現：
- **Covered**：有對應 tenant 配置的 exporter
- **Blind Spots**：有 exporter 但無 tenant 配置
- **Unrecognized**：無法推斷 DB 類型的 job

**範例**

```bash
da-tools blind-spot --config-dir ./conf.d
da-tools blind-spot --config-dir ./conf.d --exclude-jobs node-exporter,kube-state-metrics
da-tools blind-spot --config-dir ./conf.d --json-output
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功（無論是否有盲區） |
| `1` | Prometheus 連線失敗 |

---

#### maintenance-scheduler

評估排程式維護窗口（`_state_maintenance.recurring[]` 中的 cron 表達式），自動產出 Alertmanager silence YAML。

**用途**：自動化排程式維護窗口的 silence 建立；與 CronJob 配套。

**語法**

```bash
da-tools maintenance-scheduler --config-dir <path> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--config-dir <PATH>` | 租戶配置目錄 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--output <FILE>` | 輸出至 YAML 檔案 | stdout |
| `--timezone <TZ>` | 時區（IANA 格式） | `UTC` |
| `--dry-run` | 僅顯示要產出的 silence，不寫入 | false |

**輸出**

Alertmanager silence YAML（可直接餵入 Alertmanager API 或 kubectl apply）。

**範例**

```bash
da-tools maintenance-scheduler --config-dir ./conf.d --dry-run
da-tools maintenance-scheduler --config-dir ./conf.d --timezone Asia/Taipei -o silences.yaml
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | 配置目錄無效 |

---

#### backtest

執行 PR 中 threshold 變更的歷史回測。

**用途**：驗證閾值調整的影響；評估 PR 對告警的預期影響。

**語法**

```bash
da-tools backtest [--git-diff | --config-dir <dir> --baseline <dir>] [options]
```

**必需參數**

選擇一種模式：

1. **Git Diff 模式**：`--git-diff`
   （在 Git repo 內執行，自動偵測變更）

2. **目錄比對模式**：`--config-dir <dir> --baseline <dir>`
   （比對兩個配置版本）

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--lookback <DAYS>` | 歷史回測天數 | `7` |
| `--output <FILE>` | 輸出至 JSON 或 CSV | stdout |

**輸出**

對比報告，顯示各項 threshold 變更在歷史數據上的影響（可能增加/減少的 alert）。

**範例**

```bash
da-tools backtest --git-diff --lookback 7
da-tools backtest --config-dir ./conf.d-new --baseline ./conf.d-old --lookback 7
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | Prometheus 連線或 Git 操作失敗 |

---

#### shadow-verify

Shadow Monitoring 就緒度與收斂性三階段驗證。

**用途**：啟動 Shadow Monitoring 前的 preflight 檢查、運行中的 runtime 健檢、切換前的 convergence 評估。

**語法**

```bash
da-tools shadow-verify <phase> [options]
```

**必需參數**

| 參數 | 說明 | 可選值 |
|------|------|--------|
| `<phase>` | 驗證階段 | `preflight` / `runtime` / `convergence` / `all` |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--mapping <FILE>` | prefix-mapping.yaml 路徑（preflight 用） | （無） |
| `--report-csv <FILE>` | validation-report.csv 路徑（runtime/convergence 用） | （無） |
| `--readiness-json <FILE>` | cutover-readiness.json 路徑（convergence 用） | （無） |
| `--prometheus <URL>` | Prometheus Query API URL | `http://localhost:9090` |
| `--alertmanager <URL>` | Alertmanager API URL | `http://localhost:9093` |
| `--json` | JSON 結構化輸出（CI 用） | false |

**三階段檢查內容**

| 階段 | 檢查項目 |
|------|----------|
| `preflight` | Mapping 檔案存在、Recording rules loaded、AM interception route |
| `runtime` | Mismatch 計數、tenant 覆蓋率、三態模式一致性 |
| `convergence` | cutover-readiness 評估、7 天 zero-mismatch 檢查 |

**範例**

```bash
da-tools shadow-verify preflight --mapping migration_output/prefix-mapping.yaml
da-tools shadow-verify runtime --report-csv validation_output/validation-report.csv
da-tools shadow-verify convergence --report-csv validation_output/validation-report.csv --readiness-json validation_output/cutover-readiness.json
da-tools shadow-verify all --mapping mapping.yaml --report-csv report.csv --json
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 所有檢查通過 |
| `1` | 一項或多項檢查失敗 |

---

#### byo-check

自動化 BYO Prometheus & Alertmanager 整合驗證（取代手動 curl + jq 步驟）。

**用途**：驗證 BYO 環境的 tenant label injection、threshold-exporter scrape、Rule Pack 載入、Alertmanager 路由配置。

**語法**

```bash
da-tools byo-check <target> [options]
```

**必需參數**

| 參數 | 說明 | 可選值 |
|------|------|--------|
| `<target>` | 驗證目標 | `prometheus` / `alertmanager` / `all` |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--prometheus <URL>` | Prometheus Query API URL | `http://localhost:9090` |
| `--alertmanager <URL>` | Alertmanager API URL | `http://localhost:9093` |
| `--json` | JSON 結構化輸出（CI 用） | false |

**檢查項目**

| Target | 檢查 |
|--------|------|
| `prometheus` | 連線健康、tenant label injection（Step 1）、threshold-exporter scrape（Step 2）、Rule Pack 載入（Step 3）、Recording rules 產出、vector matching |
| `alertmanager` | 連線就緒、tenant routing、inhibit_rules、active alerts、silences |

**範例**

```bash
da-tools byo-check prometheus --prometheus http://prometheus:9090
da-tools byo-check alertmanager --alertmanager http://alertmanager:9093
da-tools byo-check all --json
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 所有檢查通過 |
| `1` | 一項或多項檢查失敗 |

---

#### federation-check

多叢集 Federation 整合驗證（自動化 federation-integration.md §6 手動步驟）。

**用途**：驗證邊緣叢集 external_labels 與 federate endpoint、中央叢集 edge metrics 接收與 Recording rules、端到端跨叢集 alert 狀態。

**語法**

```bash
da-tools federation-check <target> [options]
```

**必需參數**

| 參數 | 說明 | 可選值 |
|------|------|--------|
| `<target>` | 驗證模式 | `edge` / `central` / `e2e` |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--prometheus <URL>` | Prometheus URL（central 用於 e2e，或 edge/central 各自的端點） | `http://localhost:9090` |
| `--edge-urls <URLS>` | 逗號分隔的邊緣 Prometheus URLs（e2e 模式必需） | （無） |
| `--json` | JSON 結構化輸出（CI 用） | false |

**三模式檢查內容**

| 模式 | 檢查項目 |
|------|----------|
| `edge` | Prometheus 健康、external_labels（含 cluster label）、tenant label、federate endpoint |
| `central` | Prometheus 健康、edge metrics 接收、threshold-exporter、Recording rules、Alert rules |
| `e2e` | 全部 edge 檢查 + central 檢查 + cross-cluster vector matching |

**範例**

```bash
da-tools federation-check edge --prometheus http://edge-prometheus:9090
da-tools federation-check central --prometheus http://central-prometheus:9090
da-tools federation-check e2e --prometheus http://central:9090 --edge-urls http://edge-1:9090,http://edge-2:9090
da-tools federation-check central --prometheus http://central:9090 --json
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 所有檢查通過 |
| `1` | 一項或多項檢查失敗 |

---

#### grafana-import

Grafana Dashboard 匯入工具（透過 ConfigMap sidecar 自動掛載）。

**用途**：自動化 Grafana dashboard JSON → Kubernetes ConfigMap → sidecar 發現的完整流程。

**語法**

```bash
da-tools grafana-import [options]
```

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--dashboard <FILE>` | Dashboard JSON 檔案路徑 | （無） |
| `--dashboard-dir <DIR>` | 匯入目錄下所有 *.json 檔案 | （無） |
| `--name <NAME>` | ConfigMap 名稱（省略則自動產生） | （自動） |
| `--namespace <NS>` | Kubernetes namespace | `monitoring` |
| `--verify` | 驗證已匯入的 Dashboard ConfigMaps | false |
| `--dry-run` | 預覽 kubectl 命令，不實際執行 | false |
| `--json` | JSON 結構化輸出 | false |

**模式**

| 模式 | 說明 |
|------|------|
| 單檔匯入 | `--dashboard <file>` 匯入單一 dashboard |
| 批次匯入 | `--dashboard-dir <dir>` 匯入目錄下所有 JSON |
| 驗證模式 | `--verify` 檢查已存在的 dashboard ConfigMaps |

**範例**

```bash
da-tools grafana-import --dashboard k8s/03-monitoring/dynamic-alerting-overview.json --namespace monitoring
da-tools grafana-import --dashboard-dir k8s/03-monitoring/ --namespace monitoring
da-tools grafana-import --verify --namespace monitoring
da-tools grafana-import --dashboard overview.json --dry-run
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | 匯入失敗或驗證發現問題 |

---

#### alert-quality

分析 Alertmanager 歷史記錄，識別問題告警。4 項品質指標（Noise / Stale / Latency / Suppression）、三級評分（GOOD / WARN / BAD）、per-tenant 加權分數。

**用法**

```bash
da-tools alert-quality --prometheus <URL> [--alertmanager <URL>] [--period <DURATION>] [--tenant <NAME>] [--json] [--markdown] [--ci] [--min-score <N>]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--prometheus` | Prometheus URL（必填） | - |
| `--alertmanager` | Alertmanager URL（用於 suppression 資料） | - |
| `--period` | 分析期間 | `30d` |
| `--tenant` | 篩選特定 tenant | 全部 |
| `--json` | JSON 輸出 | - |
| `--markdown` | Markdown 輸出 | - |
| `--ci` | CI 模式：任何 BAD 告警時 exit 1 | - |
| `--min-score` | CI 最低分數閾值 | `0` |

**範例**

```bash
# 基本品質報告
da-tools alert-quality --prometheus http://prometheus:9090

# 特定 tenant，Markdown 輸出
da-tools alert-quality --prometheus http://prometheus:9090 --tenant db-a --markdown

# CI gate（低於 60 分 fail）
da-tools alert-quality --prometheus http://prometheus:9090 --ci --min-score 60
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功（CI 模式：所有告警品質達標） |
| `1` | CI 模式：有 BAD 告警或分數低於閾值 |

---

#### alert-correlate

分析 Alertmanager 告警並進行時間窗口聚類，計算關聯分數並推斷根因。支援線上（Prometheus API）和離線（JSON 檔案）兩種模式。

**用法**

```bash
da-tools alert-correlate --prometheus <URL> [--window <MINUTES>] [--lookback <DURATION>] [--min-score <FLOAT>] [--json] [--markdown] [--ci]
da-tools alert-correlate --input <FILE> [--window <MINUTES>] [--min-score <FLOAT>] [--json]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--prometheus <URL>` | Prometheus 端點（線上模式） | `$PROMETHEUS_URL` |
| `--input <FILE>` | Alertmanager JSON 檔案（離線模式） | — |
| `--window <MINUTES>` | 時間窗口大小（分鐘） | `10` |
| `--lookback <DURATION>` | 回溯時間範圍 | `1h` |
| `--min-score <FLOAT>` | 最低關聯分數閾值 | `0.3` |
| `--json` | JSON 輸出 | — |
| `--markdown` | Markdown 報告輸出 | — |
| `--ci` | CI 模式（有 critical 群組時 exit 1） | — |

**範例**

```bash
# 基本用法 — 查詢 Prometheus 當前告警
da-tools alert-correlate --prometheus http://prometheus:9090

# 離線分析 JSON 檔案
da-tools alert-correlate --input alerts.json --window 15

# CI gate — 有 critical 告警群組時失敗
da-tools alert-correlate --prometheus http://prometheus:9090 --ci
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功（CI 模式：無 critical 告警群組） |
| `1` | CI 模式：存在 critical 嚴重度的告警群組 |

---

#### drift-detect

比對多個 config-dir 目錄（來自不同叢集或 GitOps 分支），偵測意外的配置漂移。使用 SHA-256 manifest 進行目錄級比對。

**用法**

```bash
da-tools drift-detect --dirs <DIR1>,<DIR2>[,<DIR3>...] [--labels <L1>,<L2>,...] [--ignore-prefix <PREFIX>] [--json] [--markdown] [--ci]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--dirs <LIST>` | 以逗號分隔的配置目錄（至少 2 個） | — |
| `--labels <LIST>` | 對應每個目錄的標籤 | `dir-1,dir-2,...` |
| `--ignore-prefix <PREFIX>` | 視為預期漂移的檔案前綴 | `_cluster_,_local_` |
| `--json` | JSON 輸出 | — |
| `--markdown` | Markdown 報告輸出 | — |
| `--ci` | CI 模式（有非預期漂移時 exit 1） | — |

**範例**

```bash
# 比對兩個叢集的配置
da-tools drift-detect --dirs cluster-a/conf.d,cluster-b/conf.d --labels prod-a,prod-b

# 三叢集 pairwise 比對，JSON 輸出
da-tools drift-detect --dirs a/conf.d,b/conf.d,c/conf.d --json

# CI gate — 有非預期漂移時失敗
da-tools drift-detect --dirs staging/conf.d,prod/conf.d --ci
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 無非預期漂移 |
| `1` | CI 模式：偵測到非預期漂移 |

---

#### cardinality-forecast

分析 per-tenant 時序基數增長趨勢，預測何時觸及上限。使用純 Python 線性回歸（無 numpy 依賴）。

**用法**

```bash
da-tools cardinality-forecast --prometheus <URL> [--lookback <DURATION>] [--limit <N>] [--warn-days <N>] [--tenant <NAME>] [--json] [--markdown] [--ci]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--prometheus` | Prometheus URL（必填） | - |
| `--lookback` | 回溯期間 | `30d` |
| `--limit` | 基數上限 | `500` |
| `--warn-days` | 預警天數 | `7` |
| `--tenant` | 篩選特定 tenant | 全部 |
| `--json` | JSON 輸出 | - |
| `--markdown` | Markdown 輸出 | - |
| `--ci` | CI 模式：有 critical 風險時 exit 1 | - |

**範例**

```bash
# 基本預測報告
da-tools cardinality-forecast --prometheus http://prometheus:9090

# 自訂上限與預警天數
da-tools cardinality-forecast --prometheus http://prometheus:9090 --limit 1000 --warn-days 14

# CI gate
da-tools cardinality-forecast --prometheus http://prometheus:9090 --ci
```

**風險等級**

| 等級 | 條件 |
|------|------|
| `critical` | 預測在 `--warn-days` 天內觸頂 |
| `warning` | 趨勢為 growing 但尚未觸及預警 |
| `safe` | 趨勢穩定或下降 |

#### config-history

配置快照與歷史追蹤——在 `.da-history/` 中記錄 conf.d/ 的每次變更，提供 git-independent 的輕量級版本控制。

```bash
da-tools config-history --config-dir <PATH> <action>
```

**子命令**

| 子命令 | 用途 | 參數 |
|--------|------|------|
| `snapshot` | 建立配置快照 | `-m <message>`（選填） |
| `log` | 顯示快照歷史 | `--limit N`（選填） |
| `show` | 顯示快照詳情 | `<id>` |
| `diff` | 比較兩個快照 | `<id_a> <id_b>` |

**範例**

```bash
# 建立快照
da-tools config-history --config-dir conf.d/ snapshot -m "調整 MariaDB 閾值"

# 查看歷史
da-tools config-history --config-dir conf.d/ log --limit 5

# 比較快照 1 和 2
da-tools config-history --config-dir conf.d/ diff 1 2
```

---

### 採用與初始化

#### init

在客戶 repo 中初始化 Dynamic Alerting 整合骨架。產生 CI/CD pipeline、conf.d/ 目錄、Kustomize overlays、pre-commit 配置。

```bash
da-tools init [--ci <github|gitlab|both>] [--tenants <list>] [--rule-packs <list>] [--deploy <kustomize|helm|argocd>] [-o <dir>] [--non-interactive] [--dry-run]
```

**參數**

| 參數 | 說明 | 預設 |
|------|------|------|
| `--ci` | CI/CD 平台 | `both` |
| `--tenants` | 逗號分隔的租戶名稱 | `db-a,db-b`（互動模式） |
| `--rule-packs` | 逗號分隔的 Rule Pack | `mariadb,kubernetes`（互動模式） |
| `--deploy` | 部署方式 | `kustomize` |
| `--non-interactive` | 跳過互動提示（需搭配 `--tenants`） | — |
| `--dry-run` | 顯示會產生的檔案但不寫入 | — |
| `--force` | 覆寫既有的 `.da-init.yaml` | — |

**範例**

```bash
# 互動模式
da-tools init

# 非互動模式
da-tools init --ci github --tenants prod-db,staging-db --rule-packs mariadb,redis,kubernetes --non-interactive

# Dry-run
da-tools init --ci both --tenants db-a --dry-run
```

#### gitops-check

GitOps Native Mode 就緒度驗證——檢查 Git 倉庫可達性、本地配置結構、git-sync sidecar 部署狀態。

```bash
da-tools gitops-check <subcommand> [options]
```

**子命令**

| 子命令 | 用途 | 參數 |
|--------|------|------|
| `repo` | 驗證 Git 倉庫可達性與分支存在 | `--url <git-url> [--branch main]` |
| `local` | 驗證本地 clone 的 conf.d/ 結構 | `--dir <path>` |
| `sidecar` | 檢查 K8s git-sync sidecar 部署就緒度 | `[--namespace monitoring]` |

**範例**

```bash
# 驗證 Git 倉庫
da-tools gitops-check repo --url git@github.com:example/configs.git

# 驗證本地配置結構
da-tools gitops-check local --dir /data/config/conf.d

# 檢查 sidecar 部署
da-tools gitops-check sidecar --namespace monitoring --json
```

---

### Operator + Federation 工具

#### operator-generate

從 Rule Packs 與 Tenant 配置產出 Kubernetes Operator CRD（PrometheusRule、AlertmanagerConfig、ServiceMonitor）。

**用途**：Prometheus Operator 叢集中的動態告警規則與路由部署；Federation 場景多叢集配置管理。

**語法**

```bash
da-tools operator-generate --rule-packs-dir <dir> --config-dir <dir> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--rule-packs-dir <DIR>` | Rule Pack 目錄路徑 |
| `--config-dir <DIR>` | 租戶配置目錄路徑 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--namespace <NS>` | 目標 K8s namespace | `monitoring` |
| `--output <FILE>` | 輸出至檔案 | stdout |
| `--split` | 產出個別 CRD 檔案（按 Rule Pack 分離） | false |
| `--include-servicemonitor` | 並產出 ServiceMonitor CRD | false |
| `--dry-run` | 僅輸出預覽 | false |
| `--apply` | 直接套用至 Kubernetes | false |

**範例**

```bash
# 輸出 CRD YAML 到檔案
da-tools operator-generate --rule-packs-dir rule-packs/ --config-dir conf.d/ -o crds.yaml

# 分割產出個別檔案並直接應用
da-tools operator-generate --rule-packs-dir rule-packs/ --config-dir conf.d/ --split --apply --namespace monitoring
```

---

#### operator-check

驗證 Prometheus Operator 叢集中 CRD 部署狀態，檢查 5 項指標並產生診斷報告。

**用途**：Operator 整合健康檢查；部署完整性驗證；故障診斷。

**語法**

```bash
da-tools operator-check [options]
```

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--namespace <NS>` | K8s namespace 探索 | `monitoring`（自動探索） |
| `--json` | JSON 格式輸出 | false |

**檢查項目**

1. PrometheusRule 是否部署
2. AlertmanagerConfig 是否部署
3. ServiceMonitor 是否綁定
4. Prometheus 是否掃描
5. 告警是否正常觸發

**範例**

```bash
# 檢查 monitoring namespace
da-tools operator-check --namespace monitoring

# JSON 格式輸出（CI gate 用）
da-tools operator-check --json
```

---

#### rule-pack-split

將 Rule Pack 分層拆分為 edge（Part 1）和 central（Parts 2+3），支援 Federation Scenario B。

**用途**：多叢集 Federation 場景；邊端（edge）與中央（central）分離部署。

**語法**

```bash
da-tools rule-pack-split --rule-packs-dir <dir> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--rule-packs-dir <DIR>` | Rule Pack 目錄路徑 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--output-dir <DIR>` | 輸出目錄 | `./split-output` |
| `--scenario` | Federation 場景（A / B） | `B` |

**輸出結構**

```
split-output/
├── edge/           (Part 1 - 邊端)
│   └── part-1-*.yaml
├── central/        (Parts 2+3 - 中央)
│   ├── part-2-*.yaml
│   └── part-3-*.yaml
└── mapping.json    (edge → central 映射表)
```

**範例**

```bash
# Scenario B 分層拆分
da-tools rule-pack-split --rule-packs-dir rule-packs/ --scenario B --output-dir federation-split/
```

---

### 配置生成工具

#### generate-routes

從 tenant YAML 產出 Alertmanager route + receiver + inhibit_rules fragment（或完整 ConfigMap）。

**用途**：GitOps 配置管理；自動產生告警路由與通知接收器。

**語法**

```bash
da-tools generate-routes --config-dir <path> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--config-dir <PATH>` | 租戶配置目錄 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--output <FILE>` | 輸出至檔案 | stdout |
| `--output-configmap` | 產出完整 Kubernetes ConfigMap YAML | false |
| `--base-config <FILE>` | 自訂 Alertmanager 基礎配置（--output-configmap 時用） | 內建預設 |
| `--dry-run` | 僅輸出預覽，不寫入檔案 | false |
| `--validate` | 僅驗證，不輸出 | false |
| `--apply` | 直接套用至 Kubernetes（需 kubectl） | false |
| `--yes` | 搭配 --apply 跳過確認提示 | false |
| `--policy <DOMAINS>` | webhook 域名白名單（逗號分隔；空=無限制） | （無限制） |

**輸出**

**Fragment 模式** (`--output-configmap` 未指定)：
YAML 片段，包含 route、receivers、inhibit_rules。

**ConfigMap 模式** (`--output-configmap`)：
完整 Kubernetes ConfigMap YAML，含 global、route、receivers、inhibit_rules，可直接 `kubectl apply`。

**範例**

```bash
da-tools generate-routes --config-dir ./conf.d --dry-run
da-tools generate-routes --config-dir ./conf.d -o alertmanager-routes.yaml
da-tools generate-routes --config-dir ./conf.d --output-configmap -o alertmanager-configmap.yaml
da-tools generate-routes --config-dir ./conf.d --apply --yes
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | 配置驗證失敗 |
| `2` | kubectl 操作失敗（--apply 模式） |

---

#### patch-config

ConfigMap 局部更新工具，支援 preview（--diff）和直接應用。

**用途**：運維期間快速調整單一 metric 閾值；避免完整 ConfigMap 重新部署。

**語法**

```bash
da-tools patch-config [<tenant> <metric> <value> | --diff] [options]
```

**必需參數**

選擇一種模式：

1. **更新模式**：`<tenant> <metric> <value>`
2. **Preview 模式**：`--diff`

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--namespace <NS>` | K8s namespace | `monitoring` |
| `--configmap <CM>` | ConfigMap 名稱 | `threshold-config` |
| `--dry-run` | 僅顯示將應用的變更，不實際更新 | false |
| `--yes` | 跳過確認提示 | false |

**輸出**

Preview 或確認訊息。

**範例**

```bash
da-tools patch-config --diff
da-tools patch-config db-a mysql_connections 100 --dry-run
da-tools patch-config db-a mysql_connections 100 --yes
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | ConfigMap 或參數無效 |

---

### 檔案系統工具

#### scaffold

產生新 tenant 配置（互動式或非互動式）。

**用途**：快速建立 tenant 配置；支援多種 DB 類型與預設值。

**語法**

```bash
da-tools scaffold [options]
```

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--non-interactive` | 非互動式（需同時指定 --tenant 等） | false |
| `--tenant <NAME>` | Tenant ID | （互動詢問） |
| `--db <LIST>` | 逗號分隔 DB 類型清單 | （互動詢問） |
| `--namespaces <LIST>` | 逗號分隔 K8s namespace 清單 | （互動詢問） |
| `--output <DIR>` | 輸出目錄 | `./` |

**支援的 DB 類型**

- `mariadb` / `mysql`
- `postgresql`
- `redis`
- `mongodb`
- `elasticsearch`
- `kubernetes`
- `jvm`
- `nginx`

**輸出**

- `<tenant>.yaml` — Tenant 配置檔案
- `_defaults.yaml` — 平台預設值（首次建立時）
- `scaffold-report.txt` — 總結報告

**範例**

```bash
da-tools scaffold                                     # 互動式
da-tools scaffold --non-interactive --tenant db-c --db mariadb,redis
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | 輸入無效或 I/O 失敗 |

---

#### migrate

將傳統 Prometheus 規則轉換為動態格式（AST 引擎）。

**用途**：大規模規則遷移；自動化前期準備工作。

**語法**

```bash
da-tools migrate <input_file> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `<input_file>` | 輸入的傳統規則 YAML 檔案 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--output <DIR>` | 輸出目錄 | `./migration_output/` |
| `--dry-run` | 僅顯示報告，不產生檔案 | false |
| `--triage` | Triage 模式：只產出 CSV 分桶報告 | false |
| `--interactive` | 遇到不確定時詢問使用者 | false |
| `--no-prefix` | 停用 custom_ 前綴（不建議） | false |
| `--no-ast` | 強制使用舊版 regex 引擎 | false |

**輸出**

**標準模式**：

- `migration_output/tenant-config.yaml` — 提取出的 threshold
- `migration_output/platform-recording-rules.yaml` — Recording rules
- `migration_output/platform-alert-rules.yaml` — Alert rules
- `migration_output/migration-report.txt` — 詳細遷移報告
- `migration_output/triage-report.csv` — 需人工審閱的規則清單
- `migration_output/prefix-mapping.yaml` — Metric 前綴對應表

**Triage 模式**：

- 僅產出 `triage-report.csv`（用於人工審核）

**範例**

```bash
da-tools migrate ./my-rules.yml --dry-run
da-tools migrate ./my-rules.yml --triage
da-tools migrate ./my-rules.yml -o migration_output/
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | 輸入檔案無效或 I/O 失敗 |

---

#### validate-config

一站式配置驗證：YAML 格式、schema、routing、policy、版本一致性。

**用途**：CI/CD gate check；部署前驗證配置完整性。

**語法**

```bash
da-tools validate-config --config-dir <path> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--config-dir <PATH>` | 租戶配置目錄 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--policy <DOMAINS>` | webhook 域名白名單 | （無限制） |
| `--ci` | CI 模式（exit code 用於 CI/CD） | false |

**檢查項目**

- YAML 檔案格式（可解析）
- Schema 驗證（必需的 key、類型正確）
- 路由規則驗證（group_wait/group_interval/repeat_interval 在允許範圍）
- Policy 檢查（webhook 域名）
- Tenant 名稱一致性

**輸出**

驗證結果摘要（通過/失敗列表）。

**範例**

```bash
da-tools validate-config --config-dir ./conf.d
da-tools validate-config --config-dir ./conf.d --ci
da-tools validate-config --config-dir ./conf.d --policy "webhook.company.com,slack.com"
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 所有驗證通過 |
| `1` | 驗證失敗（一項或多項） |

---

#### offboard

下架 tenant 配置與相關資源。

**用途**：Tenant 生命週期結束時的清理。

**語法**

```bash
da-tools offboard <tenant> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `<tenant>` | Tenant ID |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir <PATH>` | 租戶配置目錄 | `./conf.d` |
| `--backup <DIR>` | 備份目錄 | `./offboarded/` |
| `--cleanup-rules` | 移除相關 Alert 規則 | false |
| `--dry-run` | 預覽將刪除的項目 | false |

**輸出**

備份 tenant 配置；可選地移除相關 Recording/Alert 規則。

**範例**

```bash
da-tools offboard db-old --dry-run
da-tools offboard db-old --backup ./backup/
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | Tenant 不存在或 I/O 失敗 |

---

#### deprecate

標記指標為 disabled，防止誤用。

**用途**：逐步淘汰舊指標；維護版本相容性。

**語法**

```bash
da-tools deprecate <metric_keys...> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `<metric_keys...>` | 一個或多個 metric key（空格分隔） |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir <PATH>` | 租戶配置目錄 | `./conf.d` |
| `--reason <TEXT>` | 棄用原因（註釋） | （無） |
| `--dry-run` | 預覽變更 | false |

**輸出**

在 _defaults.yaml 中新增或更新 metric key 的 `enabled: false` 標記。

**範例**

```bash
# 標記多個指標為 disabled
docker run --rm \
  -v $(pwd)/conf.d:/etc/config:rw \
  ghcr.io/vencil/da-tools:v2.3.0 \
  deprecate old_metric_1 old_metric_2 \
    --reason "Replaced by new_metric; migration complete"
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | 配置目錄無效 |

---

#### lint

檢查 Custom Rule 的治理合規性（根據 `custom_` 前綴規則）。

**用途**：CI/CD lint 檢查；確保 custom rule 符合命名規範。

**語法**

```bash
da-tools lint <path...> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `<path...>` | 一個或多個檔案或目錄路徑 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--strict` | Strict 模式：警告升級為錯誤 | false |
| `--json-output` | JSON 結構化輸出 | false |

**檢查項目**

- Metric 名稱是否以 `custom_` 開頭
- Recording rule 名稱格式
- Label 使用一致性

**範例**

```bash
da-tools lint ./my-custom-rules.yaml
da-tools lint ./rule-packs --strict
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 全部通過 |
| `1` | 發現違規（警告級） |
| `2` | 發現違規（錯誤級；--strict 模式） |

---

#### onboard

分析既有 Alertmanager 或 Prometheus 配置，產出遷移提示。

**用途**：引入現有監控配置；減少手動遷移工作量。

**語法**

```bash
da-tools onboard <config_file> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `<config_file>` | Alertmanager 或 Prometheus 配置檔案 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--alertmanager-config <FILE>` | Alertmanager 配置檔案（替代位置式參數） | （位置式） |
| `--output <FILE>` | 輸出提示 JSON | stdout |

**輸出**

JSON 格式的遷移提示（`onboard-hints.json`），包含：
- 偵測到的 receiver 類型和端點
- 建議的 tenant 分組
- 初始化閾值建議

**範例**

```bash
da-tools onboard ./alertmanager.yaml -o onboard-hints.json
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | 配置檔案無效 |

---

#### analyze-gaps

比對 custom rule 與 Rule Pack，找出重複/缺口。

**用途**：評估 Rule Pack 涵蓋度；決定是否可刪除 custom rule。

**語法**

```bash
da-tools analyze-gaps --config <path> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--config <PATH>` | 租戶配置檔案或目錄 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--output <FILE>` | 輸出至 CSV 或 JSON | stdout |
| `--json-output` | JSON 格式 | false |

**輸出**

CSV 列表，各行表示一條 custom rule 與對應 Rule Pack 的覆蓋關係。

**範例**

```bash
da-tools analyze-gaps --config ./conf.d/db-a.yaml
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | 配置檔案無效 |

---

#### config-diff

比較兩個配置目錄（conf.d），產出 blast radius 報告。

**用途**：GitOps PR review；快速評估配置變更影響範圍。

**語法**

```bash
da-tools config-diff --old-dir <path> --new-dir <path> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--old-dir <PATH>` | 舊配置目錄 |
| `--new-dir <PATH>` | 新配置目錄 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--json-output` | JSON 結構化輸出 | false |
| `--summary-only` | 僅輸出摘要，不詳列各個變更 | false |

**變更分類**

| 分類 | 含義 | 影響 |
|------|------|------|
| `tighter` | 閾值下降 | 可能增加告警 |
| `looser` | 閾值上升 | 可能減少告警 |
| `added` | 新增 metric key | 新增 alert 覆蓋 |
| `removed` | 移除 metric key | 失去 alert 覆蓋 |
| `toggled` | enable ↔ disable | 開啟或關閉 alert |
| `modified` | 複雜值變更 | 需人工審閱 |

**輸出**

Markdown 格式報告，含 per-tenant 變更表格與摘要統計。

**範例**

```bash
da-tools config-diff --old-dir ./conf.d-old --new-dir ./conf.d-new
da-tools config-diff --old-dir ./conf.d-old --new-dir ./conf.d-new --json-output
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | 目錄無效 |

---

#### evaluate-policy

宣告式策略引擎 — 使用內建 DSL 評估 tenant 配置合規性，零外部依賴。

**用法**

```bash
da-tools evaluate-policy --config-dir <PATH> [--policy <FILE>] [--json] [--ci]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir` | conf.d/ 目錄路徑（必填） | - |
| `--policy` | 獨立策略檔路徑（頂層 `policies:` key） | `_defaults.yaml` 中的 `_policies` |
| `--json` | JSON 輸出 | - |
| `--ci` | CI 模式：有 error 違規時 exit 1 | - |

**支援的運算子**

`required`、`forbidden`、`equals`、`not_equals`、`gte`、`lte`、`gt`、`lt`、`matches`、`one_of`、`contains`

**範例**

```bash
# 評估預設策略
da-tools evaluate-policy --config-dir conf.d/

# 使用獨立策略檔
da-tools evaluate-policy --config-dir conf.d/ --policy policies/production.yaml

# CI gate
da-tools evaluate-policy --config-dir conf.d/ --ci
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 無 error 違規 |
| `1` | CI 模式：有 error 級別違規 |

#### opa-evaluate

OPA (Open Policy Agent) 策略評估橋接 — 將 tenant 配置轉為 OPA input JSON，透過 OPA REST API 或本地二進位檔評估，回傳與 evaluate-policy 相容的結果格式。

**用法**

```bash
da-tools opa-evaluate --config-dir <PATH> [options]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir` | conf.d/ 目錄路徑（必填） | - |
| `--opa-url` | OPA REST API 端點 | - |
| `--opa-binary` | 本地 OPA 二進位檔路徑 | `opa` |
| `--policy-path` | .rego 策略檔路徑 | - |
| `--dry-run` | 僅顯示 input JSON，不呼叫 OPA | - |
| `--json` | JSON 格式輸出 | - |

**範例**

```bash
# 透過 OPA REST API 評估
da-tools opa-evaluate --config-dir conf.d/ --opa-url http://localhost:8181

# 使用本地 OPA 二進位檔
da-tools opa-evaluate --config-dir conf.d/ --opa-binary /usr/local/bin/opa --policy-path policies/

# Dry-run：僅顯示 OPA input JSON
da-tools opa-evaluate --config-dir conf.d/ --dry-run
```

---

#### threshold-recommend

閾值推薦引擎 — 根據 Prometheus 歷史 P50/P95/P99 百分位數推薦最佳閾值，整合 Noise Score 調整推薦方向。

**用法**

```bash
da-tools threshold-recommend --config-dir <PATH> [--prometheus <URL>] [--tenant <NAME>] [--lookback <DURATION>] [--min-samples <N>] [--dry-run] [--json] [--markdown]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir` | conf.d/ 目錄路徑（必填） | - |
| `--prometheus` | Prometheus Query API URL | `$PROMETHEUS_URL` 或 `http://localhost:9090` |
| `--tenant` | 只分析指定租戶（省略則分析全部） | 全部 |
| `--lookback` | 歷史資料回溯期間 | `7d` |
| `--min-samples` | 最低樣本數門檻（不足時降低信心等級） | `100` |
| `--dry-run` | 僅顯示 PromQL 查詢，不實際執行 | - |
| `--json` | JSON 輸出 | - |
| `--markdown` | Markdown 表格輸出 | - |

**信心等級**

| 等級 | 樣本數 |
|------|--------|
| HIGH | ≥ 1000 |
| MEDIUM | ≥ 100（或 `--min-samples`） |
| LOW | < 100 |

**範例**

```bash
# 推薦所有租戶閾值
da-tools threshold-recommend --config-dir conf.d/ --prometheus http://prometheus:9090

# 指定租戶，14 天回溯
da-tools threshold-recommend --config-dir conf.d/ --prometheus http://prometheus:9090 --tenant db-a --lookback 14d

# 乾跑：只顯示 PromQL
da-tools threshold-recommend --config-dir conf.d/ --dry-run

# JSON 輸出
da-tools threshold-recommend --config-dir conf.d/ --prometheus http://prometheus:9090 --json
```

#### test-notification

多通道通知連通性測試 — 驗證所有已配置 receiver 的可達性，報告連通性狀態。

**用法**

```bash
da-tools test-notification --config-dir <PATH> [--tenant <NAME>] [--dry-run] [--json] [--ci] [--timeout <SEC>] [--rate-limit <SEC>]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir` | conf.d/ 目錄路徑（必填） | - |
| `--tenant` | 只測試指定租戶（省略則測試全部） | 全部 |
| `--dry-run` | 僅驗證 URL 格式，不實際發送 | - |
| `--json` | JSON 輸出 | - |
| `--ci` | CI 模式：任一 receiver 失敗時 exit 1 | - |
| `--timeout` | 每個 receiver 的連線逾時秒數 | `10` |
| `--rate-limit` | 每次測試之間的等待秒數 | `0.5` |

**支援的 Receiver 類型**

`webhook`、`slack`、`teams`、`pagerduty`、`rocketchat`、`email`（SMTP 連通性檢查）

**範例**

```bash
# 測試所有租戶的 receiver
da-tools test-notification --config-dir conf.d/

# 只測試特定租戶
da-tools test-notification --config-dir conf.d/ --tenant db-a

# 乾跑模式（僅驗證 URL 格式）
da-tools test-notification --config-dir conf.d/ --dry-run

# CI gate
da-tools test-notification --config-dir conf.d/ --ci
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 所有 receiver 連通正常（或非 CI 模式） |
| `1` | CI 模式：任一 receiver 連通失敗 |

#### explain-route

路由合併管線除錯器 — 顯示每個 tenant 的四層路由合併展開（ADR-007），包括 `_routing_defaults` → `routing_profiles` → tenant `_routing` → `_routing_enforced`。

**用法**

```bash
da-tools explain-route --config-dir <PATH> [--tenant <NAME>...] [--show-profile-expansion] [--json]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir` | 設定目錄路徑 | (必填) |
| `--tenant` | 只顯示指定 tenant（可多次指定） | (全部) |
| `--show-profile-expansion` | 顯示所有路由設定檔的展開與引用關係 | `false` |
| `--json` | 以 JSON 格式輸出 | `false` |

**範例**

```bash
# 顯示所有 tenant 的路由合併展開
da-tools explain-route --config-dir conf.d/

# 只看特定 tenant
da-tools explain-route --config-dir conf.d/ --tenant db-a

# 顯示設定檔引用關係（哪些 profile 被誰引用）
da-tools explain-route --config-dir conf.d/ --show-profile-expansion

# JSON 輸出（適合管線整合）
da-tools explain-route --config-dir conf.d/ --json
```

---

#### discover-mappings

自動發現 1:N 實例-租戶映射 — 掃描 exporter `/metrics` 端點或查詢 Prometheus API，解析 partition label 候選值（schema、tablespace、datname 等），依適用性排名後產生 `_instance_mapping.yaml` 草稿（ADR-006）。

**用法**

```bash
da-tools discover-mappings --endpoint <URL> [-o <FILE>] [--json]
da-tools discover-mappings --prometheus <URL> --instance <INST> [--job <JOB>] [-o <FILE>] [--json]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--endpoint` | 直接掃描的 exporter /metrics URL | (與 --prometheus 二擇一) |
| `--prometheus` | Prometheus API URL | (與 --endpoint 二擇一) |
| `--instance` | Prometheus 中的 instance 標籤 | (搭配 --prometheus 使用) |
| `--job` | Prometheus 中的 job 標籤（縮小查詢範圍） | (選填) |
| `-o`, `--output` | 輸出檔案路徑（預設 stdout） | stdout |
| `--json` | 以 JSON 格式輸出 | `false` |

**範例**

```bash
# 直接掃描 exporter
da-tools discover-mappings --endpoint http://mariadb-exporter:9104/metrics

# 透過 Prometheus API 查詢
da-tools discover-mappings --prometheus http://prometheus:9090 --instance mariadb-exporter:9104

# 輸出到檔案
da-tools discover-mappings --endpoint http://mariadb-exporter:9104/metrics -o mapping-draft.yaml

# JSON 輸出
da-tools discover-mappings --endpoint http://mariadb-exporter:9104/metrics --json
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功發現 partition label 並產生映射草稿 |
| `1` | 無法連線或未發現合適的 partition label |

---

## 環境變數

| 變數 | 用途 | 預設值 | 說明 |
|------|------|--------|------|
| `PROMETHEUS_URL` | Prometheus 端點 URL | `http://localhost:9090` | 作為 `--prometheus` 的 fallback；容器內 localhost 指向容器自己，需使用正確的網路配置 |

--8<-- "docs/includes/prometheus-url-config.md"

---

## Docker 快速參考

### 作為 Kubernetes Job

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: da-tools-check
  namespace: monitoring
spec:
  template:
    spec:
      containers:
        - name: da-tools
          image: ghcr.io/vencil/da-tools:v2.3.0
          env:
            - name: PROMETHEUS_URL
              value: "http://prometheus.monitoring.svc.cluster.local:9090"
          args: ["check-alert", "MariaDBHighConnections", "db-a"]
      restartPolicy: Never
  backoffLimit: 0
```

---

## 常見問題

### Q: 如何在 CI/CD 中使用 da-tools？

**A**: 使用 `--ci` flag；exit code 0 = success，非 0 = fail。詳見各命令 `--help`。

### Q: 如何指定多個 metric 用於 validate？

**A**: 使用 `--mapping` CSV 檔案（格式：`old_rule,new_rule`）。詳見 [validate](#validate) 命令說明。

### Q: blind-spot 與 analyze-gaps 有什麼區別？

**A**:
- **blind-spot**：比對「叢集基礎設施 vs tenant 配置」，找出有 exporter 但無對應 tenant 配置的盲區。
- **analyze-gaps**：比對「custom rule vs Rule Pack」，評估 Rule Pack 的涵蓋度。

兩者互補，建議遷移後同時執行。

### Q: 如何安全地執行 cutover？

**A**:
1. 執行 `validate --auto-detect-convergence` 確認收斂
2. 執行 `cutover --dry-run` 預覽步驟
3. 執行 `cutover` 正式切換
4. 執行 `diagnose` + `batch-diagnose` 驗證健康

---

## 版本相容性

| da-tools 版本 | 平台版本 | 說明 |
|-------------|---------|------|
| v1.13.0 | v1.13.0 | DX Automation 工具（shadow-verify + byo-check + federation-check + grafana-import） |
| v1.12.0 | v1.12.0 | Rule Pack 擴展（JVM + Nginx） |
| v1.11.0 | v1.11.0 | Cutover + Blind-spot + Config-diff + Maintenance-scheduler |
| v1.10.0 | v1.10.0 | Generate-routes --output-configmap |

---

## 後續資源

| 文件 | 內容 |
|------|------|
| [getting-started/for-platform-engineers.md](getting-started/for-platform-engineers.md) | Platform Engineer 快速入門 |
| [migration-guide.md](migration-guide.md) | 遷移步驟詳解 |
| [troubleshooting.md](troubleshooting.md) | 故障排查 |
| [architecture-and-design.md](architecture-and-design.md) | 架構與設計原理 |

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["da-tools CLI Reference"] | ⭐⭐⭐ |
| ["Threshold Exporter API Reference"](api/README.md) | ⭐⭐⭐ |
| ["da-tools Quick Reference"](./cheat-sheet.md) | ⭐⭐⭐ |
| ["Grafana Dashboard 導覽"](./grafana-dashboards.md) | ⭐⭐ |
| ["故障排查與邊界情況"](./troubleshooting.md) | ⭐⭐ |
| ["性能分析與基準測試 (Performance Analysis & Benchmarks)"](./benchmarks.md) | ⭐⭐ |
| ["BYO Alertmanager 整合指南"](./byo-alertmanager-integration.md) | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南"](./byo-prometheus-integration.md) | ⭐⭐ |
