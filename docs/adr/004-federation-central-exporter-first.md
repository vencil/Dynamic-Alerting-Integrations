---
title: "ADR-004: Federation 架構——中央 Exporter 優先"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.6.0
lang: zh
---

# ADR-004: Federation 架構——中央 Exporter 優先

<!-- Language switcher is provided by mkdocs-static-i18n header. -->

## 狀態

✅ **Accepted** (v1.12.0) → **Extended** (v2.1.0+：兩種架構均已實現)

## 背景

Multi-Tenant Dynamic Alerting 平台面臨多叢集監控需求。企業通常在多個 Kubernetes 叢集上運行業務，需要統一的告警管理。

Federation 存在兩種主要架構：

**中央 Exporter + 邊緣 Prometheus**
- 單一 threshold-exporter 部署在中央，服務所有邊緣叢集
- 邊緣 Prometheus 透過 `remote_read` 從中央 exporter 讀取閾值指標
- 邊緣 Prometheus 只需本地 rules；告警由本地或中央 Alertmanager 處理

**邊緣 Exporter + 中央聚合**
- 每個邊緣叢集部署獨立的 threshold-exporter
- 中央 Prometheus 透過聯邦抓取或 remote_write 聚合邊緣資料
- 複雜度：N 個 exporter 實例、N 個配置、中央協調邏輯

### 決策標準

| 標準 | 中央 Exporter | 邊緣 Exporter |
|:-----|:-----:|:-----:|
| Exporter 部署數 | 1 | N |
| 配置管理複雜度 | 低 | 高 |
| 覆蓋用例百分比 | ~80% | ~20% |
| 實施時間 | 短 | 長 |

## 決策

**優先實現「中央 Exporter + 邊緣 Prometheus」架構。**

基於 80-20 法則：大多數企業採用中央管理的監控架構（統一告警策略、單一 exporter 即可應對多叢集），此架構可覆蓋多數用例，且實施快速。

## 基本原理

### 架構簡潔性

**中央 Exporter**：配置集中管理，所有 Prometheus 同步拉取。單個 exporter 部署 HA（多副本），成本低。邊緣 Prometheus 之間無依賴、無協調邏輯。

**邊緣 Exporter**：每個邊緣需獨立配置，中央需追蹤 N 個實例。N 個 exporter 版本升級需協調。中央需聚合邊緣資料，可能出現資料重複或遺漏。

### 時間與資源考量

中央 Exporter 的核心開發工作在 v1.12.0 已完成：`remote_read` 整合測試、文件記錄（[federation-integration.md](../integration/federation-integration.md)）、典型部署時間 2-3 小時。相比之下，邊緣 Exporter 架構需額外 6-8 週開發時間（實例管理框架、聚合邏輯、多層配置驗證）。

## 後果

### 正面影響

- 快速推出 Federation 支援，滿足多數用例
- 簡化初期運維負擔
- 為後續邊緣 Exporter 架構打下 API/工具基礎
- 客戶可漸進式採用——先用中央架構，後續按需升級

### 負面影響

- 邊緣自主運營的用例在 v1.x 無法支援
- 若邊緣 Exporter 需求旺盛，會面臨部分重設計

### 遷移路徑

中央架構的使用者升級至邊緣架構時可平滑遷移：API 相容性保證（無需修改現有部署）、`scaffold_tenant.py` 擴展支援邊緣配置、文件提供明確切換步驟。

## 替代方案考量

| 方案 | 判斷 | 原因 |
|------|------|------|
| 同時實現兩種架構 | 拒絕 | 時間表延期、初期複雜度過高、難以測試 |
| 只實現邊緣架構 | 拒絕 | 違背 MVP 原則、挫傷客戶時間表 |

## 相關決策

- [ADR-006: 租戶映射拓撲](./006-tenant-mapping-topologies.md) — 基於中央 Exporter 的 data-plane Recording Rules 實現 1:N 映射
- [ADR-005: 投影卷掛載 Rule Pack](./005-projected-volume-for-rule-packs.md) — Federation 中 Rule Pack 的掛載機制

## 演進紀錄

| 版本 | 狀態 | 變更 |
|------|------|------|
| v1.12.0 | ✅ 完成 | 中央 Exporter 核心實現、`remote_read` 整合測試、文件記錄 |
| v2.1.0 | ✅ 完成 | `federation_check.py` 支援邊緣/中央雙模驗證。**邊緣 Exporter 架構亦已實現**——`da-tools rule-pack-split` 支援邊緣正規化 + 中央聚合 + Operator CRD 輸出 |
| v2.6.0 | ✅ 完成 | `operator-generate --kustomize` 支援多叢集 CRD 部署；`drift_detect.py --mode operator` 偵測跨叢集 CRD 漂移 |

## 參考資料

- [`docs/federation-integration.md`](../integration/federation-integration.md) — Federation 詳細整合指南
- [`docs/scenarios/multi-cluster-federation.md`](../scenarios/multi-cluster-federation.md) — 多叢集場景案例
- `CHANGELOG.md` — v1.12.0 Federation 初始實現記錄

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [001-severity-dedup-via-inhibit](001-severity-dedup-via-inhibit.md) | ⭐⭐⭐ |
| [002-oci-registry-over-chartmuseum](002-oci-registry-over-chartmuseum.md) | ⭐⭐⭐ |
| [003-sentinel-alert-pattern](003-sentinel-alert-pattern.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs](005-projected-volume-for-rule-packs.md) | ⭐⭐⭐ |
| [README](README.md) | ⭐⭐⭐ |
| [架構與設計](../architecture-and-design.md) | ⭐⭐ |
