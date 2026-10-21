"""No conf.d tool judges a conf.d entry in Python — an AST gate (#3206).

ADR-036 §7 step 9 makes "which paths a conf.d holds and what each one is"
Go's answer: the entries of `da-guard parse-tree` (role, class, problem). A
Python reader that also asks the disk or a name — `is_file()`, a `_` /
`.` prefix, `has_yaml_extension` — holds a second verdict that can disagree
with Go's, and each disagreement found so far was a silent one (a file whose
stat is refused read as "absent", a `_arch/` directory Go could not list
skipped by its name). This is ADR-036 D3's "AST lint + allowlist", applied to
the conf.d readers ahead of step 12.

The population is the enumeration contract's (`_confd_population`: every
`scripts/tools` file that binds a conf.d reader library, declares a conf.d
flag, or names a conf.d). In it, these calls are forbidden:

* `<x>.startswith(...)` with `"_"` or `"."` (alone or in a tuple);
* `<x>.is_file()` / `.is_dir()` / `.exists()` / `.iterdir()` / `.glob()` /
  `.rglob()`; `os.path.isfile` / `isdir` / `exists`; `os.walk` (through any
  alias of `os` / `os.path`, or names imported from them);
* `has_yaml_extension`, `is_reserved_name`, `is_hidden_name`,
  `is_dir_symlink` (by name or as an attribute).

Each remaining one is an `ALLOWED` entry, keyed `<file>:<function>:<call>`
(file relative to `scripts/tools`, function the dotted enclosing
`def` / `class` path, `<module>` at top level) with a one-line reason. The
gate is exact both ways: a new call outside the list is red, and so is an
entry that no longer matches anything (delete it). An entry covers its call
in that function only, so a `startswith("_")` added to a function allowed an
`exists()` is still red.

⛔ What a reason may say: the path is not a conf.d entry (the operand
directory itself, an output, a repo file), the call words a message only, it
is the shared predicate's own definition, or — `PR_BETA2` — the judgement is
real and not migrated yet. That last list is the remaining Python
judgement of conf.d (ADR-036 §7 step 12 wording points here); it is meant to
shrink to nothing. An entry that judges a conf.d entry under any other
reason is the bug this gate exists to catch.

⚠️ Not covered: a comparison spelled without these calls (`name[0] == "_"`,
`os.stat` + `S_ISREG`, `fnmatch`). The gate reads calls, not intent.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import textwrap

import pytest

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from _confd_population import parse, population_keys  # noqa: E402

REPO = HERE.parents[1]
TOOLS = REPO / "scripts" / "tools"

PATH_METHODS = frozenset({"is_file", "is_dir", "exists", "iterdir", "glob", "rglob"})
OS_PATH_FUNCS = frozenset({"isfile", "isdir", "exists"})
NAME_PREDICATES = frozenset({"has_yaml_extension", "is_reserved_name", "is_hidden_name",
                             "is_dir_symlink"})
PREFIXES = frozenset({"_", "."})


def _os_aliases(tree: ast.AST) -> "tuple[set[str], set[str], dict[str, str]]":
    """(names bound to `os`, names bound to `os.path`, {local name: forbidden
    call}) from the module's imports."""
    os_names, path_names, funcs = set(), set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "os":
                    os_names.add(a.asname or "os")
                elif a.name == "os.path":
                    if a.asname:
                        path_names.add(a.asname)
                    else:
                        os_names.add("os")
        elif isinstance(node, ast.ImportFrom) and node.module in ("os", "os.path"):
            for a in node.names:
                local = a.asname or a.name
                if node.module == "os" and a.name == "path":
                    path_names.add(local)
                elif node.module == "os" and a.name == "walk":
                    funcs[local] = "os.walk"
                elif node.module == "os.path" and a.name in OS_PATH_FUNCS:
                    funcs[local] = f"os.path.{a.name}"
    return os_names, path_names, funcs


