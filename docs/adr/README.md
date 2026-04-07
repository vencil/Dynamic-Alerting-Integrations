---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.6.0
lang: zh
---

# 架構決策記錄 (ADR)

本目錄收錄 Multi-Tenant Dynamic Alerting 平台的架構決策記錄 (Architecture Decision Records)。每份 ADR 記錄特定設計決策的背景、選項評估與長期影響。

## 快速導讀

初次接觸？依你的需求選讀：

- **理解核心設計**：[001 Severity Dedup](./001-severity-dedup-via-inhibit.md) + [005 Projected Volume](./005-projected-volume-for-rule-packs.md) — 掌握規則引擎的兩個基石
- **準備部署**：[008 Operator 整合路徑](./008-operator-native-integration-path.md) — ConfigMap vs Operator CRD 雙路徑選擇
- **多叢集需求**：[004 Federation](./004-federation-central-exporter-first.md) + [006 租戶映射](./006-tenant-mapping-topologies.md) — Federation 架構與拓撲
- **管理平面**：[009 Tenant API](./009-tenant-manager-crud-api.md) + [011 PR Write-back](./011-pr-based-write-back.md) — UI/API 管理與合規流程

## ADR 索引

| ID | 標題 | 狀態 | 摘要 |
|:---|:-----|:-----|:-----|
| [001](#001-嚴重度-dedup-採用-inhibit-規則) | 嚴重度 Dedup 採用 Inhibit 規則 | ✅ Accepted | 使用 Alertmanager inhibit_rules 而非 PromQL 進行嚴重度去重，保留 TSDB 完整性 |
| [002](#002-oci-registry-替代-chartmuseum) | OCI Registry 替代 ChartMuseum | ✅ Accepted | 選擇 ghcr.io OCI 統一分發 Helm charts 與 Docker images，簡化基礎設施 |
| [003](#003-sentinel-alert-模式) | Sentinel Alert 模式 | ✅ Accepted | 利用哨兵告警 + inhibit 實現三態控制，取代直接 PromQL 抑制 |
| [004](#004-federation-架構中央-exporter-優先) | Federation 架構——中央 Exporter 優先 | ✅ Accepted → Extended | 優先實現中央 exporter + 邊緣 Prometheus 的聯邦模式（v2.1.0+ 兩種架構均已實現） |
| [005](#005-投影卷掛載-rule-pack) | 投影卷掛載 Rule Pack | ✅ Accepted | 採用 Projected Volume 與 optional:true 實現可選 Rule Pack 卸載 |
| [006](#006-租戶映射拓撲-11-n1-1n) | 租戶映射拓撲 (1:1, N:1, 1:N) | ✅ Accepted | 資料平面 Recording Rules 解決三種實例-租戶映射拓撲，Exporter 零變更 |
| [007](#007-跨域路由設定檔與域策略) | 跨域路由設定檔與域策略 | ✅ Accepted | Routing Profiles（重用）+ Domain Policies（約束）兩層架構 |
| [008](#008-operator-native-整合路徑) | Operator-Native 整合路徑 | ✅ Accepted | 工具鏈適配模式：ConfigMap / Operator CRD 雙路徑，核心 exporter 不變 |
| [009](#009-tenant-manager-crud-api-架構) | Tenant Manager CRUD API 架構 | ✅ Accepted | Go HTTP server + oauth2-proxy + commit-on-write 的管理平面 API |
| [010](#010-multi-tenant-grouping-architecture) | Multi-Tenant Grouping Architecture | ✅ Accepted | `_groups.yaml` 自定義群組 + 擴展 `_metadata` 多維度篩選 |
| [011](#011-pr-based-write-back-模式) | PR-based Write-back 模式 | ✅ Accepted | 雙模式架構（direct / pr），支援 GitHub PR 與 GitLab MR |

---

## 001: 嚴重度 Dedup 採用 Inhibit 規則

**文件**: [`001-severity-dedup-via-inhibit.md`](./001-severity-dedup-via-inhibit.md)

使用 Alertmanager inhibit_rules 而非 PromQL 的 `absent()`/`unless()` 進行嚴重度去重。關鍵考量：保留 TSDB 完整性，同一指標的多個嚴重度級別都被記錄，Alertmanager 層級進行智慧抑制。

---

## 002: OCI Registry 替代 ChartMuseum

**文件**: [`002-oci-registry-over-chartmuseum.md`](./002-oci-registry-over-chartmuseum.md)

選擇 ghcr.io OCI registry 統一分發 Helm charts 與 Docker images，消除對獨立 ChartMuseum 的依賴。需要 Helm 3.8+，但簡化運維成本。

---

## 003: Sentinel Alert 模式

**文件**: [`003-sentinel-alert-pattern.md`](./003-sentinel-alert-pattern.md)

透過 exporter flag metric → recording rule → sentinel alert → inhibit 的流程實現三態模式 (Normal/Silent/Maintenance)。相比直接 PromQL 抑制，此模式組合性強且易於調試。

---

## 004: Federation 架構——中央 Exporter 優先

**文件**: [`004-federation-central-exporter-first.md`](./004-federation-central-exporter-first.md)

優先實現「中央 Exporter + 邊緣 Prometheus」架構（80-20 法則）。v1.12.0 完成核心實現，v2.1.0 邊緣 Exporter 架構亦已實現（`rule-pack-split`），v2.6.0 擴展多叢集 CRD 部署與漂移偵測。

---

## 005: 投影卷掛載 Rule Pack

**文件**: [`005-projected-volume-for-rule-packs.md`](./005-projected-volume-for-rule-packs.md)

採用 Projected Volume 與 `optional: true` 實現 15 個 Rule Pack 的可選卸載。租戶可刪除個別 ConfigMap 來禁用特定 Rule Pack，Prometheus 不會因缺失 pack 而失敗。

---

## 006: 租戶映射拓撲 (1:1, N:1, 1:N)

**文件**: [`006-tenant-mapping-topologies.md`](./006-tenant-mapping-topologies.md)

在資料平面透過 Prometheus Recording Rules 解決三種實例-租戶映射拓撲 (1:1, N:1, 1:N)。1:N 拓撲（Oracle 多 schema、DB2 多 tablespace）透過 config-driven `instance_tenant_mapping` 自動產生 Recording Rules，threshold-exporter 保持零變更。

---

## 007: 跨域路由設定檔與域策略

**文件**: [`007-cross-domain-routing-profiles.md`](./007-cross-domain-routing-profiles.md)

兩層架構：Routing Profiles（命名路由配置，供多租戶共用）+ Domain Policies（業務域合規約束，驗證而非繼承）。配置重複從 O(N) 降為 O(1)，域策略提供機器可驗證的合規約束。

---

## 008: Operator-Native 整合路徑

**文件**: [`008-operator-native-integration-path.md`](./008-operator-native-integration-path.md)

核心平台（threshold-exporter + Rule Pack）保持 path-agnostic，新增 `operator-generate` / `operator-check` 工具鏈處理 Prometheus Operator CRD 轉換與驗證。v2.6.0 新增架構邊界宣言：exporter 不 watch 任何 CRD，CRD 轉換由外部工具負責。

---

## 009: Tenant Manager CRUD API 架構

**文件**: [`009-tenant-manager-crud-api.md`](./009-tenant-manager-crud-api.md)

獨立 Go HTTP server（tenant-api）作為 da-portal 的管理平面後端。oauth2-proxy 處理認證，commit-on-write 確保 Git 審計軌跡，`_rbac.yaml` 提供細粒度權限。v2.6.0 擴展為非同步批量操作 + SSE 推播 + PR-based 寫回。

---

## 010: Multi-Tenant Grouping Architecture

**文件**: [`010-multi-tenant-grouping.md`](./010-multi-tenant-grouping.md)

`_groups.yaml` 儲存自定義群組定義（靜態 `members[]` 列表），搭配擴展的 `_metadata` schema（environment、region、domain、db_type、tags）實現多維度篩選與群組批量操作。

---

## 011: PR-based Write-back 模式

**文件**: [`011-pr-based-write-back.md`](./011-pr-based-write-back.md)

在 commit-on-write 基礎上新增 `_write_mode: pr` 選項，UI 操作產生 GitHub PR 或 GitLab MR 而非直接 commit，滿足四眼原則等合規要求。Platform Abstraction Layer 支援 GitHub + GitLab 雙平台。

---

## 相關文件

- [`docs/architecture-and-design.md`](../architecture-and-design.md) — 完整架構設計
- [`docs/getting-started/for-platform-engineers.md`](../getting-started/for-platform-engineers.md) — 平台工程師快速入門
- [`CLAUDE.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md) — 開發上下文指引

