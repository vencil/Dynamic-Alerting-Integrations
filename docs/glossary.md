---
title: "術語表"
tags: [reference, glossary]
audience: [all]
version: v2.8.1
lang: zh
---
# 術語表

> **Language / 語言：** | **中文（當前）** | [English](./glossary.en.md)

本頁列出 Dynamic Alerting 平台文件中常見的專有名詞與縮寫，依字母排序。

---

## A

**ADR (Architecture Decision Record)**
:   記錄架構設計決策的標準化文件格式，說明問題背景、方案選項、最終決策與理由。見 `docs/adr/`。

**Alertmanager**
:   Prometheus 生態系的告警路由與通知元件。本平台使用其 `inhibit_rules` 實現 Severity Dedup，並透過 `configmap-reload` sidecar 自動載入配置變更。

**AST 遷移引擎 (AST Migration Engine)**
:   `migrate_rule.py` 的核心——將傳統 PromQL alert rule 解析為抽象語法樹（AST），自動轉換為 Dynamic Alerting 的 YAML 閾值格式。支援 Triage、Prefix、Dictionary 三種推斷模式。

## B

**Blackbox Exporter**
:   Prometheus 生態系的外部探針工具，用於 HTTP/TCP/ICMP 端點監控。本平台用於監控 threshold-exporter 的 `/health`、`/ready`、`/metrics` 端點可用性。

**Blast Radius（爆炸半徑）**
:   一個變更可能波及的範圍。`vibe-subagent-review` 對 IaC 變更（Helm values / template / Prometheus rules）採 blast-radius lens，評估 selector / RBAC / ConfigMap 等跨檔連動。

**BYO (Bring Your Own)**
:   「自帶」模式——讓已有 Prometheus / Alertmanager 的環境整合本平台，無需替換現有基礎設施。見 `docs/byo-prometheus-integration.md`、`docs/byo-alertmanager-integration.md`。

## C

**Cardinality Guard**
:   per-tenant 500 指標上限保護機制。超過上限時自動截斷（truncate）並記錄 ERROR 日誌，防止單一租戶的配置錯誤影響整個 TSDB。

**ConfigMap Reload**
:   Kubernetes ConfigMap 變更後的自動重載機制。threshold-exporter 使用 SHA-256 hash 偵測變更，Alertmanager 使用 `configmap-reload` sidecar。

**Config Drift**
:   配置飄移——實際執行中的配置與 Git 版本庫中的配置不一致。`config_diff.py` 用於 CI 中偵測此問題。

**Conventional Commits**
:   提交訊息規範（`feat:`, `fix:`, `docs:` 等前綴），搭配 `commitlint` + `release-please` 實現自動化版本管理與 Changelog 生成。

**Custom Rule**
:   租戶自訂的告警規則（Custom Rule Pack）。經 custom-rule governance 流程治理，見 `docs/custom-rule-governance.md`。

## D

**da-tools**
:   平台的 CLI 工具容器（`ghcr.io/vencil/da-tools`），封裝所有 Python 工具。`docker pull` 即可使用，無需 clone repo 或安裝依賴。見 `docs/cli-reference.md`。

**Directory Scanner**
:   threshold-exporter 的 `-config-dir` 模式，掃描 `conf.d/` 目錄下所有 YAML 檔案，支援 per-tenant 獨立檔案管理。

**Dynamic Alerting**
:   本平台的產品名稱——Multi-Tenant Dynamic Alerting 平台。Config-driven、SHA-256 hot-reload、Directory Scanner 的多租戶動態告警方案。

**Domain Expert（領域專家）**
:   負責定義特定資料庫／中介軟體告警閾值與規則的角色 persona。與 Platform Engineer、Tenant 並列為三大使用者角色之一，見角色指南。

**Domain Policies（領域政策）**
:   `generate_alertmanager_routes.py` 消費的路由政策集合，定義 webhook 域名 allowlist 等護欄。見 Routing Profile。

**Dual-Perspective Annotation**
:   `platform_summary`（NOC 視角）+ `summary`（Tenant 視角）雙重 annotation。搭配 `_routing_enforced` 實現平台與租戶各看各的告警描述。

## F

**Federation**
:   多叢集架構模式。場景 A：中央 threshold-exporter + 邊緣 Prometheus，透過 federation 拉取指標。見 `docs/federation-integration.md`。

## G

**`group_left`**
:   PromQL 向量匹配運算子。本平台的核心機制——用一條 rule 透過 `group_left` 匹配所有租戶的閾值向量，取代 per-tenant 獨立 rule。

**Grafana Dashboard**
:   本平台提供的 Grafana 儀表板，視覺化 `user_threshold` 系列與告警狀態。見 `docs/grafana-dashboards.md`。

## H