def _call_kind(call: ast.Call, os_names, path_names, funcs) -> "str | None":
    f = call.func
    if isinstance(f, ast.Name):
        if f.id in NAME_PREDICATES:
            return f.id
        return funcs.get(f.id)
    if not isinstance(f, ast.Attribute):
        return None
    v = f.value
    is_os = isinstance(v, ast.Name) and v.id in os_names
    is_os_path = ((isinstance(v, ast.Name) and v.id in path_names)
                  or (isinstance(v, ast.Attribute) and v.attr == "path"
                      and isinstance(v.value, ast.Name) and v.value.id in os_names))
    if is_os_path and f.attr in OS_PATH_FUNCS:
        return f"os.path.{f.attr}"
    if is_os and f.attr == "walk":
        return "os.walk"
    if f.attr in NAME_PREDICATES:
        return f.attr
    if f.attr == "startswith" and call.args:
        a = call.args[0]
        items = a.elts if isinstance(a, (ast.Tuple, ast.List, ast.Set)) else [a]
        if any(isinstance(i, ast.Constant) and i.value in PREFIXES for i in items):
            return "startswith"
        return None
    if f.attr in PATH_METHODS and not is_os_path:
        return f.attr
    return None


def judgements(tree: ast.AST) -> "dict[str, list[int]]":
    """`<function>:<call>` → the lines of every forbidden call in *tree*."""
    aliases = _os_aliases(tree)
    out: "dict[str, list[int]]" = {}

    def visit(node: ast.AST, scope: "list[str]") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*scope, child.name])
                continue
            if isinstance(child, ast.Call):
                kind = _call_kind(child, *aliases)
                if kind is not None:
                    key = f"{'.'.join(scope) or '<module>'}:{kind}"
                    out.setdefault(key, []).append(child.lineno)
            visit(child, scope)
    visit(tree, [])
    return out


def _population() -> "list[pathlib.Path]":
    return sorted((p for p in TOOLS.rglob("*.py") if population_keys(p)),
                  key=lambda p: p.as_posix())


def _actual() -> "dict[str, list[int]]":
    found: "dict[str, list[int]]" = {}
    for p in _population():
        tree = parse(p)
        assert tree is not None, f"{p}: does not parse — the gate cannot read it"
        rel = p.relative_to(TOOLS).as_posix()
        for key, lines in judgements(tree).items():
            found[f"{rel}:{key}"] = lines
    return found


# ── the allowlist ────────────────────────────────────────────────────

ROOT = "the conf.d operand itself (missing / not a directory), not an entry in it"
ROOT_DEFAULT = "which directory is conf.d when none is given (repo layout), not an entry in it"
ROOT_OUTPUT = "the operand or output directory itself, not an entry of a conf.d it lists"
OUTPUT = "a path this tool writes or stages (its output, a scratch copy), not a conf.d it reads"
OPERAND = "a file the operator names (--policy, --base-config, a draft, …), not a listed entry"
NOT_CONFD = "not a conf.d path: repo, docs, rule packs, schemas, binaries, history"
LOADER = ("a generic loader of a path its caller names (dictionary, policy, hints); "
          "conf.d files go through load_confd_file")
KEY = "a YAML key (the `_` reserved keys), not a file name"
PREDICATE = "the shared predicate's own definition, kept until step 9b-3 (with the parity matrix)"
WORDING = "words a WARN about hidden names, which no listing holds; selects nothing"
CONFIGMAP = ("a ConfigMap's keys, not a path on disk; names mode (classify_names) can "
             "answer them — PR-β2")
GIT = ("a git tree being materialised (no listing exists yet): the walk's hidden rule "
       "for irregular entries — PR-β2")
PR_BETA2 = "judges conf.d entries in Python; not migrated in PR-β1 — PR-β2 (#3206 step 9)"
ROUTE_GENERATOR = ("route generator: `_` test of a path the reader listed; Go's class "
                   "answers the same — threading Entry through its helpers is PR-β2")

