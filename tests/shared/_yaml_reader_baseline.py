"""The registered PyYAML reading points under `scripts/` — the baseline of
`test_yaml_reader_boundary.py` (ADR-036 §12, step 12, #3206).

⛔ DEBT ONLY SHRINKS. A new row in `DEBT` is refused in review: a new reader of
a file with a Go consumer asks that consumer's reading instead. Nothing
mechanical stops the row — the gate only keeps every row equal to what the
scan finds — so review is the gate here.

Key: (repo path, qualname of the enclosing def/class or "<module>"); the
count is how many references to a PyYAML entry that scope holds (see the
gate's docstring for what counts).

* `ALLOWED`: only Python reads the file (no Go consumer). Value:
  (count, what it reads and why no Go program does).
* `DEBT`: the file has a Go consumer, and the point moves to that consumer's
  reading (`Reading`). Value: (count, Reading). A point that reads files of
  several kinds is filed under the one with a Go consumer; between two with
  Go consumers, the earlier letter (A before D before E), whose migration
  the later steps wait on.
"""
from __future__ import annotations

import enum


class Reading(enum.Enum):
    """Where a DEBT point moves (ADR-036 §12)."""

    A = "da-read prometheus-rules"   # Prometheus rule files (rulefmt)
    B = "da-read prometheus"         # prometheus.yml (config.Load)
    C = "da-read alertmanager"       # Alertmanager config (config.Load)
    D = "da-crdecode"                # Kubernetes manifests, Helm values / charts
    E = "da-guard parse-tree"        # conf.d-shaped text


def debt_by_class() -> "dict[Reading, list[tuple[str, str]]]":
    """DEBT keys by the reading they move to, every `Reading` present."""
    out: "dict[Reading, list[tuple[str, str]]]" = {r: [] for r in Reading}
    for key, (_, reading) in sorted(DEBT.items()):
        out[reading].append(key)
    return out


