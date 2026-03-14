# 工具導覽 (Tool Map)

> 本表由 `generate_tool_map.py --generate` 自動產生。
> da-tools CLI 對應的子命令見 [cli-reference.md](../cli-reference.md)。

## 運維工具（da-tools CLI 封裝）

| 工具 | 用途 |
|------|------|
| `analyze_rule_pack_gaps.py` | Rule Pack gap analysis for custom rules. |
| `backtest_threshold.py` | Backtest threshold changes against historical Prometheus data. |
| `baseline_discovery.py` | Baseline Discovery 工具。 |
| `batch_diagnose.py` | Post-cutover multi-tenant health report. |
| `blind_spot_discovery.py` | Scan Prometheus targets and cross-reference tenant configs |
| `check_alert.py` | Check Prometheus alert state for a specific tenant. |
| `config_diff.py` | Directory-level Config Diff for GitOps PR review. |
| `cutover_tenant.py` | Shadow Monitoring 一鍵切換工具。 |
| `deprecate_rule.py` | 規則/指標下架工具。 |
| `diagnose.py` | Quick health check for a tenant's MariaDB and monitoring stack. |
| `generate_alertmanager_routes.py` | Generate Alertmanager route + receiver + inhibit config from tenant YAML. |
| `maintenance_scheduler.py` | Evaluate recurring maintenance schedules and create |
| `migrate_rule.py` | 傳統 Prometheus 警報規則遷移輔助工具 (v4 — AST Engine)。 |
| `offboard_tenant.py` | 安全的 Tenant 下架工具。 |
| `onboard_platform.py` | Reverse-analyze existing configs for Dynamic Alerting onboarding. |
| `patch_config.py` | Patch threshold-config ConfigMap for a specific tenant. |
| `scaffold_tenant.py` | Interactive tenant config generator for Dynamic Alerting. |
| `validate_config.py` | One-stop configuration validation. |
| `validate_migration.py` | Shadow Monitoring 驗證工具。 |

## DX Automation 工具

| 工具 | 用途 |
|------|------|
| `byo_check.py` | BYO Prometheus & Alertmanager integration verification. |
| `federation_check.py` | Multi-cluster federation integration verification. |
| `grafana_import.py` | Grafana dashboard import via ConfigMap sidecar. |
| `shadow_verify.py` | Shadow Monitoring readiness and convergence verification. |

## 文件 CI 工具

| 工具 | 用途 |
|------|------|
| `add_frontmatter.py` | Add YAML front matter to documentation files for MkDocs/Docusaurus integration. |
| `assemble_config_dir.py` | Sharded GitOps Assembly Tool — merge multiple conf.d/ sources into one config-dir. |
| `bump_docs.py` | 版號一致性管理工具 |
| `check_doc_freshness.py` | check_doc_freshness.py |
| `check_doc_links.py` | 文件間交叉引用一致性檢查 |
| `check_includes_sync.py` | Check that Chinese and English include snippets stay in sync. |
| `check_translation.py` | 自動化翻譯品質檢查 |
| `da_assembler.py` | da-assembler-controller — Lightweight ThresholdConfig CRD → YAML renderer. |
| `doc_coverage.py` | 文件覆蓋率 Dashboard |
| `doc_impact.py` | 文件變更影響分析 |
| `generate_alert_reference.py` | Auto-generate ALERT-REFERENCE.md from Rule Pack YAML files. |
| `generate_changelog.py` | Generate CHANGELOG draft entries from conventional commits. |
| `generate_cheat_sheet.py` | Auto-generate da-tools cheat sheet from CLI reference. |
| `generate_doc_map.py` | 文件導覽自動生成 |
| `generate_nav.py` | 從 docs/ 目錄自動生成 MkDocs nav 結構 |
| `generate_rule_pack_readme.py` | Generate ../rule-packs/README.md from actual YAML rule pack files. |
| `generate_rule_pack_stats.py` | Rule Pack 統計單一來源產生器 |
| `generate_platform_data.py` | Rule Pack 共用資料源產生器（→ `platform-data.json`，供 JSX 工具 fetch） |
| `generate_tool_map.py` | 工具導覽自動生成 |
| `inject_related_docs.py` | Auto-generate "相關資源 / Related Resources" tables in documentation files. |
| `lint_custom_rules.py` | Custom Rule deny-list linter。 |
| `sync_glossary_abbr.py` | Sync abbreviations from glossary.md to MkDocs snippet. |
| `sync_schema.py` | Sync JSON Schema with Go source definitions. |
| `check_bilingual_annotations.py` | Rule Pack 雙語 annotation 覆蓋率檢查（`--check` / `--coverage` / `--ci`）。 |
| `check_i18n_coverage.py` | i18n 覆蓋率報告（JSX / Rule Pack / Python CLI）。 |
| `check_repo_name.py` | GitHub URL repo name 防護 — 掃描 vibe-k8s-lab 誤用（`--ci` / `--fix`）。 |
| `check_structure.py` | 專案結構正規化檢查（工具/JSX/測試位置）。 |
| `fix_doc_links.py` | 文件連結自動修復（`check_doc_links.py` 的 `--fix` 對應工具）。 |
| `sync_tool_registry.py` | Tool registry ↔ Hub ↔ TOOL_META 同步。 |
| `validate_all.py` | Unified validation entry point for all documentation and config validation tools. |
| `validate_docs_versions.py` | 文件版號與計數一致性檢查 |
| `validate_mermaid.py` | Mermaid 圖渲染驗證 |

## 共用函式庫

- `scripts/tools/_lib_python.py`：Python 工具間共用
- `scripts/_lib.sh`：Shell scenario/benchmark 共用
