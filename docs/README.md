---
title: "文件導覽"
tags: [overview, introduction]
audience: [all]
version: v2.7.0
lang: zh
---
# 文件導覽

> [English](README.en.md) | 中文

Dynamic Alerting 平台的完整文件導覽。根據你的角色快速找到所需資源。

---

## 你是誰？從哪裡開始

### 🔧 Platform Engineer（平台工程師）

負責部署、擴展與維護整個 Dynamic Alerting 平台。

| 文件 | 用途 |
|------|------|
| [快速入門](getting-started/for-platform-engineers.md) | 30 分鐘快速上手：安裝、架構概覽、核心配置 |
| [核心架構](architecture-and-design.md) | 系統設計、三態運營、告警路由、去重機制、HA 設計、未來藍圖 |
| [性能基準](benchmarks.md) | 負載測試結果、吞吐量、延遲、資源消耗分析 |
| [治理與安全](governance-security.md) | RBAC、稽核日誌、Schema 驗證、Cardinality Guard、安全合規 |
| [GitOps 部署](integration/gitops-deployment.md) | ArgoCD/Flux 整合、CODEOWNERS RBAC、漂移檢測 |
| [Federation 集成](integration/federation-integration.md) | 多叢集場景、邊緣 Prometheus、中央監控架構 |
| [BYO Prometheus](integration/byo-prometheus-integration.md) | 自帶 Prometheus 整合指引 |
| [BYO Alertmanager](integration/byo-alertmanager-integration.md) | 自帶 Alertmanager 整合指引 |

### 📊 Domain Expert（域專家）— DBA、Database Engineer

負責告警規則配置、Rule Pack 管理、Custom 規則審核。

| 文件 | 用途 |
|------|------|
| [快速入門](getting-started/for-domain-experts.md) | Domain Expert 30 分鐘入門：Rule Pack 概念、Custom 規則治理 |
| [Rule Pack 文件](rule-packs/README.md) | 15 個 Rule Pack 目錄 + optional 卸載說明 |
| [Custom Rule 治理](custom-rule-governance.md) | 三層治理模型、Linting、Schema 驗證、Best Practices |
| [遷移引擎](migration-engine.md) | AST 遷移引擎架構、跨方言規則轉換、Triage 邏輯 |

### 👤 Tenant Team（租戶團隊）— SRE、DBA、On-call

負責配置租戶告警、監控、故障排查。

| 文件 | 用途 |
|------|------|
| [快速入門](getting-started/for-tenants.md) | Tenant 30 分鐘入門：配置格式、告警路由、維護窗口 |
| [遷移指南](migration-guide.md) | 從傳統告警遷移至 Dynamic Alerting：步驟、檢查清單、常見問題 |
| [故障排查](troubleshooting.md) | 故障診斷、邊界情況、常見問題、Debug 命令 |

### 📐 全局視圖

想要快速理解整個平台的角色、工具、產品互動？

| 文件 | 用途 |
|------|------|
| [專案概覽](index.md) | 痛點對比、企業價值、快速開始（根目錄） |

---

## 場景指南

實際工作中的常見場景與對應解決方案。

| 場景 | 文件 | 適用角色 |
|------|------|---------|
| Alert 雙視角通知（NOC vs Tenant） | [scenarios/alert-routing-split.md](scenarios/alert-routing-split.md) | Platform Engineer、SRE |
| Shadow Monitoring 一鍵切換 | [scenarios/shadow-monitoring-cutover.md](scenarios/shadow-monitoring-cutover.md) | Platform Engineer、DevOps |
| 多叢集 Federation | [scenarios/multi-cluster-federation.md](scenarios/multi-cluster-federation.md) | Platform Engineer |
| Tenant 生命週期（新增、修改、下架） | [scenarios/tenant-lifecycle.md](scenarios/tenant-lifecycle.md) | Platform Engineer、DevOps |
| 進階場景 & 測試覆蓋 | [internal/test-coverage-matrix.md](internal/test-coverage-matrix.md) | Platform Engineer、SRE |

---

## 深入主題

特定技術領域的深入討論。

| 主題 | 文件 | 內容概要 |
|------|------|---------|
| 系統設計 | [architecture-and-design.md](architecture-and-design.md) | Severity Dedup、Sentinel Alert、告警路由、Per-rule Overrides、Platform Enforced Routing、Regex 維度閾值、排程式閾值、Dynamic Runbook Injection、Recurring Maintenance |
| 性能分析 | [benchmarks.md](benchmarks.md) | idle、under-load、routing、alertmanager、reload 基準；詳細測試方法論 |
| 治理與稽核 | [governance-security.md](governance-security.md) | RBAC、稽核日誌、Cardinality Guard、Schema 驗證、Secret 管理、安全最佳實踐 |
| 遷移引擎 | [migration-engine.md](migration-engine.md) | AST 架構、方言支援、Triage、Dictionary 映射、Prefix 管理、轉換規則 |
| 遷移指南 | [migration-guide.md](migration-guide.md) | 逐步遷移說明、檢查清單、前置條件、驗證步驟 |
| 故障排查 | [troubleshooting.md](troubleshooting.md) | 常見問題、Debug 命令、日誌分析、邊界情況處理 |
| Shadow Monitoring | [shadow-monitoring-sop.md](shadow-monitoring-sop.md) | Shadow Mode 運營流程、數值對比、自動收斂偵測、SLA 驗證 |

---

## 工具速查

da-tools 容器封裝 23 個 CLI 命令，涵蓋租戶生命週期、日常運維、品質治理三大面向。`scripts/tools/` 下另有 73 個 Python 工具（含 DX 自動化與 lint）。

完整參考：[da-tools CLI](cli-reference.md) · [工具總表](internal/tool-map.md) · [速查表](cheat-sheet.md) · [互動工具索引](interactive-tools.md)

---

## 內部文件

給 AI Agent 與內部開發使用的 Playbook 與計畫文件。

| 文件 | 用途 |
|------|------|
| `docs/internal/testing-playbook.md` | K8s 排錯、負載注入、Benchmark 方法論、程式碼品質、SAST |
| `docs/internal/windows-mcp-playbook.md` | Docker exec、Shell 陷阱、Port-forward、Helm 防衝突 |
| `docs/internal/github-release-playbook.md` | Git push、Tag、GitHub Release、CI 觸發流程 |

---

## 反饋與貢獻

文件問題或改進建議？請提交 Issue 或 PR。開發命令與版本管理見 [根目錄 README](../README.md#開始使用)。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Documentation Guide"](./README.en.md) | ⭐⭐⭐ |
| ["Dynamic Alerting Platform — Home"](./index.md) | ⭐⭐ |
