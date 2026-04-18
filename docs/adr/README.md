---
title: "架構決策記錄 (ADR)"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.7.0
lang: zh
---

# 架構決策記錄 (ADR)

> **Language / 語言：** **中文 (Current)** | [English](./README.en.md)

本目錄收錄 Multi-Tenant Dynamic Alerting 平台的架構決策記錄 (Architecture Decision Records)。每份 ADR 記錄特定設計決策的背景、選項評估與長期影響。

## 快速導讀

初次接觸？依你的需求選讀：

- **理解核心設計**：[001 Severity Dedup](./001-severity-dedup-via-inhibit.md) + [005 Projected Volume](./005-projected-volume-for-rule-packs.md) — 掌握規則引擎的兩個基石
- **準備部署**：[008 Operator 整合路徑](./008-operator-native-integration-path.md) — ConfigMap vs Operator CRD 雙路徑選擇
- **多叢集需求**：[004 Federation](./004-federation-central-exporter-first.md) + [006 租戶映射](./006-tenant-mapping-topologies.md) — Federation 架構與拓撲
- **管理平面**：[009 Tenant API](./009-tenant-manager-crud-api.md) + [011 PR Write-back](./011-pr-based-write-back.md) — UI/API 管理與合規流程
- **Scale / Config 管理（v2.7.0）**：[010 Multi-Tenant Grouping](./010-multi-tenant-grouping.md) + [017 conf.d/ 目錄分層](./017-conf-d-directory-hierarchy-mixed-mode.md) + [018 繼承引擎 + dual-hash](./018-defaults-yaml-inheritance-dual-hash.md) — 千租戶 config 組織與 hot-reload
- **Frontend 品質治理（v2.7.0）**：[013 元件健康度 + Token Density](./013-component-health-token-density-metric.md) + [014 TECH-DEBT 預算隔離](./014-tech-debt-category-budget-isolation.md) + [015 Wizard token 遷移](./015-wizard-arbitrary-value-token-migration.md) + [016 data-theme 單軌 dark mode](./016-data-theme-single-track-dark-mode.md)
- **Accessibility hotfix（v2.7.0）**：[012 threshold-heatmap 色盲補丁](./012-colorblind-hotfix-structured-severity-return.md)

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
| [012](#012-threshold-heatmap-色盲補丁) | threshold-heatmap 色盲補丁 — 結構化 severity 返回值 | ✅ Accepted | 修正 WCAG 1.4.1 違反：以 `{severity, color, ariaLabel}` 取代僅色彩輸出，支援色盲可讀性 |
| [013](#013-元件健康度與-token-density-指標) | 元件健康度與 Token Density 指標 | ✅ Accepted | 以 5 維度加權（LOC+Audience+Phase+Writer+Recency）評分並自動分 Tier 1/2/3；引入 `token_density` metric 量化 token 遷移進度 |
| [014](#014-tech-debt-vs-regression-預算隔離) | TECH-DEBT vs Regression 預算隔離 | ✅ Accepted | 將技術債與使用者可見退化分為兩組 budget，避免 TECH-DEBT 侵蝕 REG budget；LL 跨 2 minor 版本必須三選一（固化/自動化/歸檔） |
| [015](#015-wizard-token-arbitrary-value-遷移策略) | Wizard Token Arbitrary-Value 遷移策略 (Option A) | ✅ Accepted | `bg-[color:var(--da-color-*)]` arbitrary-value 改寫 legacy `bg-slate-200`，避免 Tailwind config 擴充 + 同 commit 完成全替換 |
| [016](#016-data-theme-單軌-dark-mode) | `[data-theme]` 單軌 Dark Mode（移除 `dark:` 變體） | ✅ Accepted | 統一以 `[data-theme="dark"]` attribute 管理 dark mode，禁用 Tailwind `dark:` 變體，消除 token/class 雙軌問題 |
| [017](#017-confd-目錄分層-混合模式) | conf.d/ 目錄分層 + 混合模式 + 遷移策略 | 🟡 Proposed | Directory Scanner 同時支援 flat 與 domain/region/env 3 層結構；零中斷升級 + `migrate-conf-d` 可選工具 |
| [018](#018-defaultsyaml-繼承語意-dual-hash-hot-reload) | `_defaults.yaml` 繼承語意 + dual-hash hot-reload | 🟡 Proposed | Deep merge with override（array replace、null-as-delete）+ 雙 hash（source_hash + merged_hash）精準判定 reload 觸發，配 300ms debounce |

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

## 012: threshold-heatmap 色盲補丁

**文件**: [`012-colorblind-hotfix-structured-severity-return.md`](./012-colorblind-hotfix-structured-severity-return.md)

修正 v2.6.0 `threshold-heatmap.jsx` 僅以顏色傳遞 severity 的 WCAG 1.4.1 違反。`getSeverityColorClass()` 改為 `getSeverityInfo()` 回傳 `{severity, color, ariaLabel}` 結構；cell 額外以 `aria-label` 與 icon 雙重呈現，色盲使用者可辨識。Runtime WCAG 驗證收束至 CI。

---

## 013: 元件健康度與 Token Density 指標

**文件**: [`013-component-health-token-density-metric.md`](./013-component-health-token-density-metric.md)

v2.7.0 Phase .a 新基線：以 5 維度加權（LOC 0-3 + Audience 0-2 + Phase 0-2 + Writer 0-2 + Recency -1~+1）評分，自動分 Tier 1/2/3。引入 `token_density = tokens / (tokens + palette_hits)` 指標量化 JSX 工具的 design token 遷移進度（Group A/B/C）。合併自早期 DEC-08 與 DEC-M 兩項規劃決策。

---

## 014: TECH-DEBT vs Regression 預算隔離

**文件**: [`014-tech-debt-category-budget-isolation.md`](./014-tech-debt-category-budget-isolation.md)

在 v2.6.x 的 Regression Budget（P2/P3 修復 ≤ 當版工時 15%）上新增「TECH-DEBT」分類，與 REG 獨立 budget（4%），避免技術債佔用面向使用者的退化修復時間。LL 跨越 2 個 minor 版本強制三選一：固化規範、🛡️ 自動化、或歸檔 `archive/`。為 Playbook 知識退火提供機制保障。

---

## 015: Wizard Token Arbitrary-Value 遷移策略

**文件**: [`015-wizard-arbitrary-value-token-migration.md`](./015-wizard-arbitrary-value-token-migration.md)

v2.7.0 Phase .a0 將 `deployment-wizard.jsx` 從 legacy `bg-slate-200 / text-gray-700` palette 遷至 design tokens：選用 **Option A** — `bg-[color:var(--da-color-*)]` arbitrary-value 改寫，而非擴充 `tailwind.config`。保留 Tailwind utility 書寫風格 + token SSOT；後續 rbac / cicd / threshold-heatmap batch 4 沿用同規則。

---

## 016: `[data-theme]` 單軌 Dark Mode

**文件**: [`016-data-theme-single-track-dark-mode.md`](./016-data-theme-single-track-dark-mode.md)

全面移除 Tailwind `dark:` 變體，統一以 `[data-theme="dark"]` attribute 管理 dark mode。此前 class-based 與 attribute-based 雙軌並存造成 tooltip/palette 配色錯位與維護雙成本。`jsx-loader` 改為設定 `data-theme` 而非 toggle `class="dark"`；`tailwind.config.darkMode` 移除。為 Phase .a0 所有後續 token 遷移的前提。

---

## 017: conf.d/ 目錄分層 + 混合模式

**文件**: [`017-conf-d-directory-hierarchy-mixed-mode.md`](./017-conf-d-directory-hierarchy-mixed-mode.md)

v2.7.0 Phase .b B-1。Directory Scanner 同時支援 flat 與 `{domain}/{region}/{env}/` 三層結構，**不強制遷移**。目錄路徑可推斷 `_metadata.domain/region/environment` 預設值；檔案內明確設定欄位時 override。`migrate-conf-d` 工具為可選、支援 `--dry-run` + `git mv` 保留歷史。解決 200+ tenant 的可讀性與 blast radius 盲點。

---

## 018: `_defaults.yaml` 繼承語意 + dual-hash hot-reload

**文件**: [`018-defaults-yaml-inheritance-dual-hash.md`](./018-defaults-yaml-inheritance-dual-hash.md)

v2.7.0 Phase .b B-1。定義多層 `_defaults.yaml` 的繼承語意（L0 全域 → L1 domain → L2 region → L3 env → tenant），deep merge with override（array replace、null-as-delete、`_metadata` 不繼承）。雙 hash：`source_hash`（tenant YAML 檔案本身）+ `merged_hash`（effective config canonical JSON）精準判定 reload 觸發，避免 `_defaults.yaml` 變動時的 reload 風暴；300ms debounce 處理 batch git pull。

---

## 相關文件

- [`docs/architecture-and-design.md`](../architecture-and-design.md) — 完整架構設計
- [`docs/getting-started/for-platform-engineers.md`](../getting-started/for-platform-engineers.md) — 平台工程師快速入門
- [`CLAUDE.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md) — 開發上下文指引
