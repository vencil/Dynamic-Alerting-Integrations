# 文件導覽 (Documentation Map)

> 本表由 `generate_doc_map.py --generate` 自動產生，供 AI Agent 與開發者快速查找文件位置。

| 文件 | 受眾 | 內容 |
|------|------|------|
| `docs/adr/001-severity-dedup-via-inhibit.md` (.en.md) | Platform Engineers | ADR-001: 嚴重度 Dedup 採用 Inhibit 規則 |
| `docs/adr/002-oci-registry-over-chartmuseum.md` (.en.md) | Platform Engineers | ADR-002: OCI Registry 替代 ChartMuseum |
| `docs/adr/003-sentinel-alert-pattern.md` (.en.md) | Platform Engineers | ADR-003: Sentinel Alert 模式 |
| `docs/adr/004-federation-scenario-a-first.md` (.en.md) | Platform Engineers | ADR-004: Federation 場景 A 優先實現 |
| `docs/adr/005-projected-volume-for-rule-packs.md` (.en.md) | Platform Engineers | ADR-005: 投影卷掛載 Rule Pack |
| `docs/adr/006-tenant-mapping-topologies.md` (.en.md) | Platform Engineers | ADR-006: 租戶映射拓撲 (1:1, N:1, 1:N) |
| `docs/adr/007-cross-domain-routing-profiles.md` (.en.md) | Platform Engineers | ADR-007: 跨域路由設定檔與域策略 |
| `docs/adr/008-operator-native-integration-path.md` (.en.md) | Platform Engineers | ADR-008: Operator-Native 整合路徑 |
| `docs/api/README.md` (.en.md) | Platform Engineers, SREs | Threshold Exporter API Reference |
| `docs/architecture-and-design.md` (.en.md) | Platform Engineers | 架構與設計 — 動態多租戶警報平台技術白皮書 |
| `docs/benchmarks.md` (.en.md) | Platform Engineers, SREs | 性能分析與基準測試 (Performance Analysis & Benchmarks) |
| `docs/byo-alertmanager-integration.md` (.en.md) | Platform Engineers, SREs | BYO Alertmanager 整合指南 |
| `docs/byo-prometheus-integration.md` (.en.md) | Platform Engineers, SREs | Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南 |
| `docs/cheat-sheet.md` (.en.md) | All | da-tools Quick Reference |
| `docs/cli-reference.md` (.en.md) | Platform Engineers, SREs, DevOps, Tenants | da-tools CLI Reference |
| `docs/custom-rule-governance.md` (.en.md) | Platform Engineers | 多租戶客製化規則治理規範 (Custom Rule Governance Model) |
| `docs/federation-integration.md` (.en.md) | Platform Engineers | Federation Integration Guide |
| `docs/getting-started/for-domain-experts.md` (.en.md) | Domain Experts (DBA) | Domain Expert (DBA) 快速入門指南 |
| `docs/getting-started/for-platform-engineers.md` (.en.md) | Platform Engineers | Platform Engineer 快速入門指南 |
| `docs/getting-started/for-tenants.md` (.en.md) | Tenants | Tenant 快速入門指南 |
| `docs/getting-started/wizard.jsx` | Tenants, Platform Engineers, Domain Experts (DBA) | Getting Started Wizard |
| `docs/gitops-deployment.md` (.en.md) | Platform Engineers, DevOps | GitOps 部署指南 |
| `docs/glossary.md` (.en.md) | All | 術語表 |
| `docs/governance-security.md` (.en.md) | Platform Engineers, 安全合規 | 治理、稽核與安全合規 |
| `docs/grafana-dashboards.md` (.en.md) | Platform Engineers, SREs, DevOps | Grafana Dashboard 導覽 |
| `docs/index.md` (.en.md) | All | Dynamic Alerting Platform — Home |
| `docs/interactive/tools/AlertPreviewTab.jsx` | Platform Engineers, Tenants | Alert Preview Tab |
| `docs/interactive/tools/RoutingTraceTab.jsx` | Platform Engineers, Tenants | Routing Trace Tab |
| `docs/interactive/tools/YamlValidatorTab.jsx` | Platform Engineers, Tenants | YAML Validator Tab |
| `docs/interactive/tools/alert-noise-analyzer.jsx` | platform, Domain Experts (DBA) | Alert Noise Analyzer |
| `docs/interactive/tools/alert-simulator.jsx` | Domain Experts (DBA), Tenants | Alert Simulator |
| `docs/interactive/tools/alert-timeline.jsx` | Domain Experts (DBA), Tenants | Alert Timeline Replay |
| `docs/interactive/tools/architecture-quiz.jsx` | Platform Engineers | Architecture Decision Quiz |
| `docs/interactive/tools/capacity-planner.jsx` | Platform Engineers | Capacity Planner |
| `docs/interactive/tools/cicd-setup-wizard.jsx` | Platform Engineers | CI/CD Setup Wizard |
| `docs/interactive/tools/cli-playground.jsx` | Platform Engineers | da-tools CLI Playground |
| `docs/interactive/tools/config-diff.jsx` | Platform Engineers | Config Version Diff |
| `docs/interactive/tools/config-lint.jsx` | Platform Engineers, Tenants | Config Lint Report |
| `docs/interactive/tools/dependency-graph.jsx` | Platform Engineers, Domain Experts (DBA) | Dependency Graph |
| `docs/interactive/tools/glossary.jsx` | Platform Engineers, Domain Experts (DBA), Tenants | Interactive Glossary |
| `docs/interactive/tools/health-dashboard.jsx` | Tenants, Platform Engineers | Tenant Health Dashboard |
| `docs/interactive/tools/migration-simulator.jsx` | Platform Engineers | Migration Dry-Run Simulator |
| `docs/interactive/tools/multi-tenant-comparison.jsx` | platform, Domain Experts (DBA) | Multi-Tenant Comparison |
| `docs/interactive/tools/notification-previewer.jsx` | Platform Engineers, Tenants | Notification Template Previewer |
| `docs/interactive/tools/onboarding-checklist.jsx` | Tenants, Platform Engineers, Domain Experts (DBA) | Onboarding Checklist Generator |
| `docs/interactive/tools/platform-demo.jsx` | Platform Engineers, Domain Experts (DBA), Tenants | Platform Demo |
| `docs/interactive/tools/platform-health.jsx` | Platform Engineers | Platform Health Dashboard |
| `docs/interactive/tools/playground.jsx` | Platform Engineers, Tenants | YAML Playground |
| `docs/interactive/tools/portal-shared.jsx` | Platform Engineers | Portal Shared Module |
| `docs/interactive/tools/promql-tester.jsx` | Platform Engineers, Domain Experts (DBA) | Prometheus Query Tester |
| `docs/interactive/tools/roi-calculator.jsx` | Platform Engineers | ROI Calculator |
| `docs/interactive/tools/rule-pack-detail.jsx` | Platform Engineers, Domain Experts (DBA) | Rule Pack Detail Viewer |
| `docs/interactive/tools/rule-pack-matrix.jsx` | Platform Engineers, Domain Experts (DBA) | Rule Pack Comparison Matrix |
| `docs/interactive/tools/rule-pack-selector.jsx` | Platform Engineers, Domain Experts (DBA) | Rule Pack Selector |
| `docs/interactive/tools/runbook-viewer.jsx` | Platform Engineers, Domain Experts (DBA) | Runbook Viewer |
| `docs/interactive/tools/schema-explorer.jsx` | Platform Engineers, Domain Experts (DBA) | YAML Schema Explorer |
| `docs/interactive/tools/self-service-portal.jsx` | Platform Engineers, Domain Experts (DBA), Tenants | Tenant Self-Service Portal |
| `docs/interactive/tools/template-gallery.jsx` | Tenants, Platform Engineers | Config Template Gallery |
| `docs/interactive/tools/tenant-manager.jsx` | Platform Engineers, SREs | Tenant Manager |
| `docs/interactive/tools/threshold-calculator.jsx` | Domain Experts (DBA), Tenants | Threshold Calculator |
| `docs/interactive-tools.md` (.en.md) | All | 互動式工具 |
| `docs/internal/benchmark-playbook.md` | Platform Engineers, SREs | Benchmark 操作手冊 (Benchmark Playbook) |
| `docs/internal/commit-convention.md` | All | Conventional Commits Guide |
| `docs/internal/doc-template.md` | All | 文件模板規範 |
| `docs/internal/dx-tooling-backlog.md` | All | DX Tooling Backlog |
| `docs/internal/github-release-playbook.md` | All | GitHub Release — 操作手冊 (Playbook) |
| `docs/internal/test-map.md` (.en.md) | All | 測試架構導覽 (Test Map) |
| `docs/internal/testing-playbook.md` | All | 測試注意事項 — 排錯手冊 (Testing Playbook) |
| `docs/internal/windows-mcp-playbook.md` | All | Windows-MCP — Dev Container 操作手冊 (Playbook) |
| `docs/migration-engine.md` (.en.md) | Platform Engineers, DevOps | AST 遷移引擎架構 |
| `docs/migration-guide.md` (.en.md) | Tenants, DevOps | Migration Guide — 遷移指南 |
| `docs/prometheus-operator-integration.md` (.en.md) | Platform Engineers | Prometheus Operator 整合手冊 |
| `docs/scenarios/advanced-scenarios.md` (.en.md) | Platform Engineers, SREs | 進階場景與測試覆蓋 |
| `docs/scenarios/alert-routing-split.md` (.en.md) | Platform Engineers | 場景：同一 Alert、不同語義 — Platform/NOC vs Tenant 雙視角通知 |
| `docs/scenarios/gitops-ci-integration.md` (.en.md) | Platform Engineers | 場景：GitOps CI/CD 整合指南 |
| `docs/scenarios/hands-on-lab.md` (.en.md) | Platform Engineers, Tenants | 動手實驗：從零到生產告警 |
| `docs/scenarios/incremental-migration-playbook.md` (.en.md) | Platform Engineers, SREs | 場景：漸進式遷移 Playbook |
| `docs/scenarios/multi-cluster-federation.md` (.en.md) | Platform Engineers | 場景：多叢集聯邦架構 — 中央閾值 + 邊緣指標 |
| `docs/scenarios/shadow-audit.md` (.en.md) | Platform Engineers, Tenants | 場景：Shadow Audit — 不遷移也能評估告警健康度 |
| `docs/scenarios/shadow-monitoring-cutover.md` (.en.md) | SREs, DevOps | 場景：Shadow Monitoring 全自動切換工作流 |
| `docs/scenarios/tenant-lifecycle.md` (.en.md) | All | 場景：租戶完整生命週期管理 |
| `docs/schemas/README.md` | Platform Engineers, Tenants | JSON Schema Reference |
| `docs/shadow-monitoring-sop.md` (.en.md) | SREs, Platform Engineers | Shadow Monitoring SRE SOP |
| `docs/troubleshooting.md` (.en.md) | Platform Engineers, SREs, Tenants | 故障排查與邊界情況 |
| `docs/internal/doc-map.md` | AI Agent | 本文件（文件導覽總表） |
| `docs/internal/tool-map.md` | AI Agent | 工具導覽（自動生成） |
| `docs/schemas/tenant-config.schema.json` | All | Tenant YAML JSON Schema（VS Code 自動補全） |
| `rule-packs/README.md` | All | 15 Rule Packs + optional 卸載 |
| `rule-packs/ALERT-REFERENCE.md (.en.md)` | Tenants, SREs | 96 個 Alert 含義 + 建議動作速查 |
| `k8s/03-monitoring/dynamic-alerting-overview.json` | SRE | Grafana Dashboard |
