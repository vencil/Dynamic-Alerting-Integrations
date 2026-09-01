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
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_python import (  # noqa: E402, F401
    write_text_secure,
    PLATFORM_DEFAULTS,
)
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, die_caller_error)

# ── Re-exports from _grar_validate ─────────────────────────────────
from _grar_validate import (  # noqa: E402, F401
    POLICY_ERROR_PREFIX,
    PolicyInputError,
    _extract_host,
    _validate_profile_refs,
    assert_equal_labels_gated,
    assert_platform_alerts_not_tenant_silenceable,
    assert_watchdog_inhibit_immunity,
    check_domain_policies,
    find_tenant_silenceable_platform_inhibits,
    find_ungated_equal_label_inhibits,
    find_watchdog_suppressing_inhibits,
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
    _build_custom_alert_routes,
    _build_watchdog_route,
    _build_synthetic_probe_route,
    _build_sentinel_sinkhole_route,
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
    BaseConfigInputError,
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

def _policy_errors(all_warnings: list[str]) -> list[str]:
    """Extract blocking domain-policy ERROR lines (ADR-007 --strict).

    Only the strict domain-policy paths (check_domain_policies(strict=True)
    plus the fail-open closures in load_tenant_configs) emit
    POLICY_ERROR_PREFIX lines into the warning stream; everything else
    there is WARN-prefixed (pinned by TestPolicyErrorPrefixPin).
    """
    return [w for w in all_warnings
            if w.lstrip().startswith(POLICY_ERROR_PREFIX)]


def _validate_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                   all_warnings: list[str]) -> None:
    """Handle --validate mode: check for errors and exit."""
    # Legacy fail category: config entries that were skipped as unusable.
    errors = [w for w in all_warnings if "WARN" in w and "skipping" in w]
    # ADR-007 --strict: domain-policy violations escalated to ERROR are
    # blocking. (Without --strict these surface as WARN and never fail.)
    errors.extend(_policy_errors(all_warnings))
    # ADR-025 D1 regression tripwire: a generated inhibit rule must never target
    # the Watchdog heartbeat. (The full base+generated set is enforced fail-closed
    # at the render paths; this catches a generator-side regression early.)
    for idx, _rule in find_watchdog_suppressing_inhibits(inhibit_rules):
        errors.append(f"  WARN: generated inhibit_rules[{idx}] would suppress the "
                      "Watchdog heartbeat (ADR-025) — skipping forbidden rule")
    # Same early tripwire for the tenant-cannot-silence-platform invariant.
    for idx, _rule, _lbls in find_tenant_silenceable_platform_inhibits(inhibit_rules):
        errors.append(f"  WARN: generated inhibit_rules[{idx}] is tenant-triggered "
                      f"and would suppress platform alert {_lbls.get('alertname')} "
                      "— skipping forbidden rule")
    route_count = len(routes)
    inhibit_count = len(inhibit_rules)
    print(f"Validation: {route_count} route(s), {len(receivers)} receiver(s), "
          f"{inhibit_count} inhibit rule(s)")
    if errors:
        print(f"FAIL: {len(errors)} error(s) found:", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(EXIT_VIOLATION)
    print("OK: all configs valid")
    sys.exit(EXIT_OK)


def _write_output_or_die(output: str, content: str, label: str) -> None:
    """Write *content* to the operator-supplied ``-o`` path, or exit 2.

    A bare ``write_text_secure`` lets OSError escape as a traceback at rc=1,
    and rc=1 in this repo is EXIT_VIOLATION — "your CONFIG is wrong" — for what
    is a mistyped path.

    ⚠️ NOT THE WHOLE CLASS. Most ``write_text_secure`` call sites across
    ``scripts/**`` still have no enclosing handler that can catch OSError, and
    most of the owning tools take their output path from argv. This closes the
    sites in THIS tool only; the class, and the argument that the fix belongs
    in the shared helper rather than at each call site, is #1641.
    ⛔ Do not read this helper's existence as the class having been handled —
    and do not quote a count from here: it was already wrong once, because this
    very change moved it.
    """
    try:
        write_text_secure(output, content)
    except OSError as exc:
        die_caller_error(
            f"-o: cannot write {label} to {output}: {exc}\n"
            "  ⛔ This is an OUTPUT PATH problem, not a routing violation. "
            "Check the parent directory exists and is writable; do not read "
            "this as 'the generated routes are invalid'.")


def _apply_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                namespace: str, configmap_name: str, yes_flag: bool, strict: bool = False) -> None:
    """Handle --apply mode: merge into ConfigMap and reload."""
    route_count = len(routes)
    inhibit_count = len(inhibit_rules)
    print(f"\nApply: {route_count} route(s), {len(receivers)} receiver(s), "
          f"{inhibit_count} inhibit rule(s)")
    print(f"Target: {namespace}/{configmap_name}")
    if not yes_flag:
        # #1617: `input()` with no readable stdin raises EOFError, uncaught,
        # rc=1 — and it happens BEFORE anything touches the cluster, so the
        # traceback reads as "cluster unreachable". A subprocess with no stdin
        # to read lands here, which is what CI and cron look like.
        #
        # ⛔ The predicate is "reading a line failed", not `sys.stdin.isatty()`
        # (which is what #1617 proposed). Measured across non-interactive
        # shapes: `input()` fails in all of them, while `isatty()` reports True
        # — i.e. would NOT have fired — for the NUL/`/dev/null` device on this
        # host.
        #
        # ⛔ And "reading failed" is more than one exception. MEASURED: with
        # stdin fully CLOSED (`0<&-`, which is what a daemon or a service
        # manager hands a child) `sys.stdin is None` and `input()` raises
        # RuntimeError, not EOFError — so an EOFError-only handler left exactly
        # the traceback-at-rc-1 that #1617 exists to remove. Catch the failure
        # by its effect, not by one of its spellings, and put the type in the
        # message so the next unanticipated one is still diagnosable.
        try:
            confirm = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, RuntimeError, OSError) as exc:
            die_caller_error(
                f"\n--apply needs an interactive confirmation, but stdin could "
                f"not be read ({type(exc).__name__}: {exc}). CI, cron, a pipe, "
                f"a redirect, or a closed stdin all land here.\n"
                "  Pass --yes to confirm non-interactively.\n"
                "  ⛔ Do not drop --apply to clear this — that stops applying "
                "anything to the cluster, which is not what you asked for.")
        if confirm not in ("y", "yes"):
            print("Aborted.")
            sys.exit(EXIT_OK)
    success = apply_to_configmap(routes, receivers, inhibit_rules, namespace, configmap_name, strict=strict)
    # #1617: this was EXIT_VIOLATION (1) while docs/cli-reference.{md,en.md}
    # documented 2 — the code and the shipped table said opposite things.
    # `_lib_exitcodes` settles it: "cannot reach Prometheus / API" is
    # EXIT_CALLER_ERROR. Nothing that makes `apply_to_configmap` return False
    # is a CONFIG violation — the failures are the cluster being unreachable
    # or its ConfigMap being unusable — so the CODE was wrong, not the table.
    # ⛔ Two earlier versions of this comment were more specific and wrong.
    # It is NOT true that `/-/reload` is one of those paths
    # (`_reload_alertmanager` returns True on both branches; a failed reload
    # is deliberately warning-level, #1243), and it is NOT true that every
    # such path is a kubectl invocation failing: with kubectl returning 0,
    # a ConfigMap missing `alertmanager.yml` — or holding a value that is
    # not a mapping — also returns False. Measured; each of those now emits
    # a diagnostic (see `_read_existing_configmap`).
    sys.exit(EXIT_OK if success else EXIT_CALLER_ERROR)


