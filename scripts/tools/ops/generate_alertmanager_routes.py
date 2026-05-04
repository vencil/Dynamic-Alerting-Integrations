#!/usr/bin/env python3
"""
generate_alertmanager_routes.py — Generate Alertmanager route + receiver + inhibit config from tenant YAML.

Reads all tenant YAML files from conf.d/, extracts _routing and _severity_dedup sections,
and produces an Alertmanager route tree + receivers + inhibit_rules YAML fragment.

Severity Dedup (per-tenant):
  Default (absent or "enable"): generate inhibit_rule that suppresses warning when critical fires
  "disable": skip inhibit_rule — both warning and critical notifications are sent
  Mechanism: per-tenant inhibit_rules with tenant="<name>" + metric_group matchers

v2.0.0 Bilingual Templates (i18n):
  Rule Packs can include Chinese annotations: summary_zh, description_zh, platform_summary_zh
  Alertmanager templates use fallback logic to prefer Chinese if available:
    Example: {{ or .CommonAnnotations.summary_zh .CommonAnnotations.summary }}
  Receiver templates (email, webhook, slack, teams, pagerduty) use this pattern automatically.
  No changes to route generator needed — the fallback pattern is in Alertmanager's global templates.

Usage:
  python3 scripts/tools/generate_alertmanager_routes.py --config-dir conf.d/
  python3 scripts/tools/generate_alertmanager_routes.py --config-dir conf.d/ -o alertmanager-routes.yaml
  python3 scripts/tools/generate_alertmanager_routes.py --config-dir conf.d/ --dry-run
  python3 scripts/tools/generate_alertmanager_routes.py --config-dir conf.d/ --output-configmap -o am-configmap.yaml

v2.8.0 PR-3a: This file is now a CLI facade. The 1645-line monolith was
split into 5 helper modules (_grar_validate / _grar_merge / _grar_parse /
_grar_routes / _grar_render) for testability and to break the god-file
pattern. All public + private symbols are re-exported below so existing
test imports keep working unchanged.
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout

# ── Re-exports from _lib_python (kept for test backward-compat) ─────
from _lib_python import (  # noqa: E402, F401
    write_text_secure,
    PLATFORM_DEFAULTS,
)

# ── Re-exports from _grar_validate ─────────────────────────────────
from _grar_validate import (  # noqa: E402, F401
    _extract_host,
    _validate_profile_refs,
    check_domain_policies,
    load_policy,
    validate_receiver_domains,
    validate_tenant_keys,
)

# ── Re-exports from _grar_merge ────────────────────────────────────
from _grar_merge import (  # noqa: E402, F401
    _apply_timing_params,
    _contains_tenant_placeholder,
    _substitute_tenant,
    build_receiver_config,
    merge_routing_with_defaults,
)

# ── Re-exports from _grar_parse ────────────────────────────────────
from _grar_parse import (  # noqa: E402, F401
    _merge_tenant_routing,
    _parse_config_files,
    _parse_platform_config,
    _parse_tenant_overrides,
    load_tenant_configs,
)

# ── Re-exports from _grar_routes ───────────────────────────────────
from _grar_routes import (  # noqa: E402, F401
    _build_enforced_routes,
    _build_inhibit_rules,
    _build_override_matchers,
    _build_override_route,
    _build_per_tenant_enforced_route,
    _build_single_enforced_route,
    _build_tenant_routes,
    _process_override_receiver,
    _validate_override_matcher,
    expand_routing_overrides,
    generate_inhibit_rules,
    generate_routes,
)

# ── Re-exports from _grar_render ───────────────────────────────────
from _grar_render import (  # noqa: E402, F401
    _apply_merged_configmap,
    _merge_routes_receivers_inhibits,
    _read_existing_configmap,
    _reload_alertmanager,
    apply_to_configmap,
    assemble_configmap,
    load_base_config,
    render_output,
)


# ============================================================
# CLI Mode Handlers (--validate, --apply, --output-configmap, default render)
# ============================================================

def _validate_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                   all_warnings: list[str]) -> None:
    """Handle --validate mode: check for errors and exit."""
    errors = [w for w in all_warnings if "WARN" in w and "skipping" in w]
    route_count = len(routes)
    inhibit_count = len(inhibit_rules)
    print(f"Validation: {route_count} route(s), {len(receivers)} receiver(s), "
          f"{inhibit_count} inhibit rule(s)")
    if errors:
        print(f"FAIL: {len(errors)} error(s) found:", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)
    print("OK: all configs valid")
    sys.exit(0)


def _apply_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                namespace: str, configmap_name: str, yes_flag: bool) -> None:
    """Handle --apply mode: merge into ConfigMap and reload."""
    route_count = len(routes)
    inhibit_count = len(inhibit_rules)
    print(f"\nApply: {route_count} route(s), {len(receivers)} receiver(s), "
          f"{inhibit_count} inhibit rule(s)")
    print(f"Target: {namespace}/{configmap_name}")
    if not yes_flag:
        confirm = input("Proceed? [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)
    success = apply_to_configmap(routes, receivers, inhibit_rules, namespace, configmap_name)
    sys.exit(0 if success else 1)


def _output_configmap_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                           base_config: str | None, namespace: str, configmap_name: str,
                           dry_run: bool, output: str | None) -> None:
    """Handle --output-configmap mode: produce complete ConfigMap YAML."""
    base = load_base_config(base_config)
    cm_yaml = assemble_configmap(
        base, routes, receivers, inhibit_rules,
        namespace=namespace, configmap_name=configmap_name)

    route_count = len(routes)
    inhibit_count = len(inhibit_rules)

    if dry_run:
        print("\n--- DRY RUN: ConfigMap YAML ---")
        print(cm_yaml)
        print(f"\n--- {route_count} route(s), {len(receivers)} receiver(s), "
              f"{inhibit_count} inhibit rule(s) ---")
        return

    if output:
        write_text_secure(output, cm_yaml)
        print(f"Written to {output} ({route_count} routes, "
              f"{len(receivers)} receivers, {inhibit_count} inhibit rules)")
    else:
        print(cm_yaml)


def _render_output_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                       dry_run: bool, output: str | None) -> None:
    """Handle default render mode: output routes/receivers fragment."""
    header = (
        "# ============================================================\n"
        "# Alertmanager Route + Receiver + Inhibit Rules Fragment\n"
        "# Generated by: generate_alertmanager_routes.py\n"
        "# Merge into your Alertmanager config:\n"
        "#   - route.routes: append the routes below\n"
        "#   - receivers: append the receivers below\n"
        "#   - inhibit_rules: append the severity dedup inhibit rules below\n"
        "# ============================================================\n"
    )
    body = render_output(routes, receivers, inhibit_rules)
    content = header + body

    route_count = len(routes)
    inhibit_count = len(inhibit_rules)

    if dry_run:
        print("\n--- DRY RUN OUTPUT ---")
        print(content)
        print(f"\n--- {route_count} route(s), {len(receivers)} receiver(s), "
              f"{inhibit_count} inhibit rule(s) ---")
        return

    if output:
        write_text_secure(output, content)
        print(f"Written to {output} ({route_count} routes, {len(receivers)} receivers, "
              f"{inhibit_count} inhibit rules)")
    else:
        print(content)


def _print_config_summary(routing_configs: dict, dedup_configs: dict, enforced_routing: dict | None) -> None:
    """Print summary of loaded configs."""
    if enforced_routing:
        print("Platform enforced routing: ENABLED")
    if routing_configs:
        print(f"Found {len(routing_configs)} tenant(s) with routing config: "
              f"{', '.join(sorted(routing_configs.keys()))}")
    print(f"Found {len(dedup_configs)} tenant(s) for severity dedup: "
          f"{', '.join(sorted(dedup_configs.keys()))}")


def main() -> None:
    """CLI entry point: Generate Alertmanager route + receiver + inhibit config from tenant YAML."""
    parser = argparse.ArgumentParser(
        description="Generate Alertmanager route + receiver config from tenant YAML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              %(prog)s --config-dir components/threshold-exporter/config/conf.d/
              %(prog)s --config-dir conf.d/ -o alertmanager-routes.yaml
              %(prog)s --config-dir conf.d/ --dry-run
        """),
    )
    parser.add_argument("--config-dir", required=True,
                        help="Directory containing tenant YAML configs (conf.d/)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output file path (default: stdout)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview output without writing file")
    parser.add_argument("--validate", action="store_true",
                        help="Validate generated config (exit 0 if valid, 1 if errors)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--apply", action="store_true",
                            help="Apply: merge into Alertmanager ConfigMap + reload")
    mode_group.add_argument("--output-configmap", action="store_true",
                            help="Output complete Alertmanager ConfigMap YAML (for GitOps PR flow)")
    parser.add_argument("--base-config", default=None,
                        help="Base Alertmanager YAML for --output-configmap (global + defaults)")
    parser.add_argument("--namespace", default="monitoring",
                        help="K8s namespace for --apply/--output-configmap (default: monitoring)")
    parser.add_argument("--configmap", default="alertmanager-config",
                        help="ConfigMap name for --apply/--output-configmap (default: alertmanager-config)")
    parser.add_argument("--policy", default=None,
                        help="Policy YAML with allowed_domains for webhook URL validation")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt for --apply")

    args = parser.parse_args()

    # Load policy (webhook domain allowlist)
    allowed_domains = load_policy(args.policy)
    if allowed_domains:
        print(f"Policy: {len(allowed_domains)} allowed domain pattern(s) loaded")

    # Load tenant configs (routing + dedup + schema warnings + enforced routing + metadata)
    routing_configs, dedup_configs, schema_warnings, enforced_routing, metadata_configs = \
        load_tenant_configs(args.config_dir)

    has_routing = bool(routing_configs)
    has_dedup = bool(dedup_configs)

    if not has_routing and not has_dedup and not enforced_routing:
        print("No tenants found in config directory.")
        sys.exit(0)

    _print_config_summary(routing_configs, dedup_configs, enforced_routing)

    # Generate routes + receivers (enforced route inserted first)
    routes, receivers, route_warnings = generate_routes(
        routing_configs, allowed_domains=allowed_domains,
        enforced_routing=enforced_routing)

    # Generate per-tenant severity dedup inhibit rules
    inhibit_rules, dedup_warnings = generate_inhibit_rules(dedup_configs)

    # Collect all warnings
    all_warnings = schema_warnings + route_warnings + dedup_warnings
    for w in all_warnings:
        print(w, file=sys.stderr)

    if not routes and not inhibit_rules:
        print("No valid routes or inhibit rules generated.")
        sys.exit(1)

    # Validate mode
    if args.validate:
        _validate_mode(routes, receivers, inhibit_rules, all_warnings)

    # Apply mode
    if args.apply:
        _apply_mode(routes, receivers, inhibit_rules, args.namespace,
                    args.configmap, args.yes)

    # Output-configmap mode
    if args.output_configmap:
        _output_configmap_mode(routes, receivers, inhibit_rules, args.base_config,
                              args.namespace, args.configmap, args.dry_run, args.output)
        return

    # Default render mode
    _render_output_mode(routes, receivers, inhibit_rules, args.dry_run, args.output)


if __name__ == "__main__":
    main()
