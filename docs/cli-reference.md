---
title: "da-tools CLI Reference"
tags: [cli, reference, da-tools, tools]
audience: [platform-engineer, sre, devops, tenant]
version: v2.9.0
lang: zh
---

# da-tools CLI Reference

> **Language / 語言：** **中文 (Current)** | [English](./cli-reference.en.md)
>
> **受眾**：Platform Engineers、SREs、DevOps、Tenants
> **容器映像**：`ghcr.io/vencil/da-tools:v2.9.0`
> **版本**：v2.9.0（與平台版本同步）

da-tools 是一個可攜式 CLI 容器，打包了 Dynamic Alerting 平台的驗證、遷移、配置與運維工具。本文件是所有子命令的完整參考。

---

## 依你的角色快速跳轉

這份參考涵蓋多種角色的工具，**不需通讀全文** —— 找到你的角色，直接看你最常用的命令子集。

| 你的角色 | 你最常用的命令 | 跳到 |
|---|---|---|
| **Platform Engineer**<br>部署與運維平台 | `init` · `gitops-check` · `operator-generate` · `operator-check` · `validate-config` · `byo-check` · `federation-check` | [命令分類](#命令分類) |
| **SRE / On-call**<br>日常診斷與告警 | `diagnose` · `batch-diagnose` · `check-alert` · `alert-quality` · `alert-correlate` · `drift-detect` · `cardinality-forecast` | [Prometheus API Tools](#prometheus-api-tools) |
| **DevOps / GitOps**<br>CI 整合與漂移防護 | `validate-config` · `config-diff` · `rule-pack-diff` · `backtest` · `config-history` · `drift-detect` | [檔案系統工具](#檔案系統工具) |
| **Tenant**<br>自助閾值管理 | `scaffold` · `patch-config` · `validate-config` · `tenant-verify` · `explain-route` · `threshold-recommend` | [配置生成工具](#配置生成工具) |
| **Domain Expert**<br>規則品質治理 | `lint` · `analyze-gaps` · `alert-quality` · `evaluate-policy` · `opa-evaluate` · `migrate` · `parser` | [檔案系統工具](#檔案系統工具) |

> 多數命令支援 `--help` 與 `--json`（CI gate 用）。完整參數見下方 [命令詳解](#命令詳解)。

---

## 目錄

1. [快速開始](#快速開始)
2. [全局選項](#全局選項)
3. [命令分類](#命令分類)
4. [命令詳解](#命令詳解)
   - [Prometheus API 工具](#prometheus-api-tools)
   - [配置生成工具](#配置生成工具)
   - [檔案系統工具](#檔案系統工具)
5. [環境變數](#環境變數)
6. [Docker 快速參考](#docker-快速參考)

---

## 快速開始

### 拉取映像

```bash
# 從 OCI registry 拉取（需要 CI/CD 已推送）
docker pull ghcr.io/vencil/da-tools:v2.9.0

# 本地建構（開發用）
cd components/da-tools/app && ./build.sh v1.11.0
```

### 查看說明

```bash
docker run --rm ghcr.io/vencil/da-tools:v2.9.0 --help
docker run --rm ghcr.io/vencil/da-tools:v2.9.0 --version
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

**分派層（`da-tools` 本身）的結束碼**——與下方各命令章節的結束碼表是兩層不同的東西：

| 情境 | 結束碼 | 說明 |
|------|--------|------|
| `--help` / `--version` / 無參數 | `0` | 用法與版本印到 stdout |
| **未知子命令** | `2` | 呼叫端錯誤（[#1406](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1406) 起；之前是 `1`）。訊息一律走 stderr，stdout 為空 |
| **映像內找不到子命令對應的腳本** | `2` | 同上；stderr 點名缺的腳本檔與已搜尋路徑 |
| 子命令自己的結束碼 | 原封透傳 | 工具在同一行程內以 `__main__` 執行，無重映射——語意見各命令章節的結束碼表與 SSOT [`_lib_exitcodes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/_lib_exitcodes.py) |
| 子命令拋出未捕捉的例外（traceback） | `2` | 內部錯誤（[#3090](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3090) 起；之前是直譯器預設的 `1`，與「有發現」撞碼）。stderr 先是 `Traceback`，最後一行是 `ERROR: unexpected internal error (<例外型別>): <訊息>`；工具在 crash 前已寫出的 stdout 保留。帶 `--json` 時，stdout 另有一份 envelope：`"status": "caller_error"`、`"reason": "internal_error"`、`"error": "<同一行 ERROR>"`；工具有早退報告時，另帶該報告的鍵（歸零）（[#3094](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3094)）。這份 envelope 一律是 JSON 物件，正常輸出是陣列的 `validate-config`、`analyze-gaps` 等工具也一樣；`threshold-recommend --export-patch` 的 stdout 是 YAML，沒有 envelope。參數解析完成前就 crash、或工具已在 stdout 寫過東西之後才 crash，都沒有這份 envelope；`config-diff` 與 `operator-generate` 自己接住例外的路徑同樣以 `2` 結束，envelope 的 `reason` 分別是 `compare_failed`、`generate_failed`（[#3115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3115)；先前 stdout 是空的）；`tenant-verify` 不輸出這份 envelope。分派層只攔 `Exception`，工具自己的 `sys.exit(N)` 與 Ctrl-C 照舊原封透傳。唯一例外是 `tenant-verify`：它的 `2` 是「驗證不一致」，crash 維持 `1`（見該節） |
| 子命令以結束碼 `2` 結束（呼叫端錯誤），帶 `--json` | `2` | stdout 恰好一份 envelope：`"status": "caller_error"`，`"reason"` 是具名原因（`served_values_unavailable` 找不到或執行 da-guard 失敗、`yaml_file_unreadable` 輸入 YAML 讀不了、`output_unwritable` 輸出寫不進去、`observed_map_unavailable` observed-map 不可用；工具自己沒命名的是 `caller_error`），`"error"` 是 stderr 上那一行 `ERROR:`（沒命名原因時是工具在 stderr 最後一行以 `ERROR` 開頭的訊息，沒有就是最後一行；一律收成一行、控制字元換掉）；工具有早退報告時另帶該報告的鍵（歸零）。工具已自己在 stdout 印出文件時不再加印（[#3115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3115)；先前許多這類路徑 stdout 是空的）。argparse 拒絕的參數仍然沒有 envelope |
| 讀端先關掉 pipe（`… \| head -c1`、`… \| true`），或執行中 fd 1 被關 | `2` | 工具還有輸出沒送出時，不論它原本要以什麼結束碼結束（含 crash），一律以 `2` 結束，stderr 一行 `ERROR: stdout was closed before all output was written`；先前是直譯器自己的 `120`，寫入大量輸出時還會被報成 `unexpected internal error (BrokenPipeError)`（[#3124](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3124)）。輸出在讀端關閉前已全部送出時，結束碼照舊。寫不進去但不是讀端關閉（磁碟滿、`/dev/full`）時，那一行改為 `ERROR: cannot write to stdout: <原因>`，結束碼同樣是 `2`。`tenant-verify` 是 `1`。不是 stdout 的 `BrokenPipeError`（例如 socket）仍是 crash。shell 在工具啟動前就關掉 stdout（`>&-`）不在此列：輸出直接被丟棄，結束碼照舊，也沒有 envelope |

各命令章節的結束碼表列的是主要觸發條件，**不是窮舉**：輸入壞到讓工具拋出未捕捉例外時，Python 工具一律是上面那列的 `2`（`tenant-verify` 是 `1`），Go 工具（`guard`、`batch-pr`、`parser`）的 panic 也是 `2`，表上不逐一列出。不經 `da-tools`、直接執行 `scripts/tools/` 下的腳本時也一樣：只要該腳本載入了 `_lib_exitcodes`（多數工具經 `_lib_io`／`_lib_python` 間接載入），之後發生的 crash 就是 `2`；在那之前的 import 期錯誤（例如缺 PyYAML）仍是 `1`。互動模式（`python -i`）不受影響。

> ⚠️ 前兩列曾回 `1`，與各子命令「有發現」的 `1` 撞碼——`docker run … da-tools:<移動的 tag> <子命令>` 在子命令改名、或映像尚未收錄該子命令時，在 CI 看起來與「工具正常跑完並找到東西」完全同形（且 stdout 是一份空報告）。消費端請把 `2` 當「工具沒跑」處理，不要與 `1` 合併判定。

---

## 命令分類

### Prometheus API Tools (Network Access Required)

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
| `init` | 專案骨架產生（CI/CD + conf.d + Kustomize overlays） | `--tenants <list>` 搭配 `--ci`／`--rule-packs`／`--deploy` 任一，或互動模式（不帶旗標） |
| `gitops-check` | GitOps Native Mode 就緒度驗證（repo / local / sidecar） | `<subcommand>` |
| `state-reconcile` | 遷移狀態目錄聲明式一致化（schema_version 驗證 + manifest 重建） | `--state-dir <dir>`（預設 `.da/state`） |
| `rule-pack-diff` | Rule Pack 兩版本機械比對（added / removed / breaking label schema） | `--from <v1.yaml> --to <v2.yaml>` |
| `silencer-drift-check` | AM silence 對 v2 rule pack 漂移偵測（offline，吃 amtool dump） | `--silences-file <json> --rule-source <path>` |
| `migration-audit` | 遷移稽核：conf.d 以 PyYAML 與 `da-guard parse-tree` 各讀一次，列出讀法差異（ADR-036 §8） | `--config-dir <dir>` |

### Operator + Federation 工具

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `operator-generate` | Rule Packs + Tenant 配置 → PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML | `--rule-packs-dir <dir>` |
| `operator-check` | Operator CRD 部署狀態驗證（5 項檢查 + 診斷報告） | （自動探索或 `--namespace <ns>`） |
| `runtime-audit` | Git rule-packs ↔ Prometheus runtime 唯讀對帳（#747；MISSING / UNHEALTHY / ORPHAN） | `--prometheus <url>` 或 `--runtime-json <file>` |
| `rule-pack-split` | Rule Pack 分層拆分（edge Part 1 + central Parts 2+3），Federation Scenario B | `--rule-packs-dir <dir>` |
| `fed-key` | 產生 / 輪替 federation JWT 簽章金鑰（ADR-020 IV-2l）：私鑰 Secret manifest → stdout、公鑰 JWKS → 檔案 | （無，bootstrap 預設）/ `--rotate --existing-jwks <file>` |

### 配置生成工具

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `generate-routes` | Tenant YAML → Alertmanager route + receiver + inhibit fragment | `--config-dir <dir>` |
| `patch-config` | ConfigMap 局部更新（含 `--diff` preview） | `<tenant> <metric> <value>` 或 `--diff` |
| `generator-service` | 產生器容器的服務：經 Unix socket 判定寫入後的 conf.d 能否通過 `generate-routes --validate --strict`（ADR-036 §3） | `--socket <path> --repo <dir> --config-subdir <path> --work-dir <dir>` |

### 檔案系統工具（離線可用）

這些工具操作本地 YAML 檔案，不需網路。

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `scaffold` | 產生 tenant 配置 | `--tenant <name> --db <types>` |
| `migrate` | 傳統規則 → 動態格式轉換（AST 引擎） | `<input_file>` |
| `validate-config` | 一站式配置驗證（YAML + schema + routes + policy） | `--config-dir <dir>` |
| `offboard` | 下架 tenant 配置 | `<tenant>` |
| `deprecate` | 下架指標：從 `defaults:`／`optional_overrides:`／租戶檔刪除其 key | `<metric_keys...>` |
| `lint` | 檢查 Custom Rule 治理合規性 | `<path...>` |
| `onboard` | 分析既有 Alertmanager/Prometheus 配置進行遷移 | `<config_file>` 或 `--alertmanager-config <file>` |
| `analyze-gaps` | Custom Rule 對應 Rule Pack 缺口分析 | `--tenant-config <path>` |
| `config-diff` | 兩目錄配置差異比對（GitOps PR review） | `--old-dir <dir> --new-dir <dir>` |
| `evaluate-policy` | Policy-as-Code DSL 評估引擎 | `--config-dir <dir>` |
| `opa-evaluate` | OPA Rego 策略評估橋接（OPA 整合） | `--config-dir <dir>` |
| `guard` | Dangling Defaults Guard 包裝（v2.8.0），shell-out 至 `da-guard` Go binary | `defaults-impact --config-dir <dir>` |
| `batch-pr` | Migration Batch PR Pipeline 包裝（v2.8.0），shell-out 至 `da-batchpr` Go binary | `apply\|refresh\|refresh-source [flags]` |
| `parser` | PromRule parser 包裝（v2.8.0），shell-out 至 `da-parser` Go binary；strict-PromQL 相容性檢查 + dialect 分類 | `import\|allowlist [flags]` |
| `tenant-verify` | 印 tenant effective config + merged_hash（v2.8.0；incremental migration playbook rollback checklist） | `<tenant-id> [--conf-d <dir>] [--expect-merged-hash <hash>]` 或 `--all --json` |
| `test-notification` | 多通道通知連通性測試（驗證 receiver 可達性） | `--config-dir <dir>` |
| `threshold-recommend` | 閾值推薦引擎（基於歷史 P50/P95/P99 數據） | `--config-dir <dir>` + `--prometheus <url>` |
| `threshold-govern` | 閾值治理迴路：推薦→過濾→經 tenant-api 開 per-tenant proposed-PR（#656） | `--config-dir <dir>` + `--prometheus <url>` + `--apply` |
| `explain-route` | 路由合併管線除錯器（四層展開 + 設定檔擴展，ADR-007） | `--config-dir <dir>` |
| `explain-keys` | 逐鍵列出來源、讀取端、判決與 `disable` 的作用；`--assert` 對照預期結果（ADR-038 決定 9） | `--config-dir <dir>` |
| `discover-mappings` | 自動發現 1:N 實例-租戶映射（掃描 exporter /metrics，ADR-006） | `--endpoint <url>` 或 `--prometheus <url>` |

---

## 命令詳解

### Prometheus API Tools

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
| `1` | 只有未捕捉例外（traceback）會回 1——本命令沒有 violation 出口（inactive／pending／firing 都是 0） | <!-- datools-cmd-ignore: 只有 traceback 回 1，沒有出口可追 -->
| `2` | 呼叫端錯誤：Prometheus API 連不上或回錯（stdout 印 `{"error": ...}` JSON），或 argparse 拒絕的參數 |

---

#### diagnose

對單一 tenant 執行健康檢查：MariaDB Pod 狀態、exporter 的 `mysql_up`、運營模式（維護／靜音），以及給了 `--config-dir` 時的 profile 與繼承鏈。

**用途**：切換後或排查時快速確認單一租戶。⚠️ Pod 與 exporter 兩項是為 MariaDB 寫的：Pod 檢查查租戶同名 namespace 裡 `app=mariadb` 的 Pod（需要 `kubectl` 與叢集存取權，da-tools 映像不含 `kubectl`），exporter 檢查查 `mysql_up{instance="<tenant>"}`。工具先從 Prometheus 的 `tenant_expected_exporter{tenant="<tenant>"}` 讀租戶的 `db_type`（租戶在 `_metadata.db_type` 宣告了才有這條 series）。只有 `db_type` 是 `mariadb` 時才跑這兩項；其他資料庫或沒宣告的租戶，兩項列在輸出的 `skipped` 並附原因，`status` 是 `unchecked`，不是 `healthy` 也不是 `error`。所以沒宣告 `db_type` 的 MariaDB 租戶也不會做這兩項檢查；要檢查就在 `_metadata.db_type` 宣告 `mariadb`。⚠️ v2.9.0 映像還是舊行為：不看 `db_type`，非 MariaDB 租戶一律回 `status: error`（`Pod not found`）。 <!-- image-caveat: v2.9.0 -->

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
| `--config-dir <PATH>` | 租戶配置目錄；給了才查 profile 與繼承鏈 | （無） |
| `--show-inheritance` | 只印完整的繼承鏈解析（需要 `--config-dir`） | false |
| `--json` | JSON 輸出。本來就只輸出 JSON，接受這個旗標只為相容 | true |

Pod 一律查與租戶同名的 namespace，沒有另外指定 namespace 的選項。

**輸出**

一行 JSON。健康時只有兩個欄位：

```json
{"status": "healthy", "tenant": "db-a"}
```

有問題時列出 `issues` 與最近的錯誤 log：

```json
{"status": "error", "tenant": "db-a", "issues": ["Pod not found", "Prometheus query failed (http://localhost:9090)"], "recent_logs": []}
```

已經有問題、但抓 log 的 `kubectl logs` 在時限內沒回應時，`recent_logs` 是空的並多一個 `recent_logs_error` 說明原因，結束碼照問題本身（[#2968](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2968)）。租戶在維護或靜音模式時多一個 `operational_mode`；給了 `--config-dir` 時多 `profile`（有設才出現）與 `inheritance_chain`。跳過 Pod 與 exporter 兩項時多一個 `skipped`：

```json
{"status": "unchecked", "tenant": "db-b", "skipped": [{"check": "pod", "reason": "db_type=postgresql; the Pod and exporter checks are written for MariaDB (app=mariadb, mysql_up)"}, {"check": "exporter", "reason": "db_type=postgresql; the Pod and exporter checks are written for MariaDB (app=mariadb, mysql_up)"}]}
```

**繼承鏈（`--config-dir`）**：租戶有沒有、綁到哪個 profile、每個 key 來自哪一層取 `da-guard effective`（tenant-api `/effective`），每個值取 exporter 實際送出的（`da-guard served-values`，即 `/metrics`），不再由 Python 重讀 YAML（[#2526](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2526)、[#2547](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2547)）。整棵樹都讀（子目錄的租戶與各層 `_defaults.yaml` 也算）。`--show-inheritance` 的 `resolved` 是 `/metrics` 送出的值（`/metrics` 不送的 key，例如停用或根目錄沒宣告的，顯示 `/effective` 的值）；`chain` 是每個值寫在哪一層、寫了什麼。兩者不同時（例如讀不成數字的 `2024-02-30` 送的是 defaults 的值、`expires:` 已過期）輸出不解釋原因，原因看 `da-guard served-values` 的 stderr。`chain` 每層只列它寫下且勝出的 key（被覆寫的 defaults 不列），根目錄平台檔的 `tenants:` 是自己的一層 `platform`；`profile_name` 是 exporter 實際綁上的 profile，引用了不存在的 profile 時為 `null` 並在 stderr 說明；`declared` 仍是根目錄 `_defaults.yaml` 的 `optional_overrides:`（只取名稱原文）。tree 裡沒有這個租戶時印 `{"error": ...}`、stderr 說明，結束碼 0。需要 da-guard（映像內建；repo 內 `make da-guard-build`）；找不到 da-guard、exporter 丟掉或讀不到某個檔、或 da-guard 失敗（含 served-values 拒收整棵樹、或兩份輸出的租戶對不上）時，不給部分答案，結束碼 2 並轉出 da-guard 的 stderr。⚠️ v2.9.0 映像仍以 Python 讀 conf.d、不需要 da-guard。 <!-- image-caveat: v2.9.0 -->

**範例**

```bash
da-tools diagnose db-a --config-dir ./conf.d
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | `status: healthy`，或 `status: unchecked`（Pod 與 exporter 兩項被跳過，其餘沒有問題） |
| `1` | `status: error`：Pod 不在或不是 Running、`mysql_up` 不是 1 |
| `2` | 呼叫端錯誤：參數錯誤（缺 tenant，或 `--show-inheritance` 沒配 `--config-dir`）；Prometheus 查詢失敗（輸出仍是 `status: error` 的 JSON，`issues` 含 `Prometheus query failed`，與其他問題並存時也回 2）；要跑 Pod 檢查但環境裡沒有 `kubectl`，或 `kubectl` 在時限內沒回應（每次上限 120 秒；兩者都是 stderr 一行說明，[#2968](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2968)）；給了 `--config-dir` 而找不到 da-guard、exporter 丟掉或讀不到某個檔、或 da-guard 失敗（stderr 一行 `ERROR:` 加 da-guard 的 stderr）。這些情況 stdout 都是一份 `"status": "caller_error"` 的 envelope，da-guard 這一類的 `reason` 是 `served_values_unavailable`，其餘是 `caller_error`（[#3115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3115)；先前 stdout 是空的） |

⚠️ v2.9.0 映像還沒有這套結束碼：`status` 是 `healthy` 或 `error` 都回 `0`，沒有 `kubectl` 時以 Python traceback 結束（rc=1）。用 v2.9.0 時請讀輸出的 `status`。 <!-- image-caveat: v2.9.0 -->

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
| `--timeout <SEC>` | 單一租戶檢查的期限（秒）：到期仍未完成的租戶記為 `error`（`timed out after …s`），整批不再等它。須大於 0、可帶小數。預設值大於 diagnose 最多 4 次 Prometheus 查詢（每次 10 秒上限）的總和，Prometheus 卡住時仍能回報具體原因（[#2954](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2954)）。期限也傳到檢查裡的每次呼叫：`kubectl` 在期限前稍早（`min(0.5 秒, --timeout/4)`）被終止，不會在整批結束後留下行程（`--timeout` 很短（低於約 0.5 秒）或機器負載高時不保證）；期限已過就不再啟動 `kubectl` 或送出 Prometheus 查詢（[#2968](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2968)） | `60` |
| `--output <FILE>` | 輸出至檔案（JSON 格式） | stdout |
| `--dry-run` | 僅列出租戶，不執行檢查 | false |
| `--namespace <NS>` | K8s namespace（auto-discover 用） | `monitoring` |

**輸出**

JSON 格式統一報告，包含所有租戶的檢查結果摘要。`unchecked` 的租戶另計在 `unchecked_count`，不算進 `healthy_count` 也不算進 `issue_count`；`health_score` 的分母只算有檢查的租戶，全部都是 `unchecked` 時為 `null`。文字報告另列一段 `Unchecked Tenants` 並附跳過原因。⚠️ v2.9.0 映像沒有 `unchecked` 這個狀態與欄位。 <!-- image-caveat: v2.9.0 -->

**範例**

```bash
da-tools batch-diagnose --workers 10
da-tools batch-diagnose --tenants db-a,db-b,db-c --output report.json
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 所有租戶都是 `healthy` 或 `unchecked`（含 `--dry-run`） |
| `1` | 一個或多個租戶檢查失敗（看輸出裡各租戶的 `status` 與 `issues`） |
| `2` | 呼叫端錯誤：參數錯誤（含 `--workers` 小於 1、`--timeout` 不大於 0）、任一租戶的 Prometheus 查詢失敗、缺少 `kubectl` 或 `kubectl` 在時限內沒回應，或在 `--timeout` 內沒檢查完（優先於 `1`，與單跑 `diagnose` 一致），或 `-o/--output` 指到的輸出路徑寫不進去（#1641） |

⚠️ v2.9.0 映像還沒有這套結束碼：租戶檢查失敗與 Prometheus 查詢失敗都回 `0`。用 v2.9.0 時請讀輸出裡各租戶的 `status`。 <!-- image-caveat: v2.9.0 -->

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
| `--duration <SEC>` | 觀測時長（正整數秒；`0`、負數、小數回 `2`；小於 `--interval` 時一次都採不到，也回 `2`，`--dry-run` 亦同） | `600` |
| `--interval <SEC>` | 採樣間隔（正整數秒；`0`、負數、小數回 `2`） | `15` |
| `--metrics <LIST>` | 逗號分隔指標清單（空=全部） | （全部） |
| `-o, --output-dir <DIR>` | 輸出目錄；兩個 CSV 都寫在這裡（見下方「輸出」），目錄不存在會建立 | `baseline_output` |
| `--dry-run` | 僅顯示要觀測的指標，不實際採樣 | false |

**輸出**

統計摘要印到 stdout；同時在 `--output-dir` 目錄寫入兩個 CSV：`baseline-<tenant>-timeseries.csv`（原始採樣）與 `baseline-<tenant>-summary.csv`（各行為一個指標：min／max／avg／p50／p90／p95／p99 與建議閾值）。⚠️ 沒有「輸出到單一檔案」的旗標——`--output <FILE>` 會被 argparse 當成 `--output-dir` 的縮寫，於是產生一個叫 `<FILE>` 的**目錄**。

觀測的指標與建議寫進的租戶 key：

| 指標 | 單位 | 建議寫進 |
|------|------|----------|
| `connections` | 連線數 | `mysql_connections` |
| `cpu` | 佔 limit 的 %（租戶內最高的容器） | 對照 `container_cpu` 的平台預設，不給門檻值（見下） |
| `memory` | 佔 limit 的 %（租戶內最高的容器） | 對照 `container_memory` 的平台預設，不給門檻值（見下） |
| `slow_queries` | 每分鐘 | 沒有租戶 key：`MariaDBHighSlowQueries` 比的是固定值 |
| `disk_io` | KiB/s | 沒有租戶 key：沒有 rule pack 告警讀這個量 |

`cpu`／`memory` 直接用 cAdvisor 用量除以 kube-state-metrics 的 limit，算法與 rule pack 的 `tenant:container_{cpu,memory}_percent:by_container` 相同，需要 kube-state-metrics。沒設 limit 的容器量不到（rule pack 對這種容器的 CPU 改用 node share，baseline 不涵蓋）。`cpu`／`memory` 有上界（到 100% 就 OOMKill 或被節流），所以不用 p95×1.2／p99×1.5 算門檻，那樣 p99 超過約 67% 時會給出永遠不會響的 >100 門檻。改為對照平台預設（取自 scaffold，目前 `container_cpu` 80、`container_memory` 85）：p99 低於預設時印「預設可用，不需覆寫」；p99 已達預設時印「照預設會常響，請先調高 limit」，並附讓 p99 落在預設九成所需的 limit 倍數。其餘指標的建議以 `patch-config <tenant> <key> <值>` 的寫法印出，沒有租戶 key 的只印觀測值與原因。⚠️ v2.9.0 映像仍是舊行為：`cpu` 是單核 %、`memory` 是 MiB，建議的 key 一律拼成 `mysql_<指標>`，其中 `mysql_memory`／`mysql_disk_io`／`mysql_slow_queries` 沒有任何告警讀，`mysql_cpu` 其實是 threads_running 的閾值（後來改名為 `mysql_threads_running`）。用 v2.9.0 時不要照抄它印的 patch-config。 <!-- image-caveat: v2.9.0 -->

**範例**

```bash
da-tools baseline --tenant db-a --duration 1800 --interval 30 -o baseline_out
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功。⚠️ Prometheus 連線或查詢失敗**不是**錯誤——失敗的採樣記為空值、報告與 CSV 照出，仍是這一格 |
| `1` | 只有未捕捉例外（traceback）會回 1——本命令沒有 violation 出口。⛔ `--interval 0`（原本是 `duration // interval` 的 `ZeroDivisionError`）自 #2966 起在解析時就回 `2`；輸出路徑寫不進去自 #1789 起是 `2`，兩者都不再落在這裡 | <!-- datools-cmd-ignore: 只有 traceback 回 1，沒有出口可追 -->
| `2` | 呼叫端錯誤：`--metrics` 列出的指標沒有一個是工具認得的（錯誤訊息會列出可用清單）、缺必需的 `--tenant`、argparse 拒絕的參數，或 `-o/--output-dir` 寫不進去（實測：父路徑是檔案、目錄下要建的 CSV 已是目錄）——一行 `ERROR: cannot …` 指名 `-o/--output-dir`，不再是 traceback（#1789） |

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
   讀 `da-tools migrate` 在輸出目錄產生的 `prefix-mapping.yaml`，不接受 CSV（餵 CSV 會在載入時 traceback，結束碼 1）。檔案長這樣（實跑 `migrate` 取得）：
   ```yaml
   custom_mysql_global_status_threads_connected:
     original_metric: mysql_global_status_threads_connected
     alert_name: MySQLTooManyConnections
     golden_match: null
     golden_rule: null
     old_query: max by(tenant) (mysql_global_status_threads_connected)
     new_query: tenant:custom_mysql_global_status_threads_connected:max
   ```
   每一項產生一組比對：新查詢 `new_query` 是 migrate 產生的 recording rule；舊查詢 `old_query` 是原規則的左半邊以同一種方式依租戶聚合，rate 類規則保留 `rate()`（例如 `sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))` 對 `tenant:custom_mysql_global_status_slow_queries:sum`）。兩邊量綱相同，recording rule 有載入並正常評估時數值會一致。原始 series 沒有 `tenant` label 時，舊查詢會多出一個沒有租戶的組，報告裡列為新側缺值。字典判定改用黃金標準的項（有 `golden_rule`）migrate 不產出 recording rule，沒有這兩欄，validate 跳過並在 stderr 列出。
   舊版 migrate 產的檔沒有 `old_query`／`new_query`，validate 會在 stderr 警告並退回「原始指標 對 `tenant:<key>:max`」：rate 類與 `:sum` 的組在這種檔上比不出來，請用新版 migrate 重產。⚠️ v2.9.0 映像的 migrate 與 validate 都還是舊行為。 <!-- image-caveat: v2.9.0 -->

2. **Query 模式**：`--old <query> --new <query>`
   直接指定兩組 PromQL

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--watch` | 持續監控模式（每 N 秒比對一次） | false |
| `--interval <SEC>` | 監控間隔（秒，≥ 0，可帶小數；`0` = 輪與輪之間不等待，由 `--rounds` 封頂；負數、`nan`、`inf` 回 `2`） | `60` |
| `--rounds <N>` | 監控輪數。⚠️ `0` 不是無限，是**一輪都不跑**（`for i in range(rounds)`） | `10` |
| `--tolerance <RATIO>` | 容許誤差**比值**（不是百分比）：`0.01` = 1%。必須是 ≥ 0 的有限數，`inf`、`nan`、負值回 2 | `0.001` |
| `--auto-detect-convergence` | 自動偵測收斂並產出 readiness JSON | false |
| `-o, --output-dir <DIR>` | 輸出目錄；`validation-report.csv`（與 watch 模式的 `cutover-readiness.json`）寫在這裡 | `validation_output` |

**輸出**

`<output-dir>/validation-report.csv`，各行為一個 rule 的比對結果（舊值、新值、差異百分比、收斂狀態）；摘要另印到 stdout。⚠️ 沒有「輸出到單一檔案」的旗標——`--output <FILE>` 會被 argparse 當成 `--output-dir` 的縮寫，於是產生一個叫 `<FILE>` 的**目錄**。

`--watch` 搭配 `--auto-detect-convergence` 時，額外在同一目錄寫入 `cutover-readiness.json` 供 `cutover` 命令使用（`--convergence-output <FILE>` 可改路徑）；單次模式（無 `--watch`）不會產出這個檔。

**範例**

```bash
da-tools validate --mapping migration_output/prefix-mapping.yaml
da-tools validate --mapping migration_output/prefix-mapping.yaml --watch --interval 60 --rounds 1440
da-tools validate --mapping migration_output/prefix-mapping.yaml --watch --auto-detect-convergence -o ./validation_output
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 全部比對組一致；`--watch --auto-detect-convergence` 時為已收斂 |
| `1` | 有 mismatch 或單邊查不到值（`old_missing`／`new_missing`）；`--watch --auto-detect-convergence` 時為跑滿 `--rounds` 仍未收斂 |
| `2` | 呼叫端錯誤：參數錯誤、mapping 裡沒有任何比對組、Prometheus 連線或查詢失敗（`--watch` 只看最後一輪；已收斂時不看），或 `-o/--output-dir`／`--convergence-output` 指到的輸出路徑寫不進去（#1641） |

⚠️ v2.9.0 映像還沒有這套結束碼：除了參數錯誤回 `2`，其餘一律回 `0`，連不上 Prometheus 時也照樣印「🎉 可以安全切換」。用 v2.9.0 時不要拿結束碼當閘門，要讀摘要裡的 mismatch／missing 計數。 <!-- image-caveat: v2.9.0 -->

---

#### cutover

Shadow Monitoring 一鍵切換（migrate／shadow 流程的最後一步）：依序停止 shadow monitor Job、刪除舊 Recording Rules、移除 shadow label 與 Alertmanager 攔截，最後確認租戶的閾值指標存在。

**用途**：遷移最後一步，自動化完整切換流程。只適用 migrate／shadow 流程；它要刪、要改的物件（`shadow-monitor` Job、`prometheus-rules-old` ConfigMap、`migration_status` label）要由那條流程產生。

**語法**

```bash
da-tools cutover --readiness-json <FILE> --tenant <name> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--readiness-json <FILE>` | `validate_migration --auto-detect-convergence` 產出的 `cutover-readiness.json` |
| `--tenant <NAME>` | Tenant ID（用於切換後的健康檢查） |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--dry-run` | 印出會執行的 `kubectl` 命令，不做任何變更 | false |
| `--force` | readiness JSON 的 `ready` 為 false 也照樣切換；`--readiness-json` 仍然必填 | false |
| `--namespace <NS>` | K8s namespace | `monitoring` |
| `--json-output` | 另在 stdout 印一份 JSON 報告 | false |

**自動化步驟**

0. 讀 readiness JSON：缺欄位或讀不到即中止；`ready` 為 false 時中止（除非 `--force`）
1. `kubectl delete job shadow-monitor`
2. `kubectl delete configmap prometheus-rules-old`
3. `kubectl label configmap prometheus-rules migration_status-`
4. `kubectl label configmap alertmanager-config migration_status-`
5. 查詢 `count(user_threshold{tenant="<tenant>"})`，確認租戶的閾值指標存在

工具沒有回退選項；任一步失敗時會指向 `shadow-monitoring-sop.md` §7.2 的手動回退步驟。

**範例**

```bash
da-tools cutover --readiness-json cutover-readiness.json --tenant db-a --dry-run
da-tools cutover --readiness-json cutover-readiness.json --tenant db-a
da-tools cutover --readiness-json cutover-readiness.json --tenant db-a --force
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 切換成功 |
| `1` | readiness 顯示未就緒（沒帶 `--force`），或某個切換步驟失敗 |
| `2` | 呼叫端錯誤：缺必填參數、readiness JSON 不存在、不是合法 JSON 或缺欄位、Prometheus 連不到、找不到 `kubectl` |

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

「有對應 tenant 配置」指 exporter 的 `/metrics` 對該租戶實際發出的閾值（經 `da-guard served-values` 讀出，#2115）：值寫在 `defaults:`、平台檔 `tenants:`、租戶檔或子目錄都算，子目錄裡的租戶也算；`disable` 的鍵、沒有預設值而不會發出的鍵不算。沒有 `tenants:` 的檔不是租戶，stderr 逐檔印 `WARN`。exporter 讀不到的檔或子目錄（例如權限不足、懸空 symlink、無法列出內容的子目錄）以結束碼 2 結束，`ERROR` 行指名該檔（或目錄）與原因（`stat_error`／`read_error`／`walk_error`／`special_file`）；例外是指向目錄的 symlink——exporter 本來就不跟進（k8s ConfigMap 的巢狀路徑會掛成這種 symlink），只跳過、不影響結束碼。da-guard 在 stderr 印的內容逐行轉印到 stderr，每行前面加前綴 `da-guard|`（前面兩個空格、後面一個空格）、經控制字元跳脫（檔名裡的換行會讓 da-guard 印成兩行，轉印時無法還原，但不會出現在行首）。需要 da-guard（映像內建；repo 裡直接跑時用 `$DA_GUARD_BINARY` 或 `$PATH`）。

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
| `1` | 只有未捕捉例外（traceback）會回 1。Prometheus 連不上只印 WARN，照常以 0 結束 |
| `2` | 呼叫端錯誤：`--config-dir` 底下有 exporter 解析失敗而整份跳過的檔（例如內容不是 UTF-8 或不是合法 YAML），或 exporter 讀不到的檔或子目錄（例如權限不足、懸空 symlink；指向目錄的 symlink 除外）——`ERROR` 行指名哪一檔（或目錄），da-guard 在 stderr 印的內容（含 exporter 的原因）逐行附在下面，每行加固定前綴（見下）；整棵樹被 exporter 拒收（例如同一租戶在兩個檔宣告）；或找不到 da-guard／da-guard 執行失敗 |

結束碼 2 時，附在 `ERROR` 行下面的每一行都以前綴 `da-guard|` 開頭（前面兩個空格、後面一個空格）（與正常結束時轉印的 stderr 相同）；其中出現的 `exit 3` 是 da-guard 自己的結束碼，本工具以 2 結束。

`walk_error` 不看目錄裡有沒有設定檔：`--config-dir` 底下任何一個執行身分列不出內容的子目錄（例如權限不足的 `docs/`，或 conf.d 剛好是 ext4 volume 根目錄、以非 root 執行時的 `lost+found`）都會讓本工具以 2 結束；以 `.` 開頭的目錄（例如 `.git`）不算，exporter 的載入本來就不進去。解法是把 `--config-dir` 指向不含該目錄的子路徑，或調整權限讓執行身分可以列出它。

---

#### maintenance-scheduler

評估 `_state_maintenance.recurring[]` 的排程，對目前落在維護窗口內的租戶，在 Alertmanager 建立 silence。

**用途**：自動化排程式維護窗口；設計成每 5 分鐘跑一次的 K8s CronJob。

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
| `--alertmanager <URL>` | Alertmanager base URL；給了才會真的建立 silence，不給只印報告 | （無） |
| `--pushgateway <URL>` | 把執行結果推到 Pushgateway（`--dry-run` 時不推） | （無） |
| `--dry-run` | 只印報告，不建立 silence | false |
| `--json-output` | 另在 stdout 印一行 `{"created", "skipped", "errors", "mode"}`；`mode` 是 `apply`、`dry-run` 或 `report-only`，後兩者的 `created` 是「會建立」的數量 | false |

**排程從哪裡讀**：每個租戶的 `_state_maintenance` 取 `da-guard effective`（tenant-api `/effective`）的答案，不再由 Python 重讀 YAML（[#2751](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2751)）：根目錄平台檔 `tenants:` 裡寫的、子目錄租戶檔裡寫的，與根目錄租戶檔一樣算；`_defaults.yaml` 頂層的 `_state_maintenance` 不算（exporter 也不繼承它）。⚠️ 行為變更：根目錄租戶檔若沒有 `tenants:`、把 `_state_maintenance` 寫在頂層，以前會當成以檔名為 id 的租戶排程，現在與 exporter 一樣不讀（stderr 印一行點名該檔的 WARN，結束碼 0）。需要 da-guard（映像內建；repo 內 `make da-guard-build`）；找不到 da-guard、exporter 讀不到某個檔、或 da-guard 失敗（含 `--config-dir` 裡沒有任何 `.yaml`、同一個租戶 id 寫在兩個檔裡的 `duplicate tenant ID`）時，不當成「沒有排程」，結束碼 2 並轉出 da-guard 的 stderr。⚠️ v2.9.0 映像仍以 Python 讀 conf.d，平台檔 `tenants:` 與子目錄裡的排程讀不到。 <!-- image-caveat: v2.9.0 -->

cron 一律以 **UTC** 解讀，指定時區的選項尚未實作；例如台北時間每天 02:00 要寫成 `0 18 * * *`。輸出也不是 silence YAML 檔，工具直接呼叫 Alertmanager API。

**輸出**

stderr 列出每個排程目前是否在窗口內，最後一行是摘要：實際建立時是 `Summary: N created, N skipped, N errors`；沒給 `--alertmanager` 時是 `Summary: N in window (report only …)`；帶 `--dry-run` 時是 `Summary: N would be created (dry run …)`。實際建立的 silence 以 `tenant="<tenant>"` 與 `alert_source=""` 比對，建立者是 `da-tools/maintenance-scheduler`，comment 是排程的 `reason`，結束時間是窗口結束；同一個窗口已有 silence、且涵蓋到窗口結束時記為 skipped；既有 silence 在窗口結束前就會到期時，工具把它延長到窗口結束（stderr 印 `Extended silence …`），這種延長記為 created。⚠️ `--dry-run` 不讀 Alertmanager 既有的 silence，所以已經存在的也算在「會建立」裡。v2.9.0 映像在這兩種情況下仍印 `N created`，也沒有 `mode` 欄位，實際上什麼都沒建立。 <!-- image-caveat: v2.9.0 -->

**範例**

```bash
da-tools maintenance-scheduler --config-dir ./conf.d --dry-run
da-tools maintenance-scheduler --config-dir ./conf.d --alertmanager http://alertmanager:9093
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功（已建立、已存在或不需要 silence） |
| `1` | 至少一個 silence 建立或延長失敗 |
| `2` | 呼叫端錯誤：`--config-dir` 不存在、有 recurring 排程卻沒裝 `croniter`，或底下有檔案讀不到（內容不是 UTF-8 或不是合法 YAML；訊息指名哪一檔，#1654） |

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
   ⚠️ 需要 `git` 在 PATH 上，且工作目錄是含 `conf.d/` 的目錄（工具以 cwd 為基準跑 `git diff HEAD~1 -- conf.d/`；客戶佈局下就是 repo 根）。容器內要把含 `conf.d/` 的目錄掛進來並用 `-w` 切到掛載點（例如 `-v $(pwd):/workspace -w /workspace`）。`ghcr.io/vencil/da-tools` 映像**沒有內建 git**，在該映像內跑這個模式時 `git diff` 跑不起來——請在裝有 git 的環境執行

2. **目錄比對模式**：`--config-dir <dir> --baseline <dir>`
   （比對兩個配置版本）
   兩棵樹都由 exporter 讀（`da-guard served-values`，需 da-guard；#2119），逐租戶、逐門檻 key 比 `/metrics` 實際送出的值：平台檔、`_defaults.yaml`、profile、子目錄裡的變更都算；租戶取自各檔的 `tenants:`，沒有 `tenants:` 的檔 exporter 不送、改它也不算。一邊沒送的 key 或租戶（關掉、新增、移除）那一邊記為無值。值比對整個 UTC 日：每個 key 的每一組新舊值報一列；不是整天都成立的，`window` 列出它成立的所有時段（UTC 的 `HH:MM-HH:MM` 清單）。Prometheus 回測不限於這些時段，一律跑整個 `--lookback`。severity 只決定由哪支告警比較這一列，觸發點只看值（[#3060](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3060)）：值或 severity 任一邊不同都是一列變更，兩邊 severity 不同時 `--json` 該列帶 `old_severity`／`new_severity`、文字與 Markdown 報告在值旁註明。只改 severity 到另一個有規則選的 severity（`70` → `70:critical`）照值回測（觸發點不變）；改到規則不選的 severity（`70:high`、`70:`）那一邊視同沒有門檻，判為告警被靜默（`Metric disabled — all alerts silenced`），改回來則是重新啟用。每一列由哪支告警比較，取自 served-values 對該 key 回報的 `/metrics` series（[#2750](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2750)），不看 key 名稱：severity 為 critical 的 row（`<base>` 在根目錄 `defaults:` 宣告時的 `<base>_critical`——即使租戶把 `<base>` 本身 `disable`——以及時段內覆寫成 `N:critical` 的 key）由 `<metric_key>_critical` 的告警比較，severity 為 warning 的 row 由 `<metric_key>` 的告警比較；其他 severity 的 row（例如 `60:high`、`60:Critical`——規則只選 `severity="warning"`／`"critical"`，大小寫也要一致）沒有告警比較：那一邊視同沒有門檻，兩邊都沒有規則選的 severity 時不回測（`severity '...' is selected by no threshold rule`，與 `--git-diff`／`--tenant` 同一個理由）——另外根目錄 `defaults:` 沒宣告 `<base>` 的 `<base>_critical` 會被當成 metric 為 `<x>_critical` 的 warning row 送出，沒有任何告警選這個 metric，所以不回測；舊拼法別名的 twin（`legacy_twin: true`）不讀，舊名底下的資料不算這個門檻的資料。一列的 row 若由比較不同 series 或不同方向的告警比較，一次回放代表不了兩者，該列不回測（`rows compared differently`）。告警的門檻是同一個 metric、同一個 severity 所有 row 的 `max by(tenant)`，帶維度的 row 也算在內：租戶（任一棵樹、一天中任何時段）也送了同 metric、同 severity 的帶維度 row 時，只改 base 的值不等於告警比的門檻，該列不回測（`dimensioned rows share this alert's threshold (max by tenant)`）。帶維度的 key（served-values 的 series 有 `dimensions`／`dimensions_regex`，例如 `metric{db="a"}`）照列，但不查 Prometheus，標為 `not_backtested`（`backtest: skipped: dimensioned key`）。文字與 Markdown 報告列出未回測幾筆、各為什麼。

**回測查什麼**（[#3017](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3017)、[#3002](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3002)）

- 每筆變更只查一次 Prometheus：該 key 的告警拿來和門檻比較的那條 recording rule（[`metric_observed_map.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/metric_observed_map.yaml) 的 `observed_series`），加上 `{tenant="<租戶>"}`。越界照告警的比較方向計數：`>` 的告警數高於門檻的點，`<` 的告警（下界，例如 `db2_bufferpool_hit_ratio`、`kafka_broker_count`）數低於門檻的點。先前一律以 `>` 計數，並依序猜 `<key>`、`tenant:<key>:max`、`<key>{namespace=...}` 三種名稱——將近一半的 key，告警比的不是其中任何一條。
- `--git-diff`／`--tenant` 給的是檔裡寫的 key 與值：退役拼法先經別名表換成正名（例如 `mysql_cpu` → `mysql_threads_running`）。`--tenant` 給含 `{` 的 key 時不回測（`dimensioned key`）；⚠️ `--git-diff` 的 diff 解析根本看不到帶維度（加引號）的 key，只改這種 key 的 PR 會得到「No threshold changes found.」、結束碼 0。值照 exporter 的讀法：第一個 `:` 前是數字、後是 severity（`70:critical`；沒寫是 warning），`<base>_critical` key 的整個值就是數字。每一邊由它自己 severity 的告警比（critical 查 `<key>_critical` 的告警；`70` → `50:critical` 由兩支告警比，兩支比同一條 series、同一方向時照值回測，兩邊 severity 不同時 `--json` 該列帶 `old_severity`／`new_severity`、報告在值旁註明，[#3060](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3060)），severity 不是 warning／critical 的那一邊視同沒有門檻；兩邊都沒有規則選的 severity，或值這裡無法照 exporter 的讀法讀（`7O` 這種 exporter 也讀不懂的值，exporter 會改用預設值或丟掉那一列，從值本身看不出來；另有幾種 exporter 讀得懂、但這裡保守不讀的寫法，例如 `1_000`、`0x1p4`）時不回測。
- 下列變更標為 `not_backtested`（風險 `UNKNOWN`、不影響結束碼、不查 Prometheus），`backtest` 為 `skipped: <理由>`、訊息為 `Not backtested: <理由>`：observed-map 沒有該 key（沒有出廠告警比較它；`container_cpu` 等 KNOWN_DEFERRED key 的告警比的是 observed-map 不收錄的 version-aware recording rule，理由另寫）；條目無法回放（待人工確認、複合告警沒有單一 series、方向未解析、不是 per-tenant series）；一列的 row 由比較不同 series 或不同方向的告警比較（三種模式皆然，`--git-diff`／`--tenant` 的 `70` → `50:critical` 也由兩支告警比）。理由講的若不是該列自己的 key（critical row 的 `_critical` 告警），前面冠上那個 key。
- ⚠️ 已知近似：回放的是告警的比較運算元，不是告警本身——`for:`、維護期過濾、複合 AND 告警的其他條件都不模擬，所以計出的是「比較成立的點數」，不等於告警實際觸發的次數。`--git-diff`／`--tenant` 看不到租戶有沒有同 metric 的帶維度 row，門檻的 `max by(tenant)` 在這兩種模式下也不模擬；`<base>` 有沒有在根目錄 `defaults:` 宣告同樣看不到。沒有 `_critical` 告警的 key（observed-map 沒有 `<key>_critical` 條目，例如 mongodb）改成 `N:critical` 時不回測（理由冠上 `<key>_critical`），不會判成告警被靜默。

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--lookback <DURATION>` | 歷史回測期間，格式為 `<數字><d\|h\|m>`（如 `7d`／`24h`）。不符合格式的值（含裸數字 `7`、空字串）⇒ 結束碼 2 並列出接受的格式（#1625；之前會被靜默當成 `7d`） | `7d` |
| `--output <FILE>` | 輸出至 JSON 或 CSV | stdout |

**輸出**

對比報告，顯示各項 threshold 變更在歷史數據上的影響（可能增加/減少的 alert）。

觸發比例的分母是回看期間的全部樣本點，NaN 也算在內：NaN 那段時間告警不會響，比例代表的是「佔整段回看時間多少」。每筆 `analyzed`／`no_data` 結果帶 `query` 欄位，即實際送出的 PromQL；文字報告在訊息下一行印 `query: ...`，Markdown 表格多一欄 `Query`（`not_backtested` 沒有查詢，該欄為 `—`）。

查不到任何資料，或查到的樣本全是 NaN，該變更都標為 `no_data`（風險 `UNKNOWN`，不影響結束碼），前者訊息為 `No historical data found in Prometheus`，後者為 `Only NaN samples in lookback window`（[#2963](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2963)；先前全 NaN 會被報成已分析）。只查一次，所以沒有別的查詢可換。`+Inf`／`-Inf` 是真實樣本，照常比較。

Prometheus 連得上、但查詢本身失敗（HTTP 400／503、逾時、回 `error` 狀態），該變更標為 `query_error`（風險 `UNKNOWN`），訊息為 `Prometheus query failed: <錯誤說明>`（HTTP 錯誤時是狀態行，例如 `HTTP Error 400: Bad Request`，不含 Prometheus 回應 body 裡的 `error`）（控制字元換成 `?`、過長截斷），其他變更照跑（[#3059](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3059)；先前被報成 `no_data`、結束碼 0）。報告多一個計數 `query_error`；文字報告印 `Query error: N`，Markdown 註明幾筆沒量成。有任何一筆 `query_error` 時，`--json` 頂層帶 `status: "partial"`、`reason: "query_error"`（全部量成時不帶 `status`，與先前相同），結束碼為 2。

`-o/--output` 與 `--markdown-output` 每一條結束路徑都會寫（[#3089](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3089)），例外如下：在檢查輸出路徑之前就結束的參數錯誤（`--lookback` 不可用、`--tenant` 缺參數或不合法、沒有變更來源），以及輸出路徑本身寫不進去；另外，未捕捉的例外（rc 2，[#3090](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3090)）與通過檢查之後才發生的寫入失敗也不保證寫出。結束碼為 2 時不要讀輸出檔：其中可能是上一次執行留下的內容。`-o` 與 `--markdown-output` 指向同一個檔時，後寫的 Markdown 會蓋掉 JSON。`-o` 寫的是這次 `--json` 會印的同一份 JSON，也就是完整報告，或是 `skipped`／`no_changes`／`caller_error` envelope。沒有回測時，`--markdown-output` 寫一段簡短說明，標題與完整報告相同，內文為 `Backtest not run: <原因>`，原因是 Prometheus 不可用、沒有門檻變更，或 ``caller error `<reason>` ``；有 custom-alert recipe 租戶時，照舊在後面接上 recipe 區塊。先前只有完整報告那條路徑會寫 `-o`；`--skip-if-unavailable` 與沒有變更這兩條，只在有 recipe 租戶時才寫 markdown。CI 以檔案是否非空決定要不要更新 PR 留言，所以留言會停在上一次的結果。

**範例**

```bash
# Git Diff 模式——映像沒有 git，請在主機 checkout 上直接跑腳本
#（與本 repo 的 .github/workflows/backtest.yaml 同一組呼叫），
# 在含 conf.d/ 的目錄執行
python3 scripts/tools/ops/backtest_threshold.py --git-diff \
  --prometheus http://prometheus.monitoring.svc.cluster.local:9090 \
  --lookback 7d --skip-if-unavailable

# 目錄比對模式
da-tools backtest --config-dir ./conf.d-new --baseline ./conf.d-old --lookback 7d
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | 至少一項門檻變更被評為 HIGH 風險（合併前先審閱），且沒有任何一筆 `query_error`；Prometheus 連不上、git 跑不了、有變更查詢失敗都不是 1，見下列 |
| `2` | 呼叫端錯誤：Prometheus 連不上且沒帶 `--skip-if-unavailable`；`--lookback` 供了但不可用（不符合 `<數字><d\|h\|m>`，#1625）；`--git-diff` 供了但 git 跑不了（沒裝 git、不在 git work tree 內、沒有 HEAD~1）——⛔ 不要改用 `--config-dir` 轉綠，那比的是兩棵樹、不是你的 PR；`-o/--output`／`--markdown-output` 指到的輸出路徑寫不進去（#1641）——參數檢查之後、讀 git／conf.d／observed-map 與查 Prometheus 之前先檢查（父目錄不存在、不是目錄或不可寫，或路徑本身是目錄、是不可寫的檔），帶 `--skip-if-unavailable`、沒有門檻變更時也一樣（[#3089](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3089)；先前這兩種情況回 0 且不寫檔），`--json` 的 envelope `reason` 為 `output_unwritable`；conf.d 檔案內容讀不到（不是 UTF-8 或不是合法 YAML；訊息指名哪一檔，#1654）；目錄比對模式下找不到 da-guard、da-guard 失敗或過舊、任一棵樹有 exporter 讀不了的檔（帶 `--skip-if-unavailable` 也一樣，#2119）；observed-map（`metric_observed_map.yaml`）缺檔、讀不了、沒有 key 或有型別不對的條目（帶 `--skip-if-unavailable` 也一樣；`--json` 的 envelope `reason` 為 `observed_map_unavailable`，#3017）；`--tenant` 缺 `--metric` 或缺新舊值、沒有指定變更來源（帶 `--skip-if-unavailable` 也一樣，#3059）；`--tenant` 不是合法的租戶 id（ADR-035 的 DNS-1123 label：小寫英數與 `-`，1–63 字元，首尾須為英數；帶 `--skip-if-unavailable` 也一樣，[#3066](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3066)——先前 `a"b` 會組出不合法的 PromQL、回 0）；任何一筆變更 `query_error`——優先於 HIGH 的 1，帶 `--skip-if-unavailable` 也一樣，因為 Prometheus 連得上（#3059）。`--json` 下這三種參數錯與 Prometheus 連不上都輸出 `caller_error` envelope，`reason` 依序為 `tenant_args_missing`、`no_change_source`、`invalid_tenant_id`、`prometheus_unavailable`；`query_error` 輸出完整報告（`status: "partial"`） |

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
| `1` | 一項或多項檢查失敗（`--mapping` 指到不存在的檔也算，列為 FAIL，不是呼叫端錯誤） |
| `2` | 呼叫端錯誤：`preflight`（含 `all`）的 Prometheus 連不上或查詢失敗（該項檢查同樣列為 FAIL，但結束碼是 2 不是 1）、`runtime`（含 `all`）讀 `--report-csv` 時 I/O 錯誤（單獨跑 `convergence` 會略過這個錯誤），或 argparse 拒絕的參數。⚠️ `--report-csv` 指到不存在的檔**不是** 2——CSV 分析直接略過。⚠️ 單獨執行 `runtime` 時 Prometheus 連不上**不是** 2 也不是 1：兩項查詢失敗時不產生任何檢查項，結果是 `Overall: PASS`、結束碼 0 |

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
| `2` | 呼叫端錯誤：Prometheus 或 Alertmanager 連不上、query／rules／status API 呼叫失敗（該項檢查同樣列為 FAIL，但結束碼是 2 不是 1），或 argparse 拒絕的參數 |

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
| `2` | 呼叫端錯誤：`e2e` 沒帶 `--edge-urls`、target 不是 edge／central／e2e；edge／central Prometheus 連不上或 config／query／rules API 失敗（該項檢查同樣列為 FAIL，但結束碼是 2 不是 1） |

---

#### fed-key

產生 / 輪替 federation JWT 簽章金鑰（ADR-020 IV-2l）。

**用途**：tenant-api 用 RS256 私鑰簽 federation token，federation-gateway 用對應公鑰的 JWKS 驗章。本工具產 RSA keypair，並輸出兩個下游 artifact：私鑰 → Kubernetes Secret manifest（stdout，不落地）；公鑰 → JWKS 檔。每把公鑰的 `kid` 是它的 RFC 7638 JWK thumbprint，與 tenant-api 端算出的 `kid` header 天然一致。

**語法**

```bash
da-tools fed-key [options]
```

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--rotate` | 輪替模式：把新公鑰併入 `--existing-jwks`（舊+新並存） | false |
| `--existing-jwks <PATH>` | 要併入的現有 JWKS 檔（`--rotate` 必需） | （無） |
| `--jwks-out <PATH>` | JWKS 輸出路徑 | `./federation-jwks.json` |
| `--namespace <NS>` | Secret manifest 的 namespace | `monitoring` |
| `--secret-name <NAME>` | Secret 名稱 | `tenant-federation-signing-key` |
| `--secret-key <KEY>` | Secret 內的 data key | `federation-signing-key.pem` |
| `--key-bits <N>` | RSA modulus 大小（tenant-api 拒收 < 2048） | `2048` |

**範例**

```bash
# 首次 bootstrap：私鑰直接套用，JWKS 寫到 ./federation-jwks.json
da-tools fed-key | kubectl apply -f -

# 計畫性輪替：新公鑰併入現有 JWKS（輪替順序見 key-rotation runbook）
da-tools fed-key --rotate --existing-jwks federation-jwks.json \
  --jwks-out federation-jwks.json > new-signing-key.secret.yaml
```

> 完整輪替 / 緊急汰換流程見 [`federation-key-rotation-runbook.md`](internal/federation-key-rotation-runbook.md)。

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 金鑰已產生 |
| `1` | 只有未捕捉例外（traceback）會回 1——本命令沒有 violation 出口；實測 `--existing-jwks` 指到一份 JSON **陣列**（頂層不是物件）時是這一格。⛔ 輸出路徑寫不進去自 #1789 起是 `2`，不再落在這裡 | <!-- datools-cmd-ignore: 只有 traceback 回 1，沒有出口可追 -->
| `2` | 呼叫端錯誤：`--jwks-out` 寫不進去（實測：目錄不存在）——一行 `ERROR: cannot …` 指名 `--jwks-out`，不再是 traceback（#1789）；`openssl` 不在 PATH、逾時或失敗；`--existing-jwks` 讀不到、不是 JWKS 文件（沒有 `keys` 陣列）或已含同一個 kid；`--rotate` 沒帶 `--existing-jwks`、`--key-bits` < 2048；stdout 是終端機（拒絕把私鑰 Secret 印到 tty，請接 `\| kubectl apply -f -`） |

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
| `1` | `--verify` 發現問題（ConfigMap 內的 dashboard JSON 無效）。⚠️ 匯入模式的失敗全是 2 不是 1；`kubectl` 不在 PATH 是未捕捉例外（匯入與 `--verify` 皆 traceback、rc 1） |
| `2` | 呼叫端錯誤：`--dashboard` 檔不存在或不是合法 JSON、`--dashboard-dir` 不存在或裡面沒有 `*.json`；匯入時 `kubectl create／apply／label` 失敗；`--verify` 時 `kubectl get` 失敗或輸出解析不了；三個模式旗標一個都沒給 |

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
| `2` | 呼叫端錯誤：`--period` 解析不出、`--tenant` 含英數／底線／連字號以外的字元、缺必需的 `--prometheus`，或 argparse 拒絕的參數；以及 Prometheus 的 `ALERTS` 查詢失敗（連不上、HTTP 非 2xx、`status` 非 success、回應無法解析——HTTP 200 但非 JSON 或非物件，例如 proxy 回 HTML；不論有無 `--ci`，#3065，之前當成沒有資料、rc 0）——此時不印報告，stderr 帶錯誤原文；`--json` 印 `{"status": "caller_error", "reason": "prometheus_query_error", "error": "<原文>", ...}`（其餘鍵沿用正常報告並歸零）。`da-tools alert-quality --prometheus <URL> --json \| jq '.status // "ok"'` 可分辨 |

---

#### alert-correlate

分析 Alertmanager 告警並進行時間窗口聚類，計算關聯分數並推斷根因。支援線上（Prometheus API）和離線（JSON 檔案）兩種模式。

**用法**

```bash
da-tools alert-correlate --prometheus <URL> [--window <DURATION>] [--lookback <DURATION>] [--min-score <FLOAT>] [--json] [--markdown] [--ci]
da-tools alert-correlate --input <FILE> [--window <DURATION>] [--min-score <FLOAT>] [--json]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--prometheus <URL>` | Prometheus 端點（線上模式） | `$PROMETHEUS_URL` |
| `--input <FILE>` | Alertmanager JSON 檔案（離線模式） | — |
| `--window <DURATION>` | 時間窗口大小（Prometheus duration，如 `5m`）。⚠️ 純數字**解不出來**（`10` → `None`），必須帶單位 | `5m` |
| `--lookback <DURATION>` | 回溯時間範圍 | `24h` |
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
| `1` | CI 模式：存在含 critical 告警的群組（群組至少 2 筆告警；只有一筆 critical 告警時是 0） |
| `2` | 呼叫端錯誤：`--window` 解析不出或 ≤ 0，或 argparse 拒絕的參數。⚠️ Alertmanager／Prometheus 連不上**不是** 2——印 WARN 後以零告警繼續、rc 0；`--input` 指到不存在的檔是未捕捉例外（traceback、rc 1） |

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
| `2` | 呼叫端錯誤：`--dirs` 任一目錄不存在、configmap 模式少於 2 個目錄、operator 模式不是恰好 1 個目錄、`--labels` 數量與 `--dirs` 不符；operator 模式下 `kubectl` 不在 PATH、逾時（30s）、非零結束或輸出不是 JSON；argparse 拒絕的參數 |

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
| `--lookback` | 回溯期間，格式為 `<數字><d\|h\|m\|s>`（如 `30d`／`4h`）。不符合格式或不是正數的值（含裸數字 `30`、空字串、`0s`）⇒ 結束碼 2 並列出接受的格式（#1625；之前會被靜默當成 `30d`） | `30d` |
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

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功（CI 模式：沒有 critical 風險） |
| `1` | CI 模式：有 critical 風險 |
| `2` | 呼叫端錯誤：參數錯誤；或取不到基數資料。`--json` 的 `reason` 分得開兩種：Prometheus 查詢失敗（連不上、HTTP 非 2xx、`status` 非 success、回應無法解析——HTTP 200 但非 JSON 或非物件，例如 proxy 回 HTML）⇒ `"status": "caller_error"`、`"reason": "prometheus_query_error"`，並帶 `"error": "<原文>"`（#3065，之前與下一種同為 `"status": "no_data"`、`no_cardinality_data`）；查詢成功但沒有任何 `tenant_threshold_*` series ⇒ `"status": "no_data"`、`"reason": "no_cardinality_data"`。`da-tools cardinality-forecast --prometheus <URL> --json \| jq -r '.reason // "ok"'` |

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

在 `conf.d/` 裡，init 只擁有**根目錄**的 `_defaults.yaml` 與 `<tenant>.yaml` 這兩種路徑。某個要求的租戶若**可能**已由你**其他檔案**宣告，init **跳過該租戶、不動那個檔**，並在 stderr 與摘要列出（例如「db-c 可能已由 conf.d/team.yaml 宣告，未產生 conf.d/db-c.yaml」），rc 仍為 0；根目錄已有其他拼法的 defaults 載體（如 `_defaults.yml`）時同樣不寫 `_defaults.yaml`。「可能宣告」依**內容**判斷、不看檔名，而且刻意寬鬆：檔中 `tenants:` 的 key、以及租戶 id 以獨立 token 出現在檔中任何位置（含引號內、註解、flow／JSON 寫法、UTF-16 等編碼、YAML escape 寫法如 `"db\x2dc"`）都算；讀不到、解不開或含明確 tag（`!!binary` 等）的檔視為可能提及每一個要求的租戶，跳過訊息會寫出原因與怎麼讓 init 讀得透。⚠️ 註解與字串裡以 `!` 開頭的字（例如 `# !Important`、`"wow !!"`）也會被當成 tag，使該檔被視為提及所有租戶——這是刻意接受的誤判。這種「讀不透」不會觸發下述並存拒絕：若 init 自有的 `conf.d/<t>.yaml` 已存在**且本身宣告 t**，init 照常重寫它並點名該檔請你用 guard 確認；自有檔存在但沒有宣告 t（例如佔位檔）時則視同沒有自有檔，跳過 t、不改寫它。⚠️ **init 不檢查 exporter 能否讀取那個檔**——被跳過的租戶是否真的有宣告，請用 [`da-tools guard defaults-impact --config-dir <conf.d>`](#guard) 確認（它以 exporter 的讀法掃描整棵樹，重複宣告會直接報錯；該檔應出現在報告的 Scanned files 裡）。沒有提到要求租戶的客戶檔，init 不做任何評論（init 不是 validator）。以下情形 init **拒絕執行、rc 1、不寫入任何檔案**（含 `.da-init.yaml`），`--dry-run` 亦同：init 自己的 `conf.d/<t>.yaml` 已存在、而另一個檔**具體**提到了 t（`tenants:` 的 key 或租戶 id token；例如 `db-c.yaml` 與 `db-c.yml`；exporter 對同一租戶的兩份宣告會拒收整棵樹，該留哪一份要由你決定）；`_defaults.yaml` 與其他拼法的預設載體並存；init 要覆寫的 `conf.d/<t>.yaml` 可能也宣告了這次要求的另一個租戶（覆寫後那個租戶將無處宣告）；init 要覆寫的 `conf.d/<t>.yaml` 還宣告了這次**沒要求**的租戶（例如 `db-a.yaml` 宣告 db-a 與 db-z、只跑 `--tenants db-a`：重寫會讓 db-z 的宣告消失；請把 db-z 移到自己的檔如 `conf.d/db-z.yaml`，或把 db-z 也加進 `--tenants`），或 init 列不出那個檔宣告的所有租戶（PyYAML 讀不了它）——`--force` 也一樣拒絕；init 要寫的路徑在輸出目錄以下的**任何一層**已有只差大小寫的既有項目（例如 `Conf.D/` 之於 `conf.d/`，在不分大小寫的檔案系統上是同一個路徑）。

⚠️ `--deploy kustomize` 時，被跳過的租戶若載體不在 `conf.d` 根目錄（例如 `conf.d/prod/db-c.yaml`），它**不會**進入產生的 ConfigMap（`configMapGenerator.files` 是扁平的；`make configmap-assemble` 同樣只收頂層檔，見 [GitOps 部署 §3](integration/gitops-deployment.md#3-configmap-assembly)），init 會在 stderr 與摘要 WARN 點名，rc 仍為 0。

```bash
da-tools init [--ci <github|gitlab|both>] [--tenants <list>] [--rule-packs <list>] [--deploy <kustomize|helm>] [-o <dir>] [--non-interactive] [--dry-run] [--force]
```

**參數**

| 參數 | 說明 | 預設 |
|------|------|------|
| `--ci` | CI/CD 平台 | `both` |
| `--tenants` | 逗號分隔的租戶名稱。給了 `--ci` / `--rule-packs` / `--deploy` 卻沒給 `--tenants` 時：沒帶 `--non-interactive` 且 stdin 是終端機，就**只補問租戶名稱**（無預設值，其餘旗標照給的用；回答與 `--tenants` 同樣切分，`a,,b` 兩邊都拒絕）；帶了 `--non-interactive`，或不是終端機（CI、腳本），則 **rc 2、不寫入任何檔案**，`--dry-run` 亦同。`--tenants` 給了但沒有任何名稱（`''`、`' , '`）視同未給。init 不會再自行補上範例租戶 | 無；完全不帶旗標的互動模式提示預設為 `db-a,db-b` |
| `--rule-packs` | 逗號分隔的 Rule Pack | `mariadb,kubernetes`（互動模式） |
| `--deploy` | 部署方式 | `kustomize` |
| `--non-interactive` | 跳過互動提示（需搭配 `--tenants`） | — |
| `--dry-run` | 顯示會產生的檔案但不寫入 | — |
| `--force` | 在已初始化的目錄重跑：**重寫所有產生的檔案**，含 `conf.d/_defaults.yaml` 與每一份 `conf.d/<tenant>.yaml`（手動調整會遺失）。⚠️ **例外：不會重寫已存在的根目錄 `.gitlab-ci.yml`** —— 那可能是客戶自己的 pipeline，因此任何情況下都不覆寫（也就沒有工具內的重生路徑）；⚠️ 自有檔若還宣告了這次沒要求的租戶，`--force` 同樣拒絕（見上方）；⚠️ **也不會改寫 conf.d 裡 init 以外的載體**：可能已由你其他檔案宣告的租戶／其他拼法的 defaults 照樣跳過並列出，並存時照樣拒絕（見上方說明） | — |

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
da-tools gitops-check local --dir conf.d

# 檢查 sidecar 部署
da-tools gitops-check sidecar --namespace monitoring --json
```

---

#### state-reconcile

遷移狀態目錄聲明式一致化——掃描 `.da/state/*.json` 驗證 `schema_version` 並從檔案系統重建 `.da/manifest.json`。取代 [troubleshooting-checklist §schema_version drift / manifest drift](integration/troubleshooting-checklist.md) 中的手動 jq 流程（[issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405) Category A）。

```bash
da-tools state-reconcile [options]
```

**主要參數**

| 參數 | 用途 | 預設 |
|------|------|------|
| `--state-dir <dir>` | 含 per-cluster state 檔的目錄 | `.da/state` |
| `--manifest-path <path>` | manifest 檔路徑 | `.da/manifest.json` |
| `--dry-run` | 只報告需改動，不寫入 | 無 |
| `--ci` | **配合 `--dry-run` 用**：check-only CI gate，dry-run 偵測到需改動時 exit 1。Unresolvable drift 永遠 exit 1（不需 `--ci`）。單獨用 `--ci`（無 `--dry-run`）仍會 apply changes | 無 |
| `--json` | 輸出 JSON 結構化報告 | 文字模式 |

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | state 目錄一致（或已成功套用變更） |
| `1` | 有 unresolvable schema drift（含 state 檔讀不到或缺 `schema_version`）；或 `--ci` 搭配 `--dry-run` 偵測到需改動 |
| `2` | 呼叫端錯誤：argparse 拒絕的參數（未知旗標等）。⚠️ `--state-dir` 不存在**不是** 2——視為空目錄（文字模式印警告，`--json` 只在報告標 `state_dir_missing: true`；重建 0 筆的 manifest、rc 0；`--ci --dry-run` 下因需重建而 1） |

**為什麼是 single declarative command 而非 micro-commands**

schema migration（state 檔升版）與 manifest 重建（從 filesystem 派生）是同類「修復 state 目錄到一致態」問題；分成兩支命令會強迫使用者記住執行順序。本工具一句話「make .da/ consistent」涵蓋兩者。Schema migration 走 `MIGRATIONS` registry（v1.0 尚無遷移；v1.1 引入時加註冊即可）。

**範例**

```bash
# 基本：偵測並修復
da-tools state-reconcile

# 自訂位置
da-tools state-reconcile --state-dir custom/states/ --manifest-path custom/manifest.json

# Dry-run 看會改什麼
da-tools state-reconcile --dry-run

# CI gate：偵測到漂移就 exit 1
da-tools state-reconcile --ci --dry-run

# 自動化讀取
da-tools state-reconcile --json
```

---

#### rule-pack-diff

Rule Pack 兩版本機械比對——掃 `groups[*].rules[*]` 的 `alert` / `record` entries，分類為 added / removed / modified / breaking label schema。取代 [staged-adoption-guide §7.3](scenarios/staged-adoption-guide.md) 中的「人工對照 CHANGELOG + git diff」流程（[issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405) Category D）。

```bash
da-tools rule-pack-diff --from <v1.yaml> --to <v2.yaml> [options]
```

**主要參數**

| 參數 | 用途 |
|------|------|
| `--from <path>` | 舊版本 Rule Pack YAML 路徑 |
| `--to <path>` | 新版本 Rule Pack YAML 路徑 |
| `--json` | 輸出 JSON 結構化報告 |
| `--ci` | breaking changes 偵測到時 exit 1（CI gate）—— breaking 定義：alert 被移除、label key 增減、label value 變動、alert↔record kind 互換 |

**Breaking 判定 vs 不 breaking（informational）**

| 變動 | 是否 breaking |
|---|---|
| Alert 移除 / 改名 | ⚠️ breaking（silencer matcher on alertname 失效） |
| Label key 增 / 減 | ⚠️ breaking（matcher 上的 key 不存在 / 多出來） |
| Label value 變（如 `severity: warning → critical`） | ⚠️ breaking（嚴格相等 matcher 失效） |
| Alert ↔ record kind 互換 | ⚠️ breaking（silencer 對 recording rule 無效） |
| PromQL `expr` 變 | 📝 informational（語意等價無法自動判定） |
| Annotation 變 | 📝 informational |
| `for:` duration 變 | 📝 informational |

**典型用法**

```bash
# 從 git 抽兩個版本比對
git show v1.0.0:rule-packs/rule-pack-mariadb.yaml > v1.yaml
git show v2.0.0:rule-packs/rule-pack-mariadb.yaml > v2.yaml
da-tools rule-pack-diff --from v1.yaml --to v2.yaml

# CI gate：breaking 偵測到擋 merge
da-tools rule-pack-diff --from v1.yaml --to v2.yaml --ci

# 自動化讀取
da-tools rule-pack-diff --from v1.yaml --to v2.yaml --json
```

**Exit codes**

| Code | 含義 |
|------|------|
| 0 | 比對完成（無 breaking changes；或無 `--ci` 即便有 breaking 也 0） |
| 1 | `--ci` 模式偵測到 breaking changes |
| 2 | caller error（檔案不存在 / 解析失敗 / 參數錯） |

---

#### silencer-drift-check

Alertmanager silence 對 v2 rule pack 漂移偵測——吃 `amtool silence query -o json` dump + rule pack source，列出**已沒有任何 v2 alert 命中所有 matchers** 的 silence。取代 [troubleshooting-checklist §1.3.2](integration/troubleshooting-checklist.md) 的手動 `jq + comm` 比對流程（[issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405) Category B）。

**Offline-first 設計**：工具**不**直接連 AM——避免 VPN / Ingress / Auth-proxy 邊界踩雷，可在 CI / GitHub Action 等無 AM 網路連線環境直接跑。

```bash
da-tools silencer-drift-check --silences-file <silences.json> --rule-source <path> [options]
```

**主要參數**

| 參數 | 用途 |
|------|------|
| `--silences-file <path>` | `amtool silence query -o json` 輸出的 JSON 檔（必須） |
| `--rule-source <path>` | Rule pack YAML 檔或目錄（遞迴掃 `*.yaml`/`*.yml`；無 `groups:` root 的檔案 silently 跳過，如 `_defaults.yaml`） |
| `--include-inactive` | 包含 expired / pending silence。預設只檢 active 那批 |
| `--json` | 輸出 JSON 結構化報告 |
| `--ci` | 偵測到 orphan silence 時 exit 1（CI gate） |

**Matcher 完整語意**

工具實作 AM matcher 的 4 種組合（不是只比 `alertname=`）：

| `isEqual` | `isRegex` | 語意 |
|---|---|---|
| true | false | `label == value` |
| false | false | `label != value` |
| true | true | `label =~ value`（Go regex，**fullmatch**） |
| false | true | `label !~ value` |

Silence 命中 alert 的條件：**所有 matchers 都對該 alert 的 label set 命中**。Label set 包含 implicit `alertname=<name>` + rule 的 `labels:` block。**Absent label 視為空字串**（AM 慣例）。

**命中面回報**

silence 有兩個相反的失效方向：orphan 命中零條、什麼都壓不到；而過寬的 matcher（掉了一個 label、留了一個 `.*`）命中遠超預期、壓掉沒人打算靜音的告警。orphan 偵測只看得見前者，所以報告同時列出每條 silence 實際命中的面：

| 輸出 | 內容 |
|------|------|
| 文字「Match coverage」段落 | 每條 in-scope silence 命中幾條規則，依命中數由多到少排序；名單預覽上限 3 個並明寫 `+N more` |
| `--json` 的 `coverage[]` | 每條 in-scope silence 一筆，四個欄位：`silence_id`、原樣帶出的 `matchers`（判定依據，不必回頭比對輸入檔）、`matched_rules`（rule 定義數）、去重後的 `matched_alertnames`（**不截斷**） |
| 摘要行 / `counts.widest_match` | 所有 in-scope silence 中最大的命中數 |

⚠️ `matched_rules` 是「rule 定義數」而非「當下 firing 的 alert 數」——後者需要 live Prometheus / Alertmanager 狀態，本工具刻意 offline-first。

文字輸出中所有取自檔案的字串（alertname、silence id、matcher 值、comment、author）都會把控制字元轉義成 `\x1b` / `\u202e` 形式再印。rule pack 路徑由 `--rule-source` 指定（可含租戶自助 Custom Alerts）、silence dump 來自任何有 Alertmanager 寫入權的人，內含裸 ESC / CR 就能改寫終端機或 CI log 上那一行、讓某個名字**顯示成另一個名字**。轉義只發生在文字 renderer；`--json` 保持原字串（`json.dumps` 本來就會無損轉義），機器可讀的那份不被動過。

**Exit codes**

| Code | 含義 |
|------|------|
| 0 | 無 orphan silence（或有 orphan 但無 `--ci`） |
| 1 | `--ci` 模式偵測到 orphan 或 malformed silence |
| 2 | caller error（檔案不存在、JSON parse 失敗、`--rule-source` 空目錄等） |

**典型用法**

```bash
# Step 1: 在有 AM 連線的環境抓 silences
amtool silence query -o json --alertmanager.url=http://<am>:9093 > silences.json

# Step 2: 離線比對（不需 AM 連線）
da-tools silencer-drift-check --silences-file silences.json --rule-source rule-packs/

# CI gate：merge 前擋住會 silently miss 的 silence
da-tools silencer-drift-check --silences-file silences.json --rule-source rule-packs/ --ci

# 自動化讀取
da-tools silencer-drift-check --silences-file silences.json --rule-source rule-packs/ --json
```

⛔ **不要把 `--ci` 接到線上 silence dump 上**（上面的 CI gate 用法針對的是 cutover 時人工抓下來的那一份）。實測 2026-08-27：`rule-packs/` 的 122 條 alert **全部帶 `tenant:` label 且全為 templated（0 個字面值）**，而 `maintenance_scheduler.py` 建立的每條 silence 都帶字面 `tenant=<id>` matcher ⇒ 平台自己排程建立的 silence 全部落在既有的 templated-label 假陽性裡（見工具 docstring 的 Known limitations），那道閘門會對**每一個排程維護窗**轉紅。

#### migration-audit

遷移稽核（[ADR-036](adr/036-single-parser-effective-config.md) §8，[#2898](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2898)）。conf.d 的 YAML 之後改由 Go（yaml.v3，`da-guard parse-tree`）讀；切換前用這支在自己的樹上看清楚哪些地方會變。同一棵 conf.d 讀兩次——今天路由產生器的讀法（PyYAML，同一套選檔與 loader）、`da-guard parse-tree`——比對後**只列差異，不做判定**。

```bash
da-tools migration-audit --config-dir <dir> [--json] [--da-guard-binary <path>]
```

**主要參數**

| 參數 | 用途 |
|------|------|
| `--config-dir <dir>` | conf.d 根目錄（必須） |
| `--json` | 輸出一份 JSON（schema `da-tools.migration-audit/v1`）到 stdout |
| `--da-guard-binary <path>` | da-guard 路徑；未給則依序找 `$DA_GUARD_BINARY`、`$PATH` 上的 `da-guard`（da-tools 映像已內建） |
| `--timeout <秒>` | da-guard 執行上限（預設 600） |

**列出什麼**

| 清單（`--json` 的鍵） | 內容 |
|------|------|
| `values` | 兩邊讀成不同的值：檔案、行號、路徑、兩邊的讀法、建議寫法（例如 `on` 要布林就寫 `true`、要字串就寫 `"on"`）。`rewrites` 逐一列出可替換的寫法（`meaning`、`text`），`span` 是值寫在哪一行哪幾欄（含型別標記，不含錨點）；每一種寫法照做之後兩邊都讀成一樣，不會給有損的寫法（超出 int64 的整數、`tenants:` 清單裡的項目只給加引號的字串；非有限數只給 `.inf`／`.nan`） |
| `keys` | 只有一邊有的鍵 |
| `becomes_unreadable` | 今天讀得到、`parse-tree` 判為讀不了的檔，附 `kind`（13 種封閉清單之一） |
| `becomes_readable` | 今天整份拒收、`parse-tree` 讀得到的檔，附今天的錯誤與 yaml.v3 讀成的內容 |
| `selection` | 只有一邊會讀的檔 |
| `exporter_changes` | 刪除 vendored yaml.v3「非特定 tag」patch 後 exporter 讀法改變的兩處：`! "<<"` 不再是合併鍵、SLO `objective: ! "99.9"` 由拒收變接受。這兩種檔 `parse-tree` 也判為 `non_specific_tag`，所以同時列在 `becomes_unreadable` |
| `still_unreadable` | 兩邊都讀不了的檔（不是變化，不影響結束碼） |
| `files_compared` | 兩邊都讀得到、逐值比對過的檔（文字輸出在第二行列出），讓「沒有差異」與「什麼都沒比」分得開 |

行號取自 PyYAML 的讀法：值標在它寫的那一行，鍵標在鍵所在行；經別名或合併取得的值，標在錨點或被合併 mapping 的定義行——改那一行會同時影響所有引用處。只有 yaml.v3 有的鍵標在父層的鍵那一行。

政策檔的 `tenants: [010, 1e3, 0777]` 會列為差異（今天讀成原文 `"010"`、`"1e3"`、`"0777"`，yaml.v3 讀成 8、1000、511）：這是依 ADR-036 §2「語意層只依型別與換算後的值判斷」的預期結果，要原文就加引號。

**沒有 da-guard 時**：直接失敗（exit 2）並說明，不會退回自己解析 YAML。

**Exit codes**

| Code | 含義 |
|------|------|
| 0 | 兩邊讀法相同（`still_unreadable` 可能非空） |
| 1 | 列出了差異 |
| 2 | caller error（`--config-dir` 不存在或讀不了、找不到 da-guard、da-guard 失敗或輸出不是可讀的 parse-tree 文件）；`--json` 時 stdout 仍是一份 JSON，`status: "caller_error"` 並帶 `reason` |

```bash
da-tools migration-audit --config-dir conf.d/
da-tools migration-audit --config-dir conf.d/ --json | jq '.values[] | {file, line, suggestion}'
```

---

### Operator + Federation 工具

#### operator-generate

從 Rule Packs 與 Tenant 配置產出 Kubernetes Operator CRD（PrometheusRule、AlertmanagerConfig、ServiceMonitor）。

**用途**：Prometheus Operator 叢集中的動態告警規則與路由部署；GitOps 友好的 CRD YAML 產生。

**語法**

```bash
da-tools operator-generate [options]
```

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--rule-packs-dir <DIR>` | Rule Pack 目錄路徑 | `rule-packs/` |
| `--config-dir <DIR>` | 租戶配置目錄路徑 | `conf.d/` |
| `--output-dir <DIR>` | 把 CRD 寫進這個目錄。⚠️ **寫檔要同時滿足：給了本旗標 _且_ 沒帶 `--dry-run`**；任一不成立就改印到 **stdout**、一個檔也不寫 | 無 |
| `--namespace <NS>` | 目標 K8s namespace | `monitoring` |
| `--api-version <VER>` | AlertmanagerConfig API 版本（`v1alpha1` / `v1beta1`） | `v1beta1` |
| `--components <COMP>` | 要生成的元件（`all` / `rules` / `alertmanager` / `servicemonitor`） | `all` |
| `--receiver-template <TYPE>` | Receiver 模板類型（`slack` / `pagerduty` / `email` / `teams` / `opsgenie` / `webhook`） | — |
| `--secret-name <NAME>` | K8s Secret 名稱（receiver 機密引用），需搭配 `--receiver-template` | `da-{tenant}-{type}` |
| `--secret-key <KEY>` | K8s Secret 中的 key 名稱 | 依 receiver 類型自動推斷 |
| `--selector-label <KEY=VALUE>` | 加在 PrometheusRule 與 ServiceMonitor 上的 label（可重複；同 key 覆寫預設），讓 Prometheus 的 `ruleSelector`／`serviceMonitorSelector` 對得上。Helm release 不叫 `kube-prometheus-stack` 時用 `release=<名稱>` | PrometheusRule：`prometheus=kube-prometheus`、`release=kube-prometheus-stack`；ServiceMonitor：`release=kube-prometheus-stack` |
| `--gitops` | GitOps 模式（sorted keys、無 timestamps） | false |
| `--dry-run` | 列印輸出而不寫入檔案 | false |
| `--json` | 以 JSON 格式輸出結果報告 | false |
| `--kustomize` | 一併產生 `kustomization.yaml`。⚠️ 沒有 `--output-dir` 時它會**混進 stdout 串流**，而 `Kustomization` 不能被 `kubectl apply -f -` 接受 | false |

**範例**

```bash
# 基本：CRD 走 stdout（不寫檔），可直接 apply
da-tools operator-generate --rule-packs-dir rule-packs/ --config-dir conf.d/ | kubectl apply -f -

# 要寫成檔案就明確指定目錄（#1582：寫入是 opt-in）
da-tools operator-generate --rule-packs-dir rule-packs/ --config-dir conf.d/ --output-dir ./operator-crds

# GitOps 模式 + Slack receiver
da-tools operator-generate \
  --config-dir conf.d/ \
  --output-dir ./operator-crds \
  --receiver-template slack \
  --gitops

# PagerDuty + 自訂 Secret
da-tools operator-generate \
  --receiver-template pagerduty \
  --secret-name org-pd-secret \
  --secret-key routing-key

# 僅產出 AlertmanagerConfig
da-tools operator-generate --components alertmanager --receiver-template email

# Dry-run JSON 報告 — stdout 為單一 JSON 文件：
#   {"crds": [...], "kustomization": {...}|null, "summary": {...}}
# （進度／摘要訊息走 stderr，故可安全 pipe 進 jq）
da-tools operator-generate --dry-run --json | jq '.crds | length'
da-tools operator-generate --dry-run --json | jq -r '.crds[].metadata.name'
```

---

#### migrate-to-operator

讀取現有 ConfigMap 格式的 Prometheus 規則，產出等效的 CRD YAML 及遷移清單。

**用途**：從 ConfigMap 原生格式遷移至 Operator 原生 CRD；GitOps 友好的轉換工具；遷移前置檢查與預覽。

**語法**

```bash
da-tools migrate-to-operator [options]
```

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--source-dir <DIR>` | ConfigMap YAML 檔案所在目錄 | （必填） |
| `--config-dir <DIR>` | 租戶配置目錄 | `conf.d` |
| `--output-dir <DIR>` | 把 CRD 與 checklist 寫進這個目錄。⚠️ **寫檔要同時滿足：給了本旗標 _且_ 沒帶 `--dry-run` _且_ 沒帶 `--checklist-only`**（與下面 `--json` 信封同一組判別條件）；任一不成立就改印到 **stdout**、一個檔也不寫 | 無 |
| `--namespace <NS>` | 目標 K8s namespace | `monitoring` |
| `--receiver-template <TYPE>` | Receiver 類型（`slack` / `pagerduty` / `email` / `teams` / `opsgenie` / `webhook`） | — |
| `--secret-name <NAME>` | K8s Secret 名稱 | — |
| `--secret-key <KEY>` | K8s Secret 內的 key | — |
| `--dry-run` | 僅預覽，不寫入檔案 | false |
| `--checklist-only` | 僅產出遷移清單 | false |
| `--json` | JSON 輸出模式 | false |

> ⚠️ **`--json` 的 top-level 結構有三種，判別順序如下**（#1582，8 組合逐一實測）：
> 1. `--checklist-only` —— **壓過其他所有旗標**（含 `--dry-run` 與 `--output-dir`）⇒ **checklist 信封**（多 `checklist` / `status` 兩鍵，`prometheus_rules` 是**計數**）；
> 2. 否則 `--dry-run` **或**沒給 `--output-dir` ⇒ **預覽信封**（`metadata` / `errors`，`prometheus_rules` 是 **CRD 清單**）；
> 3. 否則（有 `--output-dir`、無 `--dry-run`、無 `--checklist-only`）⇒ **寫入報告**（`configmap_files` / `rule_groups` / `tenants` / `total_crds`，`prometheus_rules` 是**計數**）。
>
> ⛔ 三者鍵名重疊而型別不同。消費端要按上面的順序判，**不能只看 `--output-dir`**——`--output-dir DIR --dry-run` 給的是預覽信封，不是報告。

**範例**

```bash
# 基本：checklist 與 CRD 走 stdout（不寫檔）
da-tools migrate-to-operator --source-dir configmaps/

# 要寫成檔案就明確指定目錄（#1582：寫入是 opt-in）
da-tools migrate-to-operator --source-dir configmaps/ --output-dir ./migration-output

# 預覽遷移計畫
da-tools migrate-to-operator --source-dir configmaps/ --dry-run

# 含 receiver 設定
da-tools migrate-to-operator --source-dir configmaps/ \
  --receiver-template slack --secret-name da-slack

# 僅產出 checklist
da-tools migrate-to-operator --source-dir configmaps/ --checklist-only

# JSON 報告模式
da-tools migrate-to-operator --source-dir configmaps/ --json
```

#### Rollback 程序

若遷移至 Operator 後需回退至 ConfigMap 模式：

**Step 1: 停止 Operator 路徑**
```bash
# 移除 PrometheusRule CRDs
kubectl delete prometheusrules -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting

# 移除 AlertmanagerConfig CRDs (若有)
kubectl delete alertmanagerconfigs -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting
```

**Step 2: 恢復 ConfigMap 路徑**
```bash
# 切換 Helm values
helm upgrade threshold-exporter ./charts/threshold-exporter \
  --set rules.mode=configmap

# 驗證 ConfigMap rules 已重新載入
kubectl logs -n monitoring deployment/threshold-exporter | grep "SHA256"
```

**Step 3: 驗證**
```bash
# 確認 alerts 正常觸發
da-tools drift-detect --dirs conf.d --mode configmap
promtool query instant 'count(ALERTS{alertstate="firing"})'
```

> ⚠️ Rollback 後 Operator 產出的 CRD 檔案仍保留在 `operator-manifests/` 目錄，可隨時重新 apply。

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

#### runtime-audit

Git 宣告的規則（`rule-packs/rule-pack-*.yaml`）↔ Prometheus 實際載入的規則（`GET /api/v1/rules`）之間的**唯讀**硬比對。補上 #711/#714 PR-期 drift gate 蓋不到的 **runtime 那條腿**。

**用途**：incident 當下的對帳診斷鈕（`kubectl port-forward` 後跑，零新基礎設施）；排程 / CI gate（`--ci`）；reload 失敗 / 手改 configmap / 孤兒殘留偵測。

> **邊界**：本工具**唯讀、不自癒**——偵測 → 報告（exit code）→ 由人決定。明確 reject 自癒 / 常駐 reconciliation Operator（避免機器回寫人類平面 + 觀測者悖論遞迴）。範式對齊 #631/#643/#652。詳見 [custom-rule-governance.md §7.1](custom-rule-governance.md)。

**語法**

```bash
da-tools runtime-audit (--prometheus <url> | --runtime-json <file>) [options]
```

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--prometheus <URL>` | Prometheus API URL，例如 `http://localhost:9090` | （無） |
| `--runtime-json <FILE>` | 已存的 `/api/v1/rules` JSON（離線 / fixture） | （無） |
| `--rule-packs-dir <DIR>` | Rule pack YAML 目錄 | `rule-packs/` |
| `--strict-orphan` | 將 ORPHAN（孤兒殘留規則）也升為 `--ci` 閘門失敗 | false |
| `--json` | JSON 格式輸出 | false |
| `--ci` | 偵測到 drift 即 exit 1（MISSING/UNHEALTHY；ORPHAN 需 `--strict-orphan`） | false |

**偵測分類**

1. **MISSING** — Git 已宣告但 runtime 未載入（reload 失敗 / projected-volume lag / 手刪 configmap）
2. **UNHEALTHY** — 已載入但 `health != ok`（帶 `lastError`；series 觀測無法與「metric 本就不存在」區分）
3. **ORPHAN** — runtime 仍載入但 Git 不再宣告（孤兒殘留；限**已宣告群組**內，不誤報無關 infra 規則）

> **⚠️ 限制（避免假陽性）**：`declared` = `--rule-packs-dir` 內**所有** `rule-pack-*.yaml`。平台 pack 可選擇性啟用（Projected Volume `optional`）；若部署只載入子集，**停用的 pack 會被報成 MISSING**。對策：把 `--rule-packs-dir` 指向只含已啟用 pack 的目錄（ORPHAN 方向不受影響——它限已宣告群組）。另：`unknown` health（reload 後尚未首次評估的暫態）**不**列 UNHEALTHY，只有確認的 `err` 才列。

**範例**

```bash
# incident 診斷（先 port-forward 到 9090）
da-tools runtime-audit --prometheus http://localhost:9090

# CI / 排程 gate
da-tools runtime-audit --prometheus http://localhost:9090 --ci

# 離線 fixture（無活叢集）
da-tools runtime-audit --runtime-json rules.json --json
```

---

#### rule-pack-split

將 Rule Pack 分層拆分為 edge（Part 1）和 central（Parts 2+3），支援 Federation Scenario B。

**用途**：多叢集 Federation 場景；邊端（edge）與中央（central）分離部署。

**語法**

```bash
da-tools rule-pack-split [--rule-packs-dir <dir>] [options]
```

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--rule-packs-dir <DIR>` | Rule Pack 目錄路徑 | `rule-packs/` |
| `--output-dir <DIR>` | 輸出目錄 | `split-output/` |
| `--operator` | 改輸出 PrometheusRule CRD YAML | false |
| `--namespace <NS>` | CRD 的 namespace | `monitoring` |
| `--gitops` | GitOps 模式（key 排序、輸出可重現） | false |
| `--dry-run` | 不寫檔 | false |
| `--json` | 以 JSON 印出報告 | false |

工具只做 Scenario B 的 edge／central 拆分，選擇 Federation 場景的選項尚未實作；輸出格式改由 `--operator`／`--gitops` 決定。

**輸出結構**

```
split-output/
├── edge-rules/       (Part 1：正規化 recording rule)
│   └── rule-pack-<db>.yaml
└── central-rules/    (Parts 2+3：閾值正規化與告警)
    └── rule-pack-<db>.yaml
```

group 名稱沒有 `-normalization`／`-threshold-normalization`／`-alerts` 後綴時，工具依資料位置逐條分配（recording rule 到 edge、alert 到 central），並印一行 WARN。

**範例**

```bash
# Scenario B 分層拆分
da-tools rule-pack-split --rule-packs-dir rule-packs/ --output-dir federation-split/
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
| `--output <FILE>` | 輸出至檔案。**只有 render（預設）與 `--output-configmap` 會讀它，且不能配 `--dry-run`**；配 `--validate` / `--apply` / `--dry-run` 是呼叫端錯誤（結束碼 2），不會靜默不寫（#1650） | stdout |
| `--output-configmap` | 產出完整 Kubernetes ConfigMap YAML | false |
| `--base-config <FILE>` | 自訂 Alertmanager 基礎配置。**僅 `--output-configmap` 會讀它**；用在其他模式是呼叫端錯誤（結束碼 2），不會被靜默忽略 | 內建預設（**僅在未提供本旗標時**；供了但讀不到／不是合法 YAML／頂層不是 mapping 一律結束碼 2，不會退回預設） |
| `--dry-run` | 僅輸出預覽，不寫入檔案。**`--validate` / `--apply` 不讀它**（結束碼 2） | false |
| `--validate` | 僅驗證，不輸出。conf.d 裡任何解析不了的租戶檔 → 結束碼 1，不分 `--strict`（#1460）。其餘檢查都通過後，還會以**內建預設 base** 組出完整設定交給 `amtool`（見下方「Alertmanager 驗證」；#2260） | false |
| `--strict` | 下列情況報 ERROR 並結束碼 1（CI 跑 `--strict`）：domain-policy（ADR-007）違規（不加時為 WARN）；`routes[i].match` 的值或 `overrides[i].alertname`／`metric_group` 未加引號、PyYAML 讀成非字串（`yes`、`1:30`、`~` 等，#2431；修法是加引號。不加時 routes 條目以 WARN 略過、override 則以 `str()` 渲染，如 `alertname="True"`；值為 `0`、`no`、`false` 這類假值時視同沒有 matcher：該 override 略過，`--validate` 也擋）；`group_by` 的元素不是非空字串（未加引號的 `8`、`on` 等）、重複、或 `...` 與其他 label 並存（#2503；不加時該元素以 `WARN … skipping` 略過後輸出，略過後為空就不輸出 `group_by`）；租戶 `_routing` 既不是 mapping 也不是停用字串、或 `_routing_defaults` 不是 mapping（#2341；不加時以 `WARN … skipping` 略過：該租戶不產出 route／該層不貢獻，`--validate` 也擋）；`--apply`／`--output-configmap` 合併時 `equal:` 標籤沒有 presence gate 的 inhibit rule（#1132，不加時為 WARN） | false |
| `--apply` | 直接套用至 Kubernetes（需 kubectl） | false |
| `--namespace <NS>` | ConfigMap 所在 namespace。**只有 `--apply` / `--output-configmap` 會讀它**，其他模式結束碼 2 | `monitoring` |
| `--configmap <NAME>` | ConfigMap 名稱。**只有 `--apply` / `--output-configmap` 會讀它**，其他模式結束碼 2 | `alertmanager-config` |
| `--yes` | 搭配 --apply 跳過確認提示。**只有 `--apply` 會讀它**，其他模式結束碼 2 | false |
| `--policy <FILE>` | 策略 YAML 的**路徑**，內含 `allowed_domains:` 清單（省略＝不限制）。⚠️ 這裡吃的是檔案路徑，不是逗號分隔的域名；供了但讀不到會 exit 2（#1556）；檔裡沒有 `allowed_domains` 鍵（空檔、只有註解、鍵拼錯）或清單有非字串項目也是 exit 2，要明示不限制請寫 `allowed_domains: []`（#1649）。可選填 `min_generator_version: "X.Y.Z"`：執行的 da-tools 比它舊（或讀不到自己的版本）時 exit 2，修法是調高 da-tools 版本；值不是 `X.Y.Z` 形式也是 exit 2（#2901）。⚠️ v2.9.0 映像仍是舊行為 | （不限制） <!-- image-caveat: v2.9.0 --> |
| `--findings-json <PATH>` | 另外把這次執行**印出的** finding（warning stream 與拒收訊息的每一行）寫成 JSON 到 PATH，格式見下方「結構化 finding」。所有模式都讀它；每一種結束（含拒收、呼叫端錯誤、參數錯誤與程式例外）都會寫，先寫暫存檔再改名，所以 PATH 上不會留下前一次執行的文件。stdout、stderr 與結束碼不變；PATH 寫不進去是結束碼 2（#2766）。⚠️ v2.9.0 映像沒有這個旗標 <!-- image-caveat: v2.9.0 --> | （不寫） |

**輸出**

每個模式 stdout 都先印一行 `Config files: N read, M skipped (<檔名>)`（#1460）——N / M 來自結構化紀錄，不是 stderr 的 WARN 行；M > 0 且被跳過的是租戶檔時，這次執行不會再往下產出任何結果。

**階層式 conf.d（[#2326](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2326)）**：讀整棵樹（與 exporter 同一套走訪規則：隱藏目錄略過、只有 README 的目錄不貢獻任何東西），任何深度的租戶都產生 route。語意見 [ADR-017 決策 9](adr/017-defaults-yaml-inheritance-dual-hash.md#9-路由設定沿目錄逐層繼承)：`_routing_defaults` 先取根目錄任一 `_` 檔，再依序取租戶路徑上每一層子目錄 defaults 載體（`_defaults.yaml`／`.yml`）頂層的 `_routing_defaults`，逐頂層鍵淺合併、深層勝出；`_routing_profiles.yaml` 與 `_domain_policy.yaml` 可放子目錄，只作用於該子樹，各層 policy 疊加判定。以下情況**所有模式**回 2、什麼都不產出不寫入：子目錄任一檔有 `_routing_enforced`（沒被選中的 defaults 載體除外，見下方「一個目錄只讀一份 defaults 載體」）；子目錄層 `_routing_defaults` 的 `receiver` 或 `overrides` 寫成 null；同一個 routing profile 名稱定義在兩個檔（含根目錄 `.yaml` 與 `.yml` 並存）。同一個租戶 id 由兩個租戶檔宣告則是所有模式回 1（`FAIL: N tenant(s) declared in more than one file`）。子樹 policy 的 `tenants:` 點名子樹外的租戶：`--strict` 下為 ERROR（回 1）、否則 WARN，該條目不生效。子目錄平台檔的 `tenants:` 區塊照舊無人讀，只印 WARN。子目錄裡不是 defaults 載體的 `_` 檔帶 `_routing_defaults` 不會被讀，`--validate` 回 1 並點名該檔；租戶區塊為 null（`t:` 沒有內容）同樣算一次宣告。⚠️ v2.9.0 映像只讀頂層，子目錄的租戶沒有 route、結束碼 0 <!-- image-caveat: v2.9.0 -->

**一個目錄只讀一份 defaults 載體（[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：同一個目錄（根目錄或任一子目錄）有不只一份 defaults 載體拼法（`_defaults.yaml` 與 `_defaults.yml` 並存，或大小寫不同的拼法）時，每個平面都只讀其中一份（`.yaml` 優先，規則同 exporter；[#1674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1674)），另一份整份不讀。產生器對每一份沒被選中的檔印 `WARN: <檔案> is not read: … only <實際讀的檔> is read, on every plane (#1674); merge them into one file, skipping`，`--validate`（不論有沒有 `--strict`）與 `validate-config` 的 `schema` 列對這行回 1／FAIL；render、`--dry-run`、`--output-configmap`、`--apply` 照常產出、結束碼不變；經 tenant-api 的寫入因此回 422 `GENERATOR_FINDINGS`，直到兩份併成一份。產生器不打開那份檔：裡面寫了什麼（`_routing_enforced`、壞掉的 YAML）結果都一樣，不另報其他 finding。finding kind 為 `unselected_defaults_carrier`，與 `da-guard parse-tree` 的 `not_read` reason 同名。修法：把兩份併成一份。⚠️ **變嚴格**：過去這種樹只在 stderr 印一行 multi-carrier WARN、`--validate` 回 0；子目錄沒被選中的拼法帶 `_routing_enforced` 時則是所有模式回 2，現在改為只有 `--validate` 回 1。⚠️ v2.9.0 映像不報這行、`--validate` 回 0：根目錄的兩份都讀，子目錄整個不讀 <!-- image-caveat: v2.9.0 -->

**租戶 id（[ADR-035](adr/035-tenant-id-single-source.md)）**：租戶 id 必須是 DNS-1123 label——1–63 個小寫英數與 `-`，首尾為英數（規則只寫在 `tenant-config.schema.json` 的 `definitions.tenantId`）。conf.d 只要宣告一個不合法 id（空字串、含大寫、`_`、`.`、空白，或超過 63 字元），**所有模式**（render、`--dry-run`、`--output-configmap`、`--apply`、`--validate`，不分 `--strict`）都回 1、什麼都不寫出也不套用，訊息開頭 `FAIL: N invalid tenant id(s)`，逐一點名並引用規則說明。不是只略過那個租戶：略過會部署出少了它的設定，它的告警落到 root receiver，而出貨的 `default` 是空 receiver。修法是改名；全數字的 id 要加引號。⚠️ v2.9.0 映像不檢查租戶 id，照樣產出 <!-- image-caveat: v2.9.0 -->

**布林與列舉欄位只依型別與值（[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：下列欄位型別不對或值不認得時，**所有模式**（render、`--dry-run`、`--output-configmap`、`--apply`、`--validate`，不分 `--strict`）都回 1、什麼都不寫出也不套用，訊息開頭 `FAIL: N field value(s) of the wrong type or not recognised`，逐一印 `ERROR (field type): …`；`validate-config` 的 `schema` 列同樣 FAIL。布林欄位只收布林：`_routing_enforced.enabled`（null 也不收；省略照舊是不啟用）、domain policy 的 `constraints.require_critical_escalation`、receiver 的 `send_resolved`、email 的 `require_tls`、webhook 的 `http_config.proxy_from_environment`（後四者 null 算沒設；receiver 不論寫在租戶 `_routing`、`overrides`／`routes` 條目、`_routing_defaults`、routing profile 或 `_routing_enforced` 都判，沒有租戶引用的 profile 也判）。加引號的 `"true"`、`"yes"`、`"off"` 一律是型別錯誤，不再換成它拼出的布林。租戶的 `_severity_dedup` 只收 `enable`／`disable`（也收 exporter 認得的 `enabled`／`on`／`true` 與 `disabled`／`off`／`false`，去掉前後空白、不分大小寫）、布林（`true` 是 enable、`false` 是 disable）或 null（預設 enable）；其他值（`"no"`、`"yes"`、`""`、數字）過去一律當成 enable，現在拒收。未加引號、PyYAML 讀成布林的寫法（`enabled: yes`、`send_resolved: off`、`_severity_dedup: no`）照收。finding kind 依欄位為 `routing_enforced_enabled_invalid`、`domain_policy_unusable`、`invalid_receiver_field`、`severity_dedup_invalid`。修法：布林寫成未加引號的 `true`／`false`，`_severity_dedup` 寫 `enable` 或 `disable`。⚠️ [ADR-036](adr/036-single-parser-effective-config.md) 步驟 4 改讀 yaml.v3 之後，未加引號的 `yes`／`no`／`on`／`off` 會讀成字串、套用同一條規則（布林欄位拒收；`_severity_dedup` 的 `on`／`off` 照收、`yes`／`no` 拒收），現在就請改寫成 `true`／`false`。⚠️ v2.9.0 映像照舊接受這些寫法 <!-- image-caveat: v2.9.0 -->

**產生器不讀的鍵（[#2960](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2960)）**：租戶 `_routing`、routing profile、`_routing_defaults`（根目錄與子目錄載體）、`_routing_enforced`，以及其下的 receiver、`overrides` 條目、`routes` 條目的 receiver，只讀固定的鍵（`receiver`、`group_by`、`group_wait`／`group_interval`／`repeat_interval`、`overrides`、`routes`；override 另讀 `alertname`／`metric_group`；`_routing_enforced` 讀 `enabled`、`match`，不讀 `overrides`／`routes`；receiver 讀其型別的欄位）。寫了其他鍵——`continue`、`mute_time_intervals`、`active_time_intervals`、`matchers` 這類 Alertmanager 欄位，或拼錯的 `recevier`、`enabeld`——產出的設定與沒寫時相同，所以逐鍵印 `WARN: <位置>: '<鍵>' is not read by the route generator (…)`，附上是哪類欄位或「你是不是要寫」的建議；`--validate` 與 `validate-config` 對這行回 1，render、`--dry-run`、`--output-configmap`、`--apply` 照常產出、結束碼不變。鍵一律照原文讀（`1:`、`on:` 也算），null 鍵不算；`http_config` 底下不看，它原樣交給 Alertmanager。鍵的清單以產生器實際讀的為準，JSON schema 釘在同一份上；finding kind 為 `routing_key_ignored`。da-guard 不判這些鍵，樹上有路由內容時只列為未判定證據（exit 4）；經 tenant-api 的寫入由產生器判，回 422 `GENERATOR_FINDINGS`（批次 patch 直接寫產生器不讀的 `_routing*` 鍵時，tenant-api 自己先拒）。`routes` 條目自己的鍵照舊由該條目的檢查處理（整條略過）。`_routing` 開頭的鍵也照確切名稱判定（[#2983](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2983)）：租戶設定只讀 `_routing` 與 `_routing_profile`，`_` 檔的最上層只讀 `_routing_defaults` 與 `_routing_enforced`；其他 `_routing*` 鍵——拼錯的 `_routing_profle`、`_routing_defualts`，寫錯地方的 `_routing_defaults`（寫在租戶裡），或已不讀的扁平鍵 `_routing_receiver_type`——印 `WARN: <租戶或 top level in <檔案>>: '<鍵>' is not a key the route generator reads (…)`，判定與上面相同。不以 `_routing` 開頭的拼法（`_routng`）照舊只有 `unknown reserved key` 一行 WARN、不擋。⚠️ v2.9.0 映像不報這些鍵，`--validate` 回 0 <!-- image-caveat: v2.9.0 -->

**`_routing_enforced.match` 只收 list（[#2989](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2989)）**：`match` 是 Alertmanager matcher 字串的 list（`['severity="critical"']`）。寫成 `routes[i].match` 的 mapping 寫法（`{severity: critical}`）、單一字串、數字或布林，或 list 裡有不是非空字串的元素（照 PyYAML 的讀法：`[1]`、`[~]`、`[yes]`、`['']`、`[{severity: critical}]`）時，該 `_routing_enforced` 區塊不啟用：不產出 enforced route（`{{tenant}}` 寫法也一樣，一條都不展開），印 `WARN: _routing_enforced in <檔案>: 'match' must be a list of Alertmanager matcher strings … skipping`，`--validate` 與 `validate-config` 回 1；render、`--dry-run`、`--output-configmap`、`--apply` 照常產出、結束碼不變，而此時這個區塊不產出 NOC 路由（不是收全部），部署前務必跑 `--validate`。停用的區塊（`enabled: false`）不判。名稱順序上較早的根目錄 `_` 檔若已啟用 `_routing_enforced`，該區塊照常生效。省略 `match` 或寫 null 照舊是不加 matcher。先前這種寫法被靜默丟棄，NOC route 因此收全部告警。⚠️ v2.9.0 映像仍靜默丟棄、`--validate` 回 0 <!-- image-caveat: v2.9.0 -->

**時長寫法（[#2490](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2490)）**：`group_wait`／`group_interval`／`repeat_interval`（租戶 `_routing`、overrides、`routes` 條目、`_routing_defaults`、`_routing_enforced`、routing profile）照 Alertmanager 的讀法判定——整數加單位、由大到小、每個單位最多一次（`y`、`w`、`d`、`h`、`m`、`s`、`ms`），或單獨一個 `0`；規則只寫在 `tenant-config.schema.json` 的 `definitions.duration`。`1h30m`、`90m`、`1d` 照用（再套上下界護欄）；小數（`1.5h`）、單位倒序（`30m1h`）或重複（`1h1h`）、`ns`／`us`、正負號、前後空白、超過約 292 年的值，Alertmanager 會拒收整份設定，所以產生器把它換成平台預設值並印 `WARN: … invalid <參數> '<值>' …`，`--validate` 與 `validate-config` 對這行回 1；render、`--dry-run` 照常產出（換成預設值的那份）、結束碼不變。⚠️ v2.9.0 映像把 `1h30m` 換成平台預設、把 `1.5h` 原樣交給 Alertmanager，`--validate` 都回 0 <!-- image-caveat: v2.9.0 -->

**結構化 finding（`--findings-json`，[#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766)）**：頂層是 `schema`（`da-tools.findings/v1`）、`generator_version`（da-tools 版本，讀不到為 null）、`config_dir`、`exit_code`（這次執行實際的結束碼）、`validate`／`strict`（這次是否帶了該旗標；有些 `blocks: strict` 的 finding 只在 `--strict` 時才會產生（例如 domain policy 檔不可用），所以不帶時沒列出不代表沒有；違反 domain policy 這類則不帶也會以 WARN 列出）與 `findings`。每筆 finding 有 `kind`（穩定的 snake_case 代號；與 [§guard](#guard) 表裡同義的 kind 同名——該表的路由類 kind 只保留定義，da-guard 已不產出；`_routing_profile` 指向沒有定義的 profile 目前仍是 `unclassified`，不是表裡的 `unknown_routing_profile`；`unclassified` 表示還沒分類，只有文字可依賴）、`severity`（SARIF 等級：`error`／`warning`／`note`）、`blocks`（何時讓產生器失敗：`always` 所有模式、`strict` 加 `--strict` 時、`validate` 加 `--validate` 時、`never`）、`tenant`／`policy`／`file`（相對於 `--config-dir`）／`field`（不知道時為 null）與 `message`（印出的那一行，只去掉開頭縮排）。`tenant` 為 null 的擋下類 finding 視同影響整份設定，不是「與任何租戶無關」。只收 warning stream 與拒收訊息裡這次印出的行（讀檔階段直接印出的少數 WARN 不在內）：被拒收的執行只有拒收訊息，`--validate` 重印的錯誤不重複列。唯一的例外是 `run_failed`：結束碼不是 0、卻沒有一筆在這次模式下會擋的 finding 時（例如 amtool 拒收、組裝被拒、呼叫端錯誤），文件會自己補這一筆（`error`、`always`），原因要看 stderr；只讀 finding 的程式因此不會把失敗的執行當成通過。⚠️ v2.9.0 映像沒有這個輸出 <!-- image-caveat: v2.9.0 -->

**Fragment 模式** (`--output-configmap` 未指定)：
YAML 片段，包含 route、receivers、inhibit_rules。

**ConfigMap 模式** (`--output-configmap`)：
完整 Kubernetes ConfigMap YAML，含 global、route、receivers、inhibit_rules，可直接 `kubectl apply`。

**Alertmanager 驗證（#2219、#2260）**：PATH 上有 `amtool` 時，`--output-configmap` 與 `--apply` 會對「實際要寫出／套用的那份」`alertmanager.yml` 跑 `amtool check-config`，被拒收就不寫檔、不 apply；沒有 `amtool` 則在 stderr 印 `NOTICE: ... was NOT validated by Alertmanager`，其餘行為不變。`--validate` 在其餘檢查都通過、印出 `OK` 之前，也會把產生的設定組進**內建預設 base**（不是你的 `--base-config`——`--validate` 從不讀它）交給 `amtool check-config`：拒收回 1、`amtool` 自身出錯回 2；沒有 `amtool` 時印一行 NOTICE 註明這件事，結束碼不變。所以 `--validate` 通過不代表你自己的 base 組出來也會通過，那要跑 `--output-configmap --base-config`。Fragment 模式（不是完整設定）不做這項驗證。da-tools 映像內含 `amtool`，取自部署清單釘住的同一個 Alertmanager image（tag 與 digest 皆同；#2294），所以在映像裡跑時這道驗證預設就會執行。⚠️ v2.9.0 映像不內含 `amtool`，其 `--validate` 也不經 `amtool` <!-- image-caveat: v2.9.0 -->

**root receiver 沒有 integration 時印 WARN（#2660）**：`--output-configmap` 與 `--apply` 要寫出／套用的那份 `alertmanager.yml`，若 root route 的 receiver 沒有任何非空的 `*_configs`（名字找不到 receiver 也算），stderr 印一行 `WARN: the root route's receiver '<name>' has no integration …`——沒被子 route 接走的告警（含平台自監控告警）會落到那裡、不通知任何人；接法見 [BYO 整合指南 §11](integration/byo-alertmanager-integration.md#11-平台自監控告警的投遞)。判的是 `--base-config` 的 root receiver（未給時是內建 base 的空 `default`，所以預設一定會印）、`--apply` 時是叢集上的。只是提醒：不是錯誤、`--strict` 不升級、結束碼不變；render、`--validate` 不印。⚠️ v2.9.0 映像不印這行 <!-- image-caveat: v2.9.0 -->

**平台告警探針集降級時（#1533）**：「租戶觸發的 inhibit 規則不得靜音平台自監控告警」這項檢查，探測的是 `configmap-rules-platform.yaml` 裡的每一條平台告警（映像裡與工具同目錄、checkout 裡在 `k8s/03-monitoring/`）。這個檔找不到、讀不了、或讀不出任何平台告警時，探針集退回內建的 6 筆，stderr 印一行 `WARN: platform alert identity probe set degraded …`；檔只讀到一部分時（有格式壞掉而被跳過的 group／rule，或內建 6 筆裡有名字讀不到），探針集是「讀到的部分，再加回內建 6 筆裡讀不到的那幾筆」，stderr 印 `WARN: platform alert identity probe set is INCOMPLETE …`，同樣算降級（此時 `--validate` 那一行是 `Probe set: DEGRADED, shipped platform alert identities only partly read …`）。此時若你提供的 inhibit 規則——`--output-configmap` 的 `--base-config`，或 `--apply` 時叢集上現有的設定——含有租戶觸發的規則（source matchers 要求 `tenant` 存在），結束碼 2、不寫檔、不 apply：這是不變式驗證不了，不是對你設定的判定。訊息點名原因與出問題的 `inhibit_rules[<i>]`。沒有這種規則時照舊只印 WARN、繼續執行；產生出來的規則、以及 target 同樣要求 `metric_group` 的規則（例如 base 裡的副本）不算（平台告警不帶 `metric_group`），target 已排除平台告警的規則也不算（例如違規訊息建議的 `alert_source=""`）。`--validate` 結束碼不變，stdout 多一行 `Probe set: DEGRADED to the 6-entry built-in fallback …`。與 `--strict` 無關。⚠️ v2.9.0 映像只印 WARN、回 0 並照常寫出／套用 <!-- image-caveat: v2.9.0 -->

**從 da-guard 搬來的路由檢查（[#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904)）**：[ADR-036](adr/036-single-parser-effective-config.md) 讓 da-guard 不再判路由與 policy，之前只有 da-guard 有的幾項改由本工具報，finding kind 沿用 da-guard 當時的名稱（定義見 [§guard](#guard) 表）：`routing_in_unread_location`——`_routing` 或 `_routing_*` 寫在本工具不讀的位置（任一層選用的 `_defaults.yaml` 的 `defaults:` 區塊內、根目錄 `_` 檔 `profiles:` 的某個 threshold profile 內——profile 裡報的是 `_severity_dedup` 與任何以 `_routing` 開頭的鍵（[#2914](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2914)）；`_` 檔頂層讀不到的 `_routing*` 鍵是 `routing_key_ignored`，見上方「產生器不讀的鍵」），印 `WARN: <檔案>: <鍵路徑> is not rendered … skipping`，`--validate` 回 1——只有 `--validate` 擋：不加 `--validate`、只加 `--strict`（產出模式，例如 `--output-configmap --strict`）時印 WARN、照常寫出。`defaults:` 只要是 mapping 就算包裝，不論鍵是不是字串（`defaults: {1: 5}` 也算），與 exporter 的讀法相同；`routing_profiles_unusable`——`_routing_profiles.yaml` 的 `routing_profiles:` 不是 mapping，印 WARN、不擋；`duplicate_override_matcher`——`overrides[i]` 的 matcher 與前面某一條相同，前面那條先接走告警、這條永遠不生效；`redundant_override_receiver`——`overrides[i].receiver` 與主 receiver 完全相同。後兩項判解析後的 routing，印 WARN、任何模式都不擋。receiver 的 `conflicting_receiver_field`（`exactly one of` 兩個都給）的 `field` 改成點名多出來的那個欄位（例如 `receiver.routing_key`）。`http_config.proxy_url` 能不能解析成 URL 交給 Alertmanager 的解析器（`amtool`，da-tools 映像有附）判：PATH 上沒有 `amtool` 的環境（例如用 pip 裝的工具），`--validate --strict` 不判 `proxy_url` 能不能解析，寫錯的 proxy URL 照樣通過。⚠️ 對本工具而言 `routing_in_unread_location` 是**變嚴格**：同一棵樹過去 `--validate` 回 0。⚠️ v2.9.0 映像沒有這幾項 <!-- image-caveat: v2.9.0 -->

**Receiver 名稱不可重複（#2279）**：租戶 id 可以含 `-`，所以租戶 `<t>` 的 `routes[0]` receiver（`tenant-<t>-route-0`）可能和另一個叫 `<t>-route-0` 的租戶的主 receiver 同名（`-override-<n>` 同理）。兩個來源產生同名 receiver 時，所有模式都回 1、不寫檔、不 apply，不分 `--strict`，訊息點名雙方來源。`--output-configmap --base-config` 的 base 若有 receiver 和產生的 receiver 同名，也回 1、不寫檔（否則 base 那份會蓋掉 conf.d 那份）；平台固定的 `custom-alerts-firehose` / `watchdog-heartbeat` / `synthetic-receiver` / `sentinel-sinkhole` 本來就讓 base 定義優先，不算在內。`--apply` 則照舊以這次產生的覆蓋叢集裡同名的 receiver。⚠️ v2.9.0 映像兩種情況都回 0 <!-- image-caveat: v2.9.0 -->

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
| `1` | 配置驗證失敗；**或 conf.d 裡有解析不了／讀不了的租戶檔**（壞 YAML、非 UTF-8、頂層不是 mapping、目錄型 `x.yaml`）——所有模式一律拒絕，不分 `--strict`，stdout 點名檔案（#1460）；**或 PATH 上的 `amtool` 拒收 `--output-configmap` / `--apply` 要寫出／套用的設定**——不寫檔、不 apply（#2219）；**或拒收 `--validate` 以內建 base 組出的設定**（#2260）；**或兩個來源產生同名 receiver**（所有模式，不分 `--strict`）、`--output-configmap` 的 base 有和產生的 receiver 同名的 receiver（#2279）；**或組裝時違反平台不變式**（例如 base 的 inhibit 規則會讓租戶靜音平台告警）——`--output-configmap`／`--validate` 印 `FAIL:`（#2260），`--apply` 時叢集上現有的設定違反不變式也印 `FAIL:`、不 apply（#2506）；**或同一個租戶 id 由兩個租戶檔宣告**（所有模式，見上方「階層式 conf.d」）；**或 conf.d 宣告了不合法的租戶 id**（所有模式，不分 `--strict`，見上方「租戶 id」；ADR-035）；**或 `--validate` 遇到 Alertmanager 不接受的時長**（見上方「時長寫法」；#2490）；**或產生器不讀的路由鍵**（見上方「產生器不讀的鍵」；#2960、#2983） |
| `2` | 呼叫端錯誤：**工具因為「怎麼被呼叫的」或「環境」而做不了事**，不是你的設定有違規。今天到得了這一格的有（非窮舉）：`--policy` / `--base-config` 供了但不可用（不是檔案、讀不到、不是合法 YAML、頂層不是 mapping）、`--base-config` 用在 `--output-configmap` 以外的模式、**`-o` / `--dry-run` / `--namespace` / `--configmap` / `--yes` 用在不讀它們的模式**（訊息會點名旗標與模式並給一個 argparse 接受的改法；#1650）、`-o` 的輸出路徑寫不進去、`--apply` 在讀不到 stdin 的環境下沒帶 `--yes`、以及 kubectl／叢集操作失敗（#1556、#1616、#1617）；`amtool` 在 PATH 上但無法執行、逾時或自身出錯（沒有給出拒收判定）、`--apply` 之後 Alertmanager `/-/reload` 失敗（v2.10.0 前只印 WARN、結束碼 0；#2219）；conf.d 樹的形狀被路由面拒收（上方「階層式 conf.d」列回 2 的三種情況，訊息開頭 `ERROR: N routing-tree error(s)`；#2326）；平台告警探針集降級，而你提供的 inhibit 規則（base config 或叢集設定）裡有租戶觸發的規則驗證不了——不寫檔、不 apply（見上方「平台告警探針集降級時」；#1533）。⚠️ **上列是 v2.10.0 的契約**；本頁上方釘的 `v2.9.0` 映像對其中多數回 0 或 1 <!-- image-caveat: v2.9.0 --> |

---

#### patch-config

ConfigMap 局部更新工具，支援 preview（--diff）和直接應用。

**用途**：運維期間快速調整單一 metric 閾值；避免完整 ConfigMap 重新部署。

**語法**

```bash
python3 scripts/tools/ops/patch_config.py [--diff] [--json] [--exporter-namespace NS] [--exporter-selector LABELS] [--exporter-port PORT] [--reload-timeout SECONDS] [--poll-interval SECONDS] <tenant> <metric> <value>
```

讀寫 `monitoring` namespace 的 `threshold-config`，需要 PATH 上的 `kubectl` 與可用的 kubeconfig。`<value>` 是具體值、`default`（刪掉租戶的 key）或 `disable`。apply（不帶 `--diff`）另需在 exporter 所在 namespace 有 `list pods` 與 `get pods/proxy`（理由與範圍見[跨租戶 ConfigMap 硬化基線 §2.2](cross-tenant-configmap-hardening.md)）。

**選項**

| 參數 | 說明 |
|------|------|
| `--diff` | 只預覽，不套用 |
| `--json` | stdout 只印一份 JSON：`--diff` 時是預覽，否則是 apply 的結果；其餘輸出走 stderr |
| `--exporter-namespace` | apply 驗收用的 exporter pod 所在 namespace（預設取自 chart） |
| `--exporter-selector` | 那些 pod 的 label selector（預設取自 chart） |
| `--exporter-port` | 其 HTTP listener 的 container port 名稱或號碼（預設取自 chart） |
| `--reload-timeout` | 等每個 pod 服務本次寫入的位元組的期限（秒），於每輪輪詢之間檢查 |
| `--poll-interval` | 等待期間讀各 pod `/api/v1/config/identity`（舊 exporter 則讀 `Last reload`）的間隔秒數 |

**租戶定位**：要改的 key 是 `threshold-config` 裡 `tenants:` 宣告了該租戶的**那一個** YAML key（不論檔名、大小寫或 `.yml`），patch 寫回該 key。`_defaults` key 不分大小寫、`.yaml`／`.yml` 皆可。沒有 key 宣告該租戶時，具體值在 multi-file 版面建立 `<tenant>.yaml`，在 legacy 版面寫進 `config.yaml`。`default` 在租戶未設該 metric 時是 no-op（結束碼 `0`），目標格原文已等於新值時亦同；讓租戶區塊變空時區塊保留。

**讀取**：純量取原文、不做型別轉換（`010` 就是 `010`）。

**拒絕（結束碼 `2`、什麼都不寫）**：多個 key 宣告同一租戶；沒有讀得了的 key 宣告該租戶、又有 key 讀不了（訊息點名那些 key）；查找路徑上有 merge key `<<`；ConfigMap 的 `data` 不是 mapping；兩個 `_defaults`、既無 `_defaults` 也無 `config.yaml`；租戶區塊不是 mapping；要改的 key 宣告了一個以上的租戶（legacy 版面的 `config.yaml` 除外）；要新建的 key 以 `.` 或 `_` 開頭。⚠️ 改寫不改動該 key 其他值的內容（`010`、`12:30` 照原文寫回），但不保留寫法：註解會遺失，flow（`{k: v}`）改成 block，引號可能變成單引號或拿掉。

**寫後驗收（apply）**：新位元組與該 key 現有位元組相同 ⇒ 不寫、不等，結束碼 `0`。否則先經 `kubectl get --raw …/pods/<pod>:<port>/proxy/…`（只用 GET）逐一讀 Running 且未在刪除中的 pod 的 [`/api/v1/config/identity`](api/README.md)（`config_hash`：它服務的是哪一版位元組；`parse_failed`：因無法 parse 而被排除的 key）與 `/metrics`。patch 後由 patch 回傳的 ConfigMap 算出 exporter 服務該版本時應回報的 `config_hash`（exporter 會讀的 key——非 `.` 開頭、副檔名 `.yaml`／`.yml` 不分大小寫——依 key 排序、各自 SHA-256 後串接再 SHA-256；single-file 模式的 pod 則為 `config.yaml` 的 SHA-256），等每個 pod 回報相同值；之後的 `/metrics` 夾在兩次 identity 之間讀，兩次是同一次安裝才採用，否則重讀（有上限，超過視同逾時）。再逐 pod 比對全部 `user_*` series；parse 失敗看寫入後的 `parse_failed`：含被 patch 的 key、或含寫入前沒有的其他 key ⇒ 失敗，寫入前就已在其中的其他 key 只警告。⇒ 驗收通過代表每個 pod 服務的正是本次寫入產生的那一版整份位元組、且沒有把被 patch 的 key 當成無法 parse 排除；位元組生效後是否合預期（例如非數值被退回 default、未知 key 被忽略）不在此列，見 `patch-config --help`。對 identity 回 404 的舊 exporter，該 pod 退回舊驗法（等 `/api/v1/config` 的 `Last reload` 改變、比對 `da_config_parse_failure_total`），stderr 會提示、`--json` 的 `pods.<pod>.identity` 標為 `unavailable (404)`（否則為 `checked`）。`_` 開頭的 key（`_silent_mode`、`_profile` 等）不判目標租戶自己的 series，只列在 stderr（與 `--json`）。不合、逾時、途中連不到、寫入後被 Ctrl-C／SIGTERM 中斷或發生意外錯誤 ⇒ patch 回舊位元組（原本沒有的 key 會刪掉）並非 0 結束；寫入與回滾都以 ConfigMap 的 `resourceVersion` 為前置條件：讀取後被別人改過就不寫；回滾時同一個 key 已被別人改過就不回滾（不覆蓋對方）；驗收結束時 ConfigMap 必須仍是本次寫入產生的版本。patch 呼叫本身失敗時會重讀 ConfigMap 判定是否已套用。判準細節與已知殘留限制見 `patch-config --help`。

**`--diff`**：只說要寫入什麼（`after`）與 apply 會不會寫（`changed`，與 apply 同一判定——apply 不送 patch 時為 `false`）。**不顯示目前值**：`--json` 的 `before` 恆為 `null`，文字輸出改印一行請讀者到 exporter 確認（要在本工具內讀出單一 key 的現值，得在 Python 端重做 exporter 的 key→series 對應，[#1950](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1950) 決策排除）。`default` 的 `after.state` 是 `key removed`，不推斷刪掉後生效的值。

**`--json`**：每條結束路徑的 stdout 都是一份 JSON（stdout 已關閉，或寫入前就被信號結束的行程除外）；`--json --help` 為結束碼 `0`、`status: "help"`。結束碼 `2` 時鍵與預覽相同、值清空，另加 `status: "caller_error"` 與 `reason`（`bad_arguments`／`configmap_shape`／`configmap_changed`／`kubectl_failed`／`unexpected_error`）。apply 的文件沿用預覽的鍵（`before`／`after` 為 `null`），另加 `status`（`no-op`／`applied`／`verify-failed-rolled-back`／`timeout`／`unreachable`／`rollback-failed`／`interrupted-rolled-back`／`error-rolled-back`／`state-unknown`／`overwritten-by-another-writer`）、`exit_code`、`written`（結束時 ConfigMap 是否為新位元組；不明為 `null`）、`rolled_back`、`message`，以及 `pods.<pod>` 的 `identity`（`checked`／`unavailable (404)`）、`problems`／`warnings`／`target_changes`（目標租戶變動的 series，`before`／`after` 為 `null` 表示不存在）。

**範例**

```bash
python3 scripts/tools/ops/patch_config.py --diff db-a mysql_connections 100
python3 scripts/tools/ops/patch_config.py --diff --json db-a mysql_connections 100 | jq .changed
python3 scripts/tools/ops/patch_config.py db-a mysql_connections 100
python3 scripts/tools/ops/patch_config.py --json db-a mysql_connections 100 | jq .status
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功（已在每個 exporter pod 驗收）；含 `default` 與位元組相同的 no-op |
| `1` | 寫後驗收失敗（非目標租戶的 series 變了、目標租戶變動超出上限、新的 parse failure 等）；已回滾 |
| `2` | 呼叫端錯誤，**什麼都沒寫**：讀 ConfigMap 或送出 patch 時 `kubectl` 無法執行或非零結束（例如不在 PATH、叢集連不上、ConfigMap 不存在、無權限；列 pod 失敗屬 `4`）、上述任一種拒絕、讀取後 ConfigMap 已被別人改動（`reason: configmap_changed`，重跑即可）、argparse 拒絕的參數、寫入前發生的未預期例外 |
| `3` | `--reload-timeout` 到期時仍有 pod 沒在服務本次寫入的位元組（回報的 `config_hash` 不符；舊 exporter：沒 reload）；已回滾 |
| `4` | 連不到 exporter（沒有符合 selector 的 pod、pods/proxy 失敗、回應形狀不對）：寫入前發生則什麼都沒寫，寫入後發生則已回滾 |
| `5` | 回滾本身失敗：ConfigMap 可能仍是新位元組，需人工處理 |
| `6` | 寫入後被 Ctrl-C／SIGTERM 中斷，或發生意外錯誤；已回滾（寫入前被中斷則照一般方式結束，什麼都沒寫） |
| `7` | 無法判定 ConfigMap 現在的內容（讀不到，或新舊皆非），或寫入後另一個寫者改了同一個 key（不回滾）：需人工確認 |

#### generator-service

產生器容器的服務程式（[ADR-036](adr/036-single-parser-effective-config.md) §3、§5、§6，[#2901](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2901)）。常駐執行，tenant-api 在每次寫入前問它：這個 base commit 加上這些寫入之後，整棵 conf.d 能不能通過 `generate-routes --validate --strict`。只走 Unix socket 上的 HTTP/1.1，不開網路埠。tenant-api chart 把它當成必備的 sidecar 部署，tenant-api 每筆會改變 conf.d 內容的寫入 commit 前都問它，回應碼的對應見 [Tenant API 硬化 §3.9](api/tenant-api-hardening.md#39-每筆寫入先問產生器容器2903)（[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）。

```bash
da-tools generator-service --socket <path> --repo <git repo> --config-subdir conf.d --work-dir <dir> [--policy <FILE>]
```

**主要參數**

| 參數 | 用途 |
|------|------|
| `--socket <path>` | 監聽的 Unix socket（建立時權限 0600；對端程序的 uid 必須與服務相同，否則不回應直接關閉；威脅模型見 `generator_service.py` 模組說明）。路徑上若是舊的 socket 檔就換掉；是其他種類的檔則拒絕啟動（exit 2） |
| `--repo <dir>` | git repo（唯讀掛載即可）。檔案一律從 git 物件取（`git ls-tree` 加 `git cat-file`），不讀工作目錄、不用 `git archive` |
| `--config-subdir <path>` | conf.d 在 repo 內的相對路徑（`.` 為 repo 根；絕對路徑或含 `..` 時拒絕啟動，exit 2） |
| `--work-dir <dir>` | 暫存目錄，放每次判定實體化的樹；判定結束（含被終止）後刪除 |
| `--policy <FILE>` | 部署端 policy 檔（`allowed_domains`、`min_generator_version`），每次判定都重讀 |
| `--warm-ref <ref>` | 啟動時預熱的 commit（預設 `HEAD`；空字串關閉）。解析不到只記 log 與 metric，不影響可用性 |
| 其他 | 判定期限上限、保留給呼叫端的時間、子程序的 `RLIMIT_DATA`、快取上限、佇列長度、同時連線數、讀取中與排隊中的 body 總量、單一請求大小與寫入筆數等，預設值見 `--help` |

**端點**

| 端點 | 回應 |
|------|------|
| `POST /v1/validate` | body：`base_commit`（完整 hex commit id）、`writes`（`{path, content_b64}` 或 `{path, delete: true}`，路徑相對 conf.d）、`deadline_ms`（呼叫端剩餘時間）。有判定結果時一律 200：`result` 為 `pass`（產生器結束碼 0）、`findings`（非 0）或 `rejected`（只拒收這一筆，`reason` 為 `deadline`、`memory`、`incomplete`、`git_read`、`busy`；`busy` 也用在同時連線或讀取中、排隊中的 body 總量已滿時，此時 body 不會被讀）；另帶 `rc`、`findings`（產生器 `--findings-json` 的文件）、`stderr_tail`、`validated`（`base_commit`、每個寫入檔的 sha256、部署端 policy 檔的 sha256）。請求格式錯誤 400，寫入路徑與 base 樹衝突（上層在 base 樹裡是檔案或 symlink、要寫的路徑是目錄）也是 400。容器無法判定時 503：`VALIDATOR_UNAVAILABLE`（`reason`：`amtool_missing`、`generator_too_old`、`internal_error`）、`POLICY_UNAVAILABLE`（部署端 policy 檔不可用） |
| `GET /v1/status` | 部署端 policy 檔是否可用（含 sha256、錯誤、`min_generator_version`）、產生器與 da-guard 版本、amtool 是否存在、`ready` |
| `GET /metrics` | Prometheus 文字格式：快取大小（`pinned` 區分最新 base 樹裡的檔）、最近一次判定的峰值記憶體與子程序記憶體上限、各結果與各拒收原因的計數、佇列長度、版本 |

每次判定在 fork 出的子程序裡做（自成 process group、設 `RLIMIT_DATA`，期限到時整組結束），一次只跑一個，其餘排隊；排隊時間算在期限內。conf.d 檔的解析結果以（檔案內容的 SHA-256, 解析模式）為 key 跨請求快取，同一棵樹只改一個檔時只重新解析那個檔。子程序每解析完一份檔就把結果交回，所以期限到被結束的判定，已完成的解析仍留在快取裡，下一筆接著做。

**預熱**：啟動時（`--warm-ref`）以及收到與目前 base 不同的 `base_commit` 之後，服務在請求之外以同樣的子程序讀一次該 commit 的整棵樹，只為了填快取，不給判定、不跑 amtool；有自己的期限（`--warm-seconds`）。有請求進來時預熱立刻結束、讓請求先跑，已讀完的部分保留。`/metrics` 的 `da_generator_warm_runs_total{outcome}`（`completed`、`preempted`、`deadline`、`failed`）與 `da_generator_cache_base_coverage_ratio`（最新 base 樹的檔有多少比例已在快取裡；狀態端點同樣回報）可看預熱的狀況。

**Exit codes**

| Code | 含義 |
|------|------|
| 0 | 收到 SIGTERM／SIGINT 後正常結束 |
| 2 | caller error：`--repo` 不是 git repo、`--socket` 路徑上是非 socket 的檔或無法監聽、`--work-dir` 建不起來、`RLIMIT_NOFILE` 的硬上限容不下 `--max-connections` 加預留的 fd、`--max-inflight-body-bytes` 小於 `--max-body-bytes` |

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
| `-o, --output-dir <DIR>` | 輸出目錄 | `scaffold_output` |

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
- `_defaults.yaml` — 平台預設值。⚠️ **每次執行都會覆寫**目錄裡既有的 `_defaults.yaml`——已經調過平台預設值的 `conf.d/` 不要直接當 `--output-dir`，先產到暫存目錄再只搬租戶檔
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
| `1` | 只有未捕捉的例外（stderr 有 traceback）。多數輸入錯誤是 2，但 `--from-onboard` 的 JSON 壞掉或形狀不對目前也會 traceback 回 1 | <!-- datools-cmd-ignore: 只有 traceback 回 1，沒有出口可追 -->
| `2` | 呼叫端錯誤：參數錯誤、不支援的 `--db` 類型、`--non-interactive` 缺 `--tenant` 或 `--db`，或 `-o/--output-dir` 指到的輸出路徑寫不進去（#1641）；租戶 id 不是 DNS-1123 label（`--tenant`、互動輸入，或 `--from-onboard` 清單裡任一個；[ADR-035](adr/035-tenant-id-single-source.md)），什麼都不寫。⚠️ v2.9.0 映像不檢查租戶 id <!-- image-caveat: v2.9.0 --> |

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
| `-o, --output-dir <DIR>` | 輸出目錄 | `./migration_output/` |
| `--dry-run` | 僅顯示報告，不產生檔案 | false |
| `--triage` | Triage 模式：只產出 CSV 分桶報告 | false |
| `--interactive` | 遇到不確定時詢問使用者 | false |
| `--no-prefix` | 停用 custom_ 前綴（不建議） | false |
| `--no-ast` | 強制使用舊版 regex 引擎 | false |

**輸出**

**標準模式**：

- `migration_output/tenant-config.yaml` — 提取出的 threshold
- `migration_output/platform-recording-rules.yaml` — Recording rules
- `migration_output/platform-alert-rules.yaml` — Alert rules。告警改讀依租戶聚合的 recording rule，`$labels` 只剩 `tenant`：原 annotation／label 引用的 label 若在原式子裡以 `=` 釘成單一值（例如 `queue="order-processing"`），直接代入該值；其他（例如 `instance`）改讀 `$labels.tenant`，annotation 附上「（原為 instance，已依租戶聚合）」，該告警上方與報告會列出改寫了哪些 label（v2.9.0 映像沒有改寫，這些引用會渲染成空字串） <!-- image-caveat: v2.9.0 -->
- `migration_output/migration-report.txt` — 詳細遷移報告
- `migration_output/triage-report.csv` — 需人工審閱的規則清單
- `migration_output/prefix-mapping.yaml` — Metric 前綴對應表
- `migration_output/defaults-snippet.yaml` — 要合併進 `_defaults.yaml` 的 `defaults:` 片段。threshold-exporter 只發射宣告過的 key，沒合併時 `tenant-config.yaml` 的值不會生效。值取自原規則，宣告後 warning 層對所有租戶生效。有 warning 配對的 critical 層不能用 defaults 宣告，要 critical 的租戶各自在自己的檔案寫 `<key>_critical`；只有 critical 的舊規則改讀 base 列，值已在片段裡（v2.9.0 映像還沒有這個檔） <!-- image-caveat: v2.9.0 -->

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
| `1` | 只有未捕捉例外（traceback）會回 1。輸入檔不存在或 YAML 語法錯是 2，但空檔、頂層不是 mapping、不是 UTF-8 目前會 traceback 回 1 | <!-- datools-cmd-ignore: 只有 traceback 回 1，沒有出口可追 -->
| `2` | 呼叫端錯誤：參數錯誤、輸入檔讀不到或不是合法 YAML，或 `-o/--output-dir` 指到的輸出路徑寫不進去（#1641） |

---

#### validate-config

一站式配置驗證：YAML 格式、schema、routing、policy、版本一致性。

**用途**：CI/CD gate check；部署前驗證配置完整性。

**語法**

```bash
da-tools validate-config --config-dir <path> [options]
```

**前置需求：da-guard**（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）：profiles 列經 `da-guard` 讀 exporter 的答案，找不到它時該列 FAIL、結束碼 `2`；policy_dsl 列只在有 policy 規則（`_policies` 或 `--policy-dsl`）時才讀租戶，那時找不到 da-guard 同樣 FAIL、結束碼 `2`，沒有規則時照常 PASS（`No _policies defined — skipped`）。da-tools 映像已內建（`/usr/local/bin/da-guard`）。在 repo 裡直接跑 `python3 scripts/tools/ops/validate_config.py` 時，先 `make da-guard-build`（從本 checkout 的 Go 原始碼建到 `.build/da-guard`，需要 Go）並設 `DA_GUARD_BINARY=.build/da-guard`，或把 da-guard 放上 `$PATH`；`make validate-config` 只認兩種來源：`DA_GUARD_BINARY` 已設就用它、不建，否則先建 `.build/da-guard` 再用它（沒有 Go 時印 ERROR、非零退出）。

**必需參數**

| 參數 | 說明 |
|------|------|
| `--config-dir <PATH>` | 租戶配置目錄 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--policy <FILE>` | 策略 YAML 的**路徑**，內含 `allowed_domains:` 清單（省略＝不限制）。⚠️ 供了但用不了 → exit 2（不是檔案、讀不到、非 UTF-8、不是合法 YAML、頂層不是 mapping），不再靜默略過（#1556）；檔裡沒有 `allowed_domains` 鍵（空檔、只有註解、鍵拼錯）或清單有非字串項目也是 exit 2，要明示不限制請寫 `allowed_domains: []`（#1649）。⚠️ v2.9.0 映像仍是舊行為 | （不限制） <!-- image-caveat: v2.9.0 --> |
| `--rule-packs <PATH>` | `rule-packs/` 目錄的路徑，供自訂規則 lint 使用。⚠️ 供了但用不了 → exit 2；**省略時整個 `custom_rules` 檢查列不會出現**（#1556） | （不跑此檢查） |
| `--policy-dsl <FILE>` | 獨立 Policy-as-Code DSL 檔的路徑（頂層 `policies:` key）。⚠️ 供了但用不了 → exit 2（五種形狀同 `--policy`）；修前的輸出與**完全不給旗標逐字相同**（#1556） | （只讀 `_defaults.yaml` 的 `_policies`） |
| `--version-check` | 一併跑版號一致性檢查 | false |
| `--json` | 以 JSON 輸出結果（供 CI 消費） | false |
| `--strict` | 把 domain-policy（ADR-007）違規從 WARN 升為 FAIL（對齊 CI 的 `generate-routes --strict`）；另外，未加引號、PyYAML 讀成非字串的 matcher 值（`routes[i].match` 的值、`overrides[i].alertname`／`metric_group`，#2431）與不合規的 `group_by` 元素（#2503）會讓 `schema` 列 FAIL | false |

**檢查項目**

- YAML 檔案可用性（可解析、UTF-8 編碼、頂層是 mapping）。⚠️ **後兩項是 v2.9.0 之後才加的**：你手上這顆映像遇到非 UTF-8 或頂層非 mapping 的檔案是丟 traceback、stdout 零位元組 <!-- image-caveat: v2.9.0 -->
- **字串欄位的引號**（`yaml_quoting`）：JSON Schema 標為字串（含 enum）的欄位，值未加引號、而 PyYAML 把它讀成布林、數字或 null 時 FAIL——`channel: yes` 在 PyYAML 是 `True`，在 exporter 與 Alertmanager 是字串 `"yes"`，同一份檔各工具讀到不同的值。每筆列出檔案、行號與欄位路徑；解法是加引號（`channel: "yes"`）。租戶閾值也是字串欄位，所以 `mysql_connections: 70` 會被列出——寫成 `"70"`。租戶檔對照 `tenant-config.schema.json`，`_defaults*` 對照 `platform-defaults.schema.json`（其中 `_routing_defaults`／`_routing_enforced` 沿用租戶 schema 的 routing 定義）；其餘 `_*` 檔不讀。哪些字會被讀成非字串由 PyYAML 自己的 resolver 判定（所以 PyYAML 讀成字串的 `y`／`n` 不會被列出），哪些欄位是字串由 schema 決定（#2164）。⚠️ **v2.9.0 映像沒有這一項** <!-- image-caveat: v2.9.0 -->
- **JSON Schema**（`json_schema`，[#2695](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2695)）：租戶檔對照 `tenant-config.schema.json`、`_defaults*` 對照 `platform-defaults.schema.json`、`_routing_profiles.yaml` 對照 `routing-profiles.schema.json`，判定就是呼叫 `scripts/tools/lint/check_confd_schema.py` 的那一支，不另寫一份。違規時 FAIL、結束碼 `1`，每筆列出檔案、原因與 `@` 後的欄位路徑——例如 `_routing.group_by: alertname`（要的是 list）或 `_routing_defaults` 底下打錯的 key。已在 `yaml_quoting` 列出的未加引號值，這一列當成已加引號來判定，不重報型別錯誤，所以 `mysql_connections: 70` 只出現在 `yaml_quoting`；加了引號仍不合 schema 的值照報，例如 `group_wait: 30`（`"30"` 沒有單位）或 `_silent_mode: yes`（`"yes"` 不在允許值內）。環境沒有 Python 套件 `jsonschema`，或旁邊沒有 `check_confd_schema.py`（**da-tools 映像兩者都沒有**）時，這一列是 **WARN**（結束碼不變），明細寫 `Not checked against the JSON Schema: …`：這一列沒有給出結論。找不到 schema 檔是 FAIL、`caller_error: true`、結束碼 `2`，與 `yaml_quoting` 相同。⚠️ **v2.9.0 映像沒有這一項** <!-- image-caveat: v2.9.0 -->
- **產生器 key 檢查**（`schema`）：route generator 讀設定時的 key 檢查——租戶 key 對照平台宣告的 defaults 與保留鍵，另含 domain policy。⚠️ 列名是歷史名稱（`--json` 消費端依賴它，所以不改名），**這一列不做 JSON Schema 驗證**，那是上一項 `json_schema`。`_routing_enforced.enabled` 不是布林（`n`、`'yes'`、`'true'`、`~` 等），或其他布林／列舉欄位型別不對、值不認得時 FAIL：平台強制（NOC）路由**不會**啟用，`generate-routes` 在所有模式拒收整棵樹（#2164、[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)，見 [generate-routes](#generate-routes) 的「布林與列舉欄位」）。⚠️ v2.9.0 映像遇到非空字串會**啟用** NOC 路由 <!-- image-caveat: v2.9.0 -->
  - 路由區塊裡 route generator 不讀的鍵（`continue`、`mute_time_intervals`、拼錯的鍵等，見 `generate-routes` 的「產生器不讀的鍵」；`_routing_profle` 這類產生器不讀的 `_routing*` 鍵亦同，#2983）讓這一列 FAIL、結束碼 1（#2960）；這一列不靠 jsonschema，在 da-tools 映像裡同樣生效。⚠️ v2.9.0 映像不報這些鍵 <!-- image-caveat: v2.9.0 -->
- 路由規則驗證（group_wait/group_interval/repeat_interval 在允許範圍）。時長寫成 Alertmanager 不接受的寫法（`1.5h`、`30m1h`、`1ns` 等，見 `generate-routes` 的「時長寫法」）時 FAIL、結束碼 1（#2490）。兩個來源產生同名 receiver 時 FAIL，與 `generate-routes --validate` 用同一個判定（#2279）。⚠️ v2.9.0 映像對同名 receiver 回報 PASS <!-- image-caveat: v2.9.0 -->
  `routes` 列與 `generate-routes --validate` 呼叫**同一支判定**，不是各寫一份（#2311）：略過的項目、同名 receiver、產生出來的 inhibit rule 會壓掉 Watchdog 或讓租戶靜音平台告警、在內建 base 上組裝時被平台不變式拒絕、以及 Alertmanager 自己的 parser（`amtool check-config`）拒收——任一項 FAIL、結束碼 `1`，和 `--validate` 同一個結論。⚠️ 例外：租戶存在、卻**沒有產生任何 route／inhibit rule** 的樹（例如只寫了 `_severity_dedup: disable`），`--validate` 印 `No valid routes or inhibit rules generated.`、結束碼 `1`，這一列則是 WARN、結束碼 `0`。PATH 上找不到 `amtool` 時這一列是 **WARN**（結束碼不變），明細多一行 `Not validated by Alertmanager: amtool not found on PATH …`：其餘檢查都過了，但只有 Alertmanager 會拒收的值（例如 webhook URL `http://[1]/`）這一列看不到。`amtool` 在 PATH 上卻跑不出結論（跑不起來、逾時、崩潰）時是 FAIL、`caller_error: true`、結束碼 `2`，與 `--validate` 相同。⚠️ amtool 驗的是組裝在**內建預設 base** 上的設定，不是你自己的 `--base-config`。⚠️ v2.9.0 映像的這一列只做略過項目與同名 receiver 兩項 <!-- image-caveat: v2.9.0 -->
- Policy 檢查（webhook 域名）——**只在給了 `--policy` 時才會出現這一列**
- 自訂規則 lint（`rule-packs/` 的 deny-list）——**只在給了 `--rule-packs` 時**
- Profile 參照（租戶的 `_profile` 有沒有綁到 profile）：判定取 exporter 自己的答案（經 `da-guard effective` 的 `profile`，[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)），租戶從整棵樹讀、子目錄的租戶也算；profile 定義在任何根目錄平台檔的 `profiles:` 都算數（不只 `_profiles.yaml`）。`_profile` 看寫法＋繼承：寫在租戶自己條目（租戶檔或根目錄平台檔的 `tenants:`）卻沒綁到的報「unknown profile」；從 `_defaults.yaml` 繼承來的報「exporter 不從 defaults 檔綁 profile」並點名該檔。沒有 `tenants:` 的檔不算租戶：這一列在明細點名（`not a tenant: <檔>: …`，`--json` 也有），狀態不因此改變。exporter 丟掉或讀不到的檔、被拒收的樹，這一列 FAIL 並附 da-guard 的原因；`--config-dir` 底下完全沒有設定檔（exporter 對它拒絕啟動）、找不到 da-guard，或 da-guard 本身失敗時 FAIL、結束碼 `2`（`yaml_syntax` 列另有點名的無法使用檔案（其 `unusable_files`）時為 `1`，與找不到 da-guard 相同；[#2725](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2725)）。da-guard 本身失敗指：跑不起來（例如沒有執行權限）、回了子命令不會用的結束碼、輸出不是它的 JSON 或缺本工具要讀的欄位（例如 `skipped`），或不認得本工具用的子命令與旗標（例如已發布的 v2.9.x 沒有 `effective`）——最後一種以同一組參數加 `-h` 的結束碼判定，不寫暫存檔、不讀 stderr。這幾種的建議是換新的／重建 da-guard（`make da-guard-build`），不是改設定檔。⚠️ 已知限制：da-guard 的子命令本身逾時或被 signal 終止（例如 OOM）時不在此列，該列照舊歸給設定樹、結束碼 `1`；子命令已回 exit 2、之後的版本探測（`-h`）逾時或被終止時，則會被報成太舊（結束碼 `2`）。閾值 profile 寫了路由層的鍵時這一列 FAIL、結束碼 `1`，每個 (檔案, profile, 鍵) 一行；判定取 route generator 自己記下的 [`routing_in_unread_location`](#guard)（`_grar_parse._unread_routing`，只取 profile 內的那些；哪些鍵、為什麼見該列；[#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904) 之前取 da-guard 的同名 finding），所以涵蓋任何根目錄平台檔的 `profiles:`、也包含沒有租戶綁定的 profile，樹上沒有租戶時亦同（[#2914](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2914)；先前只對 `_profiles.yaml` 發 WARN）。generator 讀不了這棵樹時這一列 FAIL、說明這項沒判（不會當成沒有 finding）。⚠️ profile 本身其餘的結構檢查（空 profile、含其他保留鍵如 `_silent_mode`、`_state_*` 時 WARN；`_metadata` 不警告，它照常補入租戶）與 `N profiles defined in _profiles.yaml` 的計數仍只讀 `_profiles.yaml`。⚠️ v2.9.0 映像只讀各租戶檔自己的 `_profile`、只認 `_profiles.yaml` <!-- image-caveat: v2.9.0 -->
- 版號一致性——**只在給了 `--version-check` 時**
- Policy-as-Code DSL 評估（`_defaults.yaml` 的 `_policies`，或 `--policy-dsl`）；規則讀不懂（見 `evaluate-policy` 結束碼 `2` 那一列，#2961）時不評估任何規則：寫在 `_defaults.yaml` 的這一列 FAIL、結束碼 1，寫在 `--policy-dsl` 檔的（含 `policies:` 以外的頂層鍵）是呼叫端錯誤、結束碼 2；規則讀的值與 `evaluate-policy` 相同（閾值看 `/metrics` 發出的數字，見該節，#2115），租戶讀不到時這一列 FAIL 並附 da-guard 的原因（空的 `--config-dir`、找不到 da-guard 或 da-guard 本身失敗時結束碼 `2`，`yaml_syntax` 列另有點名的無法使用檔案（其 `unusable_files`）時為 `1`，同 Profile 參照）。`/metrics` 沒有送出任何閾值（例如根目錄沒有 `defaults:`），或有設定了卻沒送出的閾值值（served-values 的 `unserved`）時，明細另起一行點名；判定不變，閾值規則只比 `/metrics` 實際送出的數字（[#2725](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2725)）
- **租戶宣告唯一性**：同一個租戶 id 被**兩個檔案**同時宣告時 FAIL。⚠️ exporter 對這個狀態的回應是**拒載整個 config dir**（`DuplicateTenantError`），所以後果不是「那一個租戶失去告警」，而是**這棵樹裡每一個租戶都失去告警**，而且發生在部署／重啟當下、CI 通過之後。最常見的成因是編輯器在 `db-a.yaml` 旁邊留下一份 `db-a.yml`，但判準是「一個 id、兩個檔」——換成 `archive/db-a.yaml` 一樣會擋（#1577）。⚠️ **v2.9.0 映像沒有這一項**：同一棵樹在那顆映像上回報 `Result: PASS`、exit 0 <!-- image-caveat: v2.9.0 -->
- **根目錄 defaults**（`root_defaults`）：依 exporter 的解法檢查**根目錄** `_defaults.yaml` 的 `defaults:`，三類 FAIL。其一是值：exporter 把根目錄 `defaults:` 當 `map[string]float64` 解，**解不成數字的值**（`"70"`、`disable`、mapping、list、布林、日期等）會讓 exporter **丟掉整個 `defaults:` 區塊**、所有平台閾值一起失效，而載入照樣回報成功；**空值**（`k:`、`~`、`null`）會被指出沒有值。[#2518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2518) 之前它被當成 0 閾值送出；現在它實際改變什麼，取決於樹的其他部分（另一種拼法、`optional_overrides:`、`<<:` 合併），這一列不下判斷。判定與 `deprecate` 的載體體檢共用同一個 yaml.v3 鏡射（#1414）。其二是路由：`defaults:` 底下出現 `_routing` 或任何 `_routing` 前綴的鍵，不論值為何都 FAIL。`defaults:` 只放數值閾值；路由預設值寫在頂層的 `_routing_defaults:`。⚠️ 在這裡放一個 `_routing` mapping，損失的不只是路由：exporter 把根目錄的 `defaults:` 當成純數值讀取，解不進去就**整個區塊丟棄——所有平台閾值一起失效**，而載入本身照樣回報成功；路由產生器也從不讀 `defaults:`。其三是包裝（[#2386](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2386)）：根目錄 `_defaults.yaml` 的 `defaults:` 不是 mapping（沒有這個鍵，或有鍵沒有值）時，defaults 合併讀整份文件，`/effective` 顯示頂層的鍵，而 exporter 的根目錄解析只從 `defaults:` mapping 讀閾值、其他頂層鍵沒有對應欄位，`/metrics` 不會從這個檔帶出它們；頂層有下列鍵就 FAIL 並列出：閾值（不以 `_` 開頭的鍵）與 `_routing` 開頭以外的保留鍵（例如 `_severity_dedup`、`_silent_mode`、`_state_*`）。不列入：exporter 根目錄設定結構的欄位（例如 `state_filters`、`max_metrics_per_tenant`）、其他工具從頂層讀的 `_routing_defaults`／`_routing_enforced`／`_custom_alerts`、defaults 合併在每一層都丟掉的 `_metadata`、`_routing` 開頭的鍵（route generator 不從 defaults 檔讀 routing，寫錯位置由 `routing_in_unread_location` 報）、其他 `_` 開頭的鍵（例如只用來掛 YAML anchor 的 `_x: &x`）。實測：頂層閾值不出現在 `/metrics`；頂層 `_severity_dedup: disable` 在 `/effective` 顯示 `disable`、`/metrics` 送 `enable`。子目錄的 `_defaults.yaml` 不在這一列的判定範圍（#2291）。⚠️ **v2.9.0 映像沒有這一項** <!-- image-caveat: v2.9.0 -->
- **defaults 包裝**（`defaults_wrapper`，[#2386](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2386)）：每一層的 `_defaults.yaml`（每個目錄取 exporter 選用的那一份）中，`defaults:` 是 mapping（`{}` 也算，YAML `!!set` 也算：exporter 把它解成 mapping）、頂層又有會進入 `/effective` 合併結果的鍵就 FAIL，列出檔案與鍵。這些鍵是：閾值（不以 `_` 開頭的鍵）與 `_routing` 開頭以外的保留鍵（例如 `_severity_dedup`、`_silent_mode`、`_state_*`）。不列入：exporter 根目錄設定結構的欄位（例如 `state_filters`、`max_metrics_per_tenant`）、其他工具從頂層讀的 `_routing_defaults`／`_routing_enforced`／`_custom_alerts`、defaults 合併在每一層都丟掉的 `_metadata`、`_routing` 開頭的鍵（route generator 不從 defaults 檔讀 routing，寫錯位置由 `routing_in_unread_location` 報）、其他 `_` 開頭的鍵（例如只用來掛 YAML anchor 的 `_x: &x`）。`defaults:` 是 mapping 時，defaults 合併只讀這個 mapping，頂層這些鍵不會出現在任何租戶的 `/effective`；沒有 `defaults:`、或 `defaults:` 沒有值時，合併讀整份文件，不報。實測（子目錄）：包成 mapping 後，頂層的閾值與 `_severity_dedup` 不再出現在 `/metrics`；`_silent_mode`、`_state_*` 只影響 `/effective`，任何寫法都不出現在 `/metrics`。子目錄檔的保留鍵不建議移進 `defaults:`（子目錄 defaults 不支援保留鍵），訊息改請你從該檔刪掉，是否改寫在租戶條目由 da-guard 的 `subtree_default_reserved_key` 依鍵判斷（此時 Suggested action 也不再建議「讓 `defaults:` 不給值」）（[#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)）。⚠️ **v2.9.0 映像沒有這一項** <!-- image-caveat: v2.9.0 -->
- **排程裡的 null**（`schedule_null`，[#2708](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2708)）：閾值寫成帶時段覆寫的排程、而排程裡有 null（某個時段是 null、時段的 `window:` 或 `value:` 是 null，或 `default:` 是 null 而旁邊有時段）就 FAIL，列出檔案、位置與 null 在哪。檢查的位置：每個租戶檔的 `tenants:`、每個目錄選用的 `_defaults.yaml` 的 defaults 區塊（沒有 `defaults:` mapping 時是整份文件）、根目錄 `_` 檔（未選用的 defaults 載體除外）的 `tenants:` 與 `profiles:`；子目錄裡不是 defaults 載體的 `_` 檔不檢查。沒有時段、`default:` 是 null 的排程不在這一列。⚠️ **v2.9.0 映像沒有這一項** <!-- image-caveat: v2.9.0 -->
- **沒照寫送出的閾值值**（`values_not_served`，[#2065](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2065)）：讀 `da-guard effective` 的 `not_served`（exporter 自己的 resolver 與 build 的記錄，這一列不另判斷 YAML），有下列任一就 FAIL，每筆一行列出檔案、租戶、key 與 reason：`value_unparsed`（值不是 exporter 讀得懂的數字，例如 `abc`、list、`7O:critical`，`/metrics` 改送平台預設值）、`value_unparsed_dropped`（同上，但 key 是 `_critical`、維度 key 或 `optional_overrides:` 宣告的 key，沒有預設值可退，該 series 不送）、`window_invalid`（排程覆寫的 `window:` 不是 UTC `HH:MM-HH:MM`、時或分超出範圍、缺 `window:`，或起訖相同如 `05:00-05:00`，該時段永遠不生效）、`value_rejected`（子目錄 `_defaults.yaml` 的值不是閾值形狀，例如 `overrides:` 寫成字串，底下租戶沿用較淺層的值）、`spelling_duplicate`（勝出那一層的同一個 mapping 把一個閾值寫成兩種拼法——維度 key 的 label 順序或引號不同，或 #1231 新舊兩名，[#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)）。只看閾值 key（不以 `_` 開頭）；子目錄 defaults 裡的保留鍵由 da-guard 的 `subtree_default_reserved_key` 報（routing 鍵由 route generator 判）。點名的集合與 da-guard 的 `value_not_served` 相同（有測試）；exporter 載入與 reload 時以 `da_config_values_not_served{reason}`（以（租戶, key）計）與一行只點名租戶與 key 的 WARN 報同一個集合。讀不到樹時與 `profiles` 列同一種 FAIL 與建議；da-guard 的 `effective` 輸出缺 `not_served` 時視為 da-guard 太舊（FAIL、結束碼 `2`）。因為沒有宣告而根本沒送出的值（served-values 的 `unserved`，例如根目錄沒有 `defaults:` 的樹）不在這一列，PASS 也不表示每個值都有送出。`/metrics` 送出的值不因此改變。⚠️ **v2.9.0 映像沒有這一項** <!-- image-caveat: v2.9.0 -->
- **沒有讀取端讀的鍵**（`unread_keys`，[ADR-038](adr/038-unread-keys-judged-by-readers.md)）：以共用判決（`da-guard served-values --disable-effects`、`da-guard effective` 與樹上的 `_policies`）判每個租戶的每個 key 有沒有讀取端讀，與 `da-tools explain-keys` 同一份判決。**只報不擋**：每個問題一行 WARN，寫出檔案、各閘門改成「判到就擋」之後的等級（`error`／`warning`／`notice`）、種類（`unread`、`disable_noop`、`disable_partial`、`platform_violation`）、key、原因與受影響的租戶；平台檔或 profile 的同一個問題只有一行。結束碼不因這一列改變。沒讀到任何根平台檔時另列一行，閾值鍵無從判斷（`undecidable`）。da-guard 不可用或失敗時這一列是 WARN「verdict incomplete」，不是 FAIL。哪個閘門何時改成擋下見 [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)。⚠️ **v2.9.0 映像沒有這一項** <!-- image-caveat: v2.9.0 -->
- **自訂告警兩個讀取端不一致**（`custom_alert_sources`，[#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)）：把自訂告警編譯器實際收下、要產生規則的 recipe，和 exporter（`da-guard served-values`）送出門檻的 recipe，比對；兩邊都用各自的程式判斷，這一列不自己讀 YAML。編譯了、exporter 卻沒送的，規則的 join 永遠是空的，**永遠不會觸發**（例如寫在 `_defaults.yaml` 頂層、由編譯器繼承的 recipe；子目錄裡 exporter 不讀的 `_` 檔的 `tenants:`；或根平台檔的 `tenants:` 與租戶自己的條目都寫了 recipe——exporter 每個租戶只取一個來源，租戶自己的優先，編譯器則兩邊都收）；規則是整份規則包共用的，每個（`recipe_id`、severity）一條、再按租戶 join，所以 exporter 送了、而整份規則包裡沒有任何租戶的 recipe 產生同一個（`recipe_id`、severity）的，才**永遠不會告警**（例如根 `_defaults.yaml` 的 `tenants:`、profile，或因與繼承來的同名 recipe 衝突而被隔離的租戶 recipe）；同一個租戶有多條同形狀、同 severity 的 recipe 時，exporter 每條都送、編譯器至多收一條，這一列以一行列出 exporter 送出的那幾條（不判斷編譯器收了哪一條、或一條都沒收）。exporter 視為關掉的 recipe（門檻寫成 `disable`、`disabled`、`off` 或 `false`，不分大小寫；SLO 的 objective 寫成 `disable`）本來就編譯但不送，不列入。比對的是規則實際 join 的（`recipe_id`、severity）與租戶，名稱只用來顯示。**只報不擋**：每個問題一行 WARN，寫出來源、名稱（同形狀多條時為多個名稱與 severity）、`recipe_id` 與受影響的租戶；結束碼不因這一列改變。任一邊讀不了（da-guard 不可用或失敗、有 exporter 讀不了的檔、編譯器走不下去的 `_custom_alerts`，例如子目錄 `_` 檔的 `tenants:` 裡寫成數字）時這一列是 WARN「comparison incomplete」。只在裝有編譯器的地方（repo 內）出現；da-tools 映像沒有編譯器，整列不出現。⚠️ **v2.9.0 映像沒有這一項** <!-- image-caveat: v2.9.0 -->

⛔ **以報表實際印出的列為準**（`Total: N checks` 那一段）。這份清單先前列著一個叫「Tenant 名稱一致性」的項目，而**沒有任何檢查在做那件事**——實測檔名 `hotel.yaml` 宣告租戶 `totally-different`，六項全 PASS、exit 0；同時它漏掉了四個真的會跑的檢查。條件式的那幾項省略對應旗標時**整列不會出現**，不是靜默通過。

**輸出**

驗證結果摘要（通過/失敗列表）。

**JSON 輸出（`--json`）**：stdout 恰好一份 JSON 文件，頂層是**陣列**，每個元素是報表的一列（一項檢查）。列的鍵分兩種——必有鍵每列都在；可選鍵只在下表條件成立時出現，**條件不成立時鍵整個不存在**（不是空陣列或 `null`），所以消費端讀可選鍵要用 `.get()` / `// empty`，不要直接索引。

| 鍵 | 必有／可選 | 型別 | 出現條件與意思 |
|----|-----------|------|----------------|
| `check` | 必有 | string | 檢查名（`yaml_syntax`、`schema`、`routes`……，以實際印出的列為準） |
| `status` | 必有 | string | `pass` / `warn` / `fail`（小寫） |
| `details` | 必有 | string[] | 該列的明細行，可為空陣列 |
| `caller_error` | 必有 | bool | 這個 FAIL 源自呼叫端（路徑、環境、前置工具）而非設定本身；結束碼 `2` 依它判定（見下方結束碼表） |
| `unusable_files` | 可選 | string[] | **只在 `yaml_syntax` 列**、且有讀不到的檔時出現：該列點名的檔 |
| `skipped_unusable_files` | 可選 | string[] | 有檔讀不到時，出現在**其餘會讀 `--config-dir` 的列**：這一列的答案不含這些檔。不讀設定樹的列（例如 `versions`、`custom_rules`）不會有 |
| `skipped_nested_files` | 可選 | string[] | 該列的平面讀取器實際略過了子目錄裡的檔時出現（見下方 [Hierarchical conf.d](#hierarchical-confd)） |
| `suggested_action` | 可選 | string | `status` 不是 `pass` 時出現：建議的下一步 |
| `docs_link` | 可選 | string | 與 `suggested_action` 成對出現：說明頁 URL |

⚠️ 「哪些檔讀不到」要讀兩個鍵：`yaml_syntax` 列在 `unusable_files`、其餘各列在 `skipped_unusable_files`；健康的樹上兩者都不存在。鍵集合由 `tests/shared/test_json_stdout_contract.py` 守住——多出未列在上表的鍵會讓測試紅（#1653）。

`--config-dir` 不是目錄時，`--json` 下 stdout 仍是同形狀的文件：只有一列 `check: "config_dir"`、`status: "fail"`、`caller_error: true`，結束碼 `2`，stderr 照舊印 `ERROR: config-dir not found: …`。⚠️ v2.9.0 映像在這條路徑 stdout 是空的 <!-- image-caveat: v2.9.0 -->

**範例**

```bash
da-tools validate-config --config-dir ./conf.d
da-tools validate-config --config-dir ./conf.d --policy ./policy.yaml
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 沒有 FAIL（可能有 WARN——包含下方 [Hierarchical conf.d](#hierarchical-confd) 那種列沒涵蓋子目錄檔的情況；看 `Result:` 行） |
| `1` | 驗證失敗（一項或多項），或 `--config-dir` 底下有檔案讀不到。⚠️ 命令列上的路徑（`--policy` / `--rule-packs`）讀不到算 `2`，不算這一碼 |
| `2` | 呼叫端錯誤（參數、路徑、環境），不是你的設定有問題。⚠️ v2.9.0 映像不區分這一碼 <!-- image-caveat: v2.9.0 --> |

##### Hierarchical conf.d

**階層式 `conf.d/`（子目錄裡有設定檔）**：schema、routes、policy 三列自 [#2326](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2326) 起與 exporter 一樣讀整棵樹（路由面的階層語意見上方 `generate-routes` 的「階層式 conf.d」）；conf.d 樹被路由面拒收時（例如子目錄有 `_routing_enforced`），schema 列 FAIL、以 `ERROR (routing tree):` 開頭點名。仍有**平面**讀取的是 Policy-as-Code 列找根目錄 `_defaults.yaml` 那一步，exporter 則遞迴讀整棵樹。當某一列的讀取**實際略過了**子目錄裡的檔，那一列就**不會回 PASS**：原本的 PASS 降為 WARN，且不論狀態都多一行具名被略過的檔（前 5 個，其餘 `(+N more)`；完整清單在 `--json` 該列的 `skipped_nested_files`）。只找根目錄 `_defaults.yaml` 的那一步（Policy-as-Code 的 `_policies` 從這裡來）略過的只有子目錄裡的 `_defaults.yaml`。因此只要子目錄裡有 `_defaults.yaml`（標準 ADR-017 樹），Policy-as-Code 列即使沒有任何 `_policies` 也會是 WARN、具名該檔——「沒有 policies」這個答案是沒打開它就得出的。哪幾列受影響是執行時觀測出來的，不是寫死的清單；沒碰到讀取器就回答的列（例如 policy 檔沒有 `allowed_domains`）維持 PASS。⚠️ 觀測不到的：以檔名直接開根目錄檔的讀取——`profiles` 的結構檢查只讀根目錄的 `_profiles.yaml`（租戶的 `_profile` 參照則從整棵樹判定）。Policy-as-Code 列的租戶是從整棵樹評估的（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)），它那一行點名的只是子目錄 `_defaults.yaml` 裡的 `_policies` 不會被讀——把 `_policies` 放在根載體（或用 `--policy-dsl`）。不要攤平樹或把 `--config-dir` 指向子目錄：那會丟掉租戶繼承的根預設值。⛔ **結束碼不帶這個訊號**：WARN 照舊是 `0`——要知道每一列是否涵蓋每個檔，看 `Result:` 或 `--json`，不要看結束碼（#1652）。⚠️ v2.9.0 映像沒有這項：同一棵樹在那顆映像上是 `[PASS] routes  0 routes`、`Result: PASS` <!-- image-caveat: v2.9.0 -->

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
| `--config-dir <PATH>` | 租戶配置目錄。⚠️ 預設指向 repo 內部路徑，映像裡不存在——請明確指定 | `components/threshold-exporter/config/conf.d` |
| `--execute` | **實際執行**（預設只做 Pre-check／預覽，不寫入） | false |
| `--state <PATH>` | tenant-api state store 的副本：state 目錄，或 `kubectl get configmap tenant-api-state -o yaml`（或 `-o json`）存下的檔。用來列出群組成員、views 的 `filters`、federation 子集對這個租戶的引用（[#2883](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2883)） | — |
| `--skip-state-check` | 沒有 `--state` 時仍允許 `--execute`（state store 引用不檢查） | false |

不帶 `--execute` 就是預覽，不需要另外的 dry-run 旗標。

**輸出**

Pre-check 報告：租戶檔的位置、有沒有跨檔案引用這個租戶、它已設定的指標。無法解析的設定檔會被點名，並讓 pre-check 判定失敗（跨檔案引用檢查看不到那份檔的內容）。帶 `--execute` 時直接刪除 `<config-dir>/<tenant>.yaml`，**不會備份**（請先自行備份，或靠 git 還原），也**不會**動 Recording／Alert 規則（清理規則的選項尚未實作）；最後提示要一併清掉 Alertmanager 裡 `tenant=<tenant>` 的路由設定。

群組、views、federation 子集存在 tenant-api 的 state store，不在 conf.d；conf.d 裡若還留著 `_groups.yaml` 等舊檔，只印 ⚠️ 說明已不再被讀，不拿它當答案。有 `--state` 時列出 state store 裡的引用與要呼叫的 tenant-api API；conf.d 殘留的舊檔若 state store 沒有對應紀錄，tenant-api 下次開機會匯入它，所以其中的引用也一併列出（例如 `PUT /api/v1/groups/{id}` 移除成員），工具本身不寫 state store；AccountID 配發只列為資訊（永不回收）。沒給 `--state` 時這一項標為「未判定」，`--execute` 需另加 `--skip-state-check` 才會執行。

**範例**

```bash
# 預設只做 Pre-check（不寫入）
da-tools offboard db-old --config-dir ./conf.d
# 實際執行下架（先存一份 state store 給 --state）
kubectl -n tenant-api get configmap tenant-api-state -o yaml > tenant-api-state.yaml
da-tools offboard db-old --config-dir ./conf.d --state tenant-api-state.yaml --execute
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功：pre-check 通過或只有警告（例如跨檔案引用）；帶 `--execute` 時已刪除租戶檔 |
| `1` | pre-check 未通過（找不到租戶檔、有無法解析的設定檔等 ❌ 項目，報告點名該檔），不論有沒有 `--execute`；或 I/O 失敗（#2179） |
| `2` | 呼叫端錯誤：argparse 拒絕的參數（缺 tenant 位置參數、未知旗標）；`--state` 讀不了、不是 ConfigMap 文件或目錄、沒有任何紀錄，或含有不是 tenant-api state 紀錄的 key（例如存成了 `threshold-config`、或指到 conf.d）；帶 `--execute` 卻沒有 `--state` 也沒有 `--skip-state-check` |

---

#### deprecate

下架指標：從 `defaults:`／`optional_overrides:`／租戶檔移除 `<m>`、`<m>_critical`、`custom_<m>`、`custom_<m>_critical`；租戶平面另含這四個名字的維度鍵 `<名字>{…}`。

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
| `--config-dir <PATH>` | 租戶配置目錄。⚠️ 預設指向 repo 內部路徑，映像裡不存在——請明確指定 | `components/threshold-exporter/config/conf.d` |
| `--execute` | **實際執行**（預設只做 Pre-check／預覽，不寫入） | false |
| `--plane {root,subtree}` | 這個 `--config-dir` 是 conf.d 的 root，還是 exporter `-config-dir` 之下的一層子樹載體。`root` 對這一層所有 `_` 前綴檔做載體體檢（`defaults:` 的值 exporter 讀不讀得進去，依 yaml.v3 的判定）；`subtree` 跳過體檢（子樹平面的字串值合法且生效）。工具無法自己判斷，預設 fail-closed | `root` |

**輸出**

從 `_defaults.yaml`／`.yml` 的 `defaults:` 與 `optional_overrides:`（宣告層，只有名字），以及平面目錄下非 `_` 前綴的租戶檔刪除上述 key，逐 key 印出原值；清空的 `defaults:`／`optional_overrides:` 整個拿掉；載體沒有相關 key 時具名略過、不寫入。**不是**把值寫成 `disable`：`defaults:` 是 `map[string]float64`，字串會讓 exporter 丟掉整份 root 載體（[#1787](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1787)）。載體體檢（見 `--plane`）發現 exporter 讀不進去的檔時整輪降級為預覽：不寫入、rc 1；本工具 pure parser 讀不了的 `_` 檔同樣整輪降級（訊息會標明 exporter 讀得進去）；`defaults:` 的空值只警告。寫入之前（預覽與 `--execute` 都一樣）先以 `da-guard key-refs` 問 exporter（[#1822](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1822)）：root 層 `_` 前綴檔在 exporter 自己的 load 的 `parse_failed`（判的是只把 root 層 `_` 檔套上本輪寫入、放進空的暫存目錄後的樣子；那一次問不到就沿用現況的答案，不放行）或 `unreadable` 裡同樣整輪降級（rc 1）；經 exporter 正規化後屬於該 metric 的 key（alias 表的 legacy 拼法、`_critical`、各種維度鍵拼法）與 canonical key 一樣列為引用、一樣移除，子目錄 `_` 檔裡的這類拼法列為殘留；da-guard 的 stderr 整份照轉（`  da-guard| ` 前綴）。找不到 da-guard（`$DA_GUARD_BINARY` → `$PATH`）或它答不出來（含 da-guard 的任何 exit 2）時印「未體檢」與原因，行為與 rc 照舊，Step 3 另提醒檢查 alias 表的舊拼法。本工具不遞迴寫入子目錄，但完成度重掃會往下看：子樹自有 `_defaults.yaml` 與租戶檔 `tenants:` 的殘留照印出的指引對該子樹跑 `--plane subtree`；root 層 `_` 前綴檔的 `tenants:`／`profiles:` 區塊本工具射程外，殘留需手動移除；exporter 不讀或丟棄的區塊只警告。⚠️ NOT GUARDED：寫回是整份重新序列化，header 以外的註解會被移除（沒刪的未加引號純量照原文寫回，`010`、`yes` 不會改型；加引號的值可能換成單引號或拿掉引號）；平面判定仍靠 `--plane`（`subtree` 不採用 da-guard 對 `_` 前綴檔的判定）；本工具的載體鏡射擋下、exporter 其實收的檔仍擋。

**範例**

```bash
# 下架多個指標
docker run --rm \
  --user $(id -u):$(id -g) \
  -v $(pwd)/conf.d:/etc/config:rw \
  ghcr.io/vencil/da-tools:v2.9.0 \
  deprecate old_metric_1 old_metric_2 \
    --config-dir /etc/config \
    --execute
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `1` | 下架未完成，原因逐一印在輸出裡。⚠️ 預覽模式對同一棵樹給同一個判斷 |
| `2` | 配置目錄無效，或載體寫回失敗 |

---

#### lint

依平台治理政策（deny-list）檢查租戶自訂的 Prometheus 規則檔。

**用途**：CI/CD lint 檢查；Tier 3 自訂規則進平台前的護欄（見 [Custom Rule Governance](custom-rule-governance.md) §4）。

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
| `--policy <FILE>` | 政策檔（`custom-rule-policy.yaml`） | 內建政策 |
| `--ci` | 有 ERROR 級違規時以結束碼 1 結束 | false |

WARN 不會升級成 ERROR，也沒有 JSON 輸出（尚未實作）；CI 請用 `--ci`，輸出是逐條的 `ERROR:`／`WARN:` 文字。

**檢查項目**（內建政策的預設值，可用 `--policy` 覆寫）

- 禁用函式：`holt_winters`、`predict_linear`、`quantile_over_time`
- 禁用樣式：全通配 `=~".*"`、`without(tenant)`
- 必備 label：`tenant`
- range vector 最長 `1h`、rule group 的 `interval` 最長 `60s`
- 缺 `owner` 或 `expiry` label 時出 WARN

**範例**

```bash
da-tools lint ./my-custom-rules.yaml
# 接進 CI 一定要帶 --ci：沒帶時即使有 ERROR 級違規也是結束碼 0
da-tools lint ./rule-packs --ci
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 沒有 ERROR 級違規。⚠️ **未加 `--ci` 時，即使有 ERROR 級違規也是 `0`**；WARN 級從不影響結束碼 |
| `1` | `--ci` 模式下發現 ERROR 級違規（讀不到或 YAML 壞掉的規則檔也記成 ERROR） |
| `2` | 呼叫端錯誤：`--policy` 供了但不可用（不是檔案／讀不到／不是合法 YAML／頂層不是 mapping）；掃描目標不存在（訊息指名哪一個，#1618）。⛔ 不要靠拿掉 `--policy` 或路徑轉綠——那等於改用內建政策 lint、或把沒掃過的目標當乾淨 |

---

#### onboard

反向分析既有的 Alertmanager 設定、Prometheus 規則檔與 scrape config，產出遷移用的 CSV、建議片段與 `onboard-hints.json`。三個輸入各自對應一個 phase，至少要給一個；不讀位置參數。

**用途**：引入現有監控配置；減少手動遷移工作量。

**語法**

```bash
da-tools onboard [--alertmanager-config <FILE>] [--rule-files '<GLOB>'] \
  [--scrape-config <FILE>] [options]
```

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--alertmanager-config <FILE>` | Phase 1：Alertmanager 設定。可以是設定檔本身，或 ConfigMap YAML；⚠️ ConfigMap 的鍵名必須是 `alertmanager.yml`，其他鍵名（例如 prometheus-operator 慣用的 `alertmanager.yaml`）與 Secret 都會解析失敗、rc=2 | — |
| `--rule-files '<GLOB>'` | Phase 2：Prometheus 規則檔的 glob（支援 `**`）。⚠️ 要加引號：沒加時 shell 先展開，配到多個檔就只有第一個被當成值、其餘變成 `unrecognized arguments`（rc=2）；只配到一個檔時 rc=0 卻只分析了那一個（例如沒開 globstar 的 bash 把 `**` 當成 `*`） | — |
| `--scrape-config <FILE>` | Phase 3：Prometheus scrape config | — |
| `--tenant-label <NAME>` | 租戶標籤名稱 | `tenant` |
| `-o, --output-dir <DIR>` | 輸出**目錄**（給檔名會得到一個同名目錄） | `onboard_output` |
| `--dry-run` | 只分析、不寫檔，印出各 phase 摘要 | false |
| `--json` | stdout 改印一份 JSON 報告（CI 用）；各 phase 的檔照寫，但不寫 `onboard-hints.json` | false |

**輸出**

進度與 `Found N tenant route(s)`／`SKIP` 等訊息印在 stderr；`--output-dir` 底下依給的輸入寫出：

| 路徑 | 來源 | 內容 |
|------|------|------|
| `phase1-routing/routing-summary.csv` | Phase 1 | 每個租戶 route 的 receiver 類型、`group_wait`／`group_interval`／`repeat_interval`、severity dedup 判定 |
| `phase1-routing/<tenant>.yaml` | Phase 1 | 可併入 `conf.d/<tenant>.yaml` 的路由片段 |
| `phase2-rules/migration-plan.csv` | Phase 2 | 每條告警規則的 metric、閾值、運算子、建議聚合方式與可否自動轉換（`perfect`／`complex`／`unparseable`） |
| `phase2-rules/_defaults-suggestion.yaml` | Phase 2 | 由規則閾值推得、可併入 `conf.d/_defaults.yaml` 的預設值建議。⚠️ `complex` 規則的閾值也在裡面，合併前要和 `migration-plan.csv` 逐條對過；規則全部 `unparseable` 時不寫這個檔 |
| `phase3-scrape/scrape-analysis.yaml`、`<job>-relabel-suggestion.yaml` | Phase 3 | 各 job 有無租戶對映，以及建議的 `relabel_configs` |
| `onboard-hints.json` | Phase 1（加上 Phase 2 推得的 DB 類型） | 租戶清單、路由提示，以及 DB 類型——⚠️ Phase 2 推得的每一種 DB 類型都掛到**每一個**租戶上（所有租戶共用同一份聯集），`scaffold --from-onboard` 因此會替每個租戶開同一組 pack；Phase 1 沒找到任何租戶 route、或帶 `--dry-run`／`--json` 時不寫 |

**範例**

```bash
# 只分析 Alertmanager
da-tools onboard --alertmanager-config ./alertmanager.yaml -o onboard_output

# Alertmanager 加規則檔，glob 要加引號
da-tools onboard --alertmanager-config ./alertmanager.yaml \
  --rule-files './rules/*.yaml' -o onboard_output
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 至少一個 phase 產出結果（Phase 1 沒找到租戶 route 也算，只是不寫 `onboard-hints.json`） |
| `1` | 沒有任何 phase 產出結果，例如 `--rule-files` 的 glob 一個檔都沒配到、`--scrape-config` 裡沒有 `scrape_configs`。⚠️ `--scrape-config` 檔案不存在也是 1（`--alertmanager-config` 不存在則是 2） |
| `2` | 呼叫端錯誤：三個輸入一個都沒給、參數錯誤，或 `-o/--output-dir` 指到的輸出路徑寫不進去（#1641）；輸入檔讀不到或無法解析（內容不是 UTF-8 或不是合法 YAML；訊息指名哪一檔，#1654）；Alertmanager config 裡的租戶 label 值不是 DNS-1123 label（[ADR-035](adr/035-tenant-id-single-source.md)；含 `--dry-run`／`--json`，什麼都不寫）。⚠️ 例外：ConfigMap 包裝裡內嵌的 YAML 壞掉時是 traceback、回 1。⚠️ v2.9.0 映像不檢查租戶 id <!-- image-caveat: v2.9.0 --> |

---

#### analyze-gaps

比對 custom rule 與 Rule Pack，找出重複/缺口。

**用途**：評估 Rule Pack 涵蓋度；決定是否可刪除 custom rule。

**語法**

```bash
da-tools analyze-gaps (--tenant-config <FILE> | --config-dir <DIR>) [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--tenant-config <PATH>` | 單一租戶配置檔案（整個目錄用 `--config-dir <DIR>`）。⚠️ 不要縮寫成 `--config`：argparse 會把它當成 `--config-dir`，對著一個檔案路徑回答「沒有 custom_ 指標」、rc 0 |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `-o, --output <FILE>` | 另把 JSON 報告寫到檔案 | （無） |
| `--json` | stdout 只印 JSON | false |
| `--metric-dictionary <FILE>` | 指標字典；給了但檔案不存在時結束碼 2 | 工具同層的 `metric-dictionary.yaml`（映像），或上一層（repo 的 `scripts/tools/`） |

`--config-dir` 讀的是 exporter 的 `/metrics` 對每個租戶實際發出的閾值與其值（經 `da-guard served-values`，#2115）：繼承來的 `custom_` 閾值也列入，`disable` 的鍵、沒有預設值而不會發出的鍵不列；沒有 `tenants:` 的檔不是租戶，stderr 逐檔印 `WARN`；exporter 讀不到的檔或子目錄（權限不足、懸空 symlink、無法列出內容的子目錄）以結束碼 2 結束、`ERROR` 行指名該檔（或目錄）與原因（`stat_error`／`read_error`／`walk_error`／`special_file`），指向目錄的 symlink 例外（exporter 本來就不跟進，只跳過、不影響結束碼）；da-guard 在 stderr 印的內容逐行轉印到 stderr，每行前面加前綴 `da-guard|`（前面兩個空格、後面一個空格）；因檔案解析失敗或讀不到而結束時，`ERROR` 行下面附的 da-guard 訊息寫的 `exit 3` 是 da-guard 自己的結束碼，本工具以 2 結束。需要 da-guard（映像內建；repo 裡直接跑時用 `$DA_GUARD_BINARY` 或 `$PATH`）。`--tenant-config` 照舊讀單一檔案的原文。

兩個預設位置都找不到字典時，stderr 印一行 `WARN`，比對退回名稱前綴與字詞重疊（`match_type: "prefix"`、`confidence: 0.7`）。⚠️ v2.9.0 映像不受影響（字典與工具同層）；但在 repo 裡用那個版本的程式直接跑 `python3 scripts/tools/ops/analyze_rule_pack_gaps.py` 時找不到字典，而且不會警告，請帶 `--metric-dictionary scripts/tools/metric-dictionary.yaml`。 <!-- image-caveat: v2.9.0 -->

**輸出**

文字報告：依 Rule Pack 分組，列出每個 `custom_` 指標對應到哪個原始指標與比對方式，例如 `custom_mysql_global_status_threads_connected -> mysql_global_status_threads_connected (exact, 100%)`，最後統計可改用 Rule Pack 的數量。租戶配置裡沒有 `custom_` 指標時只印 `No custom_ metrics found in tenant configs.`。`--json` 時是一個陣列，每個 `custom_` 指標一筆，含 `tenant`、`custom_metric`、`original_metric`、`current_value`、`best_match_pack`、`match_type`、`confidence`、`recommendation` 等欄位。

**範例**

```bash
da-tools analyze-gaps --tenant-config ./conf.d/db-a.yaml
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 成功 |
| `2` | 呼叫端錯誤：參數錯誤；`--config-dir`／`--tenant-config`／`--metric-dictionary` 指到不存在的路徑（訊息指名是哪一個旗標）；`-o/--output` 指到的輸出路徑寫不進去（#1641）；輸入檔讀不到（內容不是 UTF-8 或不是合法 YAML；訊息指名哪一檔，#1654）；`--config-dir` 底下有 exporter 解析失敗而整份跳過的檔或讀不到的檔或子目錄（權限不足、懸空 symlink；指向目錄的 symlink 除外；`ERROR` 行指名哪一檔，da-guard 的 stderr 逐行附在下面，每行加固定前綴，見上方說明）、整棵樹被 exporter 拒收，或找不到 da-guard／da-guard 執行失敗（#2115）。⚠️ v2.9.0 映像對不存在的輸入路徑回 `0`，當成沒有 `custom_` 指標 <!-- image-caveat: v2.9.0 --> |

`walk_error` 不看目錄裡有沒有設定檔：`--config-dir` 底下任何一個執行身分列不出內容的子目錄（例如權限不足的 `docs/`，或 conf.d 剛好是 ext4 volume 根目錄、以非 root 執行時的 `lost+found`）都會讓本工具以 2 結束；以 `.` 開頭的目錄（例如 `.git`）不算，exporter 的載入本來就不進去。解法是把 `--config-dir` 指向不含該目錄的子路徑，或調整權限讓執行身分可以列出它。

---

#### config-diff

比較兩個配置目錄（conf.d）各自「渲染」出來的結果，產出 blast radius 報告：每個變更影響哪些租戶。

**用途**：GitOps PR review；平台檔（`_defaults.yaml`、`_profiles.yaml`、根目錄 `_platform.yaml`、路由預設）的變更也會報出它影響到的租戶。

**語法**

```bash
da-tools config-diff --old-dir <path> --new-dir <path> [options]
```

**必需參數**

| 參數 | 說明 |
|------|------|
| `--old-dir <PATH>` | 舊配置目錄（base）。不存在或沒有設定檔時視為第一次匯入，所有租戶都算新增 |
| `--new-dir <PATH>` | 新配置目錄（PR） |

**選項**

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--at <RFC3339>` | 讀排程、靜默與維護時段的時間點，兩側共用並印在報告上；CI 建議傳 base commit 的時間（`git show -s --format=%cI <base>`） | 執行當下 |
| `--format {markdown,json}` | 輸出格式 | `markdown` |
| `--json-output` | 等同 `--format json` | false |

**比較什麼**（[ADR-037](adr/037-config-diff-compares-rendered-output.md)）：config-diff 不自己讀 YAML，而是兩側各跑一次把 conf.d 變成線上設定的兩支程式，再比較結果：

- `da-guard served-values --at <時間> --schedules --disable-effects`：exporter 對每個租戶實際送出的閾值、severity、設定、state filter、custom alert，以及每個 `disable` 是否真的少送了什麼。
- 路由產生器 `generate_alertmanager_routes.py --output-configmap`：完整的 Alertmanager 設定。route 節點以 matcher 路徑為身分，比較 receiver、`group_by`、`group_wait`、`group_interval`、`repeat_interval`、`continue`；另比較 receiver 定義與 inhibit rules。

兩支都需要 da-guard（映像內建；repo 內先 `make da-guard-build`，或設 `DA_GUARD_BINARY`）。

**報告區段**：Not Computed / Not Compared（有面向沒算出結果時）、Platform Settings、Tenants Added / Removed、Routing Changes、Custom Alert Changes、Tenant Setting Changes、Keys the Exporter Drops、Other Served Changes，以及閾值變更表（Threshold Changes）。每個區段裡同一個變更只列一次，後面寫它影響的租戶：一個時寫租戶名，多個時寫租戶數與前 5 個名字，所以一個平台預設改到 1000 個租戶也只占一行。值來自 PR 的檔案：表格以外放在 code span 裡，閾值表的欄位則跳脫 `|` 與換行；receiver 的憑證欄位只報「有變」，前後值都遮成 `<redacted>`；同一個憑證值（字串或數字）出現在其他欄位（YAML anchor／alias 或手抄）時也遮掉，報告頂端註記遮了幾處。比對的是完整的值，8 個字元以上的值出現在較長的字串裡也遮；只抄一部分、或改寫過（大小寫、URL 編碼）的副本遮不到。

**變更分類**（閾值表）

| 分類 | 含義 | 影響 |
|------|------|------|
| `tighter` | 閾值下降 | 可能增加告警 |
| `looser` | 閾值上升 | 可能減少告警 |
| `added` | 新增 metric key | 新增 alert 覆蓋 |
| `removed` | 移除 metric key | 失去 alert 覆蓋 |
| `toggled` | enable ↔ disable | 開啟或關閉 alert |
| `modified` | 複雜值變更 | 需人工審閱 |

租戶寫了、但 `/metrics` 沒有出列的閾值 key（served-values 的 `unserved`，例如 base 不在任何 defaults carrier 裡；值為 `disable` 等關掉的不算），在表裡的值後面標 `(not served)`，JSON 該筆另帶 `not_served`（沒出列的那一側，`"old"`／`"new"`）。一側出列、另一側寫了但沒出列的 key，開始出列報成 `added`、停止出列報成 `removed`，即使寫的數字沒變——那正是告警開始或停止生效的變更（[#3022](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3022)）。

**平台設定（`_` 開頭檔的頂層鍵）**：兩側的 `_` 開頭檔（任何深度）以 `da-guard parse-tree` 讀出的值逐鍵比較，只改註解、縮排、引號或鍵的順序不算變更。值變了的鍵列在「Platform Settings」，依報告能說的分組：渲染結果有差異的列為 Rendered（見下方各區段，不判定哪一筆差異來自這個鍵）；標為完整涵蓋、讀取端就在這個位置讀它（parse-tree 的 `rendered_by`）、而渲染結果沒有差異的，列為「沒有效果」，不影響結束碼（[#2962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2962)）；部分涵蓋而渲染結果沒有差異的、寫在讀取端不讀的位置的（例如非預設檔裡的 `defaults`、子目錄 `_` 檔裡的 `tenants:`）、兩支渲染程式都不讀的鍵（平台層的 `_custom_alerts`、`instance_tenant_mapping`、`max_metrics_per_tenant`，以及 tenant-api 相關檔裡的 `tenant_orgs`、`groups` 等，這類鍵的非字串值以原文比較）、讀不了的檔、沒有讀取端讀的 `_` 檔（以位元組比較），以及寫在檢查工具不讀的位置的 `_policies`／`domain_policies`，結束碼至少為 1。`_policies` 寫在根目錄的預設檔、`domain_policies` 寫在 `_domain_policy.yaml` 時只列為提示（由 `validate-config` 與產生器的 `--validate --strict` 檢查），不影響結束碼。parse-tree 本身失敗、或它的輸出沒有 `rendered_by`（da-guard 比本工具舊）時結束碼 2。規則見 [ADR-037](adr/037-config-diff-compares-rendered-output.md) 決定 3。

**Custom Alert 區段不判定消音**：讀的是 exporter 送出的 row，`threshold` 是 `<值>:<severity>`，`op`／`window`／`for` 的變更以 `recipe_id` 不同呈現；不再送出的 recipe 列為 removed，並註明分不出是刪除、停用或被 exporter 拒收。

**JSON 輸出**：頂層 `schema` 為 `da-tools.config-diff/v1`，另有 `at`、`exit_code`、`masked_by_value`（憑證值出現在非憑證欄位而被遮的處數）、`views`（`served` 與 `routes` 各自的 `status`：`computed`／`not_computed`／`not_compared`，與 `reason`）、`tenants_added`、`tenants_removed`、`uncovered_files`（有要審的平台設定的檔）、`platform_settings`（每個值變了的鍵一筆；沒有讀取端讀的檔與讀不了的檔整檔一筆，`key` 為 null：`file`、`key`、`change`、`status`、`note`、`views`）。`views` 另有 `platform`（parse-tree 的讀取結果）。各類變更一筆一個項目，帶它影響的租戶清單（`tenants`）：`threshold_changes`、`setting_changes`、`custom_alert_changes`、`dropped_key_changes`、`other_served_changes`、`route_changes`（另帶 `paths`）、`receiver_changes`（另帶 `receivers`）、`inhibit_changes`。同一個平台變更影響多個租戶時只有一筆。`disable_warnings`（同樣帶 `tenants`）列出這次變更新引入、沒有作用（`disable: noop`：拿掉它 exporter 送出的值完全不變，常見原因是 key 拼錯）或只關到一部分（`partial`：key 關掉了，`served_family` 列出的 `_critical`／維度鍵仍在送出）的 `disable`，base 那側同樣狀態的不列；Markdown 報告對應「`disable` With No or Partial Effect」一節。它只是提醒，不單獨讓結束碼變成 1（[ADR-038](adr/038-unread-keys-judged-by-readers.md) 決定 8）。

⚠️ v2.9.0 映像仍是舊行為：只比對頂層各租戶檔自己寫的值，平台檔只列檔名，沒有 `--at`，JSON 是沒有 `schema` 的舊結構（`metric_diffs`、`profile_diffs` 等）。 <!-- image-caveat: v2.9.0 -->

**範例**

```bash
da-tools config-diff --old-dir ./conf.d-old --new-dir ./conf.d-new
da-tools config-diff --old-dir ./conf.d-old --new-dir ./conf.d-new --at 2026-10-10T03:00:00Z
da-tools config-diff --old-dir ./conf.d-old --new-dir ./conf.d-new --json-output
```

**結束碼**

| 代碼 | 說明 |
|------|------|
| `0` | 無配置變更 |
| `1` | 偵測到變更；或有要審的平台設定（見上方「平台設定」：值變了而渲染沒有差異的鍵、兩支渲染程式都不讀的鍵、寫在讀取端不讀的位置的鍵、讀不了的 `_` 檔；只改註解或排版，以及寫在檢查會讀的位置的 `_policies`／`domain_policies`，不算）；或 base 側設定壞了、渲染不出來（該面向列為 Not Compared，不擋修好它的 PR） |
| `2` | 沒有算出結果：PR 側有檔讀不了（stderr 點名該檔與 exporter／產生器自己的原因）、PR 側沒有設定檔、找不到 da-guard 或工具本身失敗，或參數錯誤（例如 `--at` 不是 RFC3339、目錄位置是一個檔）。上述情況中，參數錯誤以外報告仍會輸出並寫明哪個面向沒有計算（`da-guard parse-tree` 失敗時是平台設定面向沒有計算）；工具未預期的錯誤則只在 stderr 印一行 `ERROR:`，不輸出報告 |

> ⚠️ **`1` 是「有變更」，不是失敗。** 裸呼叫這個命令的 CI 步驟，會在它正常運作時失敗。
> 可照抄的消費端寫法（`set +e` / `rc=$?`）在
> [GitOps CI 整合 §2.3 Stage 2: Generate](scenarios/gitops-ci-integration.md)；
> 同一份結束碼契約在 [GitOps 部署整合](integration/gitops-deployment.md) 也有一份。

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

**哪些寫法會被拒**（[#2987](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2987)；主規則與 `when` 同一套，任一條命中即結束碼 `2`、不評估任何規則，訊息指名第幾條規則、哪個欄位、為什麼）：

- `required`／`forbidden` 只看有沒有值，不收 `value`（沒寫 `operator` 時缺省為 `required`，`value` 會被默默忽略）。
- 其他運算子都要有 `value`：沒有時原本拿 null 比——`not_equals` 永遠成立、`when` 永遠不成立而跳過規則、主規則的 `gte`／`matches`／`contains` 每個租戶都報違規。
- `one_of` 的 `value` 必須是非空清單（單一值也寫成 `[mariadb]`），項目為純量；`equals`／`not_equals`／`contains`／`matches` 與 `gte`／`lte`／`gt`／`lt` 的 `value` 是純量，後四者必須是有限的數字或 duration（`5m`），`inf` 不算。
- `matches` 的 `value` 必須能編譯成 regex，且不是空字串、不超過 200 字、沒有 `(a+)+` 這類巢狀量詞（防護擋下兩種寫法：可重複超過一次的群組裡有次數不固定的量詞，例如 `(a+)+`、`((a+))+`、`(a+){2,}`、`(a?)+`；以及群組裡有 `+`／`*`、群組本身又帶量詞，連可選群組 `^(\d+)?$` 也算，改寫成 `^\d*$` 或改用 `one_of`。固定次數的 `(a{2})+` 不擋；引擎的 ReDoS 防護不會執行它們，規則等於永不匹配）；`contains` 的 `value` 不可為空字串。
- 萬用字元 `target`（含 `*`）只比對頂層鍵：帶 `.` 的（例如 `_metadata.*`）原本被當字面路徑、找不到就跳過，現在拒收——改寫成具體欄位（`_metadata.owner`）。萬用字元 `when` 只收 `required`／`forbidden`。

**規則讀的是哪一個值**（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）：`--config-dir` 讀生效值，不是租戶檔的字面內容；`target` 與 `when` 依 key 種類讀：

- **閾值**（不以 `_` 開頭）：exporter 在 `/metrics` 實際發出的數字（經 `da-guard served-values --schedules`）。值寫在根 `defaults:`、平台檔 `tenants:`、租戶檔或子目錄 `_defaults.yaml` 都一樣，子目錄裡的租戶也會評估。排程閾值逐時段比對，任一時段違規即違規（訊息附 UTC 時段）；某時段 `disable` 時該段只有 `required` 算違規。整天都不發出的鍵（`disable`、沒有預設值）視同沒有：`required` 對有寫但 exporter 不發的鍵報「已配置（值: …）但 exporter 不發出」，`when` 不論精確或萬用字元 target 都當它不存在，其他運算子略過。萬用字元 target 的 `when` 只支援 `required`／`forbidden`（取值運算子對一組鍵沒有意義）。數字以數值比較（`equals: 80` 等於發出的 `80.0`）。`target` 寫舊拼法（例如 `mysql_cpu`）時以 exporter 的別名表換成現行拼法；寫成 `<key>_critical` 鍵的 critical 列以 `<key>_critical` 比對，寫成 `"95:critical"` 的值仍以 `<key>` 比對（該列 severity 為 critical）。`when` 對排程閾值：任一時段成立即成立。
- **保留鍵**（`_` 開頭，`_routing` 除外）：寫法＋繼承（經 `da-guard effective`），只收租戶可寫的保留鍵（根層專用的 `_policies`、`_routing_defaults` 等不算租戶的）；exporter 自動補的預設值（`_severity_dedup: enable` 等）不算有寫。`_metadata` 例外：`/effective` 不帶它，改取 `/metrics` 的 `_metadata`（平台檔 `tenants:` 與租戶檔逐鍵合併，[#2370](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2370)），去掉 exporter 補的空欄位（空字串、空 list）後才算有寫——所以明寫的 `owner: ""` 也算沒寫（`forbidden`／`not_equals: ""` 不再對它報）；exporter 不認得的欄位（例如 `cost_center`）不在其中；`_metadata` 整份解析失敗時（例如 `tags: foo`）exporter 不送任何欄位，`required _metadata.owner` 照樣報「未配置或為空」（da-guard 沒有結構化訊號可分辨，原因只在 stderr 轉印的 da-guard WARN 裡）。只評估 exporter 認得的租戶保留鍵；其他 `_` 開頭的鍵（例如 `_foo`）不在 policy 的視野內，`forbidden: _foo` 不會響。
- **`_routing`**：路由產生器解析後的結果（`_routing_defaults` 逐層、routing profile、租戶 `_routing` 合併，`{{tenant}}` 已代換）。

沒有 `tenants:` 的檔不是租戶，stderr 逐檔印 `WARN`；da-guard 的 stderr 逐行轉印（前綴 `da-guard|`）。需要 da-guard（映像內建；repo 裡直接跑時用 `$DA_GUARD_BINARY` 或 `$PATH`）。⚠️ 已知限制：根 `defaults:` 與租戶同時寫 `X_critical` 的樹，da-guard 拒收（結束碼 2），本工具不評估任何規則。

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
| `2` | 呼叫端錯誤：參數錯誤（含沒給 `--config-dir`）／**規則讀不懂**（[#2961](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2961)；`_policies` 或 `--policy` 檔裡任一條規則：不認得的鍵（例如 `check:`）、不認得的 `operator`／`severity`、缺 `target`、`exclude_tenants` 不是 id 清單（寫成單一字串會變成子字串比對）、`when` 不是 mapping 或帶不認得的鍵／缺 `target`／不認得的運算子、規則不是 mapping、`policies` 不是清單、運算子與 `value`／`target` 不搭（見上方「哪些寫法會被拒」，#2987）——不評估任何規則，stderr 逐條點名，`--json` 的 `reason` 為 `policy_rule_invalid`；先前這些規則被跳過或改讀成 `required`，結果算通過）／`--policy` 檔有 `policies:`（或 `_policies:`）以外的頂層鍵（例如拼錯的 `polices:`），或兩者同時出現（`reason` 為 `policy_file_invalid`；先前算「沒有規則」或只讀其一，回 0）。⚠️ v2.9.0 映像跳過或改讀這些規則 <!-- image-caveat: v2.9.0 -->／`--policy` 供了但不是檔案（含空字串）／`--config-dir` 不存在／`--policy` 檔或 `_defaults.yaml` 內容讀不到（不是 UTF-8、不是合法 YAML；訊息指名哪一檔，#1654）／（只在有 policy 規則時才讀租戶，沒有規則時回 0）沒有任何設定檔的目錄（例如空目錄配 `--policy`；原本回 0）、exporter 解析失敗而整份跳過的檔、讀不到的檔或子目錄（`ERROR` 行指名，da-guard 的 stderr 附在下面）、整棵樹被 da-guard 拒收、規則讀 `_routing` 而路由產生器拒收這棵樹、找不到 da-guard（`--json` 時 `reason` 為 `yaml_file_unreadable`／`da_guard_failed`／`routing_tree_refused`，#2115）。⛔ 不要靠拿掉 `--policy` 轉綠——那等於不帶你的策略檔評估（#1651） |

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
| `--opa-binary` | 本地 OPA 二進位檔路徑（da-tools 映像不含 `opa`，容器內請用 `--opa-url`） | `opa` |
| `--policy-path` | .rego 策略檔路徑 | - |
| `--policy-package` | rego 的 package；查 `<package>.violations`，`.` 與 `/` 分隔皆可 | `dynamic_alerting.policy` |
| `--ci` | 有 error 級違規時以結束碼 1 結束 | - |
| `--dry-run` | 僅顯示 input JSON，不呼叫 OPA | - |
| `--json` | JSON 格式輸出 | - |

**OPA 沒有真的評估時結束碼 2**（[#2724](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2724)）：連不到 OPA server、回 HTTP 錯誤或轉址（不跟隨轉址；請把 `--opa-url` 指向 OPA 的最終位址）、HTTP 回應本身壞掉、找不到 `opa` 或它執行失敗／逾時（10 秒）、回應不是 JSON、`<package>.violations` 未定義（該 package 下沒有 `violations` 規則，例如 package 打錯或 policy 沒載入），一律以結束碼 2 結束，stderr 一行 `ERROR:` 指名原因，stdout 不輸出（含 `--json`）。`violations` 的每一項必須是物件 `{"msg", "severity", "tenant", "field"}`，`severity` 為字串（不分大小寫比對 `warning`（僅 ASCII）者算 warning 級，其餘字串與未寫 `severity` 都算 error 級）；不是物件的項目（例如 `violations contains msg if {...}` 的字串集合）或 `severity` 不是字串（例如 `null`）同樣結束碼 2。過去這些情形都印 `✓ All policies passed.` 並以 0 結束（含 `--ci`）。OPA 有評估時，所有項目都列入報告；沒有 error 級項目即通過——只有 warning 級項目時 `--ci` 仍以 0 結束（設計如此），空集合亦為通過。

**範例**

```bash
# 透過 OPA REST API 評估
da-tools opa-evaluate --config-dir conf.d/ --opa-url http://localhost:8181

# Dry-run：僅顯示 OPA input JSON
da-tools opa-evaluate --config-dir conf.d/ --dry-run
```

**OPA input**（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）：`input.tenants` 形狀不變（每個租戶一份「key → 寫法」），內容是寫法＋繼承（經 `da-guard effective`：`defaults:`、平台檔 `tenants:`、profile、子目錄 `_defaults.yaml`；保留鍵與 `_metadata` 的取法同 `evaluate-policy`），子目錄裡的租戶也在；key 照寫法（舊拼法不換）。⚠️ 同一閾值跨層用新舊兩種拼法寫時，`input.tenants` 只留勝出那一層的值與拼法（[#2720](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/2720) 之後的逐閾值 view），所以用現行拼法讀可能撲空；對閾值下判斷請讀 `input.served`。新增 `input.served`：每個租戶此刻在 `/metrics` 發出的閾值數字，key 為現行拼法（別名已正規化；寫成 `<key>_critical` 鍵的 critical 列在 `<key>_critical` 之下，寫成 `"95:critical"` 的值仍在 `<key>` 之下）。對閾值的值下判斷的 rego 請讀 `input.served`。⚠️ 已知限制：`opa-evaluate` 的 `input.tenants` 裡的 `_routing` 是寫法＋繼承，不是路由產生器解析後的結果——不套 `_routing_defaults`、不代換 `{{tenant}}`，只靠 `_routing_defaults` 的租戶沒有 `_routing`；要讀解析後的路由請用 `evaluate-policy`（[#2724](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2724)）。`input.defaults` 是根 defaults 載體的頂層鍵（去掉 `_` 開頭者）照寫法、不拆包：一般的 `_defaults.yaml` 把閾值寫在 `defaults:` 底下，rego 要讀 `input.defaults.defaults.<key>`。沒有 `tenants:` 的檔不是租戶（stderr `WARN`）；exporter 解析失敗或讀不到的檔、被 da-guard 拒收的樹、找不到 da-guard 時以結束碼 2 結束（`ERROR` 行指名），沒有任何設定檔的目錄也是 2。

---

#### guard

Dangling Defaults Guard（v2.8.0）。Python 包裝 shell-out 到 `da-guard` Go binary，驗證 `conf.d/` 樹是否安全（schema / cardinality）。⚠️ 自 [#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904)（[ADR-036](adr/036-single-parser-effective-config.md) §9）起，`defaults-impact` **不再判路由與 domain policy**，交給 route generator：在原本跑 `da-tools guard defaults-impact` 的地方加一步 `da-tools generate-routes --validate --strict`。樹上有只能由 generator 判的內容時，報告列為「未判定」並回 exit 4（見下方 Exit codes）。

**用法**

```bash
da-tools guard <subcommand> [flags]
```

**子命令**

| 子命令 | 說明 |
|---|---|
| `defaults-impact` | 對 conf.d/（或 `--scope` 子目錄）下所有租戶執行 deepMerge → guard checks，輸出 Markdown / JSON 報告；不判路由與 domain policy（[#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904)，exit 4） |
| `served-values` | 以 JSON 印出 exporter `/metrics` 對每個租戶實際發出的值（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）；Python 讀取端經 `scripts/tools/_lib_tenant_values.py` 呼叫。每個租戶的 `series` 對 `severities` 裡的每個門檻 key 列出它在 `/metrics` 的 series（[#2750](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2750)）：`name` 與完整 `labels` 取自 `/metrics` 的 Gather 結果，`metric_key` 是 resolver 解析出 `component`／`metric` label 的那個 key（`X_critical` 與 `X{db="a"}` 都是 `X`），`dimensions`／`dimensions_regex` 是 key 寫的維度 label。凡由舊拼法別名的目標導出的 key（目標本身、它的 `_critical`、它的維度 key）都有兩筆：先是它自己的 row（`legacy_twin: false`），再是同一個值以舊 metric 身分送出的 twin（`legacy_twin: true`，resolver 自己的標記）。`/metrics` 不送的 key（`unserved`、只出現在 `dropped`）與 `_custom_alerts` 沒有這一項。`--schedules` 時每個有送的時段另帶該時段的 `series`（覆寫成 `N:critical` 的時段是另一條 series），值為 `null` 或帶 `error` 的時段沒有 |
| `effective` | 以 JSON 印出每個租戶在 tenant-api `/effective` 的有效設定，另加綁定的 profile 與每個 key 的來源（[#2564](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2564)）；Python 讀取端經 `scripts/tools/_lib_tenant_values.py` 的 `load_effective()` 呼叫 |
| `key-refs` | **不經 `da-tools guard` 轉發**，只供 `deprecate_rule` 內部直接呼叫 da-guard binary（`da-guard key-refs --config-dir <dir> --metric <key> [--metric …]`，[#1822](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1822)）：以 JSON 列出經 exporter 判定（ValidateTenantKeys 的同一個分類：alias 表舊拼法、`_critical`、維度鍵各種拼法）屬於各 `--metric` 的 key，每筆 `{file, section, owner, key, nested}`，`key` 為原文，`nested` 標明子目錄的 `_` 開頭檔；另附與 `served-values` 同源的 `parse_failed`／`unreadable`。結束碼同 `served-values`（0／2／3） |
| `parse-tree` | **不經 `da-tools guard` 轉發**，config-diff 與 migration-audit 讀它（[ADR-036](adr/036-single-parser-effective-config.md) §2，[#2897](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2897)）：`da-guard parse-tree --config-dir <dir>` 以一行 JSON（`schema: "da-guard.parse-tree/v1"`）印出 conf.d 的解析結果——`selected`：每個讀取端實際會讀的檔與讀它的是誰（`exporter`／`generator`；子目錄裡除被選中的 `_defaults` 以外的 `_` 檔，例如 `team/_domain_policy.yaml`，只有產生器讀）；`defaults_carriers`：每個目錄採用的那一份 `_defaults` 檔；`not_read`：沒有讀取端會讀的檔（各目錄未被選中的 `_defaults`，根目錄與子目錄皆然）；`files`：每份讀得了的檔的內容（yaml.v3 解碼後的結構，別名與未加引號的 `<<` 已展開，鍵一律是原文，純量是 `{text, tag, value}`：整數與浮點數的值是十進位字串，布林與 null 是 JSON 值，時間與 `.inf`／`.nan` 沒有 `value`）；`rendered_by`（[#2962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2962)）：每份讀得了的 `_` 檔裡，平台載體（`defaults`、`state_filters`、`optional_overrides`、`profiles`、`tenants`、`_routing_defaults`、`_routing_enforced`、`routing_profiles`）寫在這裡時，改它會影響哪一份渲染結果（`exporter`／`generator`，空清單表示這裡沒人讀；例如 `defaults` 只在被選中的預設檔裡由 exporter 讀，子目錄 `_` 檔的 `tenants:` 沒人讀）；不在這份清單的鍵不列出，表示「沒有回答」而不是「沒人讀」；`unreadable`：讀不了的檔（FIFO 等非一般檔不會去讀，直接列為 `not_regular_file`），每筆 `{file, kind, message}`，`kind` 是一份封閉清單（清單以外的寫法一律照 yaml.v3 讀，即使今天的產生器拒收，例如冒號後接 TAB）：`read_failure`、`not_regular_file`、`invalid_utf8`、`bom_not_at_start`、`syntax_error`、`multiple_documents`、`non_specific_tag`（帶 `!` 標記的節點，不論 yaml.v3 怎麼讀）、`tag_not_allowed`、`non_scalar_key`、`null_key`、`duplicate_key`、`decode_error`、`top_level_not_mapping`。單檔模式 `--path <相對路徑> --input <檔案或 ->` 把輸入當成該路徑的內容，輸出同一種文件，並標出該路徑由誰讀（沒人讀時 `selected` 為空清單、`not_read` 說明原因；路徑也對照磁碟現況判斷：路徑上已存在、但不是真正目錄的一層，例如 symlink 或一般檔，為 `parent_not_walked`，沒有讀取端；路徑本身已存在但不是一般檔時，列在 `unreadable`，內容與整棵樹模式對同一路徑的回報相同（`not_regular_file`，懸空連結為 `read_failure`），輸入的文字不解析）。一份檔的內容（別名展開後）編成 JSON 最多 8 MiB，超過即為 `decode_error`；yaml.v3 本身的別名比例上限照舊。巢狀接近 yaml.v3 上限（10,000 層）的檔，JSON 也一樣深，部分 JSON 解碼器會拒收（實測 Python 3.13 的 json 在 10,000 層巢狀陣列時 RecursionError）；讀取端解不開時，整份文件都要當成不可用。完整格式見 `da-guard parse-tree -h`。結束碼：0（讀不了的檔是輸出的資料，不影響結束碼）、2（呼叫錯誤，含檔名不是合法 UTF-8） |

**Binary 解析順序**

1. `--da-guard-binary <path>`（顯式覆寫）
2. `$DA_GUARD_BINARY` 環境變數
3. `$PATH` 上的 `da-guard`

找不到時印出安裝指引（從 `tools/v*` release 下載 / `cd components/threshold-exporter/app && go build -o /usr/local/bin/da-guard ./cmd/da-guard`）。

**`defaults-impact` 主要 flags**

| Flag | 預設 | 說明 |
|---|---|---|
| `--config-dir <path>` | （必填） | conf.d/ 根目錄 |
| `--scope <path>` | 整棵樹 | 限定 `--config-dir` 底下（含其本身）的某個目錄（CI 傳變更的 `_` 開頭 YAML（如 `_defaults.yaml`、`_profiles.yaml`）所在目錄，相對於 `--config-dir`；同一根下改到多個目錄時傳 `.`）。相對路徑**相對於 `--config-dir`**，與目前工作目錄無關：`--config-dir conf.d/ --scope db/`（`.` 為整棵樹）；絕對路徑照用。解析後落在 `--config-dir` 之外為 exit 2（[#2588](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2588)） |
| `--required-fields <a,b,c>` | 空 | dotted-path 必填欄位 CSV，對有效設定判定。第一段是路由層鍵（`_routing`、任何 `_routing` 開頭的鍵、`_severity_dedup`）的路徑直接回 exit 2 並說明（[#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904)；過去對解析後的 routing 判定），不會悄悄失效 |
| `--cardinality-limit <n>` | 根 `_defaults.yaml` 的 `max_metrics_per_tenant`（未設 = 500；負值 = 不檢查） | per-tenant 預測 metric 上限；明確給值即覆寫，`0` = 停用 |
| `--cardinality-warn-ratio <r>` | 0.8 | warn-tier 比例（0 < r < 1） |
| `--baseline-config-dir <path>` | 空 | 變更前的同一棵 conf.d（CI 傳 PR 的 merge-base）；根 `_defaults.yaml` 的 `max_metrics_per_tenant` 被調高或關閉時，報告開頭加一則提示。不影響 exit code |
| `--format md\|json` | md | 輸出格式 |
| `--output <path>` | stdout | 寫入指定檔；parent dir 必須存在 |
| `--warn-as-error` | false | 把 warning 視為 error 影響 exit code |

**Exit codes**

| Code | 意義 |
|---|---|
| 0 | clean — 沒 error 級 finding（warning 不擋，除非 `--warn-as-error`），樹上也沒有只能由 route generator 判的內容（見 4）。`--scope` 內沒有租戶時照樣跑所有檢查：不依賴租戶的那些（defaults 檔形狀、`/metrics` 無法 gather）有 finding 就照常出報告、依 finding 決定結束碼；完全沒有 finding、也沒有未判定的內容才印「vacuously safe」、回 0（[#2991](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2991)） |
| 1 | guard 偵測到 error，或 `--warn-as-error` 下有 warning — block merge / commit |
| 2 | caller error（flag 錯、路徑不對——`--config-dir`／`--scope` 不存在、路徑中途有一段不是目錄或 symlink 迴圈——、scope 跑出 root 之外（含經由 symlink 指到 root 外）、binary 找不到）。`--baseline-config-dir` 例外：指到不存在的路徑不算錯，照常判定 |
| 3 | exporter 載入時會整份丟掉的檔，加上 da-guard 自己無法 decode 的檔，以及 exporter 載入時 stat 或讀取失敗的檔與無法列出內容的子目錄（與 `served-values` 的 `unreadable` 同一份，報告另列「Files the exporter cannot read」，JSON 報告為 `unreadable`；指向目錄的 symlink 不列入，[#2588](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2588)），限與本次執行有關者（`--scope` 內的檔，及 `--scope` 以上各層目錄的 `_` 開頭檔；無法列出內容的目錄則在它就是 `--scope`、位於 `--scope` 之下或包含 `--scope` 時才算），以及讀不到的 `--scope` 本身（stat 因路徑不對以外的原因失敗，例如權限不足、位在執行身分無法進入的目錄底下；以 `stat_error` 列出，與遮住它的目錄的 `walk_error` 並列）與讀不到的 `--config-dir` 本身（無法列出內容時以 `.` 的 `walk_error`、無法 stat 時以 `.` 的 `stat_error` 列出，原因印在 stderr；過去為 2，[#2627](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2627)）；與 `--cardinality-limit` 無關。報告與 stderr 列出這些檔（相對於 `--config-dir`）；無法 decode 的檔一次可能只列出第一個，修好後重跑。優先於 1，也取代「vacuously safe」的 0。權威定義是契約測試 `TestExitThree_NamesExactlyTheFilesTheExporterDrops`（#2123、#2179） |
| 4 | **未判定**（[#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904)，[ADR-036](adr/036-single-parser-effective-config.md) §9）：樹上有只能由 route generator 判的內容，而 da-guard 不再判路由與 domain policy（不出任何路由類 finding）。判的是 `--config-dir` **整棵樹**、不看 `--scope`（generator 對整棵樹判），檔案與內容取自 `parse-tree` 的同一份結果（generator 會讀的檔）。每筆證據 `{file, field, reason}`，`reason` 是封閉清單：`routing_key`（任一深度的 mapping 鍵是路由層鍵——`_routing`、任何 `_routing` 開頭的鍵含拼錯的、`_severity_dedup`——或鍵名為 `routing_profiles`；`field` 為點分路徑，list 元素寫成 `[i]`）、`domain_policy`（`_domain_policy.yaml`／`.yml`，任一層；`field` 為空）、`routing_profiles_file`（`_routing_profiles.yaml`／`.yml`，任一層；`field` 為空）、`generator_unreadable`（`parse-tree` 判為讀不了的檔，不論 exporter 讀不讀它。步驟 4 之前的 generator（PyYAML）不一定拒收這類檔：固定語料 `tests/shared/parse_tree_corpus.json` 裡有些這類檔它照讀；da-guard 一律列為未判定，交給 generator 判；步驟 4 起 generator 改讀 `parse-tree` 的 JSON，這類檔由 generator 拒收。`field` 為 `parse-tree` 的 kind，例如 `duplicate_key`；整棵樹都掃不了時為 `.` 的 `read_failure`。⚠️ 行為變更（[#2905](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2905)，ADR-036 §9）：generator 會整份拒收、exporter 卻照讀或根本不讀的檔——它算成重複而 yaml.v3 不算的 key（alias key 與其 anchor 並列）、子目錄 `_` 檔或 `_domain_policy`／`_routing_profiles` 檔裡的重複 key、它不肯合併的 merge 值——以前是 exit 3（列在 `parse_failed`），現在只列為這項證據：exit 4、`parse_failed` 為空、其中的租戶照 exporter 的讀法檢查。exporter 自己就整份丟掉的檔仍是 exit 3，也同時列在這裡）、`invalid_tenant_id`（`tenants:` 底下租戶 id 規則不接受的 id，exporter 照送、generator 拒收整棵樹；`field` 為 `tenants.<id>`）、`duplicate_tenant`（同一個租戶 id 由兩個租戶檔宣告，列名稱排序較後的那個檔；整棵樹執行時 exporter 已以 exit 2 拒收，`--scope` 執行看不到時由這裡回報；`field` 為 `tenants.<id>`）。寧可多報 4、不漏報：拼錯而 generator 會忽略的鍵也算。JSON 報告在與 `parse_failed`／`unreadable` 同層一律有 `undetermined`（沒有時是 `[]`）：一般報告、`--scope` 內沒有租戶的報告、解析到一半停下的報告（exit 3，仍列出算得出的證據）三者都有。Markdown 報告另列「Routing and domain policy: not judged」一節（最多 20 筆，其餘只計數）與補救一句，stderr 印一行同義提示。補救：跑 `da-tools generate-routes --validate --strict`。優先序 **3 > 1 > 4 > 0**：3 與 1 照常優先；`--warn-as-error` 只影響 1。`--scope` 內沒有租戶、沒有 finding、但樹上有這類內容時是 4，不是「vacuously safe」的 0；沒有這類內容的樹照常回 0。⚠️ 已知窗口：ADR-036 步驟 4（[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）之前 generator 仍用 PyYAML 讀檔，只有 PyYAML 拒收的寫法（以 `tests/shared/parse_tree_corpus.json` 裡 `pyyaml_only`、放在租戶檔的那些列為準，例如同名錨點定義兩次、TAB 分隔、`2001-13-45` 這類不存在的日期）放在沒有路由鍵的租戶檔裡時，da-guard 回 0、generator 卻整份拒收這個檔；所以 CI 不能只跑 da-guard，必須搭配 `da-tools generate-routes --validate --strict`（[ADR-036](adr/036-single-parser-effective-config.md) §9） |

**`unregistered_finding_kind`（[#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904)）**：每筆 finding 的 `kind` 都來自一份封閉清單（下方各表）。若某筆 finding 沒有清單內的 kind（da-guard 的 bug），報告照常完整寫出（`--format json` 與 `md` 皆然，`--output` 照寫），該筆的 `kind` 寫成 `unregistered_finding_kind`、嚴重度一律改為 `error`（summary 一併重算），stderr 印一行說明有幾筆受影響，結束碼為 1。這個值屬於 JSON 契約；看到它請回報。

**Routing 檢查（[#2280](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2280)）**

⚠️ 自 [#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904) 起 `defaults-impact` **不跑本節的檢查**，自 [#2905](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2905)（ADR-036 §7 步驟 3）起 da-guard 已沒有這些檢查的程式：下表的 finding 不會出現在它的報告裡（樹上有這類內容時回 exit 4）。route generator（`generate-routes --validate --strict`）報同名 finding；本節保留作為各 kind 的定義，內容照 da-guard 當時的回報寫成。

routing 檢查的對象是租戶**解析後**的 routing，與 route generator（`generate-routes`）合併的三層相同：根目錄的 `_routing_defaults` → `_routing_profile` 參照的 routing profile → 租戶自己的 `_routing`，逐頂層鍵淺合併，最後把 `{{tenant}}` 換成租戶 id。`_routing_enforced` 不參與，只有它的 `group_by` 會判 `routing_group_by_invalid`、`match` 的形狀會判 `routing_enforced_match_invalid`。平台檔讀 `--config-dir` 整棵樹（與 generator 一致，[#2326](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2326)）：`_routing_defaults` 沿租戶的目錄鏈逐層淺合併、子目錄的 routing profile 與 domain policy 只作用於所在子樹；不看 `--scope`，所以子樹外的樹形錯誤在 scoped 執行也會報（generator 會拒收整棵樹）。租戶那一層就是 generator 讀的來源（[#2291](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2291)）：租戶檔自己的 `_routing` / `_routing_profile`，蓋在根目錄平台檔 `tenants.<id>` 同名鍵之上（租戶檔寫了該鍵就整個取代）；**不**讀合併後的有效設定，所以 defaults 區塊或 threshold profile 裡的 `_routing` 不會被當成租戶的 routing 來判，而是報 `routing_in_unread_location`。`--required-fields` 中 `_routing` 或 `_routing.` 開頭的欄位同樣對解析後的 routing 判定，其他欄位照舊讀有效設定。主 receiver、`overrides`、ADR-007 `routes` 各條目的 receiver 都做相同的形狀檢查，並依 `_domain_policy.yaml` 判 receiver type：`forbidden_receiver_types` 與 `allowed_receiver_types` 分開判，同一個 receiver 可同時違反兩條。

| Finding kind | 嚴重度 | 觸發 |
|---|---|---|
| `invalid_route_entry` | error | `routes` 不是 list，或某條目 generator 會略過（非 mapping、有 `continue` / `match_re` 等不支援的鍵、`match` 缺或空、label 不合法、值不是非空字串）；Field 為 `routes` 或 `routes[i]` |
| `routing_value_not_string` | error | `routes[i].match` 的值或 `overrides[i].alertname`／`metric_group` 未加引號、而 route generator 的 PyYAML 讀成非字串（`yes`／`on` 是布林、`1:30` 是整數 90、`2001-12-15` 是日期、`~` 是 null、`!!int 5`）；Field 為 `routes[i].match.<label>` 或 `overrides[i].alertname`／`metric_group`。與 `generate-routes --strict` 的 ERROR 同一判準（#2431）。修法：加引號，例如 `team: "yes"` |
| `routing_group_by_invalid` | error | `group_by`（主 route、`overrides[i]`、`routes[i]`）的元素照 route generator 的 PyYAML 讀法不是字串（未加引號的 `8`、`on`／`yes`）、是空字串、重複前面已列的 label，或是 `...` 與其他 label 並存；Field 為 `group_by[i]`、`overrides[i].group_by[j]` 或 `routes[i].group_by[j]`。根目錄 `_routing_enforced.group_by` 也會判，僅限 generator 會產出那條 route 時；此時 tenant 欄空白，Field 為 `<檔案>:_routing_enforced.group_by[i]`，用了 `{{tenant}}` 時為 `<檔案>:_routing_enforced (<租戶>).group_by[i]`，每個租戶各判一次，沒有 routing 的租戶也算，但租戶檔 body 為 null（或不是 mapping）、且沒有根目錄平台檔 `tenants.<id>` 補成 mapping 的租戶不算——產生器不載入它（#2519）。後三者 Alertmanager 拒收整份 config，非字串則會以名為該文字的 label 分組。與 `generate-routes --strict` 的 ERROR 同一判準（#2503）。修法：加引號（`"8"`）或刪掉該元素 |
| `domain_policy_violation` | error | 主 receiver／`overrides[i]`／`routes[i]` 的 type 違反 domain policy；訊息含 domain、constraint 與該值來自哪一層 |
| `critical_escalation_missing` | error | domain policy 設了 `require_critical_escalation: true`，但 severity=critical 告警到不了任何 pagerduty receiver：主 receiver 不是 pagerduty，也沒有會 render 的 `routes` 條目 match 含 `severity: critical` 且送 pagerduty（[#2325](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2325)）；Field 為 `receiver.type`，每個要求此約束的 domain 各一筆。值不是布林時改報 `domain_policy_unusable`（Field `<檔案>:domain_policies.<domain>.constraints.require_critical_escalation`） |
| `critical_escalation_leak` | warn | 租戶有升級路徑，但這個非 pagerduty 目的地（`overrides[i]`／`routes[i]`，最後是主 receiver）仍會比 pagerduty 先收到部分 severity=critical 告警；訊息點名攔走的 label 組合。判準與 generator `--validate` 的 WARN 相同（前面的子路由 match 是它的子集就不算、match 寫到別的 tenant 或非 critical 的 severity 也不算）；Field 為 `<ref>.receiver.type`。不擋 |
| `unknown_routing_profile` | warn | `_routing_profile` 指向沒有定義的 profile（只有空白也算） |
| `domain_policy_unusable` | error | `_domain_policy.yaml` 的結構無法使用（例如 `tenants` 不是 list）；tenant 欄空白，只略過依賴它的檢查 |
| `routing_profiles_unusable` | warn | `routing_profiles:` 不是 mapping；tenant 欄空白。`generate-routes` 同一判準、印 WARN、不擋（#2904） |
| `routing_defaults_routes_ignored` | error | `_routing_defaults` 帶了 `routes`（應放在 profile 或租戶）；兩端都在合併前丟掉，generator 的 `--validate` 同樣擋 |
| `routing_key_ignored` | error | 租戶 `_routing`、routing profile、`_routing_defaults`、`_routing_enforced`（含其下 receiver、`overrides` 條目、`routes` 條目的 receiver）裡 route generator 不讀的鍵，例如 `continue`、`mute_time_intervals`、拼錯的鍵；產出與沒寫時相同，generator 的 `--validate` 同樣擋（#2960）。租戶的 `_routing`：tenant 欄為該租戶、Field 為相對於 `_routing` 的路徑（例如 `overrides[0].matchers`）；其他：tenant 欄空白、Field 為 `<檔案>:<鍵路徑>`（例如 `_routing_profiles.yaml:routing_profiles.p1.continue`）。鍵照原文判定（`1:` 也算），null 鍵與 `http_config` 底下不看；⚠️ 布林、數字、日期這類鍵，da-guard 照自己的讀法印出（例如 `True:` 印成 `true`、`1.0:` 印成 `1`），判定不變。另報產生器不讀的 `_routing*` 鍵（#2983）：租戶設定裡 `_routing`／`_routing_profile` 以外的（tenant 欄為該租戶、Field 為該鍵，例如 `_routing_profle`；租戶檔與根目錄平台檔的 `tenants.<id>` 項目都看），`_` 檔最上層 `_routing_defaults`／`_routing_enforced` 以外的（Field 為 `<檔案>:<鍵>`，例如 `_defaults.yaml:_routing_defualts`；沒有 `defaults:` 的 defaults 檔最上層的 `_routing` 也改由這裡報，不再是 `routing_in_unread_location`） |
| `routing_not_mapping` | error | 租戶的 `_routing`（租戶檔蓋在根目錄平台檔 `tenants.<id>` 之上）照 route generator 的 PyYAML 讀法既不是 mapping、也不是停用字串（`disable`／`disabled`／`off`／`false`，須為 YAML 字串）：例如 `"slack"`、list、null、未加引號的 `false`／`off`／`no`（YAML 布林）。generator 對這個租戶不產出任何 route（不改走 `_routing_defaults`），`--validate` 與 `--strict` 都擋（#2341）。Field 為 `_routing`。要停用請加引號：`_routing: 'off'` 或 `disable` |
| `severity_dedup_invalid` | error | 租戶的 `_severity_dedup` 不是 `enable`／`disable`（也收 `enabled`／`on`／`true`、`disabled`／`off`／`false`，不分大小寫）、布林或 null：例如加引號的 `"no"`、`"yes"`、空字串、數字；generator 在所有模式拒收整棵樹（回 1，[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）。先前這些值靜默當成 enable。tenant 欄為該租戶，Field 為 `_severity_dedup` |
| `invalid_tenant_id` | error | 宣告的租戶 id 不是 DNS-1123 label（1–63 個小寫英數與 `-`，首尾為英數；[ADR-035](adr/035-tenant-id-single-source.md)）：空字串、含大寫、`_`、`.`、空白，或超過 63 字元；generator 在所有模式拒收整棵樹（回 1，#2341、ADR-035）。訊息引用 schema 的規則說明。空 id 先前會產生 `tenant=""` 的 matcher，把沒有 tenant label 的平台告警導走。tenant 欄為該 id（空 id 時空白），Field 為 `<租戶檔>:tenants.<id>` |
| `routing_defaults_not_mapping` | error | `_routing_defaults`（根目錄 `_` 檔，或子目錄的 defaults 載體）既不是 mapping 也不是 null；這一層不貢獻任何值，generator 的 `--validate` 與 `--strict` 都擋（#2341）。tenant 欄空白，Field 為 `<檔案>:_routing_defaults` |
| `routing_enforced_match_invalid` | error | 根目錄已啟用的 `_routing_enforced` 的 `match` 不是非空字串組成的 list（例如 `routes[i].match` 的 mapping 寫法 `{severity: critical}`、單一字串、`[1]`、`[~]`）；generator 不從這個區塊產出 enforced route，`--validate` 擋（#2989）。先前 generator 靜默丟掉這種 `match`，NOC route 收全部告警。tenant 欄空白，Field 為 `<檔案>:_routing_enforced.match` |
| `routing_in_unread_location` | error | `_routing` 或 `_routing_*` 寫在 generator 不讀的位置：任一層 `_defaults.yaml` 的 `defaults:` 區塊內、根目錄平台檔 `profiles:` 的某個閾值 profile 內（`_` 檔頂層讀不到的 `_routing*` 鍵自 #2983 起報 `routing_key_ignored`，不報本 finding）。defaults 區塊裡的會被 exporter 併進有效設定，但不會產生任何 route。**閾值 profile**（[#2914](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2914)）：報的是所有路由層的鍵——`_severity_dedup` 與任何以 `_routing` 開頭的鍵，與 exporter 不補入租戶的鍵同一個判定——不論有沒有租戶綁定該 profile、不論 `--scope`。generator 只從租戶自己的條目（租戶檔，或根目錄平台檔 `tenants:` 裡該租戶的條目）讀這些鍵，exporter 也不把 profile 裡的補入租戶（每次 load 印一行 ERROR，計入 `da_config_profile_routing_keys`），所以它們對任何租戶都沒有作用，實際送出的 route 與 inhibit rule 不受影響。遷移：`_routing` 改寫成 `_routing_profiles.yaml` 的 routing profile，再於租戶條目設 `_routing_profile`（或寫進租戶自己的 `_routing`）；`_severity_dedup`、`_routing_profile` 寫進租戶自己的條目；最後從 profile 刪掉。tenant 欄空白，Field 為 `<檔案>:<鍵路徑>`（例如 `_profiles.yaml:profiles.p1._routing`）；訊息指引改寫到根目錄的 `_routing_defaults`、`_routing_profiles.yaml` 或租戶自己的檔。⚠️ **根目錄** `_defaults.yaml` 的 `defaults:` 區塊帶 `_routing*` 時 exporter 會 decode 失敗、整份丟掉，這種寫法以 exit 3（`parse_failed`）呈現，不出本 finding；子目錄的 `defaults:` 區塊才會出本 finding。子目錄 `_defaults.yaml` 頂層的 `_routing_defaults` 自 #2326 起會被讀、不報；但寫在子目錄**其他** `_` 檔（例如 `team/_routing.yaml`）頂層的 `_routing_defaults` 不會被讀（同一個檔放在根目錄會被讀），報本 finding，generator 的 `--validate` 同樣擋。自 #2904 起上述每一處 generator 都報同名 finding、`--validate` 同樣擋（根目錄 `defaults:` 那一種 da-guard 以 exit 3 呈現，generator 照報）。`profiles:` 或 profile 裡有非字串鍵時，旁邊的 `_routing` 照報（#2904）。另外，`_routing: disable` 的租戶遇到 `--required-fields _routing*` 仍報 `missing_required`，訊息會註明是明示停用 |
| `routing_enforced_below_root` | error | 子目錄的檔有 `_routing_enforced`（只在根目錄讀；#2326）。generator 拒收整棵樹（結束碼 2） |
| `routing_defaults_null_below_root` | error | 子目錄層 `_routing_defaults` 的 `receiver` 或 `overrides` 寫成 null；generator 拒收（結束碼 2） |
| `routing_profile_duplicate` | error | 同一個 routing profile 名稱定義在兩個檔（含根目錄 `.yaml` 與 `.yml`）；保留名稱順序上先出現的定義（根目錄優先），Field 點名後者；generator 拒收（結束碼 2） |
| `domain_policy_out_of_scope` | error | 子目錄的 `_domain_policy.yaml` 點名了該子樹外的租戶；該條目不生效 |

同一個租戶 id 由兩個租戶檔宣告時，exporter 的解析直接拒絕（`duplicate tenant ID`），da-guard 在任何檢查之前以結束碼 2 結束。

這些 finding 不會把檔案列進 exit 3；語法壞到 exporter 讀不了的平台檔仍只以 exit 3 點名一次。

**`_defaults.yaml` 的 `defaults:` 包裝（[#2386](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2386)）**

| Finding kind | 嚴重度 | 觸發 |
|---|---|---|
| `root_defaults_unwrapped` | error | 根目錄的 defaults 檔（`_defaults.yaml`／`.yml`）的 `defaults:` 不是 mapping（沒有這個鍵，或有鍵沒有值），頂層又有下列鍵：閾值（不以 `_` 開頭的鍵）與 `_routing` 開頭以外的保留鍵（例如 `_severity_dedup`、`_silent_mode`、`_state_*`）。不列入：exporter 根目錄設定結構的欄位（例如 `state_filters`、`max_metrics_per_tenant`）、其他工具從頂層讀的 `_routing_defaults`／`_routing_enforced`／`_custom_alerts`、defaults 合併在每一層都丟掉的 `_metadata`、`_routing` 開頭的鍵（route generator 不從 defaults 檔讀 routing，寫錯位置由 `routing_in_unread_location` 報）、其他 `_` 開頭的鍵（例如只用來掛 YAML anchor 的 `_x: &x`）。defaults 合併此時讀整份文件，`/effective` 與其他檢查讀的合併結果顯示這些鍵；exporter 的根目錄解析只從 `defaults:` mapping 讀閾值，`/metrics` 不會從這個檔帶出它們，載入也不出聲。實測：頂層閾值不出現在 `/metrics`；頂層 `_severity_dedup: disable` 在 `/effective` 顯示 `disable`、`/metrics` 送 `enable`。tenant 欄空白，Field 為檔名，訊息列出這些鍵，並提醒根目錄 `defaults:` 只能放數值。只看根目錄：子目錄的 defaults 檔沒有包裝時，合併結果就是送出的值 |
| `defaults_toplevel_ignored` | error | 任一層的 defaults 檔，`defaults:` 是 mapping（`{}` 也算，YAML `!!set` 也算），頂層又有上列那些鍵。defaults 合併此時只讀 `defaults:` 這個 mapping（exporter 的 `ExtractDefaultsBlock`），這些鍵不會出現在任何租戶的 `/effective`；`defaults:` 沒有值時合併讀整份文件，不報。實測（子目錄）：包成 mapping 後，頂層的閾值與 `_severity_dedup` 不再出現在 `/metrics`；`_silent_mode`、`_state_*` 只影響 `/effective`，任何寫法都不出現在 `/metrics`。tenant 欄空白，Field 為檔案路徑，訊息列出這些鍵；子目錄的訊息建議移進 `defaults:` 或讓 `defaults:` 不給值——但子目錄 defaults 不支援的鍵（`subtree_default_reserved_key` 報的那些，例如 `_silent_mode`、`_severity_dedup`、`_state_*`）不建議移進去（移進去只會換成那個 finding），改給該 finding 依鍵而定的修法（今天它在頂層沒有作用，`/effective` 與 exporter 送出的值都不含它，先從該檔刪掉即可維持現狀；這時也不建議「讓 `defaults:` 不給值」）（[#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)），根目錄的訊息提醒根目錄 `defaults:` 只能放數值 |
| `root_defaults_critical_key` | warn | 根目錄 defaults 檔的 `defaults:` mapping 裡有 `<key>_critical`（新舊兩種拼法都算；`_state_`、`_silent_` 開頭的除外）。exporter 把它當成一個獨立的閾值送出（metric `<key>_critical`、severity=warning），**不會**變成 `<key>` 的 critical row；critical row 只來自租戶這一側（租戶自己寫的，或子目錄 `_defaults.yaml`、根平台檔 `tenants:`、profile 給的）。因此租戶的同名鍵不會以根目錄的這個值判成 `redundant_override`。根目錄寫了某個 `_critical` 鍵，而某租戶經任何來源（租戶檔、子目錄 `_defaults.yaml`、根平台檔 `tenants:` 或 profile）也有同一個鍵（不分新舊拼法），且租戶這一側的值會產生 critical row（可解析為數值、不是 `disable`，且 `<key>` 有預設）時，`served-values` 會因該鍵同時有 warning 與 critical 兩條 row 而以結束碼 2 結束。tenant 欄空白，Field 為 `<檔名>:defaults.<key>`（[#2544](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2544)） |

三者都略過 exporter 丟掉的檔（仍只以 exit 3 點名）。

**子目錄 defaults 送不出去的 key（[#1976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976)）**

| Finding kind | 嚴重度 | 觸發 |
|---|---|---|
| `subtree_default_undeliverable` | warn | 租戶從子目錄 `_defaults.yaml` 繼承了一個閾值 key，而根目錄 `_defaults.yaml` 與 `optional_overrides:` 都沒有宣告它。租戶的有效設定（`/effective`）會顯示該 key 的某個值，但 exporter 不會為它產生任何 series，該 key 的告警永遠不會觸發；exporter 載入時印 ERROR，並把該租戶計入 `da_config_subtree_undeliverable_tenants`。判定直接取 exporter 自己載入這棵樹的結果，da-guard 不另做判斷，只報 `--scope` 內的租戶。修法：把該閾值 key 宣告在根目錄 `_defaults.yaml` 或 `optional_overrides:`；以 `_` 開頭的 key 只能宣告在根目錄 `_defaults.yaml`——`optional_overrides:` 不服務 `_` 開頭的閾值 key，宣告在那裡不會送出值，警告（與 exporter 的 ERROR、gauge）也照樣在，訊息會依 key 給出對應修法。tenant 欄為該租戶，Field 為 key。`served-values` 同時把它列進 `unserved`。不報的有三類：保留鍵（例如 `_metadata`、`_profile`、`_state_*`）與 exporter 本來就不產生閾值列的鍵（`_silent_*`、`_state_*`、`_severity_dedup`、`_routing*`）——這兩類宣告到根目錄不是修法（會產生無意義或衝突的閾值列，或根本不產生），改由下方的 `subtree_default_reserved_key` 報（[#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)）；以及子目錄把它關掉（值為 `disable` 等）的 key，關掉的 key 不報。exporter 的 ERROR 與 gauge 仍會計入這些 key。其他 `_` 開頭、未受承認的鍵（例如 `_myth`）exporter 當成閾值送出，照常報。⚠️ 目前為 warn（只有 `--warn-as-error` 會讓結束碼變 1），預計下一個 minor 版改為 error |
| `root_default_null_undeclared` | warn | 租戶這一側（租戶檔、根目錄平台檔的 `tenants:` 或 profile）設了某個閾值 key，而根目錄 `_defaults.yaml` 把它（任一拼法）寫成 null（`k:`、`~`、`null`）。null 等於沒寫（[#2518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2518)），根目錄因此沒有宣告這個 key，exporter 不為它產生任何 series，該告警永遠不會觸發——升級前 null 被解成門檻 0，租戶自己的值照常送出，所以這是升級後行為改變的地方。判定直接取 exporter 自己載入這棵樹的結果，只報 `--scope` 內的租戶；`served-values` 同時把它列進 `unserved`。修法：在根目錄 `_defaults.yaml` 給它一個數字，或列進 `optional_overrides:`（只宣告、不給平台值；null 那一行留著或刪掉都可以）；以 `_` 開頭的 key 只能在根目錄給數字。租戶這一側設的 `<key>_critical`，若根目錄把 `<key>`（任一拼法）寫成 null，也報在這裡：critical row 只在根目錄 `defaults:` 有 `<key>` 時才送；修法只有在根目錄給 `<key>` 一個數字（這也會對沒自訂值的租戶送出 `<key>` 的 warning 門檻），列進 `optional_overrides:` 不會送出 critical row。根目錄從來沒寫過 `<key>` 的 `<key>_critical` 不是 null 造成的，不在這裡報。子目錄 `_defaults.yaml` 給的值由 `subtree_default_undeliverable` 報，不重複報在這裡；租戶把它設為 `disable` 等關掉的值不報 |
| `schedule_null_value` | error | 某個閾值寫成**帶時段覆寫的排程**，而排程裡有 null：某個時段是 null、時段的 `window:` 或 `value:` 是 null，或 `default:` 是 null 而旁邊有時段（[#2708](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2708)）。這種寫法沒有定義的意義，驗證期拒收。檢查的位置：租戶檔與根目錄平台檔中 `--scope` 內租戶的 `tenants:` 條目、各層選用的 `_defaults.yaml` 的 defaults 區塊、根目錄平台檔的 `profiles:`；exporter 丟掉的檔不報（仍只以 exit 3 點名）。Field 為 `<檔案>:<區塊>.[<租戶或 profile>.]<key>`，`tenants:` 條目的 tenant 欄為該租戶。修法：給 default 與每個時段一個值；想讓這一層不給值，就刪掉該 key 或寫成 `null` |
| `value_not_served` | error | 租戶的有效設定顯示的某個閾值值，`/metrics` 沒有照寫送出（[#2065](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2065)）。判定是該租戶的 `not_served`（與 `da-guard effective` 同一個函式），只取五種 reason，訊息以 reason 開頭、接著寫檔案、後果與修法：`value_unparsed`（值不是 exporter 讀得懂的數字，改送平台預設值）、`value_unparsed_dropped`（`_critical`、維度或 `optional_overrides:` 宣告的 key 的值讀不懂，沒有預設值可退，該告警永遠不會觸發）、`window_invalid`（排程覆寫的 `window:` 不被接受——不是 UTC `HH:MM-HH:MM`、缺 `window:`、或起訖相同——該時段永遠不生效）、`value_rejected`（子目錄 `_defaults.yaml` 的值不是閾值形狀，租戶沿用較淺層的值）、`spelling_duplicate`（勝出那一層的同一個 mapping 把該閾值寫成兩種拼法；[#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)）。每個（租戶, key）一筆，Field 為 key；只看閾值 key（不以 `_` 開頭），其他 reason 與子目錄的保留鍵由各自的 finding 報。⚠️ **行為變更**：先前通過（exit 0）的樹若有這類值，現在 exit 1。修法：照訊息改寫該值或時段 |
| `custom_alert_duplicate_series` | error | 租戶 `_custom_alerts` 的某一項在 `/metrics` 送出的 series，清單中較前面的一項已經送出（同名同形的自訂告警，或同形的兩個 `slo_burn_rate`——兩者共用一列 `user_slo_objective{tenant, recipe_id}`；[#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)）。exporter 保留第一項、丟棄這一項並計入 `da_custom_alert_parse_errors`（過去整個 `/metrics` 回 500）。判定取 exporter 自己載入這棵樹的結果，以送出的 series 為準：tenant-api preflight 較嚴的「同名」「同 shape＋severity」規則不在此報。Field 為 `_custom_alerts[i]`。⚠️ **行為變更**：這類樹由 exit 0 改為 exit 1。修法：刪掉其中一項 |
| `metrics_not_gatherable` | error | exporter 的 `/metrics` 對這棵樹無法 `Gather`：兩個 key 送出同一條 series（例如 `X: "70:critical"` 與 `X_critical`、regex label `q=~` 與 exact label `q_re=`），整個 scrape 對所有租戶回 500（[#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)）。以 production collector 對一天中每個排程時段各 `Gather` 一次，訊息盡量點名租戶與 key。範圍是整棵樹，不受 `--scope` 限制；tenant 欄空白。⚠️ **行為變更**：這類樹由 exit 0 改為 exit 1。修法：只保留其中一個 key |

**子目錄 defaults 中的保留鍵（[#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)）**

| Finding kind | 嚴重度 | 觸發 |
|---|---|---|
| `subtree_default_reserved_key` | warn | 租戶的 defaults 鏈中，某個子目錄 `_defaults.yaml`（不含根目錄那份）的 defaults 寫了保留鍵（例如 `_state_*`、`_silent_mode`、`_severity_dedup`、`_metadata`、`_profile`）或 exporter 本來就不產生閾值列的鍵（`_silent_*` 等）。子目錄 defaults 不支援這些鍵，屬設定錯誤；不論值為何（null 除外：null 等於這一層沒寫，不報）、不論 exporter 目前是否套用都報。目前 exporter 對這些鍵只套用 `disable` 或數值，其他值（`enable`、severity 名稱、mapping）丟掉——以 `_state_*` 為例，子目錄關得掉 filter、開不了。修法依鍵而定，訊息會給出對應的一種。受承認的保留鍵（包括根目錄已宣告其 filter 的 `_state_<filter>`）再依 exporter 子目錄 overlay 自己的判定分兩支（多層子目錄寫同一個鍵時，看租戶最後拿到的值）：exporter 今天對這個租戶**使用**這個值（例如 `disable`）——搬走它：訊息寫出該租戶今天拿到的值與來源檔（例如 `` `_silent_mode: disable` ``，來自 `a/_defaults.yaml`），把它寫進這個租戶自己的 `tenants:` 條目，再從列出的每個子目錄檔刪掉這個鍵；`_state_<filter>` 也可改根目錄的 `state_filters.<filter>.default_state`（⚠️ 這會影響樹中所有租戶）；只刪不搬可能改變送出的值（「使用」只表示 exporter 用了子目錄的值，不判斷它是否與沒有它時相同）；exporter 今天對這個租戶**忽略**這個值（例如 `enable`、severity 名稱、mapping，或租戶自己已寫了這個鍵）——若整棵樹（不限 `--scope`）沒有其他租戶從列出的檔拿到這個值，從該檔刪掉後 exporter 送出的值不變，`/effective` 不再顯示這個被忽略的值；若要它生效，再寫進各租戶條目（`_custom_alerts` 也一樣：exporter 不送任何 `_defaults.yaml` 裡的它），這會改變送出的值；租戶自己已寫了這個鍵時，訊息改說送出的是租戶自己的值，並寫出設定它的地方：哪個檔的 `tenants:` 條目（租戶檔，或根目錄平台檔如 `_platform.yaml`），或租戶的 `_profile` 名稱；若有其他租戶從列出的其中一個檔拿到這個值，訊息列出這些租戶（最多 5 個加總數），並說明不能只刪，要先照他們的訊息搬走；`_state_<filter>` 而該 filter 未宣告——任何位置都沒有讀取者（租戶條目也一樣），請在根目錄 `state_filters:` 宣告它（同樣影響整棵樹），否則從該檔刪掉；不是受承認的鍵（例如 `_silent_x`：exporter 不產生閾值列、也不是保留鍵；或 `_routingProfile`、`_routings`：只是開頭符合保留前綴，沒有任何讀取者以這個名字查它）——exporter 不會讀它，訊息不稱保留鍵，請從該檔刪掉；filter 名稱為空字串時，訊息寫成 `""`（例如 `state_filters."".default_state`）。`defaults:` 沒有值或沒有這個鍵時合併讀整份文件，頂層的這些鍵同樣報；但 `_custom_alerts` 寫在沒有 `defaults:` 包裝的檔的頂層不報——custom-alert 編譯器目前從那裡讀它，但 exporter 不送出（見 [ADR-024 §3](adr/024-version-aware-threshold-via-dimensional-label.md)），這種不一致由 `validate-config` 的 `custom_alert_sources` 報；寫在 `defaults:` mapping 內照報，訊息目前建議移到頂層，但頂層 exporter 一樣不送：請寫進租戶自己的條目（這則訊息隨 [#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042) 修正）。routing 鍵（`_routing` 與 `_routing_*`，與 route generator 用同一個判斷）不在此列，由 route generator 判（`routing_in_unread_location` 等；da-guard 只列為未判定證據）；`_routingProfile` 這類以 `_routing` 開頭但不是 routing 鍵的鍵照報，並歸入上述「不是受承認的鍵，請刪除」。判定讀 exporter 自己載入這棵樹時用的 defaults 鏈，只報 `--scope` 內的租戶；根目錄 `_defaults.yaml` 不在範圍內。tenant 欄為該租戶，Field 為 key，訊息列出寫了該 key 的子目錄檔。租戶條目寫了與這類子目錄鍵相同的值時不報 `redundant_override`——子目錄那份值應該搬出子目錄檔（下一個 minor 版起也不再套用），租戶自己的鍵正是要留下的那一份。本 finding 不改變 exporter 的行為。⚠️ 目前為 warn（只有 `--warn-as-error` 會讓結束碼變 1）；**下一個 minor 版起 exporter 不再套用子目錄 defaults 中的這些鍵（目前生效的 `disable` 也不再生效），本 finding 改為 error**，請先移到上述位置 |

**`served-values`**

| Flag | 預設 | 說明 |
|---|---|---|
| `--config-dir <path>` | （必填） | conf.d/ 根目錄 |
| `--at <RFC3339>` | 現在 | 在這個時間點解析（排程視窗、`expires`、靜默 / 維護期限都以它為準） |
| `--schedules` | 關 | 另外輸出每個租戶的 `schedules`（逐時段閾值，見下）；一天中有幾段排程變化就多解析幾次整棵樹，沒給時不計算 |
| `--disable-effects` | 關 | 另外為 `keys` 裡一天中任何時段讀到 `disable` 的 key 輸出 `disable`（`noop`／`partial`／`effective`，見下）；重算的輪數是單一租戶最多幾個 `disable`，沒給時不計算 |

值由 exporter 自己的載入與解析算出，這個子命令不另做判斷。輸出 JSON：`schema`（目前為 `da-guard.served-values/v1`；Python 讀取端遇到其他值即拒收，沒有這個欄位時當成比工具舊的 da-guard、要求升級，兩者結束碼都是 2，[ADR-037](adr/037-config-diff-compares-rendered-output.md)）、`parse_failed`（exporter 載入時整份跳過的檔，沒有時為 `[]`）、`skipped`（exporter 讀了但不當租戶的檔：檔名不以 `_` 開頭、沒有 `tenants:` 或其為空；每筆是 `file` 與 `reason`，沒有時為 `[]`）、`unreadable`（exporter 載入時 stat 或讀取失敗而跳過的檔，例如權限不足、懸空 symlink，以及無法列出內容的子目錄（其下全部略過）；每筆是 `file` 與 `reason`，`reason` 只會是 `stat_error`、`read_error`、`walk_error`（此時 `file` 是該目錄）或 `special_file`（不是一般檔：FIFO、socket、裝置檔，或指向它們的 symlink；不開不讀，#2939）；指向目錄的 symlink 不列入；沒有時為 `[]`）與 `tenants`；每個租戶有 `values`（`/metrics` 會發列的閾值 key 取 canonical 名與值，加上 reserved key 在 `--at` 當下由 exporter resolver 讀出的值）、`severities`（每個閾值 key 的 severity label）、`unserved`（租戶合併後設定中沒出現在 `values` 的 key，含被停用者；另含租戶從子目錄 `_defaults.yaml` 繼承、exporter 送不出去的閾值 key（根目錄 `_defaults.yaml` 與 `optional_overrides:` 都沒宣告它；不含保留鍵、exporter 本來就不產生閾值列的鍵與被關掉的 key），與 da-guard 的 `subtree_default_undeliverable` 同一份——這類 key 的值是最深一層「寫成閾值形狀」的那個值（更深一層若寫成非閾值形狀，例如 YAML 布林，就略過它），且為 exporter 解析後的正規化寫法（例如 `1e6` 變成 `"1e+06"`），[#1976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976)）、`dropped`（exporter 建不出 series、`/metrics` 丟掉該列的 key，值為每個被丟列的原因）與 `state_filters`（`--at` 當下對這個租戶開啟的每個 state filter → `{"severity": …}`，即 `/metrics` 上 `user_state_filter` 那一列的 severity label；關掉的 filter 沒有條目，沒有時為 `{}`。`values` 裡的 `_state_<filter>` 仍是 true／false，[ADR-037](adr/037-config-diff-compares-rendered-output.md)）與 `time_boxes`（exporter 會套用的運作模式時效：`_silent_mode` 每個被靜默的 severity 一筆、結構化的 `_state_maintenance` 一筆（maintenance filter 有宣告時；沒宣告時維護本身不生效），各為 `{"target"（只有 `_silent_mode` 有）, "expires", "expired"}`，`expires` 為 exporter 解析後的 RFC3339（保留秒以下），`expired` 為它在 `--at` 的判定；沒有時效或 exporter 不套用的為無條目，沒有時為 `{}`。`values` 只說模式在 `--at` 是否生效，延長或改成無期限只會反映在這裡），以及 `routing` 與 `keys`。`routing` 是 exporter 模組的路由 resolver 讀到的 `_routing`（`receiver_type`、`receiver`、`group_by`、`group_wait`、`group_interval`、`repeat_interval`）；collector 不讀 `_routing`（讀它的是路由產生器），所以它不在 `values` 裡；resolver 不收的 `_routing` 以原文留在 `unserved`，此時沒有 `routing`。`keys`（[ADR-038](adr/038-unread-keys-judged-by-readers.md) 決定 5）列出租戶合併後設定與 `unserved` 裡的每個 key（拼法同該處）：`exporter`（exporter 是否讀它：閾值 key、登錄表沒有的 `_` key 與 `_custom_alerts` 看有沒有 `/metrics` 列——收下或被丟掉都算；其他登錄表有的 `_` key 依登錄表的宣告，值 resolver 收不收都算讀了，但 root 沒宣告該 filter 的 `_state_<filter>` 為 false）、`reason`（exporter 不讀時的原因，只會是 `disabled`（`--at` 當下的值關掉了它；有沒有因此少送什麼，要加 `--disable-effects` 才判）、`no_row`（resolver 沒為這個閾值 key 或 `_custom_alerts` 產生任何列：拼錯與平台沒宣告的 key 同一個原因）、`undeliverable`（同上方子目錄送不出去的 key）、`shadowed`（同一租戶也寫了 canonical 拼法的退役拼法）、`not_exporter_key`（登錄表有、但 exporter 不讀的 `_` key）、`state_filter_undeclared`、`unregistered`（登錄表沒有、沒有列也沒被關掉的 `_` key）、`retired`（已退役的 key，`hint` 寫明改用什麼））、`registered`（只有 `_` 開頭的 key 有：登錄表是否列出它或它的前綴）與 `declared`（登錄表宣告的其他讀取端 → 在哪些層讀；要配合 `da-guard effective` 的 `key_sources` 判斷實際那一層）。這只是事實，不是判決：產生器負責的 key 由產生器自己判。沒有時為 `{}`。加 `--disable-effects` 時，一天中任何時段讀到 `disable` 的 key（登錄表有的 `_` key 除外，它們由各自的讀取端判）另有 `disable`：把該 `disable` 拿掉（只在它讀到 `disable` 的時段）、經同一組 collector 在租戶自己的每個排程切點重算一次，`user_threshold` 列完全不變為 `noop`，有變但同一族（`<key>_critical`、`<key>{…}`）在某時段仍有列為 `partial`（`served_family` 列出那些 key），其餘為 `effective`；某個時段兩邊都整份 Gather 失敗（`/metrics` 回 500）時，該時段不算證據，由其他時段決定。重算的輪數是「單一租戶最多幾個 `disable`」，與租戶數無關（每輪只建入該輪的租戶）；`disable` 從合併後設定拿掉，所以下層也寫了 `disable` 時，比較的是平台預設值。哪些列會被收下，是把 exporter `/metrics` 的同一組 collector 放進私有 registry 跑一次 `Gather` 決定的。Exit code：0 成功；2 caller error、exporter 拒收整棵樹（例如同一租戶跨檔重複宣告、樹中根本沒有任何設定檔、`--config-dir` 不存在），或 `Gather` 失敗（例如兩個 key 產生同一條 series；exporter 的 `/metrics` 此時整份回 500），stderr 帶出原因並盡量點名 key；3 有檔被整份跳過或讀不到，JSON 照樣輸出並在 `parse_failed`／`unreadable` 點名——所有設定檔都讀不到（含 `_defaults.yaml`，若存在）、或 `--config-dir` 本身讀不到（`unreadable` 以 `.` 列出：無法列出內容為 `walk_error`、無法 stat 為 `stat_error`）時也是 3，`tenants` 為空，不再回 2（[#2627](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2627)）。輸出中任何字串不是合法 UTF-8 時也 exit 2（JSON 裝不下），即使 exporter 對這種 key 只是丟掉該列、`/metrics` 仍回 200。exit 2 以 production `/metrics` 同一組 collector 的 `Gather` 為準。判定以 UTF-8 協商的 scrape（Prometheus 3 預設）為準；若以 legacy 或 underscores escaping 抓取，`{a-b}` 與 `{a.b}` 這類 label 可能在文字輸出上重名。`dropped` 的 key 用 canonical 拼法，`unserved` 的 key 用原文拼法。維度 key 的 `{…}` 段在 `values`、`severities`、`unserved`、`dropped`、`schedules` 一律寫成提供該值那一層的原文，與 `da-guard effective` 相同，讀取端可以直接以 key 對照兩份輸出（[#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)）。另有頂層 `unread_keys`（[#2296](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2296)）：exporter 載入時沒有讀的 key，每筆是 `file`、`key` 與 `reason`；目前只有一種 `reason`：`root_defaults_unwrapped`——根目錄 `_defaults.yaml` 沒有 `defaults:` mapping 時，它頂層被 da-guard 同名 finding 點名的那些 key（與該 finding 用同一個判定；`/effective` 會顯示它、`/metrics` 不送，`effective` 的 `not_served` 同樣點名）。不影響 exit code，沒有時為 `[]`。

逐時段閾值與別名表（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115) (c)）：給 `--schedules` 時，每個租戶另有 `schedules`（沒給時不輸出這個欄位，其餘輸出不變），key 為 canonical 名，涵蓋一天中任一時刻 `/metrics` 有送出的閾值 key（含 `values` 裡全部閾值 key；`_custom_alerts` 不列），另加 exporter 會處理其 `expires:` 的 key（只有 base key）。每個 key 的 `segments` 依序、無縫涵蓋 UTC `00:00`–`24:00`，每段是 `from`、`to`（`"HH:MM"`，最後一段止於 `"24:00"`）、`value`（與 `values` 同寫法；該段沒有 row，例如 `disable` 時段，為 `null`）與 `severity`（`value` 有值時一定輸出，空字串也照寫，例如 `"70:"` 為 `""`，與 `severities` 相同；`value` 為 `null` 時省略），相鄰同值同 severity 的段合併。各段不是讀設定原文推出來的：da-guard 在任一 key 的時段可能改變的分鐘切開一天，把整棵樹固定在每段的時刻，用 exporter 同一個 resolver 與 `user_threshold` 建構程式在 `--at` 解析並 `Gather`；含 `--at` 的那一段必須與 `values` 完全一致。`expires`（原文）與 `expired`（exporter 在 `--at` 的判定）只在 exporter 會處理該 key 的 `expires:` 時出現；各段已依此判定（已過期時整天為平台預設），不會反映 `--at` 之後才到期：例如 `expires` 為當天 10:00、`--at` 為 03:00，整天各段都是到期前的值，10:00 之後並不會變成平台預設。`--at` 以外的時段若 `Gather` 會失敗（例如只在某時段兩個 key 產生同一條 series；exporter 的 `/metrics` 那段時間整份回 500，任何 key 都不送），不影響結束碼：每個租戶的每個 key 在該段都輸出 `{"from","to","error"}`，`error` 是該次 `Gather` 失敗的說明，不帶 `value`／`severity`；`--at` 落在該段時則照舊以 exit 2 結束。文件頂層另有 `aliases`：exporter 自己的別名表（舊 base key → canonical base key），讀取端以它把舊拼法對到 `values`／`severities`／`schedules`／`dropped` 的 key，不自行維護別名表。換算規則只有三種、皆為完全比對（exporter 的 `CanonicalKeyFor`）：key 本身在表中，換成對應的 canonical key；去掉結尾 `_critical` 後在表中，換成 canonical key 加回 `_critical`；`{` 之前的部分在表中，換成 canonical key 接上原本的 `{…}` 維度段。只是前綴相同的 key（例如舊 key 後面接其他字）不換算。

**`effective`**（[#2564](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2564)；`da-tools guard effective` 轉發到 `da-guard effective`）

| Flag | 預設 | 說明 |
|---|---|---|
| `--config-dir <path>` | （必填） | conf.d/ 根目錄 |

以 JSON 印出整棵樹每個租戶的有效設定，內容與 tenant-api `/effective` 同一條程式路徑（`pkg/config` 的同一個 resolver），這個子命令不另做判斷；Python 讀取端經 `scripts/tools/_lib_tenant_values.py` 的 `load_effective()` 呼叫。輸出 JSON：`schema`（目前為 `da-guard.effective/v1`，讀取端遇到其他值即拒收）、`parse_failed`（exporter 載入時整份跳過的檔，沒有時為 `[]`）、`unreadable`（exporter 載入時 stat 或讀取失敗而跳過的檔，以及無法列出內容的子目錄；與 `served-values` 的 `unreadable` 同一份、同一形狀，沒有時為 `[]`，[#2588](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2588)）、`skipped`（不以 `_` 開頭、卻沒有宣告任何租戶的檔，例如沒有 `tenants:` 的平鋪檔；exit 0 時與 `served-values` 的 `skipped` 一致（有測試）；有檔無法 decode（exit 3）時可能是空的，而 `served-values` 仍列出檔，不能當成完整清單；不影響 exit code，沒有時為 `[]`，[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）與 `tenants`。每個租戶的欄位與 `/effective` 回應相同（`effective_config`、`merged_hash`、`source_file`、`source_hash`、`defaults_chain`、`platform_overlay`、`profile_overlay`），另加兩欄：`profile`（租戶綁定的 profile 名稱；沒有 `_profile`，或名稱在根目錄平台檔找不到時為 `null`）與 `key_sources`（`effective_config` 每個頂層 key 的來源：`layer` 為 `defaults`／`platform`／`profile`／`tenant`，`file` 為寫出該值的檔，`defaults` 層另有 `level`，即在 `defaults_chain` 中的位置，0 為根目錄）。值為 mapping、跨層逐葉合併的 key，來源記寫到其中任何部分的最高層，較低層可能還提供了其他葉子。每個租戶另一律帶 `not_served` 與 `chain_parse_failed`（[#2296](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2296)；`/effective` 回應只在非空時帶這兩欄）。`effective_config` 是寫法上的逐字視圖，值照原文保留；下列來源裡 `/metrics` 不送的值在 `not_served` 逐 key 標出——defaults 鏈、根目錄 `_defaults.yaml` 缺 `defaults:` 包裝、平台檔 `tenants:`／租戶檔的值解析不了；⚠️ profile 層被 exporter 丟掉的值（例如 `_profiles.yaml` 的 `pg_connections: abc`）與已過期（`expires:`）的 override 目前**不標**，`not_served` 為空不代表 `/metrics` 送的就是 `effective_config` 的值（key 為 `effective_config` 的拼法；值為 `reason` 與 `file`，`file` 是 `effective_config` 所顯示那個值的來源檔）。`reason` 是封閉集合，一律取自 exporter 自己載入與解析時的判定，不是比對兩份輸出推出來的：`parse_failed`（exporter 整份丟掉該檔）、`root_defaults_unwrapped`（根目錄 `_defaults.yaml` 沒有 `defaults:` 包裝，該頂層 key 不被讀）、`value_rejected`（子目錄 `_defaults.yaml` 的值不是閾值形狀，exporter 改用較淺一層的值或不送）、`value_unparsed`（租戶這一側的值 exporter 解析不了，改送平台預設；所顯示的值就是根目錄那個預設時不標）、`value_unparsed_dropped`（`_critical`、維度或 `optional_overrides` 宣告的 key 的值解析不了，該列不送）、`window_invalid`（排程覆寫的 `window:` 不被接受：不是 UTC `HH:MM-HH:MM`、時或分超出範圍、缺 `window:`，或起訖相同，該時段永遠不生效，[#2065](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2065)；同一個值也解析不了時標 `value_unparsed`／`value_unparsed_dropped`）、`undeliverable`（子目錄繼承、根目錄沒宣告的 key，[#1976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976)）、`root_null_undeclared`（根目錄寫成 null，[#2518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2518)）、`spelling_duplicate`（勝出那一層的同一個 mapping 把該閾值另寫成另一種拼法；key 為另一種拼法的原文，不在 `effective_config` 裡，[#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)）。維度 key 不論 label 順序、引號或空白都是同一個閾值：exporter 合併前把每一層的 key 改成 canonical 拼法，`effective_config`、`key_sources`、`not_served` 與兩個 overlay 的 keys 則以提供該值那一層的原文拼法輸出（[#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)）。解析不了的判定與時間無關：一天中任何時段解析不了都標。#1231 的舊拼法不算 `not_served`（`/metrics` 以正式名送出，對照用 `served-values` 的 `aliases`）。`chain_parse_failed` 列出這個租戶 `defaults_chain` 上語法壞掉、exporter 整份不讀的檔：resolve 把它當成空檔（`merged_hash` 也以略過它後的鏈計算），租戶照常輸出，不再讓整次 resolve 中止；這種檔也在 `parse_failed`（exit 3）。YAML 的 `.inf`／`.nan` 與 `/effective` 一樣以字串 `"Infinity"`／`"NaN"` 輸出。Exit code：0 成功；2 caller error、resolver 拒收整棵樹（例如同一租戶跨檔重複宣告、樹中沒有任何 `.yaml` 檔），或輸出中有字串不是合法 UTF-8，stderr 帶出原因；3 有檔無法 decode 或讀不到（`--config-dir` 本身讀不到時也是 3，以 `.` 的 `walk_error`（無法列出內容）或 `stat_error`（無法 stat）列出，[#2627](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2627)），JSON 照樣輸出並在 `parse_failed`／`unreadable` 點名（exporter 會讀、resolve 的合併卻 decode 不了的檔——例如租戶檔、或根目錄 `_defaults.yaml` 頂層有 exporter 略過的壞標籤——仍會讓 resolve 中止：`tenants` 為空、`parse_failed` 只列該檔，`unreadable` 照列）。讀不到的租戶檔，其租戶不在 `tenants` 裡；讀不到的 `_defaults.yaml`，其值不在任何租戶的 `effective_config` 裡。`parse_failed` 與 `served-values` 是同一個判定（exporter 載入的結果）：例如根 `_defaults.yaml` 因內容型別錯被 exporter 整份丟掉時，`/effective` 仍讀得到它的值，這裡則列進 `parse_failed` 並 exit 3，該檔提供的 key 在 `not_served` 標為 `parse_failed`。

**範例**

```bash
# 整棵 conf.d/ 跑 schema check
da-tools guard defaults-impact --config-dir conf.d/ --required-fields cpu,memory

# CI hook：限定變更的 _defaults.yaml 所在目錄（相對於 --config-dir；上限自動取根 _defaults.yaml）
da-tools guard defaults-impact --config-dir conf.d/ \
    --scope db/

# JSON 輸出供下游 PR comment poster
da-tools guard defaults-impact --config-dir conf.d/ \
    --format json --output guard-report.json

# 每個租戶在 /metrics 上實際生效的值（指定時間點）
da-tools guard served-values --config-dir conf.d/ --at 2026-07-01T03:00:00Z

# 每個租戶在 /effective 的有效設定，含 profile 綁定與每個 key 的來源
da-tools guard effective --config-dir conf.d/
```

**範圍簡化**：`da-guard` 是 *當前工作樹* 驗證器（讀取磁碟現狀）；CI / pre-commit 流程下與「給 _defaults.yaml 變更預測影響」delta-aware 模型等價（變更 commit / push 前已寫到磁碟）。Speculative simulation 留 `/simulate` endpoint。同 repo 內 `components/threshold-exporter/README.md` 有完整設計理由與三層檢查說明（不在 MkDocs site 內，請從 GitHub 端開啟）。

---

#### batch-pr

Migration Batch PR Pipeline（v2.8.0）。Python 包裝 shell-out 到 `da-batchpr` Go binary，把 customer 的 PromRule corpus 走完「emit → 開 PR → review → Base merge → tenant rebase → 必要時 data-layer hot-fix」整套流程。

**用法**

```bash
da-tools batch-pr <subcommand> [flags]
```

**子命令**

| 子命令 | 說明 |
|---|---|
| `apply` | 從 Plan + profile-builder emit 輸出開出（或更新）tenant chunk PR；走 Hierarchy-Aware chunking（Base Infrastructure PR + per-domain tenant chunks） |
| `refresh` | Base PR merge 後對 `Blocked by` tenant branches 跑 `git rebase --onto <merged-sha>`，conflict 落地到 `refresh-report.md` |
| `refresh-source` | 已知一組 source rule 因 parser 修 bug 導致 emission 變化時，把對應 tenant PR 的受影響檔案重寫 + 推上去（data-layer hot-fix）|

**Binary 解析順序**

1. `--da-batchpr-binary <path>`（顯式覆寫）
2. `$DA_BATCHPR_BINARY` 環境變數
3. `$PATH` 上的 `da-batchpr`

找不到時印出安裝指引（從 `tools/v*` release 下載 / `cd components/threshold-exporter/app && go build -o /usr/local/bin/da-batchpr ./cmd/da-batchpr`）。

**`apply` 主要 flags**

| Flag | 預設 | 說明 |
|---|---|---|
| `--plan <path>` | （必填）| Plan JSON（從 `BuildPlan` 序列化） |
| `--emit-dir <dir>` | （必填）| profile-builder emit 輸出目錄；CLI walk + AllocateFiles bucketing |
| `--repo <owner/name>` | （必填）| GitHub repo |
| `--workdir <dir>` | （必填）| 本地 clone（git ops 的 CWD） |
| `--base-branch <name>` | `main` | 新 PR 的 base |
| `--branch-prefix <p>` | batchpr 預設 | 自訂 branch 前綴 |
| `--commit-author "Name <email>"` | git config | commit author |
| `--dry-run` | false | 跑 orchestration 但不執行 git/GitHub API |
| `--inter-call-delay-ms <n>` | 0 | per-item delay（軟化 GitHub secondary rate limit） |
| `--report <path>` | `-`（stdout） | Markdown 報表 |
| `--result-json <path>` | 空（不寫） | JSON ApplyResult；`-` = stdout，空 = skip（避免與 `--report` 同 stdout 黏在一起） |

**`refresh` / `refresh-source` 主要 flags**

| Flag | 預設 | 說明 |
|---|---|---|
| `--input <path>` | `-`（stdin） | RefreshInput / RefreshSourceInput JSON |
| `--workdir <dir>` | （必填）| 本地 clone |
| `--patches-dir <dir>` | （必填，僅 refresh-source）| `<dir>/<pr-number>/<file-paths>` 結構，CLI 載入到每個 target 的 Files map |
| `--report <path>` | `-` | Markdown 報表 |
| `--result-json <path>` | 空（不寫） | 同 apply |

**Exit codes**

| Code | 意義 |
|---|---|
| 0 | clean — 全部 target 成功或合理 skipped（closed/merged PR / no-change / dry-run） |
| 1 | per-target failures 或 refresh 出現 conflicts（CI hook 不該把 conflicts 當 green）|
| 2 | caller error（flag 錯、JSON parse 失敗、路徑找不到、binary 找不到）|

**範例**

```bash
# 開 PR：把 profile-builder emit 輸出 push 進 customer repo
da-tools batch-pr apply \
    --plan plan.json --emit-dir ./emit/ \
    --repo vencil/customer --workdir ./customer-repo

# Base PR merge 後：rebase tenant branches 到新 main HEAD
da-tools batch-pr refresh \
    --input refresh.json --workdir ./customer-repo

# Parser bug fix：把 200 條 source rule 重新 emit 進現有 tenant PR
da-tools batch-pr refresh-source \
    --input refresh-source.json --patches-dir ./patches/ \
    --workdir ./customer-repo
```

**Honest scope（v2.8.0 v1）**：JSON-input-first 是 v1 contract（machine-friendly + automation-friendly）；convenience flags（`--base-merged-sha N`、`--source-rule-ids id1,id2,id3`）defer 後續 polish。Python 包裝走 shell-out 同 `guard` pattern，binary 由 `tools/v*` Release / da-tools docker image bundle 提供。

---

#### parser

MetricsQL-as-Superset PromRule parser（v2.8.0）。Python 包裝 shell-out 到 `da-parser` Go binary，把 customer 的 `PrometheusRule` CRD YAML 解析為標準 `ParsedRule` JSON，per rule 標註 dialect（`prom` / `metricsql` / `ambiguous`）+ VM-only function 列表 + `prom_compatible: bool`（用 `prometheus/promql/parser` 跑 strict 相容性檢查）。

**用法**

```bash
da-tools parser <subcommand> [flags]
```

**子命令**

| 子命令 | 說明 |
|---|---|
| `import` | 解析 PrometheusRule YAML → JSON ParseResult；可選 `--validate-strict-prom`（預設開）+ `--fail-on-non-portable` / `--fail-on-ambiguous` 兩道 portability gate |
| `allowlist` | 列印內嵌的 VM-only 函數白名單（text 或 json 格式）；獨立 audit / 客製 lint 用 |

**Binary 解析順序**

1. `--da-parser-binary <path>`（顯式覆寫）
2. `$DA_PARSER_BINARY` 環境變數
3. `$PATH` 上的 `da-parser`

找不到時印出安裝指引（從 `tools/v*` release 下載 / `cd components/threshold-exporter/app && go build -o /usr/local/bin/da-parser ./cmd/da-parser`）。

**`import` 主要 flags**

| Flag | 預設 | 說明 |
|---|---|---|
| `--input <path>` | （必填）| PrometheusRule YAML 路徑；`-` = stdin |
| `--output <path>` | `-`（stdout） | JSON ParseResult 輸出路徑 |
| `--generated-by <stamp>` | `da-parser@<version>` | 寫入 `Provenance.GeneratedBy`（CI job id 等） |
| `--validate-strict-prom` | true | 對每條 rule 跑 `prometheus/promql/parser`（v2.8.0 預設開，anti-vendor-lock-in） |
| `--fail-on-non-portable` | false | 任一 rule `prom_compatible=false` → exit 1（自動 imply `--validate-strict-prom`） |
| `--fail-on-ambiguous` | false | 任一 rule `dialect=ambiguous` → exit 1 |

**`allowlist` 主要 flags**

| Flag | 預設 | 說明 |
|---|---|---|
| `--format` | `text` | `text`（一行一個 fn）或 `json`（含 `metricsql_version` 與排序後的 functions 陣列）|

**Exit codes**

| Code | 意義 |
|---|---|
| 0 | parse OK，沒有 portability gate failure |
| 1 | `--fail-on-non-portable` 或 `--fail-on-ambiguous` gate 觸發 |
| 2 | caller error（flag 錯、YAML malformed、路徑找不到、binary 找不到）|

**範例**

```bash
# 1. 基本 import：把 customer 的 PromRule CRD → JSON ParseResult
da-tools parser import --input prom-rules.yaml > parsed.json

# 2. 「我只收純 PromQL」保守路徑：任一 rule 用了 VM-only 函數立即 fail
da-tools parser import --input prom-rules.yaml --fail-on-non-portable

# 3. 從 stdin pipe 進來（CI workflow / kustomize render 串接友善）
helm template ... | da-tools parser import --input -

# 4. 列印當前 metricsql 版本對應的 VM-only 函數白名單
da-tools parser allowlist --format json
```

**輸出 ParsedRule schema 重點欄位**（v2.8.0）：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `dialect` | `prom` / `metricsql` / `ambiguous` | 從 metricsql AST + VM-only 函數比對推導 |
| `prom_portable` | bool | dialect == prom 的 convenience flag（無 VM-only 函數）|
| `prom_compatible` | bool | strict 模式：跑 `prometheus/promql/parser` 也通過。比 `prom_portable` 嚴格 |
| `vm_only_functions` | []string | 該 rule 用到的 VM-only 函數，sorted |
| `analyze_error` | string | metricsql parse 失敗訊息（dialect=ambiguous 時填）|
| `strict_prom_error` | string | `prometheus/promql/parser` 失敗訊息（PromCompatible=false 時填）|
| `source_rule_id` | string | `<source-file>#groups[i].rules[j]` — `da-batchpr refresh --source-rule-ids` 的反向查詢 key |
| `provenance` | object | `generated_by` / `source_file` / `parsed_at` / `source_checksum`（rule batch 共用）|

**Anti-vendor-lock-in 承諾**：customer 用 `--fail-on-non-portable` 跑完一個 corpus 全綠時，這批 rule 在 vanilla Prometheus 上**也**能 evaluate。前提是 `vm_only_functions.yaml` 的版本 pin 與 go.mod 中的 metricsql 版本一致——**freshness CI gate** (`vm_only_functions_freshness_test.go`) 確保 metricsql 升版時不會 silently 漏掉新函數。

---

#### tenant-verify

印一個 tenant 的 effective config + `merged_hash`（v2.8.0）。設計用來支援 `docs/scenarios/incremental-migration-playbook.md` §Emergency Rollback Procedures 第 6 項驗證 checklist：rollback 後 tenant `merged_hash` 必須回到 Base PR merge 前快照。重用 `describe_tenant.py` 的 `ConfDScanner` 做 inheritance 與 `source_hash`；`merged_hash` 讀自 `da-guard effective`（與 tenant-api `/effective`、exporter 同一個值，不在 Python 端自算，[#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549)），所以需要 da-guard（`$DA_GUARD_BINARY` 或 PATH 上的 `da-guard`；da-tools 映像已內建）。本工具是薄 CLI ergonomics 層（簡潔輸出 + exit code）。

```bash
# 印單一 tenant 的 effective config + merged_hash
da-tools tenant-verify db-fin-a --conf-d conf.d/

# 拍 pre-Base-PR 快照（rollback 前存起來）
da-tools tenant-verify --all --conf-d conf.d/ --json > pre-base.json

# rollback 後比對：exit 0 = 通過，exit 2 = 不一致
da-tools tenant-verify db-fin-a --conf-d conf.d/ \
    --expect-merged-hash 0123456789abcdef
```

**Flags**

| Flag | Default | 說明 |
|---|---|---|
| `<tenant-id>` | — | 必填，除非用 `--all` |
| `--all` | false | 對 conf.d/ 內所有 tenant 各印一次 |
| `--conf-d <PATH>` | `conf.d` | conf.d/ 目錄路徑 |
| `--expect-merged-hash <H>` | 空 | 與實際比對；不一致時 exit 2 |
| `--json` | false | JSON 輸出（給 pipe 給 jq diff 用） |

**Exit codes**

| Code | 意義 |
|---|---|
| 0 | tenant 存在且只由一個檔宣告；若有 `--expect-merged-hash` 則一致。`--all`：沒有任何 tenant 被重複宣告 |
| 1 | 缺 tenant_id、conf-d 找不到、`--all` 與 `--expect-merged-hash` 並用，或樹中任一被選用的 `_defaults.yaml` 無法解析／形狀不支援（不限該租戶的繼承鏈）（stderr 指名該檔，[#2459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2459)），或沒有 `merged_hash` 可比：da-guard 找不到、失敗、樹中有 exporter 讀不了的檔，或 da-guard 不解析該 tenant（條目為 `"error": "merged_hash_unavailable"`，`detail` 帶原因，[#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549)）。未捕捉的例外（`Traceback` 加一行 `ERROR: unexpected internal error`，[#3090](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3090)）同樣是 1。argparse 擋下的參數錯（未知旗標、多餘參數）是 2。 |
| 2 | tenant 不存在、`--expect-merged-hash` 不一致，或**重複宣告**（同一 tenant 出現在兩個以上的檔）（incremental migration playbook checklist 第 6 項擋下訊號）。`--all`：有任何 tenant 被重複宣告 |

**重複宣告**（[#2093](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2093)）：同一 tenant 由多個檔宣告時，本工具不計算任何 hash——掃描器只會留其中一份、留哪份取決於檔名排序，照算會讓第 6 項在多餘檔排前面時假通過。單一 tenant 模式（不論有沒有帶 `--expect-merged-hash`）回 exit 2，JSON 為 `{"tenant_id": ..., "error": "duplicate", "files": [...], "detail": ...}`（`files` 為排序後、相對 conf.d 的路徑），human 輸出逐行列出 `declared in: <檔>`。`--all` 把該 tenant 列成同形的 error 條目（沒有 `merged_hash`），其餘 tenant 照常列出，最後 exit 2；human 輸出的 `# total:` 行把已驗證、重複宣告（未驗證）與沒有 `merged_hash`（未驗證）三類分開計數。⚠️ da-guard 對有重複宣告的樹整棵拒收，所以其餘 tenant 也沒有 `merged_hash`（列成 `merged_hash_unavailable` 條目、不計入已驗證）。處置：刪除多餘的宣告、讓 tenant 只留在一個檔，再重跑（`validate-config` 的 `tenant_uniqueness` 報的是同一件事）。

---

#### threshold-recommend

閾值推薦引擎 — 根據 Prometheus 歷史 P50/P95/P99 百分位數推薦最佳閾值。

**用法**

```bash
da-tools threshold-recommend --config-dir <PATH> [--prometheus <URL>] [--tenant <NAME>] [--lookback <DURATION>] [--min-samples <N>] [--dry-run] [--json] [--markdown] [--export-patch]
da-tools threshold-recommend --generate-observed-map
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir` | conf.d/ 目錄路徑（除 `--generate-observed-map` 外必填） | - |
| `--prometheus` | Prometheus Query API URL | `$PROMETHEUS_URL` 或 `http://localhost:9090` |
| `--tenant` | 只分析指定租戶（省略則分析全部） | 全部 |
| `--lookback` | 歷史資料回溯期間 | `7d` |
| `--min-samples` | 最低樣本數門檻：MEDIUM 與 HIGH 都至少要這麼多樣本（見下方信心等級）。不得小於 `100`，否則結束碼 `2`（[#2941](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2941)） | `100` |
| `--dry-run` | 僅顯示 PromQL 查詢，不實際執行 | - |
| `--json` | JSON 輸出 | - |
| `--markdown` | Markdown 表格輸出 | - |
| `--export-patch` | 輸出可套用的 conf.d override 片段（#720 STAGE-1）；只含 \|delta\|≥5%、信心等級 HIGH／MEDIUM 且有對映的 key | - |
| `--generate-observed-map` | 從 rule-packs 重新產生 observed-map（#719）；不需 `--config-dir` | - |

> **#720 STAGE-1（`--export-patch`）**：輸出一段 `tenants:`-rooted 的 conf.d override（只含有實際建議的 key，within-margin / 略過的 key 以註解列出）。operator review 後 merge 進對應 `conf.d/<tenant>.yaml` 並自開 PR → 既有 `backtest.yaml` CI 自動貼 old-vs-new 觸發次數風險報告（STAGE-1 價值基石）。本工具**不就地改檔**（in-place ruamel round-trip 為 defer，見 #721）。
>
> **#2116 值的來源**：租戶、目前值與每個 key 屬於哪個檔取自 `da-guard effective`（與 tenant-api `/effective` 同一套解析，含 `_defaults.yaml`、平台檔 `tenants:`、profile 與子目錄的租戶）。租戶檔自己寫的 key 是一般推薦；繼承自 defaults／平台／profile 的 key 另列「Inherited, reference only」，只列目前值與來源檔，不查 Prometheus、不產生推薦值。`--export-patch` 每行標出目前值的來源檔（`# was 60 (from tenant-a.yaml)`），繼承的 key 只以註解列出目前值與擁有它的檔——寫進租戶檔會把租戶釘在那個值。租戶名或 key 不是一般名稱（`[A-Za-z0-9][A-Za-z0-9._-]*` 以外，或 exporter 讀成空 key 的 `null`；`010`、`true`、`2024` 這類 exporter 照原文讀的 id 照常產生值行）時不產生值行，改以 `(not written)` 註解列出推薦值與目前值，請手動改該租戶檔。租戶檔用舊拼法（如 `mysql_cpu`）時以 exporter 的別名表查 observed-map，輸出保留檔案裡的拼法。需要 da-guard（da-tools image 內附；或以 `$DA_GUARD_BINARY` 指定），失敗時回 2 並轉出 da-guard 的 stderr。別名表取自 `da-guard served-values`，所以 served-values 拒收的樹（例如根 `defaults:` 與租戶同時寫 `X_critical`）上本工具與 `threshold-govern` 都會回 2。沒有任何設定檔的 `--config-dir`（空目錄）同樣回 2（da-guard 拒收）；排程跑 `threshold-govern` 的 Job 遇到時會判為失敗。
>
> **#719 資料源**：推薦值取自每個閾值 key 在 rule-pack alert 中**實際比對**的觀測 recording rule（透過 `scripts/tools/ops/metric_observed_map.yaml`），而非已設定的 `user_threshold`。無對映 / version-aware / 待人工解析的 key 會 fail-loud 略過並附原因。map 記的是事實：每個 key 的 alert 比的是哪條序列、往哪個方向比（[#3017](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3017) 起下界 key 也一樣），推薦與否另由 `recommendation_mode` 決定；比較方向是 `<` 的 key 必須在 `_observed_map_lib.py` 的 `NOT_APPLICABLE_KEYS` 或 `LOWER_BOUND_RESOLVED` 分類，否則 drift-guard 失敗。observed-map 由 `--generate-observed-map` 產生、CI drift-guard 把關；alert 有比較、map 卻沒收錄的 key（coverage gap）會讓 drift-guard 在 CI 失敗。map 缺檔、讀不了或沒有 `keys` 時，`threshold-recommend` 與 `threshold-govern` 都以結束碼 2 退出並點名檔案（[#3017](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3017)；先前缺檔或沒有 key 時會把每個 key 靜默略過、結束碼 0，YAML 壞掉時則是 traceback）。map 缺檔或沒有 key 時 `--generate-observed-map` 會重新產生；YAML 壞掉的 map 不會被覆寫，請先修正或刪除。重新產生時採 **merge-preserve**（#916）：仍有效的人工 resolved `observed_series` 會跨 rule-pack 變更保留（pick 失效則降回 needs_review、已移除的 key 則 drop），摘要列出 preserved/demoted/dropped 計數、細節走 stderr WARN。
>
> **上界 (`>`) 推薦值怎麼算（[#2964](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2964)）**：以未捨入的 P95 判斷方向（報表顯示的 P50/P95/P99 捨入到兩位小數）。
> - **P95 ≤ 0**：不給建議（`recommended` 為空，不寫進 `--export-patch`、`threshold-govern` 不開 PR），reason 為 `P95 is non-positive (max in window X) — no recommendation`。P95 = 0 不一定是閒置，現行告警可能響過，請看窗內最大值判斷。
> - **P95 低於現值（收緊）**：建議值為 `max(P95, 現值一半向上取整, 窗內不超過現值的最大點向上取整)`，上限是現值。一輪最多收緊 50%；不會讓任何在現值下沒響的點開始響（窗內一個高於現值的尖峰不會擋住收緊），因此 backtest 對它判 LOW。被這個下限夾住時，reason 以 `tighten floored at …` 開頭並寫明是哪一項。取整依現值的寫法：整數現值（含 `1e3` 這種科學記號）取整數，其餘取現值本身的小數位數、至少兩位（`0.8` → 兩位，`0.001`、`1e-3` → 三位）；所以已經是該精度最小一格的現值（如 `0.001`）收不下去，會判為無變更。大幅收緊要分幾輪（例如 94% 為 0 的指標 30 → 15 → 8 → 4 → 2）。
> - **P95 高於現值（放寬）**：改取 P99，依上述精度**向上**取整（放寬到 P95 仍約有 5% 的點超標；向上取整保證建議值不會落在現值以下）。這時 5% 的變動門檻量的是 P99 與現值的差距。
> - **現值為 0**（[#2997](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2997)）：門檻 0 代表「任何大於 0 的值都告警」，常是刻意設定（如 `kafka_under_replicated_partitions`）；相對 0 的變動百分比沒有定義，所以不給建議值。有活動（P95 > 0）時標成 `force_manual`，`recommended` 與 `delta_pct` 皆為空，reason 為 `current threshold is 0 (fires on any positive value) — manual review; observed P99 X (rounded up to current's precision: Z), max Y`（Z 是 P99 依上述精度向上取整的值，可能大於窗內最大值 Y；與 X 相同時不列括號），`--export-patch` 以 `(manual)` 註解列出，`threshold-govern` 列進人工審查段，不開 PR。全為 0 的照上方 P95 ≤ 0 處理。
> - 變動小於 5% 仍視為無變更。
>
> ⚠️ `threshold-govern --min-delta-pct` 設在 50 以上時，被「一半」夾住的 -50% 建議會被 govern 濾掉（不開 PR），但 `--export-patch` 仍會列出它。
>
> **#916 下界 (`<`) 三態**：下界閾值（hit-ratio / 可用度**下限**，腐敗＝下修 floor）不再一律 skip，而由 code-level allowlist 分三態：
> - **`percentile-lower`**（可推薦）：opt-in 的下限 key（如 `db2_bufferpool_hit_ratio`）走專屬 **P5 floor 引擎**——完整 UTC 日分桶取 daily-P5，`min(daily-median, pooled)` 抗污染；4dp `ROUND_FLOOR`（floor 防捨入偷放鬆）；以 **miss-rate（互補空間 `1-value`）** 度量腐敗。護欄依序：**放鬆（下修 floor）一律 `force_manual` 交人（無幅度豁免）** → clamp（不把容忍 miss-rate 收到現值的 25% 以下）→ 收緊 within-10%-miss-margin 視為無變更。樣本不足 / current 或 candidate 超出 (0,1) 亦 `force_manual`。
> - **`not-applicable`**（by-design 跳過）：期望值不變量 / 拓撲下限 / 整數 count 下限（如 `kafka_active_controllers` / `kafka_broker_count` / `rabbitmq_consumers`）——百分位推薦無意義，治理視為完成、列 INFO。
> - **`needs_review`**（未分類）：不在任何 allowlist 的 `<` key，fail-loud 待人工分類。
>
> mode 權威由 drift-guard 把關：手編 map 對非 allowlist key 冒充 `percentile-lower` / `not-applicable` 是 CI 硬錯。**下界 delta 為 miss-rate**，export-patch / 治理表格以 `+X% miss` 標示，避免誤讀方向。此外估計量發散閘（pooled-P5 的 miss-rate ≥ 1.5× daily-median-P5）會擋下「週期低谷坐在現值之上」的假自動收緊（否則每週對低谷誤鳴）——交人工。
>
> **下界 lookback 指引**：percentile-lower key 需 **≥5 完整 UTC 日**；預設 `--lookback 7d` 排除兩端 partial 後僅約 6 完整日、邊際薄且對 scrape gap 脆弱，**建議下界 key 用 `--lookback 14d`**（不足 5 完整日時引擎 `force_manual` fail-loud，不出垃圾）。

**信心等級**

| 等級 | 樣本數 |
|------|--------|
| HIGH | ≥ 1000 且 ≥ `--min-samples` |
| MEDIUM | ≥ `--min-samples`（預設與下限都是 100） |
| LOW | 其餘 |

樣本只算有限值，NaN／Inf 不計（[#2956](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2956)）。上界 key 另有資料覆蓋下限：樣本落在的不同 UTC 小時數少於 24 就一律為 LOW（reason 註明 `data in N distinct hour(s) < 24`），`threshold-govern` 不會為它開 PR（[#2941](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2941)）。量的是覆蓋的小時數而不是最早到最晚的跨度，所以一個孤立的早期樣本或兩段相隔很遠的短資料不會被當成一整天。下界 key 改由「至少 5 個完整 UTC 日」把關。LOW 的建議值仍會列在報表裡，但不算進 `recommended_changes`，也不會寫成 `--export-patch` 的值行：它改以整行註解 `# [租戶] (low confidence) key: recommended X, was Y, -Z% — <reason>` 列出，需要時請自行判斷、手動抄寫（[#2969](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2969)）。文字報表的標題會寫成 `0/1 changes recommended, 1 low-confidence`，`--json` 的 summary 與每個租戶多一個 `low_confidence_changes` 欄位。整份 patch 只有 LOW 時，固定標頭後的第一行會說明下方列了幾筆 low-confidence 建議、沒有可套用的值行，不會寫成 `no actionable recommendations`。與 `threshold-govern` 用的是同一道信心門檻。

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

---

#### threshold-govern

閾值治理迴路（Renovate-for-thresholds，#656）— 把 `threshold-recommend` 的推薦接成**主動迴路**：過濾出腐敗夠大的閾值，經 tenant-api 為每個租戶開一個可一鍵批准的 proposed-PR（單寫者 ADR-011/023），而非只發通知。**預設 dry-run**，須 `--apply` 才真正開 PR。

**用法**

```bash
# Dry-run（預設）：印出會為哪些租戶開 PR，不發任何寫入
da-tools threshold-govern --config-dir <PATH> [--prometheus <URL>] [--min-delta-pct <N>] [--json]

# 真正開 PR（per-tenant governance PR）
da-tools threshold-govern --config-dir <PATH> --prometheus <URL> --apply \
  --tenant-api-url <URL> --identity-groups <RBAC_GROUP>
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir` | conf.d/ 目錄路徑（必填） | - |
| `--prometheus` | Prometheus Query API URL | `$PROMETHEUS_URL` 或 `http://localhost:9090` |
| `--tenant` | 只分析指定租戶 | 全部 |
| `--lookback` | 歷史資料回溯期間 | `7d` |
| `--min-samples` | 最低樣本數門檻，語意同 `threshold-recommend`。不得小於 `100`，否則結束碼 `2` | `100` |
| `--min-delta-pct` | 治理介入門檻：\|delta%\| 須 ≥ 此值才開 PR | `25` |
| `--max-prs` | 每次最多開幾個 PR（防洪 / alert-fatigue budget）。須 ≥ 1，小於 1 時結束碼 `2`（[#2929](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2929)）；dry-run 的 Summary 在上限小於可處理租戶數時寫出「最多開幾個 PR」（dry-run 不查 tenant-api，預知不了已開過的 PR，所以只能寫「最多」） | `10` |
| `--apply` | 真正經 tenant-api 開 PR（預設僅 dry-run，不寫入） | - |
| `--tenant-api-url` | tenant-api base URL（`--apply` 必填） | `$TENANT_API_URL` |
| `--identity-email` | X-Forwarded-Email（PR git author / 治理身分） | `threshold-governance@platform.local` |
| `--identity-groups` | X-Forwarded-Groups（須具 write 權限的 RBAC 群組；直連 `--apply` 必填） | `$DA_GOVERN_GROUPS` |
| `--auth-token` | Bearer token（oauth2-proxy 前置模式；或 `$DA_GOVERN_TOKEN`） | - |
| `--auth-token-file` | Bearer token 檔路徑（K8s audience-bound projected SA token，呼叫時讀取；或 `$DA_GOVERN_TOKEN_FILE`） | - |
| `--throttle-seconds` | 開 PR 之間的間隔秒數（≥ 0，可帶小數；`0` = 不等待；負數、`nan`、`inf` 在開出任何 PR 前回 `2`） | `2` |
| `--timeout` | 呼叫 tenant-api（GET／PUT）的 HTTP 逾時秒數，只在 `--apply` 時用到；不影響 Prometheus 查詢。須大於 0、可帶小數，`0`、負數、`nan`、`inf` 與大於 `9223372036`（`threading.TIMEOUT_MAX`）的值以結束碼 `2` 拒絕（[#2942](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2942)） | `30` |
| `--json` | JSON 輸出 | - |

> **閘門**：只納 \|delta\| ≥ `--min-delta-pct` 且 confidence ∈ {HIGH, MEDIUM} 的推薦（樣本不足不開 PR，防破窗），並**排除 `force_manual`**（放鬆 floor / 超出 domain / 樣本不足的下限推薦、現值為 0 的上界 key、推薦時出例外的 key 都不自動開 PR）。**Dedup**：tenant-api 對「該租戶已有 pending PR」回 409 → 視為已在處理、跳過，重跑不洗版。**通道隔離**：PUT 帶 `X-DA-Write-Source: threshold-governance` → PR 走獨立 label / 標題 / 來源，不冒充 tenant-manager UI、不污染告警平面。讀-改-寫只 surgical 取代被推薦的值行（保留註解，PR diff 乾淨）。推薦邏輯 / 資料源完全沿用 `threshold-recommend`（Day-N observed recording rule，#719）。
>
> **#2116 只治理租戶自有的 key**：PR 只改租戶檔自己寫的 key，寫回時用檔案裡的拼法（舊拼法照舊）。繼承自 defaults／平台／profile 的 key 不進計畫、不查 Prometheus，只以 INFO 列出（JSON 的 `inherited` 陣列與計數），因此不會因為 tenant-api 的租戶檔裡找不到它而報錯、觸發連續錯誤斷路。租戶檔在子目錄時，tenant-api 只讀設定目錄最上層的租戶檔，本工具不送 GET、不開 PR，結果標為 `skipped_nested`（不算錯誤、不佔 `--max-prs`），需手動改該檔。
>
> **#916 下界整合**：下界 `percentile-lower` 收緊推薦（拉高 floor）會正常經閘門開 PR；但 `force_manual`（放鬆 / domain / 樣本 / 估計量發散）另列 **manual-review section**（附 `guardrail_reason` + miss delta），`not-applicable` 列 **INFO**（治理完成）——皆計入 summary、JSON 輸出 `force_manual` / `not_applicable` 陣列與計數。治理表格對下界 change 亦標 `+X% miss`。現值為 0 的上界 key（#2997）同樣列在 manual-review section，只附 reason、沒有 delta（JSON 的 `delta_pct` 為 `null`）；`--apply` 不會為它送出寫入，所以與 #2997 之前（建議 0 → P95、照開 PR）相比，結束碼在兩種情況下會不同：可寫入的 key 只有這類 key、且寫入失敗時，從 `1` 變成 `0`（沒有嘗試寫入）；它的寫入原本是整輪唯一成功（含已有 pending PR 的 409），而其他租戶的寫入都失敗時，從 `0` 變成 `1`（不再有成功掩蓋失敗）。`--max-prs` 的名額也不再被它占用，改由其他租戶使用。
>
> **`--min-delta-pct` 空間不對稱**：此旋鈕對上界比的是 **value 空間** delta、對下界比的是 **miss-rate 空間** delta——同一數值敏感度差很多（0.95 floor 的 25% miss ≈ 1.3% value）。`--min-delta-pct` 下限 `5.0` 指的是上界 5% value 邊界（下界 no-change 邊界為 10% miss），故該下限為下界的必要非充分條件。下界 key 建議搭配 `--lookback 14d`（週跑 govern 對下界覆蓋亦然）。
>
> **結束碼**（#656、[#3054](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3054)）：`--apply` 時，若失敗（`error` 加上斷路器略過）占「有送出、或本來要送出寫入」的租戶 ≥ 90%，結束碼為 `1`，讓排程的 Job 標為 Failed；否則為 `0`。dry-run 與沒有可處理租戶時一律 `0`。已有 pending PR（409）與 `no_changes` 算有送出、不算失敗；`--max-prs` 額滿而略過（`skipped`）與 `skipped_nested` 沒有送出，不計入。`error` 不只是寫入通道故障（PUT 失敗、HTTP 5xx、API 不在 PR 寫入模式）：讀取失敗（`GET failed`、`GET returned no raw_yaml`）與本地驗證拒寫（`keys not found in live config`、`refused to PUT (verify failed)`）也記為 `error`，同樣算失敗。連續 5 筆錯誤後斷路器略過其餘租戶，`--json` 的 `outcomes[].status` 為 `skipped_breaker`（先前與額滿略過同為 `skipped`）；`summary` 的 `skipped`（額滿）與 `skipped_breaker` 分開計數，文字 Summary 也分列 `skipped (--max-prs)` 與 `skipped (circuit breaker)`。#3054 起，開出過 PR 也不再讓這一輪直接算成功：有開出 PR、但其餘 ≥ 90% 失敗的一輪，結束碼從 `0` 變成 `1`。結束碼取決於這一輪有多少租戶（租戶少時一次失敗占的比例就高），但同一份計畫下失敗只會讓結束碼變高、不會變低；與計畫無關的寫入健康訊號請看 tenant-api 的 `tenant_api_writes_total{result}` 與告警 `TenantApiWriteErrorsHigh`。

#### test-notification

多通道通知連通性測試 — 驗證所有已配置 receiver 的可達性，報告連通性狀態。

受測的 receiver 取自路由產生器（`generate-routes`）解析出的每租戶 `_routing`——`_routing_defaults`（含子目錄層）、routing profile、平台檔 `tenants:` 覆蓋、子目錄裡的租戶檔都算，與產生器為各租戶產出的 receiver 一致；只測各租戶的 receiver，產生器另外產出的平台強制路由（`_routing_enforced`，NOC）receiver 不在測試範圍（[#2752](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2752)）。路由產生器在讀取階段就拒收的樹（檔案讀不到、同一租戶宣告於兩檔、routing-tree 錯誤、租戶 ID 不合法）不測任何 receiver，exit 2。⚠️ 產生器要到產出 receiver 時才拒收的情況（例如兩個 receiver 產出同一個名稱）本工具不檢查，照常測試。只有 receiver **本身的設定**被產生器（`build_receiver_config`）拒收時才不送出（`INVALID_URL`／`INVALID_CONFIG`）；目前不涵蓋路由層的略過，這些 receiver 照常測試（另開 [#3076](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3076) 追蹤）：`overrides` 或 `routes` 的項目被產生器略過（例如 override 缺 `alertname`／`metric_group`、`routes[i]` 有不支援的 key），以及主 receiver 被拒收時該租戶的 `overrides`／`routes` 整組不產出。要確認產生器收不收這份設定，跑 `generate-routes --validate`。

**用法**

```bash
da-tools test-notification --config-dir <PATH> [--tenant <NAME>] [--dry-run] [--json] [--ci] [--timeout <SEC>] [--rate-limit <SEC>]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir` | conf.d/ 目錄路徑（必填） | - |
| `--tenant` | 只測試指定租戶（省略則測試全部） | 全部 |
| `--dry-run` | 依路由產生器的判定檢查 receiver 配置與 URL 格式、port 須在 1–65535，不實際發送（主機名編碼只在送出時檢查） | - |
| `--json` | JSON 輸出 | - |
| `--ci` | CI 模式：任一 receiver 失敗時 exit 1 | - |
| `--timeout` | 每個 receiver 的逾時秒數，連線與等待回應兩個階段都適用，任一階段逾時皆回報 `TIMEOUT`（[#2965](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2965)）。須大於 0、可帶小數，`0`、負數、`nan`、`inf` 與大於 `9223372036`（`threading.TIMEOUT_MAX`）的值以結束碼 `2` 拒絕（[#2957](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2957)） | `10` |
| `--rate-limit` | 每次測試之間的等待秒數（≥ 0，可帶小數；`0` = 不等待；負數、`nan`、`inf` 回 `2`） | `0.5` |

**支援的 Receiver 類型**

`webhook`、`slack`、`teams`、`pagerduty`、`rocketchat`、`email`（SMTP 連通性檢查）

**狀態**

每個 receiver 的測試結果標成下列其中一種（文字輸出的狀態欄、`--json` 的 `status`）。只有 `OK` 與 `DRY_RUN` 算通過、`SKIPPED` 另計，其餘都算失敗。連線階段的錯誤依例外型別分類，不比對錯誤訊息（[#2995](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2995)）。

| 狀態 | 意義 |
|------|------|
| `OK` | receiver 回了成功的 HTTP 回應 |
| `DRY_RUN` | `--dry-run`：路由產生器（`build_receiver_config`）收這個 receiver 本身的設定、port 在 1–65535，未實際連線（不代表產生器會在路由層產出它，見上方 ⚠️）。主機名能否編碼（IDNA）只在送出時檢查，乾跑不涵蓋 |
| `SKIPPED` | 沒有可測的 URL（例如 `email`） |
| `TIMEOUT` | 連線或等待回應超過 `--timeout` |
| `CONNECTION_REFUSED` | 對端拒絕連線（埠上沒有服務，或被防火牆以 RST 拒絕） |
| `CONNECTION_ERROR` | 其他連線錯誤：DNS 解析失敗、連線被重置、收下請求後不回應就關線、網路不可達。經 HTTPS proxy 時 CONNECT 失敗，detail 前綴 `via proxy: `，表示失敗的是 proxy 這一段 |
| `TLS_ERROR` | TLS 錯誤：憑證驗證失敗、https 連到純 http 埠、握手失敗 |
| `AUTH_ERROR` | HTTP `401`／`403` |
| `HTTP_ERROR` | 其他 HTTP 錯誤狀態碼，或回應格式錯誤、被截斷（detail 以 `malformed response:` 開頭） |
| `INVALID_URL` | receiver 本身的設定被路由產生器（`build_receiver_config`）以 URL 欄位（email 為 `smarthost`）有給但格式不合或不是字串拒收、port 不在 1–65535，或送出時主機名無法編碼、URL 被拒（未送出任何請求） |
| `INVALID_CONFIG` | receiver 本身的設定被路由產生器（`build_receiver_config`）以其他原因拒收（例如缺必填欄位——URL 欄位缺鍵、空字串或 null 也算、`type` 不認得、選填欄位型別不對），與產生器的判定一致 |

`TIMEOUT`、`CONNECTION_REFUSED`、`CONNECTION_ERROR`、`TLS_ERROR` 的 detail 截在 80 字元（含 `via proxy: ` 前綴），較長的錯誤訊息會被切掉。設了 `https_proxy`／`http_proxy` 時，這些狀態可能描述的是到 proxy 的那一段：proxy 的埠沒開是 `CONNECTION_REFUSED`、proxy 主機名解析失敗是 `CONNECTION_ERROR`，http receiver 經 proxy 收到的 `HTTP 502` 是 `HTTP_ERROR`；只有 HTTPS 的 CONNECT 失敗會帶 `via proxy: ` 前綴（含 proxy 回 `504 Connection Timed Out`，這時不標 `TIMEOUT`）。

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
| `1` | CI 模式：任一 receiver 連通失敗（receiver 設定無效或 URL 不合法也算，`--dry-run` 不連線時也一樣） |
| `2` | 呼叫端錯誤：`--config-dir` 底下有檔案讀不到（內容不是 UTF-8 或不是合法 YAML；訊息指名哪一檔，#1654），或路由產生器在讀取階段拒收這棵樹（stderr 是產生器自己的拒收訊息；`--json` 印 `status: caller_error`、`reason: routing_tree_refused`，#2752） |

#### explain-route

路由合併管線除錯器 — 顯示每個 tenant 的四層路由合併展開（ADR-007），包括 `_routing_defaults` → `routing_profiles` → tenant `_routing` → `_routing_enforced`。

`overrides` 與 `routes` 不列在「最終合併結果」裡，而是列在其後的「生效的子路由」：依比對順序（`overrides` → `routes`）列出產生器實際產出的每條子路由與 receiver，被產生器略過的條目另列並附原因（[#2245](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2245)）。`--json` 的每個 tenant 多了 `sub_routes` 與 `skipped_sub_routes`；`final` 仍是合併後的原始設定。時長寫成 Alertmanager 不接受的寫法時，產生器會換成平台預設值（`--validate` 擋下），這些行另列在「產生器以平台預設值頂替」一節（主 route 與子路由都算），`--json` 放在 `replaced_values`，與 `skipped_sub_routes` 分開——那些條目仍有產出、只是值被換掉（[#2490](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2490)）。`_routing_enforced` 只列在第 4 層、不併進 `final`：它在產出的設定裡是 tenant 路由之外**另一條** `continue: true` 路由，不會取代 tenant 自己的 receiver 或 timing（[#2293](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2293)）。

**用法**

```bash
da-tools explain-route --config-dir <PATH> [--tenant <NAME>...] [--show-profile-expansion] [--json]
da-tools explain-route --config-dir <PATH> --tenant <NAME> --trace [--alertname <NAME>] [--severity <LEVEL>] [--label <KEY=VALUE>...] [--base-config <PATH>] [--json]
```

**參數**

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--config-dir` | 設定目錄路徑 | (必填) |
| `--tenant` | 只顯示指定 tenant（可多次指定） | (全部) |
| `--show-profile-expansion` | 顯示所有路由設定檔的展開與引用關係 | `false` |
| `--trace` | 追蹤模式：由 Alertmanager（`amtool`）判定一則 alert 的路由路徑（需搭配 `--tenant`；需 PATH 上有 `amtool`） | `false` |
| `--alertname` | 追蹤的 alert 名稱（只在 `--trace` 下讀取） | `GenericAlert` |
| `--severity` | 追蹤的 alert 嚴重度（只在 `--trace` 下讀取） | `warning` |
| `--label` | 追蹤用的額外 alert label，格式 `KEY=VALUE`（可多次指定；只在 `--trace` 下讀取） | (無) |
| `--base-config` | 追蹤用的 base Alertmanager YAML：`route.routes` 由產生的路由整份取代（只在 `--trace` 下讀取） | 內建 base（與 `generate_alertmanager_routes --validate` 相同） |
| `--json` | 以 JSON 格式輸出 | `false` |

`--trace` 的 alert label 由 `--alertname`、`--severity`、`--tenant` 與 `--label` 組成；`overrides` 的 `metric_group`，以及 `routes` 裡 `alertname`／`severity`／`tenant` 以外的 `match` key，只能經 `--label` 帶入，否則追蹤不會命中那條子路由。`--label` 以第一個 `=` 切分（值可含 `=`、可為空）；沒有 `=`、key 不是合法 label 名稱、key 為 `alertname`／`severity`／`tenant`（請改用對應旗標）、同一 key 重複、或沒有 `--trace` 卻給 `--label`，皆以結束碼 `2` 拒絕（[#2264](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2264)）。沒有 `--trace` 卻給 `--alertname`／`--severity`（即使給的就是預設值）同樣以結束碼 `2` 拒絕（[#2926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2926)）。

`--trace` 的 step 4 列出生效的 inhibit rules（組好的設定裡的原文，含 `--base-config` 自帶的規則），不評估告警是否會被抑制——那取決於執行時同時 firing 的告警。

`--trace` 需要 `amtool`；da-tools 映像自 [#2294](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2294) 起內含，v2.9.0 映像不含。 <!-- image-caveat: v2.9.0 -->

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

# 追蹤一則帶 metric_group 的 alert 會落到哪個 receiver（overrides 的 metric_group、routes 的 match key 都用 --label 帶）
da-tools explain-route --config-dir conf.d/ --tenant demo-tenant --trace --alertname HighConnectionCount --label metric_group=connections

# 用自己的 base Alertmanager 設定當 root（root 的 timing 會被沒寫 timing 的路由繼承）
da-tools explain-route --config-dir conf.d/ --tenant demo-tenant --trace --severity critical --base-config base-alertmanager.yaml
```

#### explain-keys

逐鍵說明租戶設定（[ADR-038](adr/038-unread-keys-judged-by-readers.md) 決定 9）。對每個租戶列出設定裡的每個 key：來源層與檔案（`da-guard effective`）、哪些讀取端讀它（exporter 以 `da-guard served-values` 實測；路由產生器、自訂告警編譯器、Policy-as-Code 與標註依保留鍵登錄表在該層的宣告；`_policies` 中適用於該租戶的規則以 `target`（或 `when.target`）指名的 key 算 Policy-as-Code 讀了：舊拼法與現行拼法視為同一個，萬用字元不算指名，`_` 開頭的 key 還要是登錄表列出的）、判決與等級、`disable` 的作用（以拿掉它重算一次判斷）、寫下的值與 `/metrics` 實際送出的值。判決來自共用判決函式，與其他閘門同一份。工具會盡量把問題報出來，值是否符合你的預期，請用這個輸出自己檢查，並用 `--assert` 放進自己的 CI。

```bash
da-tools explain-keys --config-dir <dir> [--tenant <name>...] [--at <RFC3339>] [--assert <file>] [--json]
```

**主要參數**

| 參數 | 用途 |
|------|------|
| `--config-dir <dir>` | conf.d 根目錄（必須） |
| `--tenant <name>` | 只列這個租戶（可重複）；預設全部 |
| `--at <RFC3339>` | 讀排程與時效的時間點；預設現在。放進 CI 的 `--assert` 請固定一個時間，否則排程值會隨執行時間變 |
| `--assert <file>` | 預期結果（與 `--json` 同形狀）；有任何不同即結束碼 1，差異逐行印在 stderr（`--json` 時另在 `assert_differences`） |
| `--json` | 輸出一份 JSON（schema `da-tools.explain-keys/v1`）到 stdout |
| `--da-guard-binary <path>` | da-guard 路徑；未給則依序找 `$DA_GUARD_BINARY`、`$PATH` 上的 `da-guard`（da-tools 映像已內建） |
| `--timeout <秒>` | 每次執行 da-guard 的上限（預設 600） |

**每個 key 的欄位**（`--json` 的 `tenants.<租戶>[]`）：`key`、`layer`／`file`（來源）、`read_by`、`verdict`（`read`／`unread`／`undecidable`／`platform_violation`）、`severity`（各閘門改成「判到就擋」之後的等級：`error`／`warning`／`notice`）、`reason`、`disable`（`effective`／`partial`／`noop`）、`served_family`、`value`（寫下的值）、`served`（`/metrics` 送出的值）。`findings` 是去重後的問題清單：平台檔或 profile 的同一個問題只列一筆，附受影響的租戶。

**`--assert` 的預期檔**：列在 `tenants` 裡的每個租戶，要列出它全部的 key（多一個、少一個都算不同）；每個 key 只比對預期檔寫出的欄位，例如只寫 `key` 與 `verdict` 就只釘判決。沒列的租戶不比。預期檔有不認得的欄位（頂層或 key 的欄位，例如拼錯的 `verdcit`）或同一租戶重複列同一個 key 時，結束碼 2——否則它會什麼都沒比就通過。

**Exit codes**

| Code | 含義 |
|------|------|
| 0 | 完成；`--assert` 時結果與預期相符 |
| 1 | `--assert` 的結果與預期不同 |
| 2 | caller error（`--config-dir` 不存在、找不到 da-guard 或 da-guard 失敗、`--assert` 檔讀不了、`--tenant` 不存在），或沒讀到任何根平台檔、閾值鍵無從判斷（`undecidable`，結果照樣印出）；`--json` 時 stdout 仍是一份 JSON，caller error 帶 `status: "caller_error"` 與 `reason` |

```bash
da-tools explain-keys --config-dir conf.d/ --tenant db-a
da-tools explain-keys --config-dir conf.d/ --tenant db-a --at 2026-07-01T03:00:00Z --json > expected.json
da-tools explain-keys --config-dir conf.d/ --at 2026-07-01T03:00:00Z --assert expected.json
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
| `1` | `--prometheus` 連不上（查詢失敗會被當成沒有標籤），或未發現合適的 partition label |
| `2` | 呼叫端錯誤：參數錯誤、`--endpoint` 連不上或 URL 不合法，或 `-o/--output` 指到的輸出路徑寫不進去（#1641） |

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
          image: ghcr.io/vencil/da-tools:v2.9.0
          env:
            - name: PROMETHEUS_URL
              value: "http://prometheus.monitoring.svc.cluster.local:9090"
          args: ["check-alert", "MariaDBHighConnections", "db-a"]
      restartPolicy: Never
  backoffLimit: 0
```

---

## 常見問題

### Q: How to Use da-tools in CI/CD?

**A**: 使用 `--ci` flag；exit code 0 = success，非 0 = fail。詳見各命令 `--help`。

### Q: 如何指定多個 metric 用於 validate？

**A**: 使用 `--mapping` 指向 `da-tools migrate` 產生的 `prefix-mapping.yaml`（不接受 CSV）。詳見 [validate](#validate) 命令說明。

### Q: blind-spot 與 analyze-gaps 有什麼區別？

**A**:
- **blind-spot**：比對「叢集基礎設施 vs tenant 配置」，找出有 exporter 但無對應 tenant 配置的盲區。
- **analyze-gaps**：比對「custom rule vs Rule Pack」，評估 Rule Pack 的涵蓋度。

兩者互補，建議遷移後同時執行。

### Q: 如何安全地執行 cutover？

**A**：`cutover` 只適用 migrate／shadow 流程（見 [Shadow Monitoring 切換](scenarios/shadow-monitoring-cutover.md)）：
1. `validate_migration --watch --auto-detect-convergence` 持續比對，收斂時產出 `cutover-readiness.json`
2. `da-tools shadow-verify convergence --readiness-json <檔>` 確認收斂
3. `da-tools cutover --readiness-json <檔> --tenant <tenant> --dry-run` 預覽會執行的 `kubectl` 命令
4. 拿掉 `--dry-run` 正式切換
5. `da-tools batch-diagnose` 看各租戶的健康狀態（`diagnose` 只查 MariaDB Pod，其他類型的租戶用 `check-alert` 確認）

工具沒有回退選項；失敗時照 `shadow-monitoring-sop.md` §7.2 手動回退。

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
| ["性能基準 (Performance Benchmarks)"](./benchmarks.md) | ⭐⭐ |
| ["BYO Alertmanager 整合指南"](integration/byo-alertmanager-integration.md) | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南"](integration/byo-prometheus-integration.md) | ⭐⭐ |