def _output_configmap_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                           base: dict, namespace: str, configmap_name: str,
                           dry_run: bool, output: str | None, strict: bool = False) -> None:
    """Handle --output-configmap mode: produce complete ConfigMap YAML.

    Takes the ALREADY-LOADED base config, not a path: #1616 requires the
    supplied-but-unusable check to happen before the tenant scan, and a
    parameter that cannot be a path is the structural way to keep it there.
    """
    cm_yaml = assemble_configmap(
        base, routes, receivers, inhibit_rules,
        namespace=namespace, configmap_name=configmap_name, strict=strict)

    route_count = len(routes)
    inhibit_count = len(inhibit_rules)

    if dry_run:
        print("\n--- DRY RUN: ConfigMap YAML ---")
        print(cm_yaml)
        print(f"\n--- {route_count} route(s), {len(receivers)} receiver(s), "
              f"{inhibit_count} inhibit rule(s) ---")
        return

    if output:
        _write_output_or_die(output, cm_yaml, "the ConfigMap")
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
        _write_output_or_die(output, content, "the routing fragment")
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
              f"{safe_label(', '.join(sorted(routing_configs.keys())))}")
    print(f"Found {len(dedup_configs)} tenant(s) for severity dedup: "
          f"{safe_label(', '.join(sorted(dedup_configs.keys())))}")


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
    parser.add_argument("--strict", action="store_true",
                        help="Escalate to ERROR and fail (exit 1): domain-policy "
                             "violations (ADR-007) and, on the --apply/--output-configmap "
                             "merge, any inhibit rule with an ungated `equal:` label "
                             "(#1132). Without --strict these surface as WARN. CI runs "
                             "--strict.")
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

    # #1616, third CARRIER-OF-THE-FLAG (the sixth carrier of the #1556 class):
    # --base-config is read ONLY on the ConfigMap-assembly path.
    # In every other mode it was accepted and then never looked at — measured:
    # a VALID base config in plain render mode produced output byte-identical
    # to omitting the flag, with no warning. "Supplied but not honoured" is the
    # same class as "supplied but unusable"; the operator believes their
    # `global:` is in play and it is not. This must be checked BEFORE any work
    # so the failure is about the invocation, not about a half-finished run.
    # The base config is read on exactly one path — the one that assembles the
    # ConfigMap — so it is honoured iff this run reaches that path. `--validate`
    # returns from `_validate_mode()` before it, which is why the predicate is
    # not simply "did you pass --output-configmap".
    #
    # ⛔ The remedy has to be derived from the same predicate, not written once
    # for every mode. MEASURED: an earlier version told every non-configmap
    # mode to "Add --output-configmap". Under `--apply` that flag is forbidden
    # by argparse; under `--validate` it is ACCEPTED and the run is still
    # byte-identical to omitting `--base-config` — i.e. following the advice
    # silently reproduces #1616 instead of fixing it. A message that routes the
    # operator into the defect is worse than no message.
    honours_base_config = args.output_configmap and not args.validate
    if args.base_config is not None and not honours_base_config:
        if args.apply:
            why, remedy = (
                "--apply merges into the ConfigMap already in the cluster, so "
                "a base file has no effect",
                "  Drop --base-config. (--output-configmap, which does read "
                "it, cannot be combined with --apply.)")
        elif args.validate:
            why, remedy = (
                "--validate returns before the ConfigMap is assembled, so the "
                "base file is never read — adding --output-configmap does NOT "
                "change that",
                "  Drop --base-config, or drop --validate if you meant to "
                "produce a ConfigMap.")
        else:
            why, remedy = (
                "only --output-configmap assembles a ConfigMap, and that is "
                "the only thing that reads a base file",
                "  Add --output-configmap, or drop --base-config.\n"
                "  ⛔ Dropping it is only correct if you did not mean to "
                "supply a base config — it does NOT make this mode honour one.")
        die_caller_error(f"--base-config is not read in this mode: {why}.\n{remedy}")

    # Load policy (webhook domain allowlist).
    # #1556: a supplied-but-unusable --policy is a caller error, not "no
    # policy". Exiting 2 here rather than proceeding with an empty allowlist
    # is the whole point — the previous behaviour generated routes with SSRF
    # domain checking off and said nothing.
    try:
        allowed_domains = load_policy(args.policy)
    except PolicyInputError as exc:
        die_caller_error(str(exc))
    if allowed_domains:
        print(f"Policy: {len(allowed_domains)} allowed domain pattern(s) loaded")

    # #1616: load the base config HERE, not inside _output_configmap_mode.
    # ⛔ Measured with the load left at its original site (after the tenant
    # scan): a typo'd --base-config against a tree that yields no routes still
    # exited 0 in silence, because main() returns at "No tenants found in
    # config directory." before anything looks at the flag. The original defect
    # survived the fix in a narrower shape, and it was a NEW test that caught
    # it, not review. "Supplied but unusable" is a property of the INVOCATION,
    # so it has to be decided before any work — exactly like --policy above.
    base_config: dict | None = None
    if args.output_configmap:
        try:
            base_config = load_base_config(args.base_config)
        except BaseConfigInputError as exc:
            die_caller_error(str(exc))

    # Load tenant configs (routing + dedup + schema warnings + enforced routing + metadata)
    routing_configs, dedup_configs, schema_warnings, enforced_routing, metadata_configs = \
        load_tenant_configs(args.config_dir, strict_policies=args.strict)

    has_routing = bool(routing_configs)
    has_dedup = bool(dedup_configs)

    if not has_routing and not has_dedup and not enforced_routing:
        print("No tenants found in config directory.")
        sys.exit(EXIT_OK)

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
        # #1538: escape here, not in `all_warnings` — the same list is handed
        # to --validate / --json consumers, which must stay raw.
        print(safe_label(w), file=sys.stderr)

    if not routes and not inhibit_rules:
        print("No valid routes or inhibit rules generated.")
        sys.exit(EXIT_VIOLATION)

    # Validate mode
    if args.validate:
        _validate_mode(routes, receivers, inhibit_rules, all_warnings)

    # ADR-007 --strict outside --validate: abort before rendering/applying a
    # config that violates a domain policy ("--strict 模式：報錯終止").
    if args.strict:
        policy_errors = _policy_errors(all_warnings)
        if policy_errors:
            print(f"FAIL: {len(policy_errors)} domain-policy violation(s) "
                  "under --strict:", file=sys.stderr)
            for e in policy_errors:
                print(e, file=sys.stderr)
            sys.exit(EXIT_VIOLATION)

    # Apply mode
    if args.apply:
        _apply_mode(routes, receivers, inhibit_rules, args.namespace,
                    args.configmap, args.yes, strict=args.strict)

    # Output-configmap mode
    if args.output_configmap:
        _output_configmap_mode(routes, receivers, inhibit_rules, base_config,
                              args.namespace, args.configmap, args.dry_run, args.output,
                              strict=args.strict)
        return

    # Default render mode
    _render_output_mode(routes, receivers, inhibit_rules, args.dry_run, args.output)


if __name__ == "__main__":
    main()
