# 文件導覽 (Documentation Map)

> 本表由 `generate_doc_map.py --generate` 自動產生，供 AI Agent 與開發者快速查找文件位置。

| 文件 | 受眾 | 內容 |
|------|------|------|
| `docs/api/README.md` (.en.md) | Platform Engineers, SREs | Threshold Exporter API Reference |
| `docs/architecture-and-design.md` (.en.md) | Platform Engineers | 架構與設計 — 動態多租戶警報平台技術白皮書 |
| `docs/benchmarks.md` (.en.md) | Platform Engineers, SREs | 性能分析與基準測試 (Performance Analysis & Benchmarks) |
| `docs/byo-alertmanager-integration.md` (.en.md) | Platform Engineers, SREs | BYO Alertmanager 整合指南 |
| `docs/byo-prometheus-integration.md` (.en.md) | Platform Engineers, SREs | Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南 |
| `docs/cheat-sheet.md` (.en.md) | All | da-tools Quick Reference |
| `docs/interactive/tools/cli-playground.jsx` | All | da-tools CLI Playground |
| `docs/cli-reference.md` (.en.md) | Platform Engineers, SREs, DevOps, Tenants | da-tools CLI Reference |
| `docs/context-diagram.md` (.en.md) | All | 專案 Context 圖：角色、工具與產品互動關係 |
| `docs/custom-rule-governance.md` (.en.md) | Platform Engineers | 多租戶客製化規則治理規範 (Custom Rule Governance Model) |
| `docs/federation-integration.md` (.en.md) | Platform Engineers | Federation Integration Guide |
| `docs/getting-started/for-domain-experts.md` (.en.md) | Domain Experts (DBA) | Domain Expert (DBA) 快速入門指南 |
| `docs/getting-started/for-platform-engineers.md` (.en.md) | Platform Engineers | Platform Engineer 快速入門指南 |
| `docs/getting-started/for-tenants.md` (.en.md) | Tenants | Tenant 快速入門指南 |
| `docs/getting-started/wizard.jsx` | All | Wizard |
| `docs/gitops-deployment.md` (.en.md) | Platform Engineers, DevOps | GitOps 部署指南 |
| `docs/glossary.md` (.en.md) | All | 術語表 |
| `docs/governance-security.md` (.en.md) | Platform Engineers, 安全合規 | 治理、稽核與安全合規 |
| `docs/grafana-dashboards.md` (.en.md) | Platform Engineers, SREs, DevOps | Grafana Dashboard 導覽 |
| `docs/index.md` | All | Dynamic Alerting Platform — Home |
| `docs/interactive-tools.md` (.en.md) | All | 互動式工具 |
| `docs/internal/commit-convention.md` | All | Conventional Commits Guide |
| `docs/internal/dx-tooling-backlog.md` | All | DX Tooling Backlog |
| `docs/internal/github-release-playbook.md` | All | GitHub Release — 操作手冊 (Playbook) |
| `docs/internal/testing-playbook.md` | All | 測試注意事項 — 排錯手冊 (Testing Playbook) |
| `docs/internal/windows-mcp-playbook.md` | All | Windows-MCP — Dev Container 操作手冊 (Playbook) |
| `docs/migration-engine.md` (.en.md) | Platform Engineers, DevOps | AST 遷移引擎架構 |
| `docs/migration-guide.md` (.en.md) | Tenants, DevOps | Migration Guide — 遷移指南 |
| `docs/interactive/tools/playground.jsx` | All | Playground |
| `docs/interactive/tools/rule-pack-selector.jsx` | Platform Engineers, SREs | Rule Pack Selector |
| `docs/scenarios/advanced-scenarios.md` (.en.md) | Platform Engineers, SREs | 進階場景與測試覆蓋 |
| `docs/scenarios/alert-routing-split.md` (.en.md) | Platform Engineers | 場景：同一 Alert、不同語義 — Platform/NOC vs Tenant 雙視角通知 |
| `docs/scenarios/multi-cluster-federation.md` (.en.md) | Platform Engineers | 場景：多叢集聯邦架構 — 中央閾值 + 邊緣指標 |
| `docs/scenarios/shadow-monitoring-cutover.md` (.en.md) | SREs, DevOps | 場景：Shadow Monitoring 全自動切換工作流 |
| `docs/scenarios/tenant-lifecycle.md` (.en.md) | All | 場景：租戶完整生命週期管理 |
| `docs/schemas/README.md` | Platform Engineers, Tenants | JSON Schema Reference |
| `docs/shadow-monitoring-sop.md` (.en.md) | SREs, Platform Engineers | Shadow Monitoring SRE SOP |
| `docs/troubleshooting.md` (.en.md) | Platform Engineers, SREs, Tenants | 故障排查與邊界情況 |
| `docs/internal/doc-map.md` | AI Agent | 本文件（文件導覽總表） |
| `docs/internal/tool-map.md` | AI Agent | 工具導覽（自動生成） |
| `docs/schemas/tenant-config.schema.json` | All | Tenant YAML JSON Schema（VS Code 自動補全） |
| `../rule-packs/README.md` | All | 15 Rule Packs + optional 卸載 |
| `rule-packs/ALERT-REFERENCE.md (.en.md)` | Tenants, SREs | 96 個 Alert 含義 + 建議動作速查 |
| `k8s/03-monitoring/dynamic-alerting-overview.json` | SRE | Grafana Dashboard |
| `docs/assets/tool-registry.yaml` | AI Agent | 互動工具單一真相源（23 tools metadata） |