ALLOWED = {
    "_lib_confd.py:_is_hidden:startswith": PREDICATE,
    "_lib_confd.py:_python_nested_files:is_dir": PR_BETA2,
    "_lib_confd.py:_python_nested_files:is_file": PR_BETA2,
    "_lib_confd.py:_python_nested_files:os.walk": PR_BETA2,
    "_lib_confd.py:config_listing:is_dir": ROOT,
    "_lib_confd.py:config_stem:is_hidden_name": PREDICATE,
    "_lib_confd.py:config_stem:is_reserved_name": PREDICATE,
    "_lib_confd.py:is_defaults_document_name:has_yaml_extension": PREDICATE,
    "_lib_confd.py:is_dir_symlink:is_dir": PREDICATE,
    "_lib_confd.py:is_reserved_name:startswith": PREDICATE,
    "_lib_confd.py:legacy_state_files:is_file": PR_BETA2,
    "_lib_confd.py:overlay_platform_tenants:is_reserved_name": ROUTE_GENERATOR,
    "_lib_io.py:find_metric_dictionary:os.path.isfile": NOT_CONFD,
    "_lib_io.py:load_yaml_file:is_file": LOADER,
    "_lib_io.py:load_yaml_file_exporter_keys:is_file": LOADER,
    "_lib_io.py:load_yaml_file_strict:is_file": LOADER,
    "_lib_io.py:read_onboard_hints:is_file": LOADER,
    "_lib_key_verdict.py:_judge_key:startswith": KEY,
    "_lib_parse_tree.py:<module>:is_dir": NOT_CONFD,
    "_lib_tenant_values.py:<module>:is_dir": NOT_CONFD,
    "_lib_tenant_values.py:_missing_binary_message:os.path.isfile": NOT_CONFD,
    "_lib_tenant_values.py:canonical_key:startswith": KEY,
    "_lib_tenant_values.py:value_not_served_as_written:startswith": KEY,
    "_lib_tenant_values.py:written_config:startswith": KEY,
    "_lib_write_check.py:_as_root:is_dir": OUTPUT,
    "_lib_write_check.py:_is_empty_dir:is_dir": OUTPUT,
    "_lib_write_check.py:_is_empty_dir:rglob": OUTPUT,
    "_lib_write_check.py:_staging_parent:exists": OUTPUT,
    "_lib_write_check.py:_staging_parent:is_dir": OUTPUT,
    "_lib_write_check.py:confirm_tree:exists": OUTPUT,
    "dx/compile_custom_alerts.py:_committed_rules:exists": OUTPUT,
    "dx/compile_custom_alerts.py:main:exists": ROOT,
    "dx/describe_tenant.py:main:exists": ROOT,
    "dx/describe_tenant.py:main:glob": ROOT_DEFAULT,
    "dx/describe_tenant.py:main:is_dir": ROOT_DEFAULT,
    "dx/describe_tenant.py:main:is_file": OPERAND,
    "dx/describe_tenant.py:what_if_result:is_file": PR_BETA2,
    "dx/generate_platform_data.py:_chart_app_version:is_file": NOT_CONFD,
    "dx/generate_platform_data.py:_load_tenant_metadata:exists": NOT_CONFD,
    "dx/generate_platform_data.py:_load_tenant_metadata:is_dir": ROOT,
    "dx/generate_platform_data.py:gather_rule_counts:glob": NOT_CONFD,
    "dx/generate_platform_data.py:main:exists": NOT_CONFD,
    "dx/generate_tenant_fixture.py:main:exists": OUTPUT,
    "dx/generate_tenant_fixture.py:main:iterdir": OUTPUT,
    "dx/generate_tenant_fixture.py:main:rglob": OUTPUT,
    "dx/generate_tenant_metadata.py:count_metrics:startswith": KEY,
    "dx/generate_tenant_metadata.py:infer_rule_packs:startswith": KEY,
    "dx/generate_tenant_metadata.py:main:exists": ROOT_OUTPUT,
    "dx/migrate_conf_d.py:main:exists": ROOT,
    "dx/run_chaos_soak.py:main:exists": ROOT,
    "dx/run_chaos_soak.py:trigger_reload:exists": PR_BETA2,
    "dx/run_chaos_soak.py:trigger_reload:has_yaml_extension": PR_BETA2,
    "dx/run_chaos_soak.py:trigger_reload:is_hidden_name": PR_BETA2,
    "dx/run_chaos_soak.py:trigger_reload:is_reserved_name": PR_BETA2,
    "dx/run_chaos_soak.py:trigger_reload:rglob": PR_BETA2,
    "dx/sync_schema.py:load_registry:startswith": KEY,
    "dx/sync_schema.py:run:exists": NOT_CONFD,
    "dx/sync_schema.py:sync_underscore_members:startswith": KEY,
    "dx/tenant_verify.py:main:is_dir": ROOT,
    "lint/_rule_tree.py:_find_repo_root:exists": NOT_CONFD,
    "lint/_rule_tree.py:_generated_pack_names:is_file": NOT_CONFD,
    "lint/_rule_tree.py:_generated_pack_names:rglob": NOT_CONFD,
    "lint/check_cli_contract.py:_unscanned_carriers:glob": NOT_CONFD,
    "lint/check_cli_contract.py:doc_files:is_file": NOT_CONFD,
    "lint/check_cli_contract.py:doc_files:rglob": NOT_CONFD,
    "lint/check_cli_contract.py:load_baseline:is_file": NOT_CONFD,
    "lint/check_cli_contract.py:main:is_file": NOT_CONFD,
    "lint/check_cli_contract.py:scan:is_dir": NOT_CONFD,
    "lint/check_cli_default_drift.py:_resolve:is_file": NOT_CONFD,
    "lint/check_cli_default_drift.py:_resolve:rglob": NOT_CONFD,
    "lint/check_cli_default_drift.py:main:is_file": NOT_CONFD,
    "lint/check_confd_schema.py:main:os.path.isdir": ROOT,
    "lint/check_doc_datools_cmds.py:_doc_files:rglob": NOT_CONFD,
    "lint/check_doc_datools_cmds.py:_is_bind_mount:startswith": NOT_CONFD,
    "lint/check_doc_datools_cmds.py:pin_capability_doc_files:is_file": NOT_CONFD,
    "lint/check_doc_datools_cmds.py:run:is_file": NOT_CONFD,
    "lint/check_frontmatter_versions.py:fix_drift:exists": NOT_CONFD,
    "lint/check_frontmatter_versions.py:main:is_dir": NOT_CONFD,
    "lint/check_frontmatter_versions.py:main:rglob": NOT_CONFD,
    "lint/check_frontmatter_versions.py:scan_docs:exists": NOT_CONFD,
    "lint/check_frontmatter_versions.py:scan_docs:is_hidden_name": NOT_CONFD,
    "lint/check_frontmatter_versions.py:scan_docs:rglob": NOT_CONFD,
    "lint/check_k8s_manifests.py:find_manifest_files:is_dir": NOT_CONFD,
    "lint/check_k8s_manifests.py:find_manifest_files:rglob": NOT_CONFD,
    "lint/check_k8s_manifests.py:kube_linter_lint_dir:exists": NOT_CONFD,
    "lint/check_k8s_manifests.py:manifest_root_exists:is_dir": NOT_CONFD,
    "lint/check_md_yaml_drift.py:MdYamlDriftChecker._load_schema:exists": NOT_CONFD,
    "lint/check_md_yaml_drift.py:MdYamlDriftChecker.iter_config_units:rglob": NOT_CONFD,
    "lint/check_md_yaml_drift.py:MdYamlDriftChecker.run_crd:exists": NOT_CONFD,
    "lint/check_md_yaml_drift.py:MdYamlDriftChecker.run_crd:rglob": NOT_CONFD,
    "lint/check_md_yaml_drift.py:MdYamlDriftChecker.run_fences:rglob": NOT_CONFD,
    "lint/check_md_yaml_drift.py:main:exists": NOT_CONFD,
    "lint/check_path_metadata_consistency.py:_main:exists": ROOT,
    "lint/check_path_metadata_consistency.py:_main:is_dir": PR_BETA2,
    "lint/check_path_metadata_consistency.py:_main:startswith": PR_BETA2,
    "lint/check_path_metadata_consistency.py:scan_file:is_file": PR_BETA2,
    "lint/check_retire_drift.py:helm_declared_targets:glob": NOT_CONFD,
    "lint/check_retire_drift.py:helm_declared_targets:is_dir": NOT_CONFD,
    "lint/check_retire_drift.py:main:is_dir": ROOT,
    "lint/check_retire_drift.py:namespace_declared_targets:is_file": NOT_CONFD,
    "lint/check_routing_profiles.py:main:os.path.isdir": ROOT,
    "lint/check_scrape_reachability.py:load_repo_services:rglob": NOT_CONFD,
    "lint/check_threshold_reachability.py:_defaults_writer_census:rglob": NOT_CONFD,
    "lint/check_threshold_reachability.py:_tracked_defaults_artifacts:is_file": NOT_CONFD,
    "lint/check_threshold_registry.py:run_check:glob": NOT_CONFD,
    "lint/check_threshold_registry.py:run_check:os.path.isfile": NOT_CONFD,
    "lint/check_threshold_unit_sanity.py:_collect:os.path.isfile": NOT_CONFD,
    "lint/check_threshold_unit_sanity.py:_declared_units:os.path.isfile": NOT_CONFD,
    "lint/check_threshold_unit_sanity.py:_iter_confd_docs:os.path.isdir": ROOT,
    "lint/check_threshold_unit_sanity.py:_iter_doc_files:os.walk": NOT_CONFD,
    "lint/check_threshold_unit_sanity.py:_iter_yaml_files:os.path.isfile": NOT_CONFD,
    "ops/_grar_parse.py:_apply_tenant_entries:is_reserved_name": ROUTE_GENERATOR,
    "ops/_grar_parse.py:_parse_config_files:is_reserved_name": ROUTE_GENERATOR,
    "ops/_grar_parse.py:_parse_config_files:os.path.isdir": ROOT,
    "ops/_grar_parse.py:_parse_nested_config:is_reserved_name": ROUTE_GENERATOR,
    "ops/_grar_parse.py:_parse_platform_config:startswith": ROUTE_GENERATOR,
    "ops/_grar_render.py:load_base_config:is_file": OPERAND,
    "ops/_grar_validate.py:_find_platform_rules_configmap:is_file": NOT_CONFD,
    "ops/_grar_validate.py:load_policy_document:is_file": NOT_CONFD,
    "ops/_grar_validate.py:validate_tenant_keys:startswith": KEY,
    "ops/_grar_validate.py:writes_nothing:startswith": KEY,
    "ops/_gsvc_child.py:apply_writes:os.path.isdir": OUTPUT,
    "ops/_gsvc_child.py:materialise:startswith": GIT,
    "ops/_observed_map_lib.py:_find_rule_packs_dir:glob": NOT_CONFD,
    "ops/_observed_map_lib.py:_find_rule_packs_dir:is_dir": NOT_CONFD,
    "ops/_observed_map_lib.py:default_pack_paths:glob": NOT_CONFD,
    "ops/_observed_map_lib.py:load_observed_map:os.path.isfile": NOT_CONFD,
    "ops/_render_diff.py:diff_served:startswith": KEY,
    "ops/_render_diff.py:render_routes:exists": OUTPUT,
    "ops/analyze_rule_pack_gaps.py:extract_custom_metrics:startswith": KEY,
    "ops/assemble_config_dir.py:carriers_already_in:is_dir": ROOT_OUTPUT,
    "ops/assemble_config_dir.py:discover_yamls:is_dir": ROOT,
    "ops/assemble_config_dir.py:list_visible_config_entries:has_yaml_extension": WORDING,
    "ops/assemble_config_dir.py:list_visible_config_entries:is_hidden_name": WORDING,
    "ops/assemble_config_dir.py:list_visible_config_entries:iterdir": WORDING,
    "ops/backtest_threshold.py:_unwritable_output:exists": OUTPUT,
    "ops/backtest_threshold.py:_unwritable_output:is_dir": OUTPUT,
    "ops/batch_diagnose.py:discover_tenants:has_yaml_extension": CONFIGMAP,
    "ops/batch_diagnose.py:discover_tenants:is_reserved_name": CONFIGMAP,
    "ops/blast_radius.py:classify_field:startswith": KEY,
    "ops/blind_spot_discovery.py:load_monitored_db_types:is_dir": ROOT,
    "ops/config_diff.py:_read_tree:os.path.isdir": ROOT,
    "ops/config_diff.py:_run:exists": ROOT,
    "ops/config_diff.py:_run:is_dir": ROOT,
    "ops/config_diff.py:served_metric_configs:startswith": KEY,
    "ops/config_diff.py:served_metric_diff:startswith": KEY,
    "ops/config_diff.py:served_setting_configs:startswith": KEY,
    "ops/config_diff.py:served_setting_diff:startswith": KEY,
    "ops/config_history.py:_load_history:exists": NOT_CONFD,
    "ops/config_history.py:_scan_config_dir:has_yaml_extension": PR_BETA2,
    "ops/config_history.py:_scan_config_dir:is_dir": PR_BETA2,
    "ops/config_history.py:_scan_config_dir:is_hidden_name": PR_BETA2,
    "ops/config_history.py:_scan_config_dir:iterdir": PR_BETA2,
    "ops/config_history.py:cmd_diff:exists": OPERAND,
    "ops/da_assembler.py:_rendered_cr_name:is_file": OUTPUT,
    "ops/da_assembler.py:reconcile_one:exists": OUTPUT,
    "ops/da_assembler.py:reconcile_one:is_file": OUTPUT,
    "ops/da_assembler.py:remove_rendered:exists": OUTPUT,
    "ops/da_assembler.py:write_rendered:exists": OUTPUT,
    "ops/deprecate_rule.py:_main:is_dir": ROOT,
    "ops/diagnose.py:resolve_inheritance_chain:is_dir": ROOT,
    "ops/diagnose.py:resolve_inheritance_chain:startswith": KEY,
    "ops/drift_detect.py:compute_dir_manifest:has_yaml_extension": PR_BETA2,
    "ops/drift_detect.py:compute_dir_manifest:is_dir": PR_BETA2,
    "ops/drift_detect.py:compute_dir_manifest:is_hidden_name": PR_BETA2,
    "ops/drift_detect.py:compute_dir_manifest:iterdir": PR_BETA2,
    "ops/drift_detect.py:main:is_dir": ROOT,
    "ops/explain_keys.py:_explain:is_dir": ROOT,
    "ops/explain_route.py:main:is_dir": ROOT,
    "ops/generate_rule_pack_split.py:process_rule_packs:glob": NOT_CONFD,
    "ops/generate_tenant_mapping_rules.py:main:is_dir": ROOT,
    "ops/generate_tenant_mapping_rules.py:main:is_file": NOT_CONFD,
    "ops/generator_service.py:main:os.path.isdir": ROOT,
    "ops/gitops_check.py:check_local:is_dir": ROOT,
    "ops/gitops_check.py:check_local:startswith": KEY,
    "ops/init_project.py:_ConfdTree.__init__:is_dir": ROOT,
    "ops/init_project.py:_case_variants_along:is_dir": OUTPUT,
    "ops/init_project.py:_case_variants_along:os.walk": OUTPUT,
    "ops/init_project.py:_check_existing_init:exists": OUTPUT,
    "ops/init_project.py:_check_existing_init:is_file": OUTPUT,
    "ops/init_project.py:_classify_root_file:exists": OUTPUT,
    "ops/init_project.py:_enclosing_repo_root:exists": NOT_CONFD,
    "ops/init_project.py:_existing_files:is_dir": OUTPUT,
    "ops/init_project.py:_existing_files:is_file": OUTPUT,
    "ops/init_project.py:_existing_files:rglob": OUTPUT,
    "ops/maintenance_scheduler.py:load_recurring_schedules:is_dir": ROOT,
    "ops/maintenance_scheduler.py:main:is_dir": ROOT,
    "ops/migrate_rule.py:load_metric_dictionary:os.path.exists": NOT_CONFD,
    "ops/migrate_to_operator.py:_tree_tenants:is_dir": ROOT,
    "ops/migrate_to_operator.py:parse_configmap_rules:has_yaml_extension": PR_BETA2,
    "ops/migrate_to_operator.py:parse_configmap_rules:is_dir": ROOT,
    "ops/migrate_to_operator.py:parse_configmap_rules:is_file": PR_BETA2,
    "ops/migrate_to_operator.py:parse_configmap_rules:iterdir": PR_BETA2,
    "ops/migration_audit.py:_audit:is_dir": ROOT,
    "ops/notification_tester.py:main:is_dir": ROOT,
    "ops/offboard_tenant.py:find_config_file:is_dir": ROOT,
    "ops/onboard_platform.py:scan_rule_files:glob": NOT_CONFD,
    "ops/onboard_platform.py:scan_rule_files:is_file": NOT_CONFD,
    "ops/operator_generate.py:discover_rule_packs:glob": NOT_CONFD,
    "ops/operator_generate.py:discover_rule_packs:is_dir": NOT_CONFD,
    "ops/operator_generate.py:discover_tenant_configs:is_dir": ROOT,
    "ops/patch_config.py:exporter_reads:has_yaml_extension": CONFIGMAP,
    "ops/patch_config.py:exporter_reads:is_hidden_name": CONFIGMAP,
    "ops/patch_config.py:is_carrier_candidate:has_yaml_extension": CONFIGMAP,
    "ops/patch_config.py:is_carrier_candidate:is_hidden_name": CONFIGMAP,
    "ops/patch_config.py:is_carrier_candidate:is_reserved_name": CONFIGMAP,
    "ops/patch_config.py:verify_pod:startswith": KEY,
    "ops/policy_engine.py:_main:is_dir": ROOT,
    "ops/policy_engine.py:_main:is_file": OPERAND,
    "ops/policy_engine.py:load_policy_inputs:startswith": KEY,
    "ops/policy_engine.py:read_policy_file:is_file": OPERAND,
    "ops/policy_opa_bridge.py:build_opa_input:startswith": KEY,
    "ops/threshold_govern.py:main:os.path.isdir": ROOT,
    "ops/threshold_recommend.py:analyze_tenant:startswith": KEY,
    "ops/threshold_recommend.py:main:is_dir": ROOT,
    "ops/validate_config.py:_custom_alert_compiler:os.path.isdir": NOT_CONFD,
    "ops/validate_config.py:_find_schema:is_file": NOT_CONFD,
    "ops/validate_config.py:_keys_acting_when_merged:startswith": KEY,
    "ops/validate_config.py:_main:os.path.isdir": ROOT,
    "ops/validate_config.py:_policy_dsl_row:os.path.isfile": OPERAND,
    "ops/validate_config.py:_root_decoded_keys:startswith": KEY,
    "ops/validate_config.py:_schedule_null_lines:startswith": KEY,
    "ops/validate_config.py:check_custom_rules:os.path.isdir": NOT_CONFD,
    "ops/validate_config.py:check_json_schema:os.path.isdir": NOT_CONFD,
    "ops/validate_config.py:check_profiles:startswith": KEY,
    "ops/validate_config.py:check_root_defaults:startswith": KEY,
    "ops/validate_config.py:check_versions:is_file": NOT_CONFD,
    "validate_all.py:_compare_baseline:exists": NOT_CONFD,
    "validate_all.py:_drop_isolated_tree:exists": NOT_CONFD,
    "validate_all.py:_drop_isolated_tree:is_dir": NOT_CONFD,
    "validate_all.py:_isolated_tree:is_file": NOT_CONFD,
    "validate_all.py:_snapshot_mtimes:exists": NOT_CONFD,
    "validate_all.py:_snapshot_mtimes:is_file": NOT_CONFD,
    "validate_all.py:_snapshot_mtimes:rglob": NOT_CONFD,
    "validate_all.py:main:exists": NOT_CONFD,
}