ALLOWED = {
    ('scripts/dx/generate_adr_index.py', 'parse_adr'):
        (2, 'ADR frontmatter under docs/adr; the ADR index generator is its only reader'),
    ('scripts/dx/generate_planning_index.py', '_discover_flaky_tests'):
        (1, 'the flaky-test registry, a repo data file read only by CI scripts'),
    ('scripts/dx/generate_planning_index.py', '_safe_yaml_load'):
        (1, "planning-item frontmatter in the repo's own docs; only the index generator reads it"),
    ('scripts/ops/audit_rules_drift.py', '_frontmatter'):
        (1, "frontmatter of the repo's rule and memory markdown; read only by this audit"),
    ('scripts/ops/audit_rules_drift.py', 'load_hooks'):
        (1, '.pre-commit-config.yaml, read only by pre-commit (Python)'),
    ('scripts/ops/ci_flake_retry.py', 'load_registry'):
        (1, 'the flaky-test registry, a repo data file read only by CI scripts'),
    ('scripts/ops/go_test_reads.py', 'leg_gate_patterns'):
        (2, 'a GitHub workflow and its dorny path filters; read by GitHub Actions, no Go consumer'),
    ('scripts/tools/dx/_waveform_lib.py', 'load_pack'):
        (1, "a waveform pack, the repo's own test-signal input read only by the waveform tools"),
    ('scripts/tools/dx/bump_docs.py', '_count_precommit_hook_stages'):
        (1, '.pre-commit-config.yaml, read only by pre-commit (Python)'),
    ('scripts/tools/dx/generate_changelog.py', 'parse_fragment'):
        (1, 'changelog.d fragment frontmatter; only the changelog tool reads it'),
    ('scripts/tools/dx/generate_crd_schemas.py', 'run'):
        (1, "SOURCES.yaml, the generator's own list of upstream CRD URLs"),
    ('scripts/tools/dx/pr_preflight.py', '_soft_fail_check_names'):
        (1, 'GitHub workflows; read by GitHub Actions, no Go consumer'),
    ('scripts/tools/dx/scan_component_health.py', 'scan'):
        (1, "docs/assets/tool-registry.yaml, the repo's own tool catalogue"),
    ('scripts/tools/dx/verify_diff.py', 'load_rules'):
        (1, "verify_diff_rules.yaml, this tool's own rule file"),
    ('scripts/tools/dx/waveform_score.py', 'load_tolerances'):
        (1, "the waveform scorer's own tolerance file"),
    ('scripts/tools/lint/check_chart_pull_surface.py', 'registry_and_owner'):
        (1, '.github/workflows/release.yaml; read by GitHub Actions, no Go consumer'),
    ('scripts/tools/lint/check_cli_contract.py', 'load_baseline'):
        (1, "the CLI-contract baseline ledger, this lint's own data"),
    ('scripts/tools/lint/check_commit_scope_doc.py', 'extract_sot_scopes'):
        (1, 'the commit-scope source of truth, a repo data file read only by lints'),
    ('scripts/tools/lint/check_cross_ns_url_consistency.py', 'load_config'):
        (1, "this lint's own config data file"),
    ('scripts/tools/lint/check_devcontainer_dep_parity.py', 'ci_packages'):
        (1, '.github/workflows/ci.yml; read by GitHub Actions, no Go consumer'),
    ('scripts/tools/lint/check_flaky_registry.py', 'main'):
        (1, 'the flaky-test registry, a repo data file read only by CI scripts'),
    ('scripts/tools/lint/check_log_egress_policy.py', 'lint_chart'):
        (1, 'a Vector config read by Vector (Rust); no Go consumer and no da-tools reading'),
    ('scripts/tools/lint/check_md_yaml_drift.py', 'MdYamlDriftChecker.run_fences'):
        (1, "only a PyYAML syntax smoke over every yaml fence in docs/**/*.md (rule, "
            "Alertmanager and Kubernetes examples included); no deployed consumer reads "
            "these examples. The conf.d examples are read through parse-tree by this "
            "lint's conf.d path; the CRD examples by run_crd, which is itself a DEBT D "
            "point (still PyYAML, moves to da-crdecode)"),
    ('scripts/tools/lint/check_metric_dictionary.py', 'check_dictionary_coverage'):
        (1, "metric-dictionary.yaml, the repo's own metric catalogue read only by Python tools"),
    ('scripts/tools/lint/check_metric_dictionary.py', 'load_dictionary_golden_rules'):
        (1, "metric-dictionary.yaml, the repo's own metric catalogue read only by Python tools"),
    ('scripts/tools/lint/check_metric_dictionary.py', 'load_dictionary_metrics'):
        (1, "metric-dictionary.yaml, the repo's own metric catalogue read only by Python tools"),
    ('scripts/tools/lint/check_metric_dictionary.py', 'main'):
        (1, "metric-dictionary.yaml, the repo's own metric catalogue read only by Python tools"),
    ('scripts/tools/lint/check_portal_audience_enum.py', '_check_registry'):
        (1, "docs/assets/tool-registry.yaml, the repo's own tool catalogue"),
    ('scripts/tools/lint/check_property_coverage.py', 'main'):
        (1, "the property-test coverage manifest, this lint's own data"),
    ('scripts/tools/lint/check_tool_registry_jsx_parity.py', 'load_registry'):
        (1, "docs/assets/tool-registry.yaml, the repo's own tool catalogue"),
    ('scripts/tools/lint/check_workflow_git_push_permissions.py', 'scan_workflow'):
        (1, 'GitHub workflows; read by GitHub Actions, no Go consumer'),
    ('scripts/tools/lint/check_workload_closure_drift.py', 'main'):
        (1, "the workload-closure pin file, this lint's own data"),
    ('scripts/tools/ops/_grar_validate.py', 'load_policy_document'):
        (1, 'the routing policy (--policy); its reader is the generator itself (Python), tenant-api only hashes it'),
    ('scripts/tools/ops/_observed_map_lib.py', 'load_observed_map'):
        (1, "the observed-map, the repo's own threshold data read only by Python tools"),
    ('scripts/tools/ops/_registry_lib.py', 'load_registry'):
        (1, "rule-packs/threshold-registry.yaml, the repo's own registry read only by Python tools"),
    ('scripts/tools/ops/_registry_lib.py', 'regen'):
        (1, "rule-packs/threshold-registry.yaml, the repo's own registry read only by Python tools"),
    ('scripts/tools/ops/analyze_rule_pack_gaps.py', 'load_metric_dictionary'):
        (1, "metric-dictionary.yaml, the repo's own metric catalogue read only by Python tools"),
    ('scripts/tools/ops/generate_tenant_mapping_rules.py', 'load_metrics_from_dictionary'):
        (1, "metric-dictionary.yaml, the repo's own metric catalogue read only by Python tools"),
    ('scripts/tools/ops/init_project.py', '_classify_root_file'):
        (2, "the customer repo's CI files (GitHub workflow, GitLab CI, pre-commit), read by those CI systems; no Go consumer"),
    ('scripts/tools/ops/init_project.py', '_gitlab_root_includes'):
        (1, "the customer repo's .gitlab-ci.yml, read by GitLab CI; no Go consumer"),
    ('scripts/tools/ops/init_project.py', '_precommit_root_relative_paths'):
        (1, "the customer repo's .pre-commit-config.yaml, read only by pre-commit (Python)"),
    ('scripts/tools/ops/init_project.py', '_root_relative_ci_paths'):
        (1, "the customer repo's GitHub workflows, read by GitHub Actions; no Go consumer"),
    ('scripts/tools/ops/lint_custom_rules.py', 'load_policy'):
        (1, "the custom-rule lint policy (--policy), this tool's own input"),
    ('scripts/tools/ops/migrate_rule.py', 'load_metric_dictionary'):
        (1, "metric-dictionary.yaml, the repo's own metric catalogue read only by Python tools"),
    ('scripts/tools/ops/shadow_verify.py', 'check_preflight'):
        (1, 'the prefix-mapping file, a migration input read only by Python tools'),
    ('scripts/tools/ops/validate_migration.py', 'load_mapping_pairs'):
        (1, 'the prefix-mapping file, a migration input read only by Python tools'),
}

