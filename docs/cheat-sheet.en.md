---
title: "da-tools Quick Reference"
tags: [reference, cli, cheat-sheet]
audience: [all]
version: v1.13.0
lang: en
---

# da-tools Quick Reference

> **Language / 語言：** **English (Current)** | [中文](cheat-sheet.md)

da-tools command quick reference. Full docs at [cli-reference.md](cli-reference.md)。

## 命令速查

| 命令 | 說明 | 常用 Flag | 範例 |
|------|------|----------|------|
| `check-alert` | 查詢特定 alert 在某個 tenant 上的狀態 | --prometheus <URL> | `da-tools check-alert --help` |
| `diagnose` | 對單一 tenant 執行全面健康檢查 | --prometheus <URL>, --config-dir <PATH>, --namespace <NS> | `da-tools diagnose --help` |
| `batch-diagnose` | 對所有 tenant 執行並行健康檢查 | --prometheus <URL>, --tenants <LIST>, --workers <N> | `da-tools batch-diagnose --help` |
| `baseline` | 觀測指標時間序列，計算統計摘要（p50/p90/p95/p99/max），產出閾值建議 | --tenant <NAME>, --prometheus <URL>, --duration <SEC> | `da-tools baseline --help` |
| `validate` | Shadow Monitoring 驗證工具：比對新舊 Recording Rule 數值，偵測自動... | --prometheus <URL>, --watch, --interval <SEC> | `da-tools validate --help` |
| `cutover` | Shadow Monitoring 一鍵切換：停止舊規則、啟用新規則、驗證健康 | --tenant <NAME>, --prometheus <URL>, --readiness-json <FILE> | `da-tools cutover --help` |
| `blind-spot` | 掃描 Prometheus 叢集的活躍 targets，與 tenant 配置交叉比對，找出盲區（有... | --config-dir <PATH>, --prometheus <URL>, --exclude-jobs <LIST> | `da-tools blind-spot --help` |
| `maintenance-scheduler` | 評估排程式維護窗口（`_state_maintenance | --config-dir <PATH>, --output <FILE>, --timezone <TZ> | `da-tools maintenance-scheduler --help` |
| `backtest` | 執行 PR 中 threshold 變更的歷史回測 | --prometheus <URL>, --lookback <DAYS>, --output <FILE> | `da-tools backtest --help` |
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
| `config-diff` | 比較兩個配置目錄（conf | --old-dir <PATH>, --new-dir <PATH>, --json-output | `da-tools config-diff --help` |
| `shadow-verify` | Shadow Monitoring readiness & convergence verification | --mapping <FILE>, --report-csv <FILE>, --json | `da-tools shadow-verify --help` |
| `byo-check` | BYO Prometheus & Alertmanager integration verification | --prometheus <URL>, --alertmanager <URL>, --json | `da-tools byo-check --help` |
| `federation-check` | Multi-cluster federation verification (edge / central / e2e) | --prometheus <URL>, --edge-urls <URLS>, --json | `da-tools federation-check --help` |
| `grafana-import` | Grafana dashboard ConfigMap import (sidecar auto-mount) | --dashboard <FILE>, --verify, --dry-run | `da-tools grafana-import --help` |

## 快速提示

- **Prometheus API 工具**：需要能連到 Prometheus HTTP API
  - `check-alert` — 查詢 alert 狀態
  - `diagnose` / `batch-diagnose` — Tenant 健康檢查
  - `baseline` — 觀測指標，產出閾值建議
  - `validate` — Shadow Monitoring 雙軌比對
  - `cutover` — 一鍵切換（遷移最後一步）
  - `shadow-verify` — Shadow Monitoring three-phase verification
  - `byo-check` — BYO integration verification
  - `federation-check` — Multi-cluster federation verification
  - `grafana-import` — Dashboard ConfigMap import
  - Other: `blind-spot`, `maintenance-scheduler`, `backtest`

- **配置生成工具**
  - `generate-routes` — Tenant YAML → Alertmanager fragment
  - `patch-config` — ConfigMap 快速更新

- **檔案系統工具**（離線可用）
  - `scaffold` — 產生 tenant 配置
  - `migrate` — 規則格式轉換
  - `validate-config` — 配置驗證
  - `offboard` / `deprecate` — Tenant 下架／指標棄用
  - `lint` / `onboard` / `analyze-gaps` / `config-diff` — 治理工具

## 網路配置

```bash
# K8s 內部
export PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090

# Docker Desktop
export PROMETHEUS_URL=http://host.docker.internal:9090

# Linux Docker (--network=host)
export PROMETHEUS_URL=http://localhost:9090
```

## 常用樣板

```bash
# 基本命令
docker run --rm --network=host \
  -e PROMETHEUS_URL=$PROMETHEUS_URL \
  ghcr.io/vencil/da-tools:v1.13.0 \
  <command> [arguments]

# 搭配本地檔案
docker run --rm --network=host \
  -v $(pwd)/conf.d:/etc/config:ro \
  -e PROMETHEUS_URL=$PROMETHEUS_URL \
  ghcr.io/vencil/da-tools:v1.13.0 \
  <command> --config-dir /etc/config
```

---

完整參考見 [cli-reference.md](cli-reference.md)。

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["da-tools Quick Reference"](./cheat-sheet.md) | ★★★ |
| ["da-tools CLI Reference"](./cli-reference.en.md) | ★★★ |
| ["Glossary"](./glossary.en.md) | ★★ |
| ["Threshold Exporter API Reference"](api/README.en.md) | ★★ |