#: The reasons an entry may give (see the module docstring).
REASONS = frozenset({ROOT, ROOT_DEFAULT, ROOT_OUTPUT, OUTPUT, OPERAND, NOT_CONFD, LOADER, KEY,
                     PREDICATE, WORDING, CONFIGMAP, GIT, PR_BETA2, ROUTE_GENERATOR})


# ── the gate ─────────────────────────────────────────────────────────

def test_the_population_is_read() -> None:
    """Tripwire: the gate is only as wide as the population it walks."""
    pop = _population()
    assert len(pop) >= 40, [p.name for p in pop]
    names = {p.relative_to(TOOLS).as_posix() for p in pop}
    for must in ("_lib_confd.py", "ops/validate_config.py", "ops/deprecate_rule.py",
                 "ops/init_project.py", "lint/check_retire_drift.py"):
        assert must in names, must


def test_no_python_judgement_outside_the_allowlist() -> None:
    actual = _actual()
    new = {k: actual[k] for k in sorted(set(actual) - set(ALLOWED))}
    stale = sorted(set(ALLOWED) - set(actual))
    assert not new and not stale, (
        "Python judges a conf.d path where the gate does not allow it.\n"
        + "".join(f"  NEW   {k} (line {', '.join(map(str, v))})\n" for k, v in new.items())
        + "".join(f"  STALE {k} — matches nothing now: delete it\n" for k in stale)
        + "Read the entry from da-guard's listing instead (`Entry.role` / `.reserved` / "
          "`.dir_link`, `_lib_parse_tree.tenant_scope`), or — if the path is not a conf.d "
          "entry — add it with one of the module's reasons.")