DEBT = {
    ('scripts/ops/check_image_refs_resolve.py', '_walk_sources'):
        (1, Reading.D),
    ('scripts/ops/configmap_assemble.py', 'measure_artifact'):
        (1, Reading.D),
    ('scripts/tools/dx/generate_alert_reference.py', 'load_rule_packs'):
        (1, Reading.A),
    ('scripts/tools/dx/generate_byo_rulepack_table.py', 'read_projected_packs'):
        (1, Reading.D),
    ('scripts/tools/dx/generate_crd_schemas.py', '_CRDLoader'):
        (1, Reading.D),
    ('scripts/tools/dx/generate_platform_data.py', '_chart_app_version'):
        (1, Reading.D),
    ('scripts/tools/dx/generate_platform_data.py', 'count_rules_in_configmap'):
        (2, Reading.A),
    ('scripts/tools/dx/generate_platform_data.py', 'count_rules_in_yaml'):
        (1, Reading.A),
    ('scripts/tools/dx/generate_rule_pack_readme.py', 'extract_rule_counts'):
        (1, Reading.A),
    ('scripts/tools/dx/generate_rule_pack_stats.py', 'count_rules_in_configmap'):
        (2, Reading.A),
    ('scripts/tools/dx/generate_rule_pack_stats.py', 'count_rules_in_yaml'):
        (1, Reading.A),
    ('scripts/tools/dx/generate_rulepack_configmaps.py', 'main'):
        (2, Reading.A),
    ('scripts/tools/dx/inject_waveform.py', 'parse_rules'):
        (1, Reading.A),
    ('scripts/tools/dx/measure_judgement_budget.py', 'read_limits'):
        (1, Reading.D),
    ('scripts/tools/lint/_rule_tree.py', '_containers_from_text'):
        (1, Reading.A),
    ('scripts/tools/lint/_rule_tree.py', '_documents'):
        (1, Reading.A),
    ('scripts/tools/lint/_rule_tree.py', '_generated_pack_names'):
        (1, Reading.A),
    ('scripts/tools/lint/_rule_tree.py', '_rule_shaped_but_unparsed'):
        (2, Reading.A),
    ('scripts/tools/lint/check_bilingual_annotations.py', 'BilingualAnnotationChecker.check_rule_pack'):
        (1, Reading.A),
    ('scripts/tools/lint/check_configmap_mount_completeness.py', 'configmap_rule_packs'):
        (1, Reading.D),
    ('scripts/tools/lint/check_configmap_mount_completeness.py', 'deployment_mounts'):
        (1, Reading.D),
    ('scripts/tools/lint/check_cross_ns_url_consistency.py', '_scan_yaml_docs'):
        (1, Reading.D),
    ('scripts/tools/lint/check_ha_threshold_aggregation.py', '_iter_rule_groups'):
        (2, Reading.A),
    ('scripts/tools/lint/check_image_pin_capability.py', '_StrictLoader'):
        (1, Reading.D),
    ('scripts/tools/lint/check_image_pin_capability.py', 'load_all_strict'):
        (1, Reading.D),
    ('scripts/tools/lint/check_image_pin_capability.py', 'load_strict'):
        (1, Reading.D),
    ('scripts/tools/lint/check_k8s_manifests.py', 'collect_pss_findings'):
        (1, Reading.D),
    ('scripts/tools/lint/check_ksm_version_allowlist.py', '_iter_docs'):
        (1, Reading.D),
    ('scripts/tools/lint/check_log_egress_policy.py', 'render_chart'):
        (1, Reading.D),
    ('scripts/tools/lint/check_maintenance_symmetry.py', 'classify_all'):
        (1, Reading.A),
    ('scripts/tools/lint/check_md_yaml_drift.py', 'MdYamlDriftChecker._looks_like_confd'):
        (1, Reading.E),
    ('scripts/tools/lint/check_md_yaml_drift.py', 'MdYamlDriftChecker.run_crd'):
        (1, Reading.D),
    ('scripts/tools/lint/check_metric_dictionary.py', 'extract_rule_pack_metrics'):
        (3, Reading.A),
    ('scripts/tools/lint/check_metric_group_pairs.py', '_load_yaml'):
        (1, Reading.A),
    ('scripts/tools/lint/check_metric_group_pairs.py', 'collect_alerts'):
        (1, Reading.A),
    ('scripts/tools/lint/check_orphan_recordings.py', '_load_yaml'):
        (1, Reading.A),
    ('scripts/tools/lint/check_orphan_recordings.py', 'iter_rules'):
        (1, Reading.A),
    ('scripts/tools/lint/check_portal_rulepack_claims.py', 'collect_shipped_rules'):
        (4, Reading.A),
    ('scripts/tools/lint/check_rulepack_sync.py', '_groups_from_configmap'):
        (2, Reading.A),
    ('scripts/tools/lint/check_rulepack_sync.py', '_groups_from_operator'):
        (1, Reading.A),
    ('scripts/tools/lint/check_rulepack_sync.py', '_groups_from_rulepack'):
        (1, Reading.A),
    ('scripts/tools/lint/check_scrape_reachability.py', 'load_repo_jobs'):
        (1, Reading.B),
    ('scripts/tools/lint/check_scrape_reachability.py', 'parse_scrape_jobs'):
        (1, Reading.B),
    ('scripts/tools/lint/check_scrape_reachability.py', 'parse_services'):
        (1, Reading.D),
    ('scripts/tools/lint/check_single_writer_invariant.py', 'check_targets'):
        (1, Reading.D),
    ('scripts/tools/lint/check_threshold_reachability.py', '_chart_supply'):
        (1, Reading.D),
    ('scripts/tools/lint/check_threshold_reachability.py', '_defaults_section'):
        (1, Reading.D),
    ('scripts/tools/lint/check_threshold_reachability.py', '_init_project_declared'):
        (1, Reading.E),
    ('scripts/tools/lint/check_threshold_reachability.py', '_shipped_optional'):
        (1, Reading.D),
    ('scripts/tools/lint/check_threshold_unit_sanity.py', '_collect'):
        (3, Reading.D),
    ('scripts/tools/lint/check_vmalert_coverage.py', '_load_yaml'):
        (1, Reading.A),
    ('scripts/tools/lint/validate_docs_versions.py', 'count_rule_packs'):
        (3, Reading.A),
    ('scripts/tools/ops/_grar_render.py', '_read_existing_configmap'):
        (1, Reading.C),
    ('scripts/tools/ops/_grar_render.py', 'apply_to_configmap'):
        (1, Reading.C),
    ('scripts/tools/ops/_grar_render.py', 'evaluate_generated_config'):
        (1, Reading.C),
    ('scripts/tools/ops/_grar_render.py', 'load_base_config'):
        (1, Reading.C),
    ('scripts/tools/ops/_grar_validate.py', 'platform_alert_identities'):
        (2, Reading.A),
    ('scripts/tools/ops/_observed_map_lib.py', 'alert_referenced_keys'):
        (1, Reading.A),
    ('scripts/tools/ops/_observed_map_lib.py', 'extract_pack'):
        (1, Reading.A),
    ('scripts/tools/ops/_registry_lib.py', 'plan_surfaces'):
        (1, Reading.A),
    ('scripts/tools/ops/_render_diff.py', 'render_routes'):
        (2, Reading.C),
    ('scripts/tools/ops/_threshold_alerts.py', 'build_index'):
        (1, Reading.A),
    ('scripts/tools/ops/byo_check.py', 'check_alertmanager'):
        (1, Reading.C),
    ('scripts/tools/ops/da_assembler.py', '_legacy_name_hint'):
        (3, Reading.D),
    ('scripts/tools/ops/explain_route.py', 'build_trace_tree'):
        (2, Reading.C),
    ('scripts/tools/ops/explain_route.py', 'trace_alert_routing'):
        (1, Reading.C),
    ('scripts/tools/ops/generate_alertmanager_routes.py', '_output_configmap_mode'):
        (2, Reading.C),
    ('scripts/tools/ops/generate_rule_pack_split.py', 'load_rule_pack'):
        (1, Reading.A),
    ('scripts/tools/ops/lint_custom_rules.py', 'lint_file'):
        (2, Reading.A),
    ('scripts/tools/ops/migrate_rule.py', '_yaml_scalar'):
        (1, Reading.A),
    ('scripts/tools/ops/migrate_rule.py', 'main'):
        (1, Reading.A),
    ('scripts/tools/ops/migrate_to_operator.py', 'parse_configmap_rules'):
        (2, Reading.A),
    ('scripts/tools/ops/migration_audit.py', '_Walk.__init__'):
        (1, Reading.E),
    ('scripts/tools/ops/migration_audit.py', '_read_today'):
        (1, Reading.E),
    ('scripts/tools/ops/onboard_platform.py', 'analyze_rule_files'):
        (2, Reading.A),
    ('scripts/tools/ops/onboard_platform.py', 'parse_scrape_configs'):
        (2, Reading.B),
    ('scripts/tools/ops/operator_generate.py', 'generate_crds'):
        (1, Reading.A),
    ('scripts/tools/ops/rule_pack_diff.py', 'load_rule_pack'):
        (1, Reading.A),
    ('scripts/tools/ops/runtime_audit.py', 'parse_declared_rules'):
        (1, Reading.A),
    ('scripts/tools/ops/silencer_drift_check.py', 'load_alerts'):
        (1, Reading.A),
}