**Hot-Reload**
:   配置熱重載——修改 ConfigMap 後 threshold-exporter 自動偵測 SHA-256 hash 變更並重新載入，無需重啟 Pod。平均重載時間 < 2 秒。

## I

**Inhibit Rule**
:   Alertmanager 的告警抑制規則。本平台用於：(1) Severity Dedup（critical 抑制 warning）、(2) 三態模式的 Silent/Maintenance 狀態抑制。

## M

**Maintenance Mode（維護模式）**
:   三態之一。`_state_maintenance` 設定後，產生 sentinel alert 觸發 Alertmanager inhibit 抑制該租戶所有告警。支援 `expires` 自動失效與 `recurring[]` 排程式維護窗口。

**Migration Toolkit（遷移工具組）**
:   協助既有 Prometheus 環境遷入本平台的工具集合（`migrate_rule.py` / `validate_migration.py` / `cutover_tenant.py` 等）。見 `docs/migration-toolkit-installation.md`。

**Multi-Tenant（多租戶）**
:   本平台的核心特性——單一 threshold-exporter 透過 `group_left` 同時服務多個邏輯隔離的租戶，每租戶獨立配置、路由與維護窗口。

## N

**N:1 Tenant Mapping**
:   多個 Kubernetes namespace 對應同一個邏輯租戶。透過 `scaffold_tenant.py --namespaces` + Prometheus `relabel_configs` 實現。

## O

**OCI Registry**
:   Open Container Initiative 標準的容器/Helm chart 儲存庫。本平台的 Helm chart 和 Docker images 均發佈至 `ghcr.io/vencil/`。

**Operator CRD**
:   Prometheus Operator 的 Custom Resource Definition（`ServiceMonitor` / `PrometheusRule` / `AlertmanagerConfig`）。本平台支援以 operator-manifests 形式整合既有 Operator 環境。

## P

**Platform Engineer（平台工程師）**
:   負責部署、維運平台基礎設施與路由的角色 persona。與 Domain Expert、Tenant 並列三大使用者角色，見角色指南。

**Profile Builder**
:   da-portal 的互動式路由 profile 產生工具（JSX），協助以視覺化方式組裝 Routing Profile 與 Domain Policies。

**Projected Volume**
:   Kubernetes Volume 類型，可將多個 ConfigMap 掛載至同一目錄。本平台用於將 15 個 Rule Pack ConfigMap 掛載至 Prometheus 的 rules 目錄，每個設定 `optional: true`。

**Prometheus Operator**
:   CNCF 的 Kubernetes operator，以 CRD 管理 Prometheus / Alertmanager。本平台可與既有 Operator 環境整合（見 Operator CRD、operator-manifests）。

## R

**Recording Rule**
:   Prometheus 預計算規則，將複雜查詢結果存為新的時間序列。本平台的 Rule Pack 中大量使用，如 `tenant:mysql_threads_connected:max`。

**Rule Pack**
:   預定義的 Prometheus recording rule + alert rule 套件。目前共 15 個（MariaDB, Redis, PostgreSQL, MongoDB, ElasticSearch, Kafka, RabbitMQ, HAProxy, Kubernetes, Node, JVM, Nginx, Blackbox, Custom, Platform Health）。

**Routing Profile（路由設定檔）**
:   定義告警如何路由至各接收端的設定檔，由 `generate_alertmanager_routes.py` 消費，搭配 Domain Policies 的 webhook 域名護欄。

**Runbook**
:   告警處置手冊。透過 `_metadata` 注入 `runbook_url`，在告警觸發時自動附帶處置指引連結。

## S

**Scaffold（鷹架）**
:   `scaffold_tenant.py` / `da-tools scaffold`——互動式或 CLI 模式產生新租戶的 YAML 配置模板。

**Sentinel Alert**
:   哨兵告警模式。exporter 產生 flag metric（如 `_silent_mode: 1`），對應的 sentinel recording rule 觸發 alert，再由 Alertmanager inhibit rule 抑制目標告警。這是三態模式的核心實現機制。

**Severity Dedup（嚴重度去重）**
:   同一指標的 critical 與 warning 告警共存時，透過 Alertmanager `inhibit_rules`（非 PromQL）抑制 warning，確保 TSDB 保留完整數據。

**Shadow Monitoring**
:   影子監控——遷移期間同時運行舊規則與新規則，比對數值差異。`validate_migration.py` 偵測 auto-convergence 後，`cutover_tenant.py` 一鍵切換。

**Silent Mode（靜默模式）**
:   三態之一。`_silent_mode` 設定後，告警持續評估但通知被抑制。用於已知問題期間避免告警疲勞。支援 `expires` 自動失效。

