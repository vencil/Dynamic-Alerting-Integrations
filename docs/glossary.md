---
title: "術語表"
tags: [reference, glossary]
audience: [all]
version: v2.0.0-preview.2
lang: zh
---
# 術語表

> **Language / 語言：** [English](glossary.en.md) | **中文（當前）**

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

## D

**da-tools**
:   平台的 CLI 工具容器（`ghcr.io/vencil/da-tools`），封裝所有 Python 工具。`docker pull` 即可使用，無需 clone repo 或安裝依賴。見 `docs/cli-reference.md`。

**Directory Scanner**
:   threshold-exporter 的 `-config-dir` 模式，掃描 `conf.d/` 目錄下所有 YAML 檔案，支援 per-tenant 獨立檔案管理。

**Dual-Perspective Annotation**
:   `platform_summary`（NOC 視角）+ `summary`（Tenant 視角）雙重 annotation。搭配 `_routing_enforced` 實現平台與租戶各看各的告警描述。

## F

**Federation**
:   多叢集架構模式。場景 A：中央 threshold-exporter + 邊緣 Prometheus，透過 federation 拉取指標。見 `docs/federation-integration.md`。

## G

**`group_left`**
:   PromQL 向量匹配運算子。本平台的核心機制——用一條 rule 透過 `group_left` 匹配所有租戶的閾值向量，取代 per-tenant 獨立 rule。

## H

**Hot-Reload**
:   配置熱重載——修改 ConfigMap 後 threshold-exporter 自動偵測 SHA-256 hash 變更並重新載入，無需重啟 Pod。平均重載時間 < 2 秒。

## I

**Inhibit Rule**
:   Alertmanager 的告警抑制規則。本平台用於：(1) Severity Dedup（critical 抑制 warning）、(2) 三態模式的 Silent/Maintenance 狀態抑制。

## M

**Maintenance Mode（維護模式）**
:   三態之一。`_state_maintenance` 設定後，產生 sentinel alert 觸發 Alertmanager inhibit 抑制該租戶所有告警。支援 `expires` 自動失效與 `recurring[]` 排程式維護窗口。

## N

**N:1 Tenant Mapping**
:   多個 Kubernetes namespace 對應同一個邏輯租戶。透過 `scaffold_tenant.py --namespaces` + Prometheus `relabel_configs` 實現。

## O

**OCI Registry**
:   Open Container Initiative 標準的容器/Helm chart 儲存庫。本平台的 Helm chart 和 Docker images 均發佈至 `ghcr.io/vencil/`。

## P

**Projected Volume**
:   Kubernetes Volume 類型，可將多個 ConfigMap 掛載至同一目錄。本平台用於將 15 個 Rule Pack ConfigMap 掛載至 Prometheus 的 rules 目錄，每個設定 `optional: true`。

## R

**Recording Rule**
:   Prometheus 預計算規則，將複雜查詢結果存為新的時間序列。本平台的 Rule Pack 中大量使用，如 `tenant:mysql_threads_connected:max`。

**Rule Pack**
:   預定義的 Prometheus recording rule + alert rule 套件。目前共 15 個（MariaDB, Redis, PostgreSQL, MongoDB, ElasticSearch, Kafka, RabbitMQ, HAProxy, Kubernetes, Node, JVM, Nginx, Blackbox, Custom, Platform Health）。

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

## T

**Tenant（租戶）**
:   本平台的核心概念——一個邏輯上獨立的監控對象（通常對應一個 Kubernetes namespace 或應用團隊）。每個 tenant 有獨立的閾值配置、路由規則、維護窗口。

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

## 相關資源

| 資源 | 用途 |
|------|------|
| [Architecture & Design](./architecture-and-design.md) | 核心架構與設計詳解 |
| [CLI Reference](./cli-reference.md) | da-tools 完整指令參考 |
| [API Reference](./api/README.md) | threshold-exporter API 端點 |
| [Alert Reference](../rule-packs/ALERT-REFERENCE.md) | 96 個告警含義速查 |