def test_every_entry_gives_one_of_the_reasons() -> None:
    bad = {k: v for k, v in ALLOWED.items() if v not in REASONS}
    assert not bad, bad
    assert all("\n" not in r and r.strip() for r in REASONS)


#: The reasons that mean "a real judgement of conf.d, not migrated yet".
NOT_MIGRATED = frozenset({PR_BETA2, CONFIGMAP, GIT, ROUTE_GENERATOR})


def _named_in_docs(key: str) -> str:
    """How ADR-036's step 9 progress line names a not-yet-migrated entry: a
    function of the shared library by its own name, any other tool by its
    module name."""
    rel, func, _call = key.split(":")
    return func if rel == "_lib_confd.py" else pathlib.PurePosixPath(rel).stem


@pytest.mark.parametrize("adr", ["036-single-parser-effective-config.md",
                                 "036-single-parser-effective-config.en.md"])
def test_the_adr_names_exactly_what_is_not_migrated(adr) -> None:
    """m5: ADR-036 §7's progress line for step 9 lists the Python judgements
    still left — and that list IS this allowlist's not-migrated entries, in
    both directions: an entry the ADR does not name is a judgement nobody was
    told about; a module the ADR names that has none left is a stale claim."""
    text = (REPO / "docs" / "adr" / adr).read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines()
                if "test_confd_tools_no_python_judgement.py" in ln and "`config_history`" in ln)
    want = {_named_in_docs(k) for k, why in ALLOWED.items() if why in NOT_MIGRATED}
    named = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", line))
    missing = sorted(want - named)
    assert not missing, f"{adr}: not-migrated entries the ADR does not name: {missing}"
    candidates = {pathlib.PurePosixPath(k.split(":")[0]).stem for k in ALLOWED} | {
        k.split(":")[1] for k in ALLOWED if k.startswith("_lib_confd.py:")}
    stale = sorted((named & candidates) - want - {"_lib_confd"})
    assert not stale, f"{adr}: named as not migrated, but nothing of theirs is left: {stale}"