**Staged Adoption（分階段導入）**
:   建議的漸進式導入路徑——從單一租戶 shadow monitoring 起步，逐步擴大涵蓋面與切換規則。見 `docs/scenarios/staged-adoption-guide.md`。

## T

**Tenant（租戶）**
:   本平台的核心概念——一個邏輯上獨立的監控對象（通常對應一個 Kubernetes namespace 或應用團隊）。每個 tenant 有獨立的閾值配置、路由規則、維護窗口。

**Tenant Manager（租戶管理介面）**
:   da-portal 的 Live Tenant Manager 視圖（try-local Mode 0 雙星之一）——在瀏覽器編輯租戶配置並 Save，觸發 tenant-api 真實 git commit。

**Tenant API**
:   本平台的租戶配置寫入 API 元件（`tenant-api`）。提供租戶 CRUD、GitOps write-back、forge PR 整合。

**threshold-exporter**
:   本平台的核心元件。讀取 tenant YAML 配置，轉換為 Prometheus metrics（`user_threshold` 系列）。支援 HA 部署（×2），端口 8080。

**Three-State Model（三態模式）**
:   Normal / Silent / Maintenance 三種運營狀態。每種狀態透過 Sentinel Alert + Alertmanager Inhibit 實現，均支援 `expires` 自動失效。

**TSDB (Time Series Database)**
:   Prometheus 的時間序列資料庫。本平台的 Severity Dedup 設計確保 TSDB 永遠保留完整數據（critical + warning），只在通知層面去重。

## W

**Webhook Domain Allowlist**
:   `generate_alertmanager_routes.py` 的安全護欄。`--policy` 參數使用 fnmatch 檢查 webhook URL 的域名，空清單 = 不限制。

---

## 內部代號 — 禁止用於對外文件

> ⚠️ **這一段是 Layer 2 codename gate 的 SSOT（[#469](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/469)）。** 下表列出的是**規劃／追蹤用內部代號**——對 AI agent 與 maintainer 有意義，但對沒有規劃上下文的外部讀者只會造成困惑。`check_codename_gate.py` 會掃描對外文件，命中下列模式即 **fail loud**；對外文件請改用「對外應改用」欄的說法。
>
> **模板語法**（給 lint 用，讀者可忽略）：`{N}`=一段數字、`{X}`=單一字母、`{x}`=單一小寫字母、`{AE}`=單一大寫字母 A–E。其餘字元視為字面值。新代號家族在這裡登錄一列即生效，毋須改 lint 程式碼。

| 代號模式 | 說明 | 對外應改用 |
|---|---|---|
| `TD-{N}` | 舊技術債 ticket（已併入 TRK 命名空間） | 功能名稱或 issue 連結 |
| `HA-{N}` | 舊 HA 規劃 id（已併入 TRK） | 功能名稱 |
| `S#{N}` | Sprint／closure 議題編號 | 版本標籤或功能名稱 |
| `DEC-{X}` | 跨切面 maintainer 決策標籤 | 決策結果本身的描述 |
| `{AE}-{N}` | 單字母字首規劃 id（B-1 / C-12 等，A–E） | 功能名稱 |
| `PR-{N}` | 內部 PR 序號代號（PR-2d 等） | GitHub PR 連結 |
| `Phase .{x}` | 內部 sprint 階段代號（Phase .a/.b/.c） | 「第一階段」等敘述，或具體里程碑 |
| `Track {X}` | 內部工作分軌（Track A/B/C，A–E） | 工作項目名稱 |
| `Wave {N}` | 內部分批代號（Wave 3 等） | 批次說明或時程 |
| `v{N}.{N}.{N}-final` | release 暫存後綴代表式（`-rc` / `-alpha` / `-beta` / `-preview` 等含數字尾綴由 Layer 1 `check_codename_leak.py` 精準 hard-gate；本 gate 收 `-final` 代表式、其餘走 shape 發現） | 純 semver（如 `v2.8.0`） |

<!-- 註：TRK-{N}（ADR-019 統一 tracking namespace，ADR 中公開引用）/ ADR-{N} / CVE-* / SHA-* /
     UTF-* / 兩字大寫產品詞（Rule Pack / Tenant API 等）屬「對外核可」，由上方字母表 **Term** 條目與
     lint 內建 allowlist 涵蓋，不列為內部代號（與 Layer 1 check_codename_leak.py 的 PATTERNS 對齊）。 -->

---

## 相關資源

| 資源 | 用途 |
|------|------|
| [Architecture & Design](./architecture-and-design.md) | 核心架構與設計詳解 |
| [CLI Reference](./cli-reference.md) | da-tools 完整指令參考 |
| [API Reference](./api/README.md) | threshold-exporter API 端點 |
| [Alert Reference](rule-packs/ALERT-REFERENCE.md) | 96 個告警含義速查 |
