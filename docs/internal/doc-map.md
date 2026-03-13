# 文件導覽 (Documentation Map)

> 本表由 CLAUDE.md 提取，供 AI Agent 與開發者快速查找文件位置。

| 文件 | 受眾 | 內容 |
|------|------|------|
| `README.md` / `README.en.md` | 技術主管、初訪者 | 痛點對比 + 企業價值 |
| `docs/context-diagram.md` (.en.md) | All | 專案 Context 圖：角色、工具、產品互動關係 |
| `docs/architecture-and-design.md` (.en.md) | Platform Engineers | 核心架構（§1-3 系統設計 + §4 HA + §5 Roadmap） |
| `docs/benchmarks.md` (.en.md) | Platform Engineers, SREs | 性能分析與基準測試（原 §4） |
| `docs/governance-security.md` (.en.md) | Platform Engineers, 安全合規 | 治理、稽核與安全合規（原 §6-7） |
| `docs/troubleshooting.md` (.en.md) | All | 故障排查與邊界情況（原 §8） |
| `docs/migration-engine.md` (.en.md) | Platform Engineers, DevOps | AST 遷移引擎架構（原 §10） |
| `docs/migration-guide.md` (.en.md) | Tenants, DevOps | 遷移步驟 + routing 說明 |
| `docs/byo-prometheus-integration.md` (.en.md) | Platform Engineers | BYOP 最小整合 |
| `docs/byo-alertmanager-integration.md` (.en.md) | Platform Engineers | Alertmanager 整合指引 |
| `docs/custom-rule-governance.md` (.en.md) | Platform Leads | 三層治理模型 + CI Linting |
| `docs/shadow-monitoring-sop.md` (.en.md) | SRE | Shadow Monitoring SOP |
| `docs/gitops-deployment.md` (.en.md) | DevOps | ArgoCD/Flux + CODEOWNERS RBAC |
| `docs/federation-integration.md` (.en.md) | Platform Engineers | Federation 場景 A 藍圖 |
| `docs/scenarios/alert-routing-split.md` (.en.md) | Platform Engineers | 雙視角 Alert 通知（NOC vs Tenant） |
| `docs/scenarios/advanced-scenarios.md` (.en.md) | Platform Engineers, SREs | 進階場景與測試覆蓋（原 §9） |
| `docs/scenarios/shadow-monitoring-cutover.md` (.en.md) | SRE, DevOps | Shadow Monitoring 安全切換場景 |
| `docs/scenarios/multi-cluster-federation.md` (.en.md) | Platform Engineers | 多叢集 Federation 場景 |
| `docs/scenarios/tenant-lifecycle.md` (.en.md) | All | Tenant 完整生命週期（上線→運維→下架） |
| `docs/getting-started/for-platform-engineers.md` (.en.md) | Platform Engineers | Platform Engineer 快速入門指南 |
| `docs/getting-started/for-domain-experts.md` (.en.md) | Domain Experts (DBA) | Domain Expert 快速入門指南 |
| `docs/getting-started/for-tenants.md` (.en.md) | Tenants | Tenant 快速入門指南 |
| `docs/getting-started/wizard.jsx` | All | 互動式角色導向入門精靈（React） |
| `docs/cli-reference.md` (.en.md) | All | da-tools CLI 完整指令參考 |
| `docs/api/README.md` (.en.md) | Platform Engineers, SREs | threshold-exporter API 端點參考 + OpenAPI spec |
| `docs/grafana-dashboards.md` (.en.md) | SREs | Grafana Dashboard 使用指南（Platform Overview + Shadow Monitoring） |
| `docs/playground.jsx` | All | 互動式 Tenant YAML 驗證 Playground（React） |
| `docs/index.md` | All | MkDocs 站點首頁 |
| `docs/internal/testing-playbook.md` | AI Agent | K8s 排錯 + Benchmark 方法論 |
| `docs/internal/windows-mcp-playbook.md` | AI Agent | Dev Container + MCP 操作 |
| `docs/internal/github-release-playbook.md` | AI Agent | Git push + GitHub Release 流程 |
| `docs/internal/commit-convention.md` | Contributors | Conventional Commits 規範指南 |
| `docs/internal/doc-map.md` | AI Agent | 本文件（文件導覽總表） |
| `docs/rule-pack-selector.jsx` | Platform Engineers, SREs | 互動式 Rule Pack 選擇器（React） |
| `docs/cli-playground.jsx` | All | 互動式 CLI 指令建構器（React） |
| `docs/cheat-sheet.md` (.en.md) | All | da-tools 指令速查表（自動生成） |
| `docs/glossary.md` (.en.md) | All | 術語表（30+ 專有名詞定義） |
| `docs/adr/README.md` (.en.md) | Platform Engineers | 架構決策記錄索引（5 ADRs） |
| `docs/interactive-tools.md` (.en.md) | All | 互動式工具導覽頁（4 個 React 元件使用說明） |
| `docs/schemas/tenant-config.schema.json` | All | Tenant YAML JSON Schema（VS Code 自動補全） |
| `rule-packs/README.md` | All | 15 Rule Packs + optional 卸載 |
| `rule-packs/ALERT-REFERENCE.md` (.en.md) | Tenants, SREs | 96 個 Alert 含義 + 建議動作速查 |
| `k8s/03-monitoring/dynamic-alerting-overview.json` | SRE | Grafana Dashboard |
