---
title: "文件導覽 (Documentation Map)"
tags: [documentation, navigation, internal]
audience: [maintainers, ai-agent]
version: v2.7.0
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
| `docs/adr/014-tech-debt-category-budget-isolation.md` (.en.md) | Platform Engineers, tech-leads | ADR-014: TECH-DEBT 類別與 REG Budget 隔離 |
| `docs/adr/015-wizard-arbitrary-value-token-migration.md` (.en.md) | frontend-developers, maintainers | ADR-015: wizard.jsx design token 遷移採 Option A（Tailwind arbitrary value 全改寫） |
| `docs/adr/016-data-theme-single-track-dark-mode.md` (.en.md) | frontend-developers, designers, maintainers | ADR-016: 全面改用 `[data-theme]` 單軌 dark mode，移除 Tailwind `dark:` 變體 |
| `docs/adr/017-conf-d-directory-hierarchy-mixed-mode.md` (.en.md) | Platform Engineers, SREs, contributors | ADR-017: conf.d/ 目錄分層 + 混合模式 + 遷移策略 |
| `docs/adr/018-defaults-yaml-inheritance-dual-hash.md` (.en.md) | Platform Engineers, SREs, contributors | ADR-018: _defaults.yaml 繼承語意 + dual-hash hot-reload |
| `docs/api/README.md` (.en.md) | Platform Engineers, SREs | Threshold Exporter API Reference |
| `docs/architecture-and-design.md` (.en.md) | Platform Engineers | 架構與設計 — 動態多租戶警報平台技術白皮書 |
| `docs/benchmarks.md` (.en.md) | Platform Engineers, SREs | 性能分析與基準測試 (Performance Analysis & Benchmarks) |
| `docs/cheat-sheet.md` (.en.md) | All | da-tools Quick Reference |
| `docs/cli-reference.md` (.en.md) | Platform Engineers, SREs, DevOps, Tenants | da-tools CLI Reference |
| `docs/custom-rule-governance.md` (.en.md) | Platform Engineers | 多租戶客製化規則治理規範 (Custom Rule Governance Model) |
| `docs/design/config-driven.md` (.en.md) | Platform Engineers, DevOps | Config-Driven 架構設計 — 三態配置、動態路由、Tenant API |
| `docs/design/high-availability.md` (.en.md) | Platform Engineers, DevOps | 高可用性 (HA) 設計 — 副本、PDB、防雙倍計算 |
| `docs/design/roadmap-future.md` (.en.md) | Platform Engineers, DevOps | 未來擴展路線 — K8s Operator、Design System、Auto-Discovery 等 |
| `docs/design/rule-packs.md` (.en.md) | Platform Engineers, DevOps | Rule Packs 與 Projected Volume 架構 |
| `docs/getting-started/decision-matrix.md` (.en.md) | Platform Engineers | Deployment Decision Matrix |
| `docs/getting-started/for-domain-experts.md` (.en.md) | Domain Experts (DBA) | Domain Expert (DBA) 快速入門指南 |
| `docs/getting-started/for-platform-engineers.md` (.en.md) | Platform Engineers | Platform Engineer 快速入門指南 |
| `docs/getting-started/for-tenants.md` (.en.md) | Tenants | Tenant 快速入門指南 |
| `docs/getting-started/README.md` | All | 快速入門 — 角色導引 |
| `docs/getting-started/wizard.jsx` | Tenants, Platform Engineers, Domain Experts (DBA) | Getting Started Wizard |
| `docs/glossary.md` (.en.md) | All | 術語表 |
| `docs/governance-security.md` (.en.md) | Platform Engineers, 安全合規 | 治理、稽核與安全合規 |
| `docs/grafana-dashboards.md` (.en.md) | Platform Engineers, SREs, DevOps | Grafana Dashboard 導覽 |
| `docs/index.md` (.en.md) | All | Dynamic Alerting Platform — 首頁 |
| `docs/integration/byo-alertmanager-integration.md` (.en.md) | Platform Engineers, SREs | BYO Alertmanager 整合指南 |
| `docs/integration/byo-prometheus-integration.md` (.en.md) | Platform Engineers, SREs | Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南 |
| `docs/integration/federation-integration.md` (.en.md) | Platform Engineers | Federation Integration Guide |
| `docs/integration/gitops-deployment.md` (.en.md) | Platform Engineers, DevOps | GitOps 部署指南 |
| `docs/integration/operator-alertmanager-integration.md` (.en.md) | Platform Engineers | Operator Alertmanager 整合指南 |
| `docs/integration/operator-gitops-deployment.md` (.en.md) | Platform Engineers | Operator GitOps 部署指南 |
| `docs/integration/operator-prometheus-integration.md` (.en.md) | Platform Engineers | Operator Prometheus 整合指南 |
| `docs/integration/operator-shadow-monitoring.md` (.en.md) | Platform Engineers | Operator Shadow Monitoring 策略 |
| `docs/integration/prometheus-operator-integration.md` (.en.md) | Platform Engineers | Prometheus Operator 整合手冊（Hub） |
| `docs/interactive/tools/alert-noise-analyzer.jsx` | platform, Domain Experts (DBA) | Alert Noise Analyzer |
| `docs/interactive/tools/alert-simulator.jsx` | Domain Experts (DBA), Tenants | Alert Simulator |
| `docs/interactive/tools/alert-timeline.jsx` | Domain Experts (DBA), Tenants | Alert Timeline Replay |
| `docs/interactive/tools/AlertPreviewTab.jsx` | Platform Engineers, Tenants | Alert Preview Tab |
| `docs/interactive/tools/architecture-quiz.jsx` | Platform Engineers | Architecture Decision Quiz |
| `docs/interactive/tools/capacity-planner.jsx` | Platform Engineers | Capacity Planner |
| `docs/interactive/tools/cicd-setup-wizard.jsx` | Platform Engineers | CI/CD Setup Wizard |
| `docs/interactive/tools/cli-playground.jsx` | Platform Engineers | da-tools CLI Playground |
| `docs/interactive/tools/component-health.jsx` | Platform Engineers, Contributors | Component Health Dashboard |
| `docs/interactive/tools/config-diff.jsx` | Platform Engineers | Config Version Diff |
| `docs/interactive/tools/config-lint.jsx` | Platform Engineers, Tenants | Config Lint Report |
| `docs/interactive/tools/cost-estimator.jsx` | Platform Engineers, SREs, management | Cost Estimator |
| `docs/interactive/tools/dependency-graph.jsx` | Platform Engineers, Domain Experts (DBA) | Dependency Graph |
| `docs/interactive/tools/deployment-wizard.jsx` | Platform Engineers, SREs, DevOps | Deployment Profile Wizard |
| `docs/interactive/tools/glossary.jsx` | Platform Engineers, Domain Experts (DBA), Tenants | Interactive Glossary |
| `docs/interactive/tools/health-dashboard.jsx` | Tenants, Platform Engineers | Tenant Health Dashboard |
| `docs/interactive/tools/migration-roi-calculator.jsx` | Platform Engineers, SREs | Migration ROI Calculator |
| `docs/interactive/tools/migration-simulator.jsx` | Platform Engineers | Migration Dry-Run Simulator |
| `docs/interactive/tools/multi-tenant-comparison.jsx` | platform, Domain Experts (DBA) | Multi-Tenant Comparison |
| `docs/interactive/tools/notification-previewer.jsx` | Platform Engineers, Tenants | Notification Template Editor |
| `docs/interactive/tools/onboarding-checklist.jsx` | Tenants, Platform Engineers, Domain Experts (DBA) | Onboarding Checklist Generator |
| `docs/interactive/tools/operator-setup-wizard.jsx` | Platform Engineers, SREs, DevOps | Operator Setup Wizard |
| `docs/interactive/tools/platform-demo.jsx` | Platform Engineers, Domain Experts (DBA), Tenants | Platform Demo |
| `docs/interactive/tools/platform-health.jsx` | Platform Engineers | Platform Health Dashboard |
| `docs/interactive/tools/playground.jsx` | Platform Engineers, Tenants | YAML Playground |
| `docs/interactive/tools/portal-shared.jsx` | Platform Engineers | Portal Shared Module |
| `docs/interactive/tools/promql-tester.jsx` | Platform Engineers, Domain Experts (DBA) | Prometheus Query Tester |
| `docs/interactive/tools/rbac-setup-wizard.jsx` | Platform Engineers, SREs | RBAC Setup Wizard |
| `docs/interactive/tools/release-notes-generator.jsx` | Platform Engineers, SREs | Release Notes Generator |
| `docs/interactive/tools/roi-calculator.jsx` | Platform Engineers | ROI Calculator |
| `docs/interactive/tools/RoutingTraceTab.jsx` | Platform Engineers, Tenants | Routing Trace Tab |
| `docs/interactive/tools/rule-pack-detail.jsx` | Platform Engineers, Domain Experts (DBA) | Rule Pack Detail Viewer |
| `docs/interactive/tools/rule-pack-matrix.jsx` | Platform Engineers, Domain Experts (DBA) | Rule Pack Comparison Matrix |
| `docs/interactive/tools/rule-pack-selector.jsx` | Platform Engineers, Domain Experts (DBA) | Rule Pack Selector |
| `docs/interactive/tools/runbook-viewer.jsx` | Platform Engineers, Domain Experts (DBA) | Runbook Viewer |
| `docs/interactive/tools/schema-explorer.jsx` | Platform Engineers, Domain Experts (DBA) | YAML Schema Explorer |
| `docs/interactive/tools/self-service-portal.jsx` | Platform Engineers, Domain Experts (DBA), Tenants | Tenant Self-Service Portal |
| `docs/interactive/tools/template-gallery.jsx` | Tenants, Platform Engineers | Config Template Gallery |
| `docs/interactive/tools/tenant-manager.jsx` | Platform Engineers, SREs | Tenant Manager |
| `docs/interactive/tools/threshold-calculator.jsx` | Domain Experts (DBA), Tenants | Threshold Calculator |
| `docs/interactive/tools/threshold-heatmap.jsx` | Platform Engineers, Domain Experts (DBA), SREs | Threshold Heatmap |
| `docs/interactive/tools/YamlValidatorTab.jsx` | Platform Engineers, Tenants | YAML Validator Tab |
| `docs/interactive-tools.md` (.en.md) | All | 互動式工具 |
| `docs/internal/archive/automation-origins/trap-60-fuse-fsync.md` | maintainers, AI Agent | Trap #60 原 RCA — `generate_doc_map.py` FUSE fsync 中斷 |
| `docs/internal/archive/automation-origins/trap-61-bom.md` | maintainers, AI Agent | Trap #61 原 RCA — PowerShell BOM 污染 commit message |
| `docs/internal/archive/lessons-learned.md` | Platform Engineers, SREs, Contributors | Lessons Learned Archive |
| `docs/internal/benchmark-playbook.md` | Platform Engineers, SREs | Benchmark 操作手冊 (Benchmark Playbook) |
| `docs/internal/commit-convention.md` | contributors, maintainers | Conventional Commits Guide |
| `docs/internal/component-health-snapshot.md` | maintainer, ui-engineer | Component Health Snapshot (v2.7.0 Phase .a baseline) |
| `docs/internal/design-system-guide.md` | maintainers | Design System Guide |
| `docs/internal/dev-rules.md` | All | 開發規範 (Development Rules) |
| `docs/internal/doc-template.md` | All | 文件模板規範 |
| `docs/internal/dx-tooling-backlog.md` | maintainers, contributors | DX Tooling Backlog |
| `docs/internal/frontend-quality-backlog.md` | maintainers, AI Agent | 前端品質待辦 (Frontend Quality Backlog) |
| `docs/internal/github-release-playbook.md` | All | GitHub Release — 操作手冊 (Playbook) |
| `docs/internal/pitch-deck-talking-points.md` | maintainers, business | Pitch Deck Talking Points — v2.8.0 Phase 1 Baseline |
| `docs/internal/ssot-language-evaluation.md` | maintainers | SSOT 切換影響評估 |
| `docs/internal/ssot-migration-pilot-report.md` | maintainers | SSOT 語言遷移 Phase 1 Pilot Report |
| `docs/internal/test-coverage-matrix.md` | Platform Engineers, SREs | 測試覆蓋矩陣與進階場景 |
| `docs/internal/test-map.md` | maintainers, AI Agent | 測試架構導覽 (Test Map) |
| `docs/internal/testing-playbook.md` | All | 測試注意事項 — 排錯手冊 (Testing Playbook) |
| `docs/internal/windows-mcp-playbook.md` | All | Windows-MCP — Dev Container 操作手冊 (Playbook) |
| `docs/migration-engine.md` (.en.md) | Platform Engineers, DevOps | AST 遷移引擎架構 |
| `docs/migration-guide.md` (.en.md) | Tenants, DevOps | Migration Guide — 遷移指南 |
| `docs/scenarios/advanced-scenarios.md` (.en.md) | Platform Engineers, SREs | 進階場景與測試覆蓋 |
| `docs/scenarios/alert-routing-split.md` (.en.md) | Platform Engineers | 場景：同一 Alert、不同語義 — Platform/NOC vs Tenant 雙視角通知 |
| `docs/scenarios/gitops-ci-integration.md` (.en.md) | Platform Engineers | 場景：GitOps CI/CD 整合指南 |
| `docs/scenarios/hands-on-lab.md` (.en.md) | Platform Engineers, Tenants | 動手實驗：從零到生產告警 |
| `docs/scenarios/incremental-migration-playbook.md` (.en.md) | Platform Engineers, SREs | 場景：漸進式遷移 Playbook |
| `docs/scenarios/manage-at-scale.md` (.en.md) | Platform Engineers, operator, DevOps | 場景：千租戶規模管理 |
| `docs/scenarios/multi-cluster-federation.md` (.en.md) | Platform Engineers | 場景：多叢集聯邦架構 — 中央閾值 + 邊緣指標 |
| `docs/scenarios/multi-domain-conf-layout.md` (.en.md) | Platform Engineers, operator, DevOps | 場景：多域名階層式配置 — conf.d/ 目錄結構重構（v2.7.0） |
| `docs/scenarios/README.md` | All | 場景指南導覽 |
| `docs/scenarios/shadow-audit.md` (.en.md) | Platform Engineers, Tenants | 場景：Shadow Audit — 告警品質評估 |
| `docs/scenarios/shadow-monitoring-cutover.md` (.en.md) | Platform Engineers, SREs, DevOps, Tenants | 場景：Shadow Monitoring — 從告警健康評估到全自動切換 |
| `docs/scenarios/tenant-lifecycle.md` (.en.md) | All | 場景：租戶完整生命週期管理 |
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