# ── the detector catches each shape (synthetic, no repo file) ────────

_SHAPES = {
    "startswith": "def f(name):\n    return name.startswith('_')\n",
    "startswith-tuple": "def f(p):\n    return p.name.startswith(('.', '~'))\n",
    "is_file": "def f(p):\n    return p.is_file()\n",
    "is_dir": "def f(p):\n    return p.is_dir()\n",
    "exists": "def f(p):\n    return p.exists()\n",
    "iterdir": "def f(p):\n    return list(p.iterdir())\n",
    "rglob": "def f(p):\n    return list(p.rglob('*'))\n",
    "glob": "def f(p):\n    return list(p.glob('*.yaml'))\n",
    "os.path.isfile": "import os\ndef f(p):\n    return os.path.isfile(p)\n",
    "os.path.isdir-alias": "import os.path as osp\ndef f(p):\n    return osp.isdir(p)\n",
    "os.path.exists-from": "from os.path import exists\ndef f(p):\n    return exists(p)\n",
    "os.walk": "import os\ndef f(p):\n    return list(os.walk(p))\n",
    "os.walk-from": "from os import walk as w\ndef f(p):\n    return list(w(p))\n",
    "has_yaml_extension": "def f(n):\n    return has_yaml_extension(n)\n",
    "is_reserved_name": "import _lib_confd\ndef f(n):\n    return _lib_confd.is_reserved_name(n)\n",
    "is_hidden_name": "def f(n):\n    return is_hidden_name(n)\n",
    "is_dir_symlink": "def f(p):\n    return is_dir_symlink(p)\n",
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_each_forbidden_shape_is_caught(shape) -> None:
    found = judgements(ast.parse(_SHAPES[shape]))
    assert list(found) and all(k.startswith("f:") for k in found), (shape, found)


def test_the_key_names_the_enclosing_function_and_the_call() -> None:
    src = textwrap.dedent("""
        class C:
            def m(self, e):
                def inner(n):
                    return n.startswith("_")
                return e.reserved
        x = __import__("pathlib").Path(".").exists()
    """)
    assert judgements(ast.parse(src)) == {"C.m.inner:startswith": [5], "<module>:exists": [7]}


@pytest.mark.parametrize("src", [
    "def f(e):\n    return e.reserved or e.dir_link or e.config_file\n",
    "def f(name):\n    return name.startswith('team/')\n",
    "def f(k):\n    return k.endswith('_')\n",
])
def test_reading_the_entry_is_not_flagged(src) -> None:
    assert judgements(ast.parse(src)) == {}


# ── the tenant set is one answer (#3206 M-A) ─────────────────────────

#: Who may take the tenant files WITHOUT the entries that leave the set
#: unknown (`_lib_parse_tree.TenantScope`), and why. Every other reader of
#: "which tenants does this tree declare" takes `tenant_scope` /
#: `declared_tenants` (both halves).
HALF_SCOPE_CALLERS = {
    "ops/patch_config.py": "a ConfigMap's keys: no directory, link or special file can exist",
    "ops/gitops_check.py": "reads every selected entry itself and names each one it cannot read",
    "lint/check_path_metadata_consistency.py": "names every unreadable entry from the same "
                                               "document (its own `_` rule: PR_BETA2 above)",
}


def test_only_the_named_readers_take_half_the_tenant_scope() -> None:
    callers: "dict[str, set[str]]" = {}
    for p in sorted(TOOLS.rglob("*.py")):
        tree = parse(p)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in ("tenant_files", "tenant_file_paths"):
                    callers.setdefault(name, set()).add(p.relative_to(TOOLS).as_posix())
    assert callers.get("tenant_files", set()) == set(HALF_SCOPE_CALLERS), callers
    assert callers.get("tenant_file_paths", set()) == {"_lib_parse_tree.py",
                                                        "_lib_parse_cache.py"}, callers
