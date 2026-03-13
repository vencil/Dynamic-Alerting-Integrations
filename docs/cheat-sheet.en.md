---
title: "da-tools Quick Reference"
tags: [reference, cli, cheat-sheet]
audience: [all]
version: v2.0.0-preview.2
lang: en
---

# da-tools Quick Reference

> **Language / 語言：** **English (Current)** | [中文](cheat-sheet.md)

da-tools command quick reference. Full docs at [cli-reference.en.md](cli-reference.en.md).

## Command Reference

| Command | Description | Key Flags | Example |
|---------|-------------|-----------|---------|
| `check-alert` | 查詢特定 alert 在某個 tenant 上的狀態 | - | `da-tools check-alert --help` |
| `diagnose` | 對單一 tenant 執行全面健康檢查 | --config-dir <PATH>, --namespace <NS> | `da-tools diagnose --help` |
| `batch-diagnose` | 對所有 tenant 執行並行健康檢查 | --tenants <LIST>, --workers <N>, --timeout <SEC> | `da-tools batch-diagnose --help` |
| `baseline` | 觀測指標時間序列，計算統計摘要（p50/p90/p95/p99/max），產出閾值建議 | --tenant <NAME>, --duration <SEC>, --interval <SEC> | `da-tools baseline --help` |
| `validate` | Shadow Monitoring 驗證工具：比對新舊 Recording Rule 數值，偵測自動... | --watch, --interval <SEC>, --rounds <N> | `da-tools validate --help` |
| `cutover` | Shadow Monitoring 一鍵切換：停止舊規則、啟用新規則、驗證健康 | --tenant <NAME>, --readiness-json <FILE>, --dry-run | `da-tools cutover --help` |
| `blind-spot` | 掃描 Prometheus 叢集的活躍 targets，與 tenant 配置交叉比對，找出盲區（有... | --config-dir <PATH>, --exclude-jobs <LIST>, --json-output | `da-tools blind-spot --help` |
| `maintenance-scheduler` | 評估排程式維護窗口（`_state_maintenance | --config-dir <PATH>, --output <FILE>, --timezone <TZ> | `da-tools maintenance-scheduler --help` |
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
| `config-diff` | 比較兩個配置目錄（conf | --old-dir <PATH>, --new-dir <PATH>, --json-output | `da-tools config-diff --help` |

## Quick Tips

- **Prometheus API Tools**: Require connectivity to Prometheus HTTP API
  - `check-alert` — Query alert status
  - `diagnose` / `batch-diagnose` — Tenant health check
  - `baseline` — Observe metrics, generate threshold suggestions
  - `validate` — Shadow Monitoring comparison
  - `cutover` — One-click switchover (final migration step)
  - Others: `blind-spot`, `maintenance-scheduler`, `backtest`

- **Config Generation Tools**
  - `generate-routes` — Tenant YAML → Alertmanager fragment
  - `patch-config` — ConfigMap partial update

- **Filesystem Tools** (offline capable)
  - `scaffold` — Generate tenant config
  - `migrate` — Rule format conversion
  - `validate-config` — Config validation
  - `offboard` / `deprecate` — Tenant offboarding / metric deprecation
  - `lint` / `onboard` / `analyze-gaps` / `config-diff` — Governance tools

## Network Configuration

```bash
# K8s internal
export PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090

# Docker Desktop
export PROMETHEUS_URL=http://host.docker.internal:9090

# Linux Docker (--network=host)
export PROMETHEUS_URL=http://localhost:9090
```

## Common Templates

```bash
# Basic command
docker run --rm --network=host \
  -e PROMETHEUS_URL=$PROMETHEUS_URL \
  ghcr.io/vencil/da-tools:v1.11.0 \
  <command> [arguments]

# With local files
docker run --rm --network=host \
  -v $(pwd)/conf.d:/etc/config:ro \
  -e PROMETHEUS_URL=$PROMETHEUS_URL \
  ghcr.io/vencil/da-tools:v1.11.0 \
  <command> --config-dir /etc/config
```

---

Full reference at [cli-reference.en.md](cli-reference.en.md).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["da-tools Quick Reference"](./cheat-sheet.md) | ★★★ |
| ["da-tools CLI Reference"](./cli-reference.en.md) | ★★★ |
| ["Glossary"](./glossary.en.md) | ★★ |
| ["Threshold Exporter API Reference"](api/README.en.md) | ★★ |
