---
title: "架構決策記錄 (ADR)"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
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
- **千租戶 Scale / Config 管理**：[010 Multi-Tenant Grouping](./010-multi-tenant-grouping.md) + [016 conf.d/ 目錄分層](./016-conf-d-directory-hierarchy-mixed-mode.md) + [017 繼承引擎 + dual-hash](./017-defaults-yaml-inheritance-dual-hash.md) — 千租戶 config 組織與 hot-reload
- **Frontend 品質治理**：[013 元件健康度 + Token Density](./013-component-health-token-density-metric.md) + [014 Wizard token 遷移](./014-wizard-arbitrary-value-token-migration.md) + [015 data-theme 單軌 dark mode](./015-data-theme-single-track-dark-mode.md)
- **Accessibility 修補**：[012 threshold-heatmap 色盲補丁](./012-colorblind-hotfix-structured-severity-return.md)
- **客戶導入管線**：[018 Profile-as-Directory-Default](./018-profile-as-directory-default.md) — Profile Builder 寫回 conf.d/ 的 default vs override 邊界

## ADR 索引

> 自動產生：`scripts/dx/generate_adr_index.py` 從各 ADR 的 frontmatter 與 `## 狀態` 區塊渲染。新增或修改 ADR 後跑 `make adr-index`；pre-commit 的 `adr-index-check` 會擋過期的表。每份 ADR 的背景、選項與取捨，請直接讀該檔。

