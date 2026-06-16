---
title: "文件導覽 (Documentation Map)"
tags: [documentation, navigation, internal]
audience: [maintainers, ai-agent]
version: v2.9.0
lang: zh
---

# 文件導覽 (Documentation Map)

> 本表由 `generate_doc_map.py --generate` 自動產生，供 AI Agent 與開發者快速查找文件位置。

| 文件 | 受眾 | 內容 |
|------|------|------|
| `docs/adr/001-severity-dedup-via-inhibit.md` (.en.md) | Platform Engineers | ADR-001: 嚴重度 Dedup 採用 Inhibit 規則 |
| `docs/adr/002-oci-registry-over-chartmuseum.md` (.en.md) | Platform Engineers | ADR-002: OCI Registry 替代 ChartMuseum |
| `docs/adr/003-sentinel-alert-pattern.md` (.en.md) | Platform Engineers | ADR-003: Sentinel Alert 模式 |
| `docs/adr/004-federation-central-exporter-first.md` (.en.md) | Platform Engineers | ADR-004: Federation 架構——中央 Exporter 優先 |
| `docs/adr/005-projected-volume-for-rule-packs.md` (.en.md) | Platform Engineers | ADR-005: 投影卷掛載 Rule Pack |
| `docs/adr/006-tenant-mapping-topologies.md` (.en.md) | Platform Engineers | ADR-006: 租戶映射拓撲 (1:1, N:1, 1:N) |
| `docs/adr/007-cross-domain-routing-profiles.md` (.en.md) | Platform Engineers | ADR-007: 跨域路由設定檔與域策略 |
| `docs/adr/008-operator-native-integration-path.md` (.en.md) | Platform Engineers | ADR-008: Operator-Native 整合路徑 |
| `docs/adr/009-tenant-manager-crud-api.md` (.en.md) | Platform Engineers, developers | ADR-009: Tenant Manager CRUD API 架構 |
| `docs/adr/010-multi-tenant-grouping.md` (.en.md) | Platform Engineers, developers | ADR-010: Multi-Tenant Grouping Architecture |
| `docs/adr/011-pr-based-write-back.md` (.en.md) | Platform Engineers, developers | ADR-011: PR-based Write-back 模式 |
| `docs/adr/012-colorblind-hotfix-structured-severity-return.md` (.en.md) | frontend-developers, design-system-maintainers | ADR-012: threshold-heatmap 色盲補丁 — 結構化 severity 返回值 |
| `docs/adr/013-component-health-token-density-metric.md` (.en.md) | frontend-developers, Platform Engineers, maintainers | ADR-013: Component Health Scanner — Tier 評分演算法與 token_density 輔助指標 |
| `docs/adr/014-wizard-arbitrary-value-token-migration.md` (.en.md) | frontend-developers, maintainers | ADR-014: wizard.jsx design token 遷移採 Option A（Tailwind arbitrary value 全改寫） |
| `docs/adr/015-data-theme-single-track-dark-mode.md` (.en.md) | frontend-developers, designers, maintainers | ADR-015: 全面改用 `[data-theme]` 單軌 dark mode，移除 Tailwind `dark:` 變體 |
| `docs/adr/016-conf-d-directory-hierarchy-mixed-mode.md` (.en.md) | Platform Engineers, SREs, contributors | ADR-016: conf.d/ 目錄分層 + 混合模式 + 遷移策略 |
| `docs/adr/017-defaults-yaml-inheritance-dual-hash.md` (.en.md) | Platform Engineers, SREs, contributors | ADR-017: _defaults.yaml 繼承語意 + dual-hash hot-reload |
| `docs/adr/018-profile-as-directory-default.md` (.en.md) | Platform Engineers, SREs, contributors | ADR-018: Profile-as-Directory-Default |
| `docs/adr/019-planning-ssot.md` | Platform Engineers, contributors, ai-agents | ADR-019: Planning SSOT — Frontmatter Contract + Discovery-based Index |
| `docs/adr/020-tenant-federation.md` | Platform Engineers, contributors | ADR-020: Tenant Federation — Label-Injection Proxy over Self-Built Endpoint |
| `docs/adr/021-tenant-log-query-federation.md` | Platform Engineers, contributors | ADR-021: Tenant Log Query — Authorization-Plane-Only, Ingestion-Decoupled |
| `docs/adr/022-dev-auth-bypass-four-layer-containment.md` | Platform Engineers, contributors | ADR-022: tenant-api Dev-Auth Bypass — Local-Dev Identity Substitute, Four-Layer Containment |
| `docs/adr/023-write-plane-single-writer-invariant.md` | Platform Engineers, contributors | ADR-023: tenant-api 寫入平面 — 單一寫者不變式 |
| `docs/adr/024-version-aware-threshold-via-dimensional-label.md` (.en.md) | Platform Engineers, contributors, SREs | ADR-024: 宣告式 Dimensional 告警引擎 — Version-Aware Thresholds + Custom Alerts |
| `docs/adr/025-alerting-plane-self-liveness.md` (.en.md) | Platform Engineers, SREs, contributors | ADR-025: 告警平面自我存活性 — 讓告警系統能偵測自己的死亡 |
| `docs/api/README.md` (.en.md) | Platform Engineers, SREs | Threshold Exporter API Reference |
| `docs/api/tenant-api-hardening.md` (.en.md) | platform-ops, sre, security | Tenant API Hardening (v2.8.0) |
| `docs/architecture-and-design.md` (.en.md) | Platform Engineers, SREs, decision-maker | 架構與設計 — 動態多租戶警報平台技術白皮書 |
| `docs/benchmarks.md` (.en.md) | Platform Engineers, SREs, decision-maker | 性能基準 (Performance Benchmarks) |
| `docs/cheat-sheet.md` (.en.md) | All | da-tools Quick Reference |
| `docs/cli-reference.md` (.en.md) | Platform Engineers, SREs, DevOps, Tenants | da-tools CLI Reference |
| `docs/custom-rule-governance.md` (.en.md) | Platform Engineers | 多租戶客製化規則治理規範 (Custom Rule Governance Model) |
| `docs/design/config-driven.md` (.en.md) | Platform Engineers, DevOps | Config-Driven 架構設計 — 三態配置、動態路由、Tenant API |
| `docs/design/high-availability.md` (.en.md) | Platform Engineers, DevOps | 高可用性 (HA) 設計 — 副本、PDB、防雙倍計算 |
| `docs/design/README.md` (.en.md) | Platform Engineers, DevOps | 設計深潛導覽 — 架構 spoke 文件 |
| `docs/design/roadmap-future.md` (.en.md) | Platform Engineers, DevOps | 未來擴展路線 — K8s Operator、Design System、Auto-Discovery 等 |
| `docs/design/rule-packs.md` (.en.md) | Platform Engineers, DevOps | Rule Packs 與 Projected Volume 架構 |
| `docs/design/runtime-canary.md` (.en.md) | Platform Engineers, SREs | Runtime Canary 設計 — 自訂告警編譯管線的端到端活性保證 |
| `docs/getting-started/decision-matrix.md` (.en.md) | Platform Engineers | Deployment Decision Matrix |
| `docs/getting-started/for-decision-makers.md` (.en.md) | decision-maker | 決策者 / 主管評估指南 |
| `docs/getting-started/for-domain-experts.md` (.en.md) | Domain Experts (DBA) | Domain Expert (DBA) 快速入門指南 |
| `docs/getting-started/for-platform-engineers.md` (.en.md) | Platform Engineers | Platform Engineer 快速入門指南 |
| `docs/getting-started/for-tenants.md` (.en.md) | Tenants | Tenant 快速入門指南 |
| `docs/getting-started/README.md` | All | 快速入門 — 角色導引 |
| `docs/glossary.md` (.en.md) | All | 術語表 |
| `docs/governance-security.md` (.en.md) | Platform Engineers, 安全合規 | 治理、稽核與安全合規 |
| `docs/grafana-dashboards.md` (.en.md) | Platform Engineers, SREs, DevOps | Grafana Dashboard 導覽 |
| `docs/index.md` (.en.md) | All | Dynamic Alerting Platform — 首頁 |
| `docs/integration/alerting-plane-self-liveness.md` (.en.md) | Platform Engineers | 告警平面自我存活性 — Watchdog + 外部 Dead-Man's-Switch（Operator 指南） |
| `docs/integration/byo-alertmanager-integration.md` (.en.md) | Platform Engineers, SREs | BYO Alertmanager 整合指南 |
| `docs/integration/byo-prometheus-integration.md` (.en.md) | Platform Engineers, SREs | Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南 |
| `docs/integration/deployment-sizing.md` (.en.md) | Platform Engineers, SREs, DevOps | 部署容量規劃指南 |
| `docs/integration/federation-integration.md` (.en.md) | Platform Engineers | Federation Integration Guide |
| `docs/integration/gitops-deployment.md` (.en.md) | Platform Engineers, DevOps | GitOps 部署指南 |
| `docs/integration/operator-alertmanager-integration.md` (.en.md) | Platform Engineers | Operator Alertmanager 整合指南 |
| `docs/integration/operator-gitops-deployment.md` (.en.md) | Platform Engineers | Operator GitOps 部署指南 |
| `docs/integration/operator-prometheus-integration.md` (.en.md) | Platform Engineers | Operator Prometheus 整合指南 |
| `docs/integration/operator-shadow-monitoring.md` (.en.md) | Platform Engineers | Operator Shadow Monitoring 策略 |
| `docs/integration/prometheus-operator-integration.md` (.en.md) | Platform Engineers | Prometheus Operator 整合手冊（Hub） |
| `docs/integration/README.md` (.en.md) | Platform Engineers, SREs, DevOps | 整合指南導覽 — 依你現有的監控架構選讀 |
| `docs/integration/synthetic-probe-interop.md` (.en.md) | Platform Engineers, SREs | 合成探測對接 (Synthetic-Probe Interop) — 用你現有的探測器驗證端到端投遞 |
| `docs/integration/tenant-federation.md` | Platform Engineers, SREs | Tenant Federation Integration Guide |
| `docs/integration/troubleshooting-checklist.md` (.en.md) | SREs, on-call, Platform Engineers, migration-engineers | Troubleshooting Checklist |
| `docs/integration/victoriametrics-integration.md` (.en.md) | Platform Engineers, SREs, vm-operators | VictoriaMetrics 整合指南 |
| `docs/interactive-tools.md` (.en.md) | All | 互動式工具 |
| `docs/migration-engine.md` (.en.md) | Platform Engineers, DevOps | AST 遷移引擎架構 |
| `docs/migration-guide.md` (.en.md) | Tenants, DevOps, Platform Engineers, SREs | Migration Guide — 遷移指南 |
| `docs/migration-toolkit-installation.md` (.en.md) | Platform Engineers, SREs, customer-ops | Migration Toolkit 安裝指南（da-tools / da-guard） |
| `docs/scenarios/alert-routing-split.md` (.en.md) | Platform Engineers | 場景：同一 Alert、不同語義 — Platform/NOC vs Tenant 雙視角通知 |
| `docs/scenarios/flat-to-conf-d-cutover-decision.md` (.en.md) | platform-ops, sre | Flat → conf.d/ Cutover Decision Guide |
| `docs/scenarios/gitops-ci-integration.md` (.en.md) | Platform Engineers | 場景：GitOps CI/CD 整合指南 |
| `docs/scenarios/hands-on-lab.md` (.en.md) | Platform Engineers, Tenants | 動手實驗：從零到生產告警 |
| `docs/scenarios/incremental-migration-playbook.md` (.en.md) | Platform Engineers, SREs | 場景：漸進式遷移 Playbook |
| `docs/scenarios/manage-at-scale.md` (.en.md) | Platform Engineers, operator, DevOps | 場景：千租戶規模管理 |
| `docs/scenarios/multi-cluster-federation.md` (.en.md) | Platform Engineers | 場景：多叢集聯邦架構 — 中央閾值 + 邊緣指標 |
| `docs/scenarios/multi-domain-conf-layout.md` (.en.md) | Platform Engineers, operator, DevOps | 場景：多域名階層式配置 — conf.d/ 目錄結構重構（v2.7.0） |
| `docs/scenarios/multi-system-migration-playbook.md` (.en.md) | Platform Engineers, SREs, architects | Multi-System Migration Playbook |
| `docs/scenarios/README.md` | All | 場景指南導覽 |
| `docs/scenarios/shadow-audit.md` (.en.md) | Platform Engineers, Tenants | 場景：Shadow Audit — 告警品質評估 |
| `docs/scenarios/shadow-monitoring-cutover.md` (.en.md) | Platform Engineers, SREs, DevOps, Tenants | 場景：Shadow Monitoring — 從告警健康評估到全自動切換 |
| `docs/scenarios/staged-adoption-guide.md` (.en.md) | Platform Engineers, SREs, tenant-admins | Staged Rule Adoption Lifecycle |
| `docs/scenarios/tenant-lifecycle.md` (.en.md) | All | 場景：租戶完整生命週期管理 |
| `docs/scenarios/verified-scenarios.md` (.en.md) | Platform Engineers, SREs, decision-maker | 驗證場景與平台行為 (Verified Scenarios) |
| `docs/scenarios/version-aware-thresholds.md` (.en.md) | tenant-admins, Platform Engineers, SREs | Version-Aware Thresholds — 版本感知閾值使用攻略 |
| `docs/schemas/migration-state.md` | Platform Engineers, SREs, automation | Migration State Schema (.da/migration-state.json) |
| `docs/schemas/README.md` | Platform Engineers, Tenants | JSON Schema Reference |
| `docs/shadow-monitoring-sop.md` (.en.md) | SREs, Platform Engineers | Shadow Monitoring SRE SOP |
| `docs/troubleshooting.md` (.en.md) | Platform Engineers, SREs, Tenants | 故障排查與邊界情況 |
| `docs/vcs-integration-guide.md` | Platform Engineers | VCS 整合指南 — GitHub / GitLab / 自託管實例 |
| `docs/internal/doc-map.md` | AI Agent | 本文件（文件導覽總表） |
| `docs/internal/tool-map.md` | AI Agent | 工具導覽（自動生成） |
| `docs/schemas/tenant-config.schema.json` | All | Tenant YAML JSON Schema（VS Code 自動補全） |
| `rule-packs/README.md` | All | 15 Rule Packs + optional 卸載 |
| `rule-packs/ALERT-REFERENCE.md (.en.md)` | Tenants, SREs | 96 個 Alert 含義 + 建議動作速查 |
| `k8s/03-monitoring/dynamic-alerting-overview.json` | SRE | Grafana Dashboard |
