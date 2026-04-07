---
title: "未來擴展路線 — K8s Operator、Design System、Auto-Discovery 等"
tags: [architecture, roadmap, design]
audience: [platform-engineer, devops]
version: v2.6.0
parent: architecture-and-design.md
lang: zh
---
# 未來擴展路線

> **Language / 語言：** | **中文（當前）** | [English](roadmap-future.en.md)
>
> ← [返回主文件](../architecture-and-design.md)

DX 工具改善追蹤見 [dx-tooling-backlog.md](../internal/dx-tooling-backlog.md)。

---

## 計畫中（v2.7.0）

v2.7.0 的核心是「讓平台更容易被全球團隊採用，並擴展自動化」。

### EN-first 雙語 SSOT

123 markdown + 32 JSX + 15 Rule Pack + 7 lint hook 遷移。消除 ZH/EN 內容漂移的根源。（評估文件見 `docs/internal/ssot-language-evaluation.md`，`status: draft`）

### Field-level RBAC

拆分 write 為 `edit-threshold` / `edit-routing` / `edit-state`。Enterprise 合規需求：不同角色修改不同欄位。

### Tenant Auto-Discovery

Kubernetes-native 環境：根據 namespace label（`dynamic-alerting.io/tenant: "true"`）自動註冊。推薦 sidecar 模式：定期掃描 namespace label → 產生 tenant YAML → config-dir 既有 Directory Scanner 載入。明確配置永遠優先。`discover_instance_mappings.py` 可復用。

### Grafana Dashboard as Code

`scaffold_tenant.py --grafana` 自動產生 per-tenant dashboard JSON。利用 `platform-data.json` 已有的 metadata 產生對應 panel。搭配 Grafana provisioning 或 API 自動部署。

### Playwright E2E 完整覆蓋

擴展至全部 39 支 JSX 工具 smoke test + 真實 backend integration test。

### Release Automation 完善

tag push → GitHub Release Notes 自動產生（基於 CHANGELOG section）→ OCI image build/push 全自動。五線版號手動 release 的人為錯誤率歸零。

---

## 探索方向（長期）

| 方向 | 前置條件 | 預期價值 |
|------|---------|---------|
| **Anomaly-Aware Dynamic Threshold** | ML 基礎設施（時序分析、季節性偵測） | 閾值從「人工設定」進化為「自動調適」。`_threshold_mode: adaptive` + `quantile_over_time`。靜態閾值作為安全下限（floor） |
| **Log-to-Metric Bridge** | Loki / Elasticsearch 整合 | 統一 log + metric 告警管理。推薦生態系解法：`grok_exporter / mtail → Prometheus → 本平台` |
| **Multi-Format Export** | metric-dictionary.yaml 對照表 | `da-tools export --format datadog/terraform` — 平台成為告警策略的抽象層 |
| **DynamicAlertTenant CRD** | Operator SDK + CRD versioning | 取代 ConfigMap + Directory Scanner（需重新評估 ADR-008 架構邊界） |
| **ChatOps 深度整合** | Slack/Teams Bot SDK | 雙向操作（查詢 tenant 狀態、觸發靜默模式） |
| **CI/CD Pipeline 狀態透傳** | PR write-back 穩定化 | PR/MR CI Status Check 回傳 Portal UI |
| **SRE Alert Tracker** | 告警生命週期模型設計 | 觸發 → 認領 → 調查 → 解決 → 事後分析 |

---

## 版本演進紀錄

| 版本 | 主題 | 里程碑 |
|------|------|--------|
| v2.6.0 | Operator × PR Write-back × Design System | ADR-011、GitLab MR、axe-core WCAG |
| v2.5.0 | Multi-Tenant Grouping × E2E Testing | Playwright 基礎、Saved Views |
| v2.4.0 | Tenant Management API × pkg/config | REST API RBAC、Portal UI |
| v2.3.0 | Operator Native Path × Rule Pack Split | ADR-008、federation-check、rule-pack-split |
| v2.2.0 | Adoption Pipeline × CLI 擴展 | init、config-history、gitops-check |
| v2.1.0 | Routing Profiles × Domain Policy | ADR-007、四層路由合併 |

完整版本歷程見 [CHANGELOG.md](../../CHANGELOG.md)。