<!-- ADR_INDEX_START -->
| ADR | 標題 | 狀態 | 版本 |
|-----|------|------|------|
| ADR-001 | [嚴重度 Dedup 採用 Inhibit 規則](./001-severity-dedup-via-inhibit.md) | ✅ Accepted | v1.0.0 |
| ADR-002 | [OCI Registry 替代 ChartMuseum](./002-oci-registry-over-chartmuseum.md) | ✅ Accepted | v1.12.0 |
| ADR-003 | [Sentinel Alert 模式](./003-sentinel-alert-pattern.md) | ✅ Accepted | v1.0.0 |
| ADR-004 | [Federation 架構——中央 Exporter 優先](./004-federation-central-exporter-first.md) | ✅ Accepted | v1.12.0 |
| ADR-005 | [投影卷掛載 Rule Pack](./005-projected-volume-for-rule-packs.md) | ✅ Accepted | v1.0.0 |
| ADR-006 | [租戶映射拓撲 (1:1, N:1, 1:N)](./006-tenant-mapping-topologies.md) | ✅ Accepted | v2.1.0 |
| ADR-007 | [跨域路由設定檔與域策略](./007-cross-domain-routing-profiles.md) | ✅ Accepted | v2.1.0 |
| ADR-008 | [Operator-Native 整合路徑](./008-operator-native-integration-path.md) | ✅ Accepted | v2.3.0 |
| ADR-009 | [Tenant Manager CRUD API 架構](./009-tenant-manager-crud-api.md) | ✅ Accepted | v2.4.0 |
| ADR-010 | [Multi-Tenant Grouping Architecture](./010-multi-tenant-grouping.md) | ✅ Accepted | v2.5.0 |
| ADR-011 | [PR-based Write-back 模式](./011-pr-based-write-back.md) | ✅ Accepted | v2.6.0 |
| ADR-012 | [threshold-heatmap 色盲補丁 — 結構化 severity 返回值](./012-colorblind-hotfix-structured-severity-return.md) | ✅ Accepted | v2.7.0 |
| ADR-013 | [Component Health Scanner — Tier 評分演算法與 token_density 輔助指標](./013-component-health-token-density-metric.md) | ✅ Accepted | v2.7.0 |
| ADR-014 | [wizard.jsx design token 遷移採 Option A（Tailwind arbitrary value 全改寫）](./014-wizard-arbitrary-value-token-migration.md) | ✅ Accepted | v2.7.0 |
| ADR-015 | [全面改用 `[data-theme]` 單軌 dark mode，移除 Tailwind `dark:` 變體](./015-data-theme-single-track-dark-mode.md) | ✅ Accepted | v2.7.0 |
| ADR-016 | [conf.d/ 目錄分層 + 混合模式 + 遷移策略](./016-conf-d-directory-hierarchy-mixed-mode.md) | ✅ Accepted | v2.7.0 |
| ADR-017 | [_defaults.yaml 繼承語意 + dual-hash hot-reload](./017-defaults-yaml-inheritance-dual-hash.md) | ✅ Accepted | v2.7.0 |
| ADR-018 | [Profile-as-Directory-Default](./018-profile-as-directory-default.md) | 🟢 Accepted | v2.8.0 |
| ADR-019 | [Planning SSOT — Frontmatter Contract + Discovery-based Index](./019-planning-ssot.md) | ✅ Accepted | v2.8.0 |
| ADR-020 | [Tenant Federation — Label-Injection Proxy over Self-Built Endpoint](./020-tenant-federation.md) | ✅ Accepted | v2.8.0 |
| ADR-021 | [Tenant Log Query — Authorization-Plane-Only, Ingestion-Decoupled](./021-tenant-log-query-federation.md) | ✅ Accepted | v2.9.0 |
| ADR-022 | [tenant-api Dev-Auth Bypass — Local-Dev Identity Substitute, Four-Layer Containment](./022-dev-auth-bypass-four-layer-containment.md) | ✅ Accepted | v2.9.0 |
| ADR-023 | [tenant-api 寫入平面 — 單一寫者不變式](./023-write-plane-single-writer-invariant.md) | ✅ Accepted | — |
| ADR-024 | [宣告式 Dimensional 告警引擎 — Version-Aware Thresholds + Custom Alerts](./024-version-aware-threshold-via-dimensional-label.md) | ✅ Accepted | v2.9.0 |
| ADR-025 | [告警平面自我存活性 — 讓告警系統能偵測自己的死亡](./025-alerting-plane-self-liveness.md) | ✅ Accepted | — |
| ADR-026 | [Node/Cluster 維護告警抑制 — 不需要子系統](./026-node-maintenance-liveness-suppression.md) | 🟡 Proposed | — |
| ADR-028 | [Federation 撤銷儲存 tamper-evidence — off-cluster 對帳為主控](./028-federation-revocation-tamper-evidence.md) | ✅ Accepted | — |
| ADR-029 | [租戶自訂告警跨租戶查詢隔離 — 編譯期邊界中和為主、評估期 ruler 隔離延後](./029-custom-alert-cross-tenant-query-scoping.md) | ✅ Accepted | — |
| ADR-030 | [決策層遷移驗證 — 製造 Oracle 而非觀測](./030-decision-layer-migration-validation.md) | 🟢 Accepted | — |
| ADR-031 | [slo_burn_rate recipe — 宣告式 SLO 告警編譯](./031-slo-burn-rate-recipe.md) | ✅ Accepted | — |
| ADR-032 | [夜跑效能監測改用成對交錯量測，取代跨夜滑動錨點](./032-paired-interleaved-bench-measurement.md) | ✅ Accepted | — |
| ADR-033 | [與運維執行平面的協同介面 — MariaDB 計畫性作業](./033-ops-execution-plane-interface.md) | ✅ Accepted | — |
| ADR-034 | [合法值不得同時當作無法辨識時的 fallback](./034-legal-value-as-fallback.md) | ✅ Accepted | — |
| ADR-035 | [tenant id 合法字元集的單一來源](./035-tenant-id-single-source.md) | ✅ Accepted | — |
| ADR-036 | [conf.d 的路由與 policy 只由產生器解析一次，Go 讀取端吃它的輸出](./036-single-parser-effective-config.md) | 🟡 Proposed | — |

<!-- ADR_INDEX_END -->

---

## 相關文件

- [`docs/architecture-and-design.md`](../architecture-and-design.md) — 完整架構設計
- [`docs/getting-started/for-platform-engineers.md`](../getting-started/for-platform-engineers.md) — 平台工程師快速入門
- [`CLAUDE.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md) — 開發上下文指引
