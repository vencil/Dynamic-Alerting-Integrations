---
title: "Architecture Decision Records (ADR)"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: en
---

# Architecture Decision Records (ADR)

> **Language / 語言：** **English (Current)** | [中文](README.md)

This directory contains Architecture Decision Records (ADRs) for the Multi-Tenant Dynamic Alerting platform. Each ADR documents the background, option evaluation, and long-term impact of a specific design decision.

## Quick Guide

New here? Pick based on your needs:

- **Understand core design**: [001 Severity Dedup](./001-severity-dedup-via-inhibit.en.md) + [005 Projected Volume](./005-projected-volume-for-rule-packs.en.md) — two foundations of the rule engine
- **Preparing to deploy**: [008 Operator Integration](./008-operator-native-integration-path.en.md) — ConfigMap vs Operator CRD dual-path
- **Multi-cluster needs**: [004 Federation](./004-federation-central-exporter-first.en.md) + [006 Tenant Mapping](./006-tenant-mapping-topologies.en.md) — Federation architecture and topologies
- **Management plane**: [009 Tenant API](./009-tenant-manager-crud-api.en.md) + [011 PR Write-back](./011-pr-based-write-back.en.md) — UI/API management and compliance workflows
- **Thousand-tenant Scale / Config management**: [010 Multi-Tenant Grouping](./010-multi-tenant-grouping.en.md) + [016 conf.d/ directory hierarchy](./016-conf-d-directory-hierarchy-mixed-mode.en.md) + [017 inheritance engine + dual-hash](./017-defaults-yaml-inheritance-dual-hash.en.md) — thousand-tenant config organization and hot-reload
- **Frontend quality governance**: [013 Component health + Token Density](./013-component-health-token-density-metric.en.md) + [014 Wizard token migration](./014-wizard-arbitrary-value-token-migration.en.md) + [015 data-theme single-track dark mode](./015-data-theme-single-track-dark-mode.en.md)
- **Accessibility patches**: [012 threshold-heatmap colorblind patch](./012-colorblind-hotfix-structured-severity-return.en.md)
- **Customer-migration pipeline**: [018 Profile-as-Directory-Default](./018-profile-as-directory-default.en.md) — Profile Builder's default-vs-override rule when emitting into conf.d/

## ADR Index

> Auto-generated: `scripts/dx/generate_adr_index.py` renders this from each ADR's frontmatter and `## 狀態` (status) block. Run `make adr-index` after adding or changing an ADR; the `adr-index-check` pre-commit hook blocks a stale table. For each ADR's context, options and trade-offs, read the ADR itself. Some ADRs are Chinese-only by language policy; those rows link to the Chinese file.

