---
title: "工具導覽 (Tool Map)"
tags: [tooling, navigation, internal]
audience: [maintainers, ai-agent]
version: v2.8.1
lang: zh
---

# 工具導覽 (Tool Map)

> 本表由 `generate_tool_map.py --generate` 自動產生。
> da-tools CLI 對應的子命令見 [cli-reference.md](../cli-reference.md)。

## 運維工具（da-tools CLI 封裝）

| 工具 | 用途 |
|------|------|
| `_grar_merge.py` | Routing-config merging + tenant substitution + receiver building. |
| `_grar_parse.py` | Configuration loading + parsing for generate_alertmanager_routes. |
| `_grar_render.py` | Output rendering + Alertmanager ConfigMap operations. |
| `_grar_routes.py` | Route generation: tenant routes, override expansion, enforced routes, inhibit rules. |
| `_grar_validate.py` | URL / domain / schema validation for generate_alertmanager_routes. |
| `_observed_map_lib.py` | Shared SoT extractor for the threshold observed-map (#719). |
| `alert_correlate.py` | 告警關聯分析引擎（離線 CLI 模式）。 |
| `alert_quality.py` | 警報品質評估工具。 |
| `analyze_rule_pack_gaps.py` | Rule Pack gap analysis for custom rules. |
| `assemble_config_dir.py` | Sharded GitOps Assembly Tool — merge multiple conf.d/ sources into one config-dir. |
| `backtest_threshold.py` | Backtest threshold changes against historical Prometheus data. |
| `baseline_discovery.py` | Baseline Discovery 工具。 |
| `batch_diagnose.py` | Post-cutover multi-tenant health report. |
| `batchpr_dispatch.py` | `da-tools batch-pr` Python entrypoint. |
| `blast_radius.py` | Blast Radius diff engine — compare base vs PR effective tenant configs. |
| `blind_spot_discovery.py` | Scan Prometheus targets and cross-reference tenant configs |
| `byo_check.py` | BYO Prometheus & Alertmanager integration verification. |
| `cardinality_forecasting.py` | 基數預測工具（§5.8）。 |
| `check_alert.py` | Check Prometheus alert state for a specific tenant. |
| `config_diff.py` | Directory-level Config Diff for GitOps PR review. |
| `config_history.py` | Config Snapshot & History tracker. |
| `cutover_tenant.py` | Shadow Monitoring 一鍵切換工具。 |
| `da_assembler.py` | da-assembler-controller — Lightweight ThresholdConfig CRD → YAML renderer. |
| `deprecate_rule.py` | 規則/指標下架工具。 |
| `diagnose.py` | Quick health check for a tenant's MariaDB and monitoring stack. |
| `discover_instance_mappings.py` | Auto-discover 1:N instance-tenant mappings. |
| `drift_detect.py` | Cross-Cluster Configuration Drift Detection |
| `explain_route.py` | Routing merge pipeline debugger (ADR-007). |
| `federation_check.py` | Multi-cluster federation integration verification. |
| `federation_keygen.py` | federation JWT 簽章金鑰的生成 / 輪替工具。 |
| `generate_alertmanager_routes.py` | Generate Alertmanager route + receiver + inhibit config from tenant YAML. |
| `generate_rule_pack_split.py` | Split Rule Packs into edge (Part 1) and central (Parts 2+3) YAML files. |
| `generate_tenant_mapping_rules.py` | Generate Prometheus Recording Rules for 1:N tenant mapping. |
| `gitops_check.py` | GitOps Native Mode readiness validator. |
| `grafana_import.py` | Grafana dashboard import via ConfigMap sidecar. |
| `guard_dispatch.py` | `da-tools guard` Python entrypoint. |
| `init_project.py` | Bootstrap a Dynamic Alerting integration in a customer repo. |
| `inject_metadata_join.py` | One-time script: inject tenant_metadata_info group_left join into Rule Pack alert rules. |
| `lint_custom_rules.py` | Custom Rule deny-list linter。 |
| `maintenance_scheduler.py` | Evaluate recurring maintenance schedules and create |
| `migrate_rule.py` | 傳統 Prometheus 警報規則遷移輔助工具 (v4 — AST Engine)。 |
| `migrate_to_operator.py` | migrate-to-operator — Migrate ConfigMap-based rules to Operator CRD format. |
| `notification_tester.py` | Multi-channel notification connectivity testing. |
| `offboard_tenant.py` | 安全的 Tenant 下架工具。 |
| `onboard_platform.py` | Reverse-analyze existing configs for Dynamic Alerting onboarding. |
| `operator_check.py` | Verify Prometheus Operator CRD deployment status. |
| `operator_generate.py` | operator-generate — Generate Kubernetes CRD YAML for Prometheus + Alertmanager. |
| `parser_dispatch.py` | `da-tools parser` Python entrypoint. |
| `patch_config.py` | Patch threshold-config ConfigMap for a specific tenant. |
| `policy_engine.py` | Policy-as-Code 引擎（Path A — 內建 DSL）。 |
| `policy_opa_bridge.py` | OPA (Open Policy Agent) bridge for tenant config policy evaluation. |
| `rule_pack_diff.py` | Rule Pack version diff for upgrade audits. |
| `scaffold_tenant.py` | Interactive tenant config generator for Dynamic Alerting. |
| `shadow_verify.py` | Shadow Monitoring readiness and convergence verification. |
| `silencer_drift_check.py` | Alertmanager silence drift auditor. |
| `state_reconcile.py` | Migration State directory reconciliation. |
| `threshold_recommend.py` | 閾值推薦引擎。 |
| `validate_config.py` | One-stop configuration validation. |
| `validate_migration.py` | Shadow Monitoring 驗證工具。 |
| `validate_all.py` | Unified validation entry point for all documentation and config validation tools. |

## DX / 自動化工具

| 工具 | 用途 |
|------|------|
| `_atomic_write.py` | Atomic write helper for regen tools (v2.8.0 Trap #60 mitigation). |
| `add_frontmatter.py` | Add YAML front matter to documentation files for MkDocs/Docusaurus integration. |
| `analyze_bench_history.py` | Aggregate bench-record nightly history into per-benchmark stats. |
| `analyze_tier1_fp_rate.py` | Tier 1 bench-gate friction-rate observer (issue #433 W3). |
| `axe_lite_static.py` | Axe-lite: static WCAG heuristics for JSX files (Phase .a0 Day 5 verification). |
| `bump_docs.py` | 版號一致性管理工具 |
| `bump_playbook_versions.py` | Bump `verified-at-version:` front-matter across the 4 operational playbooks. |
| `check_aria_references.py` | Static JSX ARIA reference closure validator (Phase .a0 Day 5 verification). |
| `coverage_delta.py` | Per-file + total coverage delta between two runs. |
| `coverage_gap_analysis.py` | Per-file coverage ranking report |
| `describe_tenant.py` | Describe effective tenant config — resolve _defaults.yaml inheritance chain. |
| `diag_pr_ci.py` | PR CI auto-diagnostic CLI (issue #446). |
| `doc_coverage.py` | 文件覆蓋率 Dashboard |
| `doc_impact.py` | 文件變更影響分析 |
| `generate_alert_reference.py` | Auto-generate ALERT-REFERENCE.md from Rule Pack YAML files. |
| `generate_changelog.py` | Generate CHANGELOG draft entries from conventional commits. |
| `generate_cheat_sheet.py` | Auto-generate da-tools cheat sheet from CLI reference. |
| `generate_doc_map.py` | 文件導覽自動生成 |
| `generate_nav.py` | 從 docs/ 目錄自動生成 MkDocs nav 結構 |
| `generate_platform_data.py` | 共用平台資料產生器 |
| `generate_rule_pack_readme.py` | Generate rule-packs/README.md from actual YAML rule pack files. |
| `generate_rule_pack_stats.py` | Rule Pack 統計單一來源產生器 |
| `generate_rulepack_configmaps.py` | Generate k8s/03-monitoring/configmap-rules-<pack>.yaml from rule-packs/. |
| `generate_tenant_fixture.py` | Synthetic tenant fixture generator — produce N-tenant conf.d/ for benchmark & integration testing. |
| `generate_tenant_metadata.py` | 租戶元資料產生器 — 從 conf.d/ 解析 YAML，推斷 rule_packs、owner、tier、routing_channel。 |
| `generate_tool_map.py` | 工具導覽自動生成 |
| `inject_related_docs.py` | Auto-generate "相關資源 / Related Resources" tables in documentation files. |
| `migrate_conf_d.py` | Migrate flat conf.d/ to hierarchical domain/region/env/ layout. |
| `migrate_ssot_language.py` | SSOT 語言切換遷移工具 (DORMANT, S#101 policy lock) |
| `pr_preflight.py` | PR Preflight Check — branch 收尾前的自動化檢查。 |
| `render_soak_diff.py` | v2.8.0 readiness harness: chaos soak result renderer. |
| `reword_chain.py` | 批次改寫 commit chain 的 subject line（preserve tree + author/committer date） |
| `run_chaos_soak.py` | v2.8.0 readiness harness: compressed-time chaos soak runner. |
| `scaffold_jsx_dep.py` | generate a tenant-manager-style JSX dep file |
| `scaffold_lint.py` | generate a new pre-commit lint script from template. |
| `scan_component_health.py` | JSX 元件健康快照（v2.7.0 Phase .a A-1 首發） |
| `suggest_related.py` | 基於 audience 重疊 + tags 相似度推薦 related tools |
| `sync_glossary_abbr.py` | Sync abbreviations from glossary.md to MkDocs snippet. |
| `sync_schema.py` | Sync JSON Schema with Go source definitions. |
| `sync_tool_registry.py` | 從 tool-registry.yaml 同步 Hub 卡片 + TOOL_META + JSX frontmatter |
| `tenant_verify.py` | Verify a tenant's effective config — print merged_hash and source_hash. |

## 文件 Lint / CI 工具

| 工具 | 用途 |
|------|------|
| `_lint_helpers.py` | Shared utilities for lint tools. |
| `_version_patterns.py` | Version pattern registry for validate_docs_versions.py |
| `check_ad_hoc_git_scripts.py` | Ad-hoc Windows shell script guard (L1 pre-commit hook). |
| `check_bat_ascii_purity.py` | - L1 guard for pitfall #45 (CJK-in-.bat). |
| `check_bilingual_annotations.py` | check_bilingual_annotations.py |
| `check_bilingual_content.py` | 雙語內容一致性 lint |
| `check_bilingual_structure.py` | ZH/EN 文件結構同步 lint |
| `check_build_completeness.py` | build.sh ↔ COMMAND_MAP 雙向同步檢查。 |
| `check_changelog_no_tbd.py` | Detect TBD/TODO placeholders in CHANGELOG (Self-review Gap A.c). |
| `check_cli_coverage.py` | CLI 命令覆蓋率檢查 |
| `check_codename_gate.py` | Layer 2 glossary-driven codename gate (#469). |
| `check_codename_leak.py` | Block internal codenames from leaking to user-facing files. |
| `check_commit_scope_doc.py` | Commit-scope doc drift gate (L1 pre-commit hook + validate_all integration). |
| `check_design_token_usage.py` | JSX 設計 token 使用完整性 lint |
| `check_dev_bypass_manifest.py` | ADR-022 Layer 4 (deploy-time guard). |
| `check_dev_rules_enforcement.py` | detect doc-drift in dev-rules.md. |
| `check_devrules_size.py` | Dev-rules 尺寸上限檢查。 |
| `check_dist_source_consistency.py` | Catch portal dist commits without matching source change (testing-playbook §LL §2, TRK-239). |
| `check_doc_datools_cmds.py` | documented `da-tools` binary-wrapper subcommands |
| `check_doc_freshness.py` | 文件新鮮度檢查工具。 |
| `check_doc_k8s_refs.py` | docs must reference k8s manifests accurately. |
| `check_doc_links.py` | 文件間交叉引用一致性檢查 |
| `check_doc_reading_time.py` | 文件閱讀時間檢查工具。 |
| `check_doc_template.py` | 文件模板合規性檢查工具。 |
| `check_flaky_registry.py` | Validate `flaky-tests.yaml` schema + expire_at. |
| `check_frontmatter_versions.py` | Frontmatter version global scan |
| `check_glossary_coverage.py` | 術語表覆蓋率檢查 |
| `check_ha_threshold_aggregation.py` | HA-max invariant lint: `user_threshold` must be aggregated with `max`. |
| `check_hardcode_tenant.py` | Detect hardcoded tenant literals in PromQL label selectors (Rule #2). |
| `check_head_blob_hygiene.py` | Inspect committed HEAD blobs for corruption. |
| `check_helm_values_secrets.py` | Container/k8s IaC SAST, Layer 3. |
| `check_hub_badge_drift.py` | detect hardcoded tool counts in the Hub UI (PR-portal-7). |
| `check_i18n_coverage.py` | check_i18n_coverage.py |
| `check_iac_helm.py` | Container/k8s IaC SAST, Layer 2 (Helm templates). |
| `check_iac_vibe_rules.py` | Container/k8s IaC SAST, Layer 1 (Dockerfile). |
| `check_includes_sync.py` | Check that Chinese and English include snippets stay in sync. |
| `check_jsx_i18n.py` | JSX 工具 i18n 完整性 lint |
| `check_jsx_loader_compat.py` | Detect JSX-loader-incompatible module syntax (named exports / non-allowlist imports / require() calls). |
| `check_k8s_manifests.py` | Container/k8s IaC SAST, Layer 4 (raw k8s manifests). |
| `check_ksm_version_allowlist.py` | KSM version-allowlist invariant lint (ADR-024 partial-misconfig defense). |
| `check_leftouterjoin_enrichment.py` | Left-outer-join enrichment invariant lint (ADR-024 PR3-pre Commit 3). |
| `check_lint_toolchain_fit.py` | meta-lint: stop reinventing ESLint/stylelint. |
| `check_log_egress_policy.py` | #566 batch D (T4-1/T4-2) egress allowlist gate. |
| `check_makefile_targets.py` | Makefile target 與 DX 工具聯動檢查 |
| `check_md_yaml_drift.py` | Markdown 內 YAML 範例與 Schema 漂移偵測 |
| `check_metric_dictionary.py` | Metric Dictionary 自動驗證 |
| `check_open_encoding.py` | flag open() text-mode calls without encoding=. |
| `check_orphan_docs.py` | 孤兒文件偵測 |
| `check_path_metadata_consistency.py` | Warn when conf.d/ hierarchical path disagrees with tenant `_metadata`. |
| `check_planning_status_sync.py` | CI-time PR-trailer ↔ frontmatter sync gate. |
| `check_playbook_freshness.py` | Playbook 知識退火檢查工具。 |
| `check_playwright_rtl_drift.py` | Detect React Testing Library API names in Playwright specs (S#96, mechanical safety net for testing-playbook §LL §10). |
| `check_portal_bundle_size.py` | Portal dist bundle size budget gate. |
| `check_portal_i18n.py` | Portal JSX i18n hardcoded string detector |
| `check_pr_scope_drift.py` | PR scope drift 偵測（pr-preflight 級）。 |
| `check_property_coverage.py` | Property-pilot coverage drift detector. |
| `check_repo_name.py` | Prevent wrong repository name in source files. |
| `check_routing_profiles.py` | Lint routing profiles and domain policies (ADR-007). |
| `check_rulepack_sync.py` | Rule-pack copy drift guard (ADR-024 PR3-pre). |
| `check_skip_a11y_justification.py` | Require ticket-justification for `skipA11y: true` in E2E specs (testing-playbook §LL §5, TD-039). |
| `check_structure.py` | Project structure enforcement. |
| `check_subprocess_timeout.py` | flag subprocess calls without explicit timeout. |
| `check_threshold_observed_map.py` | Drift-guard for the threshold observed-map (#719). |
| `check_tool_registry_jsx_parity.py` | every tool-registry.yaml entry must have a backing .jsx file (and vice versa). |
| `check_translation.py` | 自動化翻譯品質檢查 |
| `check_undefined_tokens.py` | Detect JSX/CSS/HTML references to --da-* tokens not defined in design-tokens.css (with --report-orphans discovery mode). |
| `check_window_x_no_fallback.py` | Forbid module-scope `const X = window.__X;` no-fallback reads (dev-rules.md §S6). |
| `detect_sed_damage.py` | Detect sed -i damage on staged files. |
| `fix_doc_links.py` | Auto-fix broken MkDocs cross-reference links. |
| `fix_file_hygiene.py` | Fix file hygiene issues: strip null bytes and ensure EOF newline. |
| `lint_html_doc_links.py` | Raw HTML doc-link validator for MkDocs output. |
| `lint_jsx_babel.py` | Validate JSX files parse correctly via Babel standalone. |
| `lint_tool_consistency.py` | 互動工具一致性驗證 |
| `trufflehog_to_sarif.py` | convert trufflehog JSON findings to SARIF 2.1.0 |
| `validate_docs_versions.py` | 文件版號與計數一致性檢查 |
| `validate_mermaid.py` | Mermaid 圖渲染驗證 |
| `validate_planning_session_row.py` | Detect bloated §12.1 Session Ledger rows in versioned planning docs. |

## 共用函式庫

- `scripts/tools/_lib_compat.py`：Cross-platform compatibility helpers for Dynamic Alerting CLI tools.
- `scripts/tools/_lib_constants.py`：Domain constants for Dynamic Alerting platform.
- `scripts/tools/_lib_exitcodes.py`：Canonical exit-code contract for da-tools CLI tools (#452 Track A).
- `scripts/tools/_lib_godispatch.py`：Shared dispatcher for da-tools subcommands that wrap a Go binary.
- `scripts/tools/_lib_io.py`：File I/O and YAML helpers for Dynamic Alerting platform.
- `scripts/tools/_lib_prometheus.py`：HTTP and Prometheus query helpers for Dynamic Alerting platform.
- `scripts/tools/_lib_python.py`：Shared library for Dynamic Alerting Python tools.
- `scripts/tools/_lib_validation.py`：Validation and parsing helpers for Dynamic Alerting platform.
- `scripts/tools/_lib_versions.py`：Version SSOT readers for the dx doc-generation tools.
- `scripts/_lib.sh`：Shell scenario/benchmark 共用