---

## Change Impact Matrix

> 變更任何項目前，先查此表確認連動更新範圍。

| 變更類型 | 必須更新的文件 | 驗證指令 |
|---------|--------------|---------|
| **新增互動工具** | ① JSX 檔案（含 frontmatter）→ ② `tool-registry.yaml` → ③ Hub `index.html`（卡片 + data-audience）→ ④ jsx-loader `TOOL_META` → ⑤ 相關 .md callout → ⑥ CHANGELOG | `make lint-docs` |
| **修改工具 audience** | ① `tool-registry.yaml` → ② Hub `data-audience` → ③ JSX frontmatter | `make lint-docs` |
| **修改工具 related** | ① JSX frontmatter `related:` → ② `tool-registry.yaml` related | `make lint-docs` |
| **新增 Rule Pack** | ① exporter 程式碼 → ② `scaffold_tenant.py` RULE_PACKS → ③ `make platform-data`（自動更新 JSX 共用資料） → ④ dependency-graph EDGES → ⑤ architecture docs → ⑥ Hub stats | `make platform-data && make lint-docs` |
| **修改 Rule Pack 規則** | ① rule-packs/*.yaml → ② `make platform-data`（JSX 工具自動取得新計數）→ ③ `make lint-docs` | `make platform-data && make lint-docs` |
| **新增/修改 Guided Flow** | ① `docs/assets/flows.json` 新增/修改 flow（含 condition/validation 欄位） → ② Hub 自動載入（動態） → ③ 確認 step component 路徑正確 → ④ CLAUDE.md 更新 flow 數量 → ⑤ Custom flow 需同步 jsx-loader `CUSTOM_FLOW_MAP` | `make lint-docs` + `pre-commit run --hook-stage manual flow-e2e-check` |
| **修改 routing 機制** | ① Go code → ② Python gen → ③ architecture-and-design.md → ④ scenarios/ → ⑤ alert-simulator JSX 邏輯 | Go + Python tests |
| **修改三態邏輯** | ① Go code → ② schema-explorer JSX → ③ playground validation → ④ config-lint rules | Go + Python tests |
| **新增 Scenario 文件** | ① .md 檔案 → ② doc-map.md 表格 → ③ 加互動工具 callout → ④ Hub Documentation links | `make lint-docs` |
| **版號升級** | `make bump-docs` → CLAUDE.md / README / CHANGELOG / JSX frontmatter version | `make version-check` |

### 新增互動工具 Checklist（詳細版）

1. 建立 `docs/<tool-name>.jsx`（含 YAML frontmatter: title, tags, audience, version, lang, related）
2. 更新 `docs/assets/tool-registry.yaml` — 新增 entry
3. 更新 `docs/interactive/index.html` — 新增卡片（含 `data-audience`）
4. 更新 `docs/assets/jsx-loader.html` — TOOL_META 新增條目
5. 更新相關 Getting-Started / Scenario / Architecture .md — 加 callout
6. 更新 `tool-registry.yaml` 的 `appears_in` 反映上一步
7. 執行 `make lint-docs` 驗證一致性
8. 更新 CHANGELOG.md