<!-- ADR_INDEX_START -->
| ADR | Title | Status | Version |
|-----|------|------|------|
| ADR-001 | [Severity Dedup via Inhibit Rules](./001-severity-dedup-via-inhibit.en.md) | ✅ Accepted | v1.0.0 |
| ADR-002 | [OCI Registry over ChartMuseum](./002-oci-registry-over-chartmuseum.en.md) | ✅ Accepted | v1.12.0 |
| ADR-003 | [Sentinel Alert Pattern](./003-sentinel-alert-pattern.en.md) | ✅ Accepted | v1.0.0 |
| ADR-004 | [Federation Architecture — Central Exporter First](./004-federation-central-exporter-first.en.md) | ✅ Accepted | v1.12.0 |
| ADR-005 | [Projected Volume for Rule Packs](./005-projected-volume-for-rule-packs.en.md) | ✅ Accepted | v1.0.0 |
| ADR-006 | [Tenant Mapping Topologies (1:1, N:1, 1:N)](./006-tenant-mapping-topologies.en.md) | ✅ Accepted | v2.1.0 |
| ADR-007 | [Cross-Domain Routing Profiles and Domain Policies](./007-cross-domain-routing-profiles.en.md) | ✅ Accepted | v2.1.0 |
| ADR-008 | [Operator-Native Integration Path](./008-operator-native-integration-path.en.md) | ✅ Accepted | v2.3.0 |
| ADR-009 | [Tenant Manager CRUD API Architecture](./009-tenant-manager-crud-api.en.md) | ✅ Accepted | v2.4.0 |
| ADR-010 | [Multi-Tenant Grouping Architecture](./010-multi-tenant-grouping.en.md) | ✅ Accepted | v2.5.0 |
| ADR-011 | [PR-based Write-back Mode](./011-pr-based-write-back.en.md) | ✅ Accepted | v2.6.0 |
| ADR-012 | [threshold-heatmap Colorblind Accessibility Hotfix — Structured Severity Return Value](./012-colorblind-hotfix-structured-severity-return.en.md) | ✅ Accepted | v2.7.0 |
| ADR-013 | [Component Health Scanner — Tier Scoring Algorithm and token_density Auxiliary Metric](./013-component-health-token-density-metric.en.md) | ✅ Accepted | v2.7.0 |
| ADR-014 | [wizard.jsx design token migration adopts Option A (full Tailwind arbitrary value rewrite)](./014-wizard-arbitrary-value-token-migration.en.md) | ✅ Accepted | v2.7.0 |
| ADR-015 | [Migrate comprehensively to `[data-theme]` single-track dark mode, remove Tailwind `dark:` variant](./015-data-theme-single-track-dark-mode.en.md) | ✅ Accepted | v2.7.0 |
| ADR-016 | [conf.d/ Directory Hierarchy + Mixed Mode + Migration Strategy](./016-conf-d-directory-hierarchy-mixed-mode.en.md) | ✅ Accepted | v2.7.0 |
| ADR-017 | [_defaults.yaml Inheritance Semantics + Dual-Hash Hot-Reload](./017-defaults-yaml-inheritance-dual-hash.en.md) | ✅ Accepted | v2.7.0 |
| ADR-018 | [Profile-as-Directory-Default](./018-profile-as-directory-default.en.md) | 🟢 Accepted | v2.8.0 |
| ADR-019 | [Planning SSOT — Frontmatter Contract + Discovery-based Index](./019-planning-ssot.md) | ✅ Accepted | v2.8.0 |
| ADR-020 | [Tenant Federation — Label-Injection Proxy over Self-Built Endpoint](./020-tenant-federation.md) | ✅ Accepted | v2.8.0 |
| ADR-021 | [Tenant Log Query — Authorization-Plane-Only, Ingestion-Decoupled](./021-tenant-log-query-federation.md) | ✅ Accepted | v2.9.0 |
| ADR-022 | [tenant-api Dev-Auth Bypass — Local-Dev Identity Substitute, Four-Layer Containment](./022-dev-auth-bypass-four-layer-containment.md) | ✅ Accepted | v2.9.0 |
| ADR-023 | [tenant-api 寫入平面 — 單一寫者不變式](./023-write-plane-single-writer-invariant.md) | ✅ Accepted | — |
| ADR-024 | [Declarative Dimensional Alerting Engine — Version-Aware Thresholds + Custom Alerts](./024-version-aware-threshold-via-dimensional-label.en.md) | ✅ Accepted | v2.9.0 |
| ADR-025 | [Alerting-Plane Self-Liveness — Detecting When the Alerting System Itself Dies](./025-alerting-plane-self-liveness.en.md) | ✅ Accepted | — |
| ADR-026 | [Node/Cluster 維護告警抑制 — 不需要子系統](./026-node-maintenance-liveness-suppression.md) | 🟡 Proposed | — |
| ADR-028 | [Federation 撤銷儲存 tamper-evidence — off-cluster 對帳為主控](./028-federation-revocation-tamper-evidence.md) | ✅ Accepted | — |
| ADR-029 | [租戶自訂告警跨租戶查詢隔離 — 編譯期邊界中和為主、評估期 ruler 隔離延後](./029-custom-alert-cross-tenant-query-scoping.md) | ✅ Accepted | — |
| ADR-030 | [決策層遷移驗證 — 製造 Oracle 而非觀測](./030-decision-layer-migration-validation.md) | 🟢 Accepted | — |
| ADR-031 | [slo_burn_rate recipe — 宣告式 SLO 告警編譯](./031-slo-burn-rate-recipe.md) | ✅ Accepted | — |
| ADR-032 | [夜跑效能監測改用成對交錯量測，取代跨夜滑動錨點](./032-paired-interleaved-bench-measurement.md) | ✅ Accepted | — |
| ADR-033 | [與運維執行平面的協同介面 — MariaDB 計畫性作業](./033-ops-execution-plane-interface.md) | ✅ Accepted | — |
| ADR-034 | [合法值不得同時當作無法辨識時的 fallback](./034-legal-value-as-fallback.md) | ✅ Accepted | — |
| ADR-035 | [Single Source for the Legal Tenant-ID Character Set](./035-tenant-id-single-source.en.md) | ✅ Accepted | — |

<!-- ADR_INDEX_END -->

---

## Related Documents

- [`docs/architecture-and-design.en.md`](../architecture-and-design.en.md) — Complete architecture design
- [`docs/getting-started/for-platform-engineers.en.md`](../getting-started/for-platform-engineers.en.md) — Platform engineer quick start guide
- [`CLAUDE.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md) — AI development context guide
