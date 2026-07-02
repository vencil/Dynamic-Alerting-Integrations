---
title: "整合指南導覽 — 依你現有的監控架構選讀"
tags: [integration, navigation]
audience: [platform-engineer, sre, devops]
version: v2.9.1
lang: zh
---

# 整合指南導覽

> **Language / 語言：** **中文（當前）** | [English](./README.en.md)

本目錄收錄把 Multi-Tenant Dynamic Alerting 平台接進**你既有環境**的端到端指南。每份指南獨立可讀，含前置條件、步驟與驗證命令。

> **不確定走哪條路？** 先用 [互動式決策矩陣](../getting-started/decision-matrix.md) 依你的 Prometheus 形態、GitOps 成熟度與租戶規模選出推薦路徑，再回到這裡讀對應指南。

## 依你現有的監控架構選讀

| 你的現況 | 推薦路徑 | 指南 |
|---|---|---|
| 自管 Prometheus（非 Operator） | ConfigMap + SHA-256 熱重載 | [BYO Prometheus](byo-prometheus-integration.md) · [BYO Alertmanager](byo-alertmanager-integration.md) |
| 已用 Prometheus Operator（CRD 原生） | `rules.mode=operator`，產 `PrometheusRule` CRD | [Operator 整合手冊（Hub）](prometheus-operator-integration.md) |
| GitOps（ArgoCD / Flux） | Helm + Git repo，宣告式同步 | [GitOps 部署](gitops-deployment.md) |
| 用 VictoriaMetrics 取代 Prometheus | VM 相容路徑 | [VictoriaMetrics 整合](victoriametrics-integration.md) |
| 多叢集 / 租戶自管 federation | label-injection proxy | [Federation](federation-integration.md) · [Tenant Federation](tenant-federation.md) |

## 指南分類

### 自管 Prometheus 堆疊（BYO）

接到你自己維護的 Prometheus / Alertmanager，不依賴 Operator。

- [BYO Prometheus](byo-prometheus-integration.md) — 把平台 Rule Pack 與 `user_threshold` 指標接進現有 Prometheus
- [BYO Alertmanager](byo-alertmanager-integration.md) — 套用四層路由與 inhibit 規則到現有 Alertmanager

### Prometheus Operator（CRD 原生）

若叢集已跑 Prometheus Operator，平台以 `PrometheusRule` CRD 形式交付規則。

- [Operator 整合手冊（Hub）](prometheus-operator-integration.md) — Operator 路徑總入口，先讀這份
- [Operator Prometheus 整合](operator-prometheus-integration.md) — CRD 規則交付細節
- [Operator Alertmanager 整合](operator-alertmanager-integration.md) — Operator 下的路由配置
- [Operator GitOps 部署](operator-gitops-deployment.md) — Operator + GitOps 組合
- [Operator Shadow Monitoring](operator-shadow-monitoring.md) — Operator 下的影子監控切換策略

### GitOps 與替代 TSDB

- [GitOps 部署](gitops-deployment.md) — ArgoCD / Flux 宣告式部署
- [VictoriaMetrics 整合](victoriametrics-integration.md) — 以 VM 為 TSDB 的相容路徑

### 多叢集 / Federation

- [Federation 整合](federation-integration.md) — 跨叢集中央匯聚
- [Tenant Federation](tenant-federation.md) — 租戶把自己 metrics 拉回自管側

### 容量與排錯

- [部署容量規劃](deployment-sizing.md) — 副本數、資源請求、cardinality 預估
- [Migration Troubleshooting Checklist](troubleshooting-checklist.md) — 遷移期 symptom-keyed runbook

## 下一步

- 還在評估？→ [決策矩陣](../getting-started/decision-matrix.md) · [架構與設計](../architecture-and-design.md)
- 想先動手？→ [Platform Engineer 快速入門](../getting-started/for-platform-engineers.md) · [實戰場景指南](../scenarios/README.md)
- 遷移既有系統？→ [遷移指南](../migration-guide.md)
