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
  python3 scripts/tools/ops/generate_alertmanager_routes.py --config-dir conf.d/
  python3 scripts/tools/ops/generate_alertmanager_routes.py --config-dir conf.d/ -o alertmanager-routes.yaml
  python3 scripts/tools/ops/generate_alertmanager_routes.py --config-dir conf.d/ --dry-run
  python3 scripts/tools/ops/generate_alertmanager_routes.py --config-dir conf.d/ --output-configmap -o am-configmap.yaml

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
# ⛔ Imported here and NOT added to the `_lib_python` block below: that block
# carries `# noqa: F401` because everything in it is a re-export kept for
# test backward-compat, and `test_grar_facade_reexports` pins that set. These
# two are USED, not re-exported.
from _lib_io import (  # noqa: E402  (#1538 output-layer escaping, #1789)
    safe_label, write_text_or_die,
)
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
    TenantTree,  # #1460 file accounting travels with the tree
    _merge_tenant_routing,
    _parse_config_files,
    _parse_platform_config,
    _parse_tenant_overrides,
    load_tenant_configs,
    load_tenant_tree,
)

# Used here, not re-exported: which #2326 routing-tree findings refuse the
# tree (`tree_refusal`, the same filter as `TenantTree.routing_tree_errors`).
from _grar_parse import BLOCKING_TREE_KINDS  # noqa: E402

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
    expand_routing_routes,
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
# #2219: used here, not re-exported — hence no `# noqa: F401` (see the
# `_lib_io` block above for why that marker must stay off used imports).
from _grar_render import (  # noqa: E402
    AlertmanagerConfigRejected, amtool_gate,
)
# #2311: the whole --validate verdict, shared with validate-config's routes
# row (it calls `gen.evaluate_generated_config`). Used, so no F401 marker.
from _grar_render import evaluate_generated_config  # noqa: E402
from _grar_render import (  # noqa: E402, F401  (read as `gen.…` by validate_config)
    AMTOOL_ACCEPTED, AMTOOL_NOT_FOUND, AMTOOL_REJECTED, AMTOOL_UNUSABLE,
)
# Re-exported only (tests and callers read it as `gar.…`); the --validate
# wording moved into _grar_render with the verdict it belongs to (#2311).
from _grar_render import VALIDATE_AMTOOL_NOT_FOUND_NOTICE  # noqa: E402, F401
# #2279: the shared "is this generation result unusable" predicate.
# validate_config reads it as `gen.blocking_generation_errors`; --validate now
# reaches it through `evaluate_generated_config` (#2311), so here it is a
# re-export and carries the F401 marker.
from _grar_validate import blocking_generation_errors  # noqa: E402, F401
from _grar_validate import is_receiver_name_collision  # noqa: E402
# #2315: the duplicate-tenant refusal, shared with validate-config's
# tenant_uniqueness row through `_lib_confd.tenant_declarations`.
from _grar_validate import duplicate_tenant_errors  # noqa: E402
import yaml  # noqa: E402


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


def _assembly_failed(exc: ValueError, refusing: str) -> None:
    """A platform invariant refused the assembled config (#2260 / #2279).

    ``assemble_configmap`` raises ``ValueError`` for a CONFIG that must not
    ship — a base receiver shadowing a generated one, an inhibit rule that
    would silence a platform alert, … That is a verdict on the config (rc 1),
    and it must read as one: before this it escaped as a traceback, which
    reads as the tool having crashed.
    """
    print(f"FAIL: the assembled Alertmanager config was refused — {refusing}:"
          f"\n  {safe_label(str(exc))}", file=sys.stderr)
    sys.exit(EXIT_VIOLATION)


def _validate_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                   all_warnings: list[str]) -> None:
    """Handle --validate mode: check for errors and exit.

    #2311: every verdict comes from ``evaluate_generated_config`` — the SAME
    function validate-config's ``routes`` row calls, so the two cannot drift
    apart (#2164 was two copies of one predicate doing exactly that). This
    function only prints and exits; it decides nothing. The one input that
    stays here is ``--strict``: its ADR-007 policy errors are blocking only
    for this CLI's flag, and are handed in as ``extra_errors``.
    """
    verdict = evaluate_generated_config(
        routes, receivers, inhibit_rules, all_warnings,
        extra_errors=_policy_errors(all_warnings))
    # Non-blocking lines the checks' helpers used to print directly (an
    # un-gated equal-label, a degraded probe set): same stream, same text.
    for w in verdict.warnings:
        print(w, file=sys.stderr)
    route_count = len(routes)
    inhibit_count = len(inhibit_rules)
    print(f"Validation: {route_count} route(s), {len(receivers)} receiver(s), "
          f"{inhibit_count} inhibit rule(s)")
    if verdict.errors:
        print(f"FAIL: {len(verdict.errors)} error(s) found:", file=sys.stderr)
        for e in verdict.errors:
            print(e, file=sys.stderr)
        sys.exit(EXIT_VIOLATION)
    if verdict.assembly_error is not None:
        _assembly_failed(ValueError(verdict.assembly_error),
                         "validation cannot pass")
    # #2260: Alertmanager's own parser, same verdicts as the write paths
    # (#2219): rejected → rc 1, amtool unusable → rc 2, amtool absent → a
    # NOTICE and the rc is unchanged.
    print(verdict.amtool.message, file=sys.stderr)
    if verdict.exit_code is not None:
        sys.exit(verdict.exit_code)
    print("OK: all configs valid")
    sys.exit(EXIT_OK)


def _write_output_or_die(output: str, content: str) -> None:
    """Write *content* to the operator-supplied ``-o`` path, or exit 2.

    A bare ``write_text_secure`` lets OSError escape as a traceback at rc=1,
    and rc=1 in this repo is EXIT_VIOLATION — "your CONFIG is wrong" — for what
    is a mistyped path.

    #1789: the rc was already 2 here, but the message was hand-written and
    said the WRONG thing — it printed two lines, and because the flag was
    never declared at the writer, ``OutputWriteError`` filled in "internal
    output path, this is a bug or an unwritable workspace" for a path the
    operator had typed on the command line (measured). Declaring
    ``flag="-o/--output"`` and going through the SHARED writer makes it the
    same single line every other tool in the batch prints, and puts this tool
    into the population of ``tests/shared/test_output_path_write_failure.py``
    — which is what a hand-written message here could never join.

    ⚠️ ``write_text_or_die`` rather than ``write_text_secure`` + a local
    ``except OutputWriteError: _die_on_write_error(...)``. They do the same
    two things in the same order — that helper IS that pair — and this spells
    it with the PUBLIC name instead of reaching past the underscore into
    ``_lib_io``'s internals.

    ⚠️ The exit still happens HERE rather than at a
    ``@exit_on_output_write_error`` on ``main``. Measured: with nothing at
    this call site, ``test_write_failure_class`` goes red on this line — its
    tree-wide rule is that every ``write_text_secure`` call in a tool module
    is closed where it is written, read LEXICALLY (it stops at a ``def``), so
    a decorator four frames up does not answer for it. ``write_text_or_die``
    satisfies that rule by BEING the closed writer. The decorator is for
    tools whose write sites are raw and scattered.

    ⚠️ The old text's *label* ("the ConfigMap" / "the routing fragment") is
    gone: both call sites write to the same ``-o`` value and the path is in
    the line, so the label distinguished nothing an operator has to act on.
    What replaced the coverage it carried is
    ``test_control_each_writer_mode_writes_its_own_artefact`` in
    tests/ops/test_generate_routes_orchestration.py, which checks the CONTENT
    each mode puts at ``-o``.

    ⛔ STILL NOT THE WHOLE CLASS, and the caveat this docstring has carried
    since #1641 stands. #1789 closed a NAMED BATCH of tools, not every writer
    under ``scripts/**``; what is guarded tree-wide is the secure-writer
    population (``tests/shared/test_write_failure_class.py``), while the raw
    sinks are pinned only for the files that ticket touched. Do not read this
    helper, or a green run of either gate, as the class having been handled
    everywhere — and do not quote a count from here: it was wrong once
    already, because a change like this one moved it.
    """
    write_text_or_die(output, content, flag="-o/--output",
                      exit_code=EXIT_CALLER_ERROR)


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
    try:
        success = apply_to_configmap(routes, receivers, inhibit_rules, namespace,
                                     configmap_name, strict=strict)
    except AlertmanagerConfigRejected as exc:
        # #2219: amtool refused the merged config BEFORE kubectl was called.
        sys.exit(exc.exit_code)
    # #1617: this was EXIT_VIOLATION (1) while docs/cli-reference.{md,en.md}
    # documented 2 — the code and the shipped table said opposite things.
    # `_lib_exitcodes` settles it: "cannot reach Prometheus / API" is
    # EXIT_CALLER_ERROR. Nothing that makes `apply_to_configmap` return False
    # is a CONFIG violation — the failures are the cluster being unreachable
    # or its ConfigMap being unusable — so the CODE was wrong, not the table.
    # #2219: a failed `/-/reload` IS one of those paths now — it used to be
    # warning-level (#1243) and exited 0 while the running Alertmanager stayed
    # on the old config. Unreachable is environment, so 2 fits; a reload the
    # new config made fail is prevented earlier by the amtool gate when amtool
    # is on PATH. ⛔ And it is NOT true that every
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
    target = safe_label(output) if (output and not dry_run) else "stdout"
    try:
        cm_yaml = assemble_configmap(
            base, routes, receivers, inhibit_rules,
            namespace=namespace, configmap_name=configmap_name, strict=strict)
    except ValueError as exc:
        _assembly_failed(exc, f"nothing was written to {target}")

    # #2219: validate what Alertmanager will LOAD from what this run EMITS.
    # `cm_yaml` is the string that goes to -o / stdout below, unchanged; the
    # alertmanager.yml handed to amtool is parsed back OUT of it, not taken
    # from the dict or the inner dump — the outer dump is where a U+0085 in a
    # value got folded into a line break, so only a round trip of the emitted
    # text sees what the cluster will see. Gated before every emission
    # (file, stdout, dry-run preview): a rejected config is not written.
    am_yml = yaml.safe_load(cm_yaml)["data"]["alertmanager.yml"]
    rc = amtool_gate(am_yml,
                     what="the ConfigMap's alertmanager.yml",
                     refusing=f"write it to {target} (nothing was written)")
    if rc is not None:
        sys.exit(rc)

    route_count = len(routes)
    inhibit_count = len(inhibit_rules)

    if dry_run:
        print("\n--- DRY RUN: ConfigMap YAML ---")
        print(cm_yaml)
        print(f"\n--- {route_count} route(s), {len(receivers)} receiver(s), "
              f"{inhibit_count} inhibit rule(s) ---")
        return

    if output:
        _write_output_or_die(output, cm_yaml)
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

    # #2219: a fragment is not a complete Alertmanager config (no root
    # receiver), and `amtool check-config` refuses it for that alone
    # ("root route must specify a default receiver", measured with amtool
    # 0.33.1). So this mode is NOT validated whether or not amtool is on PATH
    # — say so, rather than let rc 0 read as "Alertmanager accepts this".
    print("NOTICE: routing fragment mode; generated output was NOT validated "
          "by Alertmanager (a fragment is not a complete Alertmanager config — "
          "it has no root receiver, which amtool check-config rejects on its "
          "own). Use --output-configmap to validate the merged config when "
          "amtool is on PATH.",
          file=sys.stderr)

    route_count = len(routes)
    inhibit_count = len(inhibit_rules)

    if dry_run:
        print("\n--- DRY RUN OUTPUT ---")
        print(content)
        print(f"\n--- {route_count} route(s), {len(receivers)} receiver(s), "
              f"{inhibit_count} inhibit rule(s) ---")
        return

    if output:
        _write_output_or_die(output, content)
        print(f"Written to {output} ({route_count} routes, {len(receivers)} receivers, "
              f"{inhibit_count} inhibit rules)")
    else:
        print(content)


def _refuse_unreadable_tenant_files(tree: TenantTree) -> None:
    """#1460: say what was read, and refuse to go on if any of it was not.

    Runs in EVERY mode, right after the scan and before the "No tenants
    found" early exit. Measured before this: a tenant file with one quote
    missing was skipped with a stderr WARN, and `--validate` (with or without
    `--strict`) went on to print `OK: all configs valid` at rc=0 — two
    tenants in, one tenant out, and the stdout tail byte-identical to the
    all-good run. Inside the shipped pre-commit hook, which shows hook output
    only on failure, not even the WARN was visible.

    The rule shared with #1405 / #1420 (config-diff): an input that could not
    be parsed makes the verdict UNTRUSTWORTHY, it does not make a smaller
    success. So this is EXIT_VIOLATION regardless of `--strict` — a file that
    cannot be read is not a policy finding to be escalated, it is a hole in
    the tree — and it is refused in render / --apply / --output-configmap
    too: a partial ConfigMap applied to the cluster is the worst outcome
    here, not a tolerable one.

    The counts and names come from the structured record on the tree, never
    from grepping the WARN text.
    """
    skipped = tree.files_skipped
    line = f"Config files: {tree.files_read} read, {len(skipped)} skipped"
    if skipped:
        line += " (" + ", ".join(safe_label(f) for f, _ in skipped) + ")"
    print(line)
    refusal = unreadable_tenant_files_refusal(tree.files_read,
                                              tree.tenant_file_errors)
    if not refusal:
        return
    for msg in refusal:
        print(msg, file=sys.stderr)
    sys.exit(EXIT_VIOLATION)


def unreadable_tenant_files_refusal(
        files_read: int, tenant_file_errors: list[tuple[str, str]]) -> list[str]:
    """The stderr lines of the #1460 refusal; [] = not refused.

    The judgment `_refuse_unreadable_tenant_files` acts on, as data, so
    explain-route's trace (#2293) refuses the same trees with the same words.
    """
    if not tenant_file_errors:
        return []
    return [
        f"FAIL: {len(tenant_file_errors)} config file(s) could not be read — "
        f"refusing to treat the remaining {files_read} as the whole tree:",
        *(f"  {safe_label(fname)}: {safe_label(reason)}"
          for fname, reason in tenant_file_errors),
        "  ⛔ Every tenant in a skipped file is ABSENT from this run, so no "
        "verdict over the rest is a verdict over your conf.d. Repair the "
        "file (or remove it from conf.d) and re-run.",
    ]


def _refuse_duplicate_tenants(tree: TenantTree) -> None:
    """#2315: one tenant id in two tenant files — refused in EVERY mode.

    Runs next to `_refuse_unreadable_tenant_files` and for the same reason:
    the exporter rejects the WHOLE tree in this state, so no route set
    generated from it describes anything that will run, and a ConfigMap
    written or applied from it is the worst outcome. Before this the reader
    merged the two blocks and exited 0 in every mode (render, --validate,
    --output-configmap, with or without --strict).
    """
    refusal = duplicate_tenants_refusal(tree.duplicate_tenants)
    if not refusal:
        return
    for msg in refusal:
        print(msg, file=sys.stderr)
    sys.exit(EXIT_VIOLATION)


def duplicate_tenants_refusal(duplicates: dict[str, list[str]]) -> list[str]:
    """The stderr lines of the #2315 refusal; [] = not refused.

    The judgment `_refuse_duplicate_tenants` acts on, as data (see
    `unreadable_tenant_files_refusal`).
    """
    dups = duplicate_tenant_errors(duplicates)
    if not dups:
        return []
    return [f"FAIL: {len(dups)} tenant(s) declared in more than one file — "
            "nothing was written or applied:",
            *(safe_label(e) for e in dups)]


def _refuse_routing_tree_errors(tree: TenantTree) -> None:
    """#2326: refuse a conf.d tree the routing plane cannot route as one.

    ADR-017 "Amendment 2026-09-28" makes the routing plane hierarchical and
    names what blocks it: `_routing_enforced` below the root, `receiver` /
    `overrides` written as null in a subdirectory level's `_routing_defaults`,
    one routing-profile name defined in two files. (One tenant id declared in
    two files is `_refuse_duplicate_tenants`, #2315 — rc 1, and it runs
    first.) Runs in EVERY mode, right after the file accounting and before
    anything is rendered, written, validated or applied — its output is the
    Alertmanager config a customer deploys, and each of these would ship a
    route tree that silently routes some tenant's alerts somewhere else.

    EXIT_CALLER_ERROR, per the ADR: these are refusals of the tree's SHAPE
    (the same statement as "the directory you pointed me at is not usable as
    input"), not per-tenant findings a --strict switch escalates.
    """
    refusal = routing_tree_errors_refusal(tree.routing_tree_errors)
    if not refusal:
        return
    for msg in refusal:
        print(msg, file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)


def routing_tree_errors_refusal(
        errors: list[tuple[str, str, str, str]]) -> list[str]:
    """The stderr lines of the #2326 refusal; [] = not refused.

    *errors* are the BLOCKING routing-tree findings
    (`TenantTree.routing_tree_errors`). The judgment
    `_refuse_routing_tree_errors` acts on, as data (see
    `unreadable_tenant_files_refusal`).
    """
    if not errors:
        return []
    return [f"ERROR: {len(errors)} routing-tree error(s) — nothing was "
            f"generated, written or applied (ADR-017 amendment 2026-09-28):",
            *(f"  {safe_label(msg)}" for _kind, _fname, _field, msg in errors)]


def tree_refusal(files_read: int, tenant_file_errors: list[tuple[str, str]],
                 duplicates: dict[str, list[str]],
                 routing_tree_problems: list[tuple[str, str, str, str]]
                 ) -> tuple[int, list[str]]:
    """What `main()` refuses a scanned tree for: ``(rc, stderr lines)``.

    ``(EXIT_OK, [])`` = not refused. The order is `main()`'s — unreadable
    tenant file (#1460, rc 1), duplicate tenant (#2315, rc 1), routing-tree
    error (#2326, rc 2) — and `main()` exits on the first refusal, so only
    that one's lines and rc. *routing_tree_problems* is every routing-tree
    finding; only the blocking kinds refuse, the same filter as
    `TenantTree.routing_tree_errors`.
    """
    blocking = [p for p in routing_tree_problems
                if p[0] in BLOCKING_TREE_KINDS]
    for rc, lines in (
            (EXIT_VIOLATION, unreadable_tenant_files_refusal(
                files_read, tenant_file_errors)),
            (EXIT_VIOLATION, duplicate_tenants_refusal(duplicates)),
            (EXIT_CALLER_ERROR, routing_tree_errors_refusal(blocking))):
        if lines:
            return rc, lines
    return EXIT_OK, []


def _print_config_summary(routing_configs: dict, dedup_configs: dict, enforced_routing: dict | None) -> None:
    """Print summary of loaded configs."""
    if enforced_routing:
        print("Platform enforced routing: ENABLED")
    if routing_configs:
        print(f"Found {len(routing_configs)} tenant(s) with routing config: "
              f"{safe_label(', '.join(sorted(routing_configs.keys())))}")
    print(f"Found {len(dedup_configs)} tenant(s) for severity dedup: "
          f"{safe_label(', '.join(sorted(dedup_configs.keys())))}")


_DEFAULT_NAMESPACE = "monitoring"
_DEFAULT_CONFIGMAP = "alertmanager-config"

# #1650: every argparse dest of this tool, classified. The derivation test
# (`tests/ops/test_generate_routes_flag_modes.py::TestEveryFlagIsClassified`)
# builds the parser and checks that these three sets partition its dests
# exactly — so a flag added to `_build_parser` without a row in
# `_flags_this_mode_never_reads` (or an explicit entry here) goes red instead
# of joining the class this guard exists to close.
#   * FLAGS_CLASSIFIED_BY_MODE_GUARD: decided by `_flags_this_mode_never_reads`
#     (each name must be read as `args.<dest>` in that function's source).
#   * FLAGS_READ_IN_EVERY_MODE: consulted on every path main() can take.
#   * FLAGS_GUARDED_BY_BASE_CONFIG_CHECK: the #1616 guard in main().
FLAGS_CLASSIFIED_BY_MODE_GUARD = frozenset({
    "output", "dry_run", "namespace", "configmap", "yes",
    "apply", "output_configmap",
})
FLAGS_READ_IN_EVERY_MODE = frozenset({"config_dir", "validate", "strict", "policy"})
FLAGS_GUARDED_BY_BASE_CONFIG_CHECK = frozenset({"base_config"})


def _mode_of(args: argparse.Namespace) -> str:
    """The ONE mode this invocation runs, by the precedence main() applies.

    `--validate` wins over everything: `_validate_mode` exits before the
    apply / output-configmap / render branches are reached, so under
    `--validate` those flags are as unread as any other (#1616 measured the
    `--validate --output-configmap --base-config` shape). `--apply` and
    `--output-configmap` are mutually exclusive by argparse.
    """
    if args.validate:
        return "--validate"
    if args.apply:
        return "--apply"
    if args.output_configmap:
        return "--output-configmap"
    return "render"


def _flags_this_mode_never_reads(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """#1650: every supplied flag the current mode will never look at.

    Returns ``(flag, why, remedy)`` triples. "Supplied" is "not the argparse
    default": ``is not None`` for the path / string flags (which is why
    ``--namespace`` / ``--configmap`` default to ``None`` and are resolved
    after this check), ``True`` for the store_true ones.

    The read sites, from main() and the mode handlers (line numbers as of
    this change; the matrix is what matters, the numbers will drift):

        flag          --validate  --apply  --output-configmap  render
        -o/--output   never       never    unless --dry-run    unless --dry-run
        --dry-run     never       never    read                read
        --namespace   never       read     read                never
        --configmap   never       read     read                never
        --yes         never       read     never               never

    Measured on main before this: `--validate -o <path>` exited 0 with the
    file absent and stderr empty — even for a path that could not have been
    written, because nothing ever tried. The operator's signal was identical
    to "all good".

    ⛔ Every remedy is a combination argparse ACCEPTS and that this table says
    is READ. The #1616 first cut told `--apply` users to add
    `--output-configmap`, which argparse forbids; the tests here run each
    prescription and check it does not come back through this door.
    """
    mode = _mode_of(args)
    validating = mode == "--validate"
    applying = mode == "--apply"
    names_a_configmap = mode in ("--apply", "--output-configmap")
    found: list[tuple[str, str, str]] = []

    # The mode flags themselves are of the same class under --validate:
    # `_validate_mode` exits before the --apply / --output-configmap branch
    # is reached, so `--validate --apply` applies nothing and
    # `--validate --output-configmap` emits nothing — measured rc 0, silent,
    # on the fixed tree too (the blind review of the first cut found it).
    if validating and args.apply:
        found.append((
            "--apply",
            "--validate returns before the apply step, so nothing is applied",
            "Drop one of them: keep --validate to check, or keep --apply "
            "(with --yes for a non-interactive run) to apply."))
    if validating and args.output_configmap:
        found.append((
            "--output-configmap",
            "--validate returns before the ConfigMap is assembled, so no "
            "ConfigMap is emitted",
            "Drop one of them: keep --validate to check, or keep "
            "--output-configmap to emit the ConfigMap."))

    if args.output is not None:
        if validating:
            found.append((
                "-o/--output",
                "--validate returns before anything is written, so the file "
                "at -o is never created (not even checked for writability)",
                "Drop -o, or drop --validate if you meant to write the file — "
                "validate in one run and write in another."))
        elif applying:
            found.append((
                "-o/--output",
                "--apply writes into the cluster's ConfigMap, not to a file",
                "Drop -o. (To write the merged ConfigMap to a file instead, "
                "use --output-configmap -o <file>; that mode cannot be "
                "combined with --apply.)"))
        elif args.dry_run:
            found.append((
                "-o/--output",
                "--dry-run prints the preview to stdout and never writes a file",
                "Drop --dry-run to write the file, or drop -o to preview."))

    if args.dry_run:
        if validating:
            found.append((
                "--dry-run",
                "--validate returns before the render step, so there is no "
                "preview to print — and --validate never writes a file anyway",
                "Drop --dry-run, or drop --validate if you meant to preview "
                "the output."))
        elif applying:
            found.append((
                "--dry-run",
                "--apply has no preview: it merges into the cluster or it "
                "does nothing",
                "Drop --dry-run. To preview the merged YAML without touching "
                "the cluster, use --output-configmap --dry-run (cannot be "
                "combined with --apply)."))

    for flag, value in (("--namespace", args.namespace),
                        ("--configmap", args.configmap)):
        if value is None or names_a_configmap:
            continue
        if validating:
            # ⛔ One hop, read literally. "drop --validate if you meant to
            # --apply" left `--namespace ns` alone in render mode — which is
            # this guard again. Every clause here is a complete argv change.
            found.append((
                flag,
                "--validate returns before any ConfigMap is named or touched",
                f"Drop {flag}. To apply or write a ConfigMap instead, drop "
                f"--validate AND add --apply or --output-configmap "
                f"(keeping {flag})."))
        else:
            found.append((
                flag,
                "plain render mode emits a routing fragment, which has no "
                "ConfigMap to name",
                f"Add --apply or --output-configmap, or drop {flag}."))

    if args.yes and not applying:
        if validating:
            found.append((
                "--yes",
                "--validate never prompts, and never applies",
                "Drop --yes. To apply instead, drop --validate AND add "
                "--apply (keeping --yes)."))
        elif mode == "--output-configmap":
            found.append((
                "--yes",
                "--output-configmap writes YAML and never touches the "
                "cluster, so there is nothing to confirm",
                "Drop --yes. (--apply, the mode that prompts, cannot be "
                "combined with --output-configmap.)"))
        else:
            found.append((
                "--yes",
                "plain render mode never prompts",
                "Add --apply if you meant to apply, or drop --yes."))
    return found


def _build_parser() -> argparse.ArgumentParser:
    """The CLI parser, separately so tests can enumerate its dests (#1650)."""
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
                             "violations (ADR-007), an unquoted routes[].match value "
                             "or overrides[].alertname / metric_group that PyYAML "
                             "reads as a non-string (yes, 1:30, ~; #2431 — quote "
                             "it), a group_by entry that is not a non-empty string, "
                             "repeats a label or mixes '...' with labels (#2503), "
                             "a tenant _routing that is neither a mapping nor a "
                             "disabling string or a _routing_defaults that is not "
                             "a mapping (#2341) "
                             "and, on the --apply/--output-configmap "
                             "merge, any inhibit rule with an ungated `equal:` label "
                             "(#1132). Without --strict these surface as WARN (an "
                             "unquoted override value is rendered via str() "
                             "instead; a bad group_by entry is dropped). CI runs "
                             "--strict.")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--apply", action="store_true",
                            help="Apply: merge into Alertmanager ConfigMap + reload")
    mode_group.add_argument("--output-configmap", action="store_true",
                            help="Output complete Alertmanager ConfigMap YAML (for GitOps PR flow)")
    parser.add_argument("--base-config", default=None,
                        help="Base Alertmanager YAML for --output-configmap (global + defaults)")
    # #1650: default=None so "supplied" is decidable; the value is resolved
    # to _DEFAULT_NAMESPACE / _DEFAULT_CONFIGMAP after the mode check below.
    parser.add_argument("--namespace", default=None,
                        help=f"K8s namespace for --apply/--output-configmap "
                             f"(default: {_DEFAULT_NAMESPACE})")
    parser.add_argument("--configmap", default=None,
                        help=f"ConfigMap name for --apply/--output-configmap "
                             f"(default: {_DEFAULT_CONFIGMAP})")
    parser.add_argument("--policy", default=None,
                        help="Policy YAML with allowed_domains for webhook URL validation")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt for --apply")
    return parser


def main() -> None:
    """CLI entry point: Generate Alertmanager route + receiver + inhibit config from tenant YAML."""
    parser = _build_parser()
    args = parser.parse_args()

    # #1650: four more flags of the #1616 class — accepted, then never read
    # by the mode that is about to run. Decided HERE, before any conf.d work,
    # for the reason the #1616 comment below spells out: a check inside a
    # mode handler is bypassed when the tree yields no routes.
    # Both guards report into ONE message: `--validate --output-configmap
    # --base-config X` is refused for two reasons at once (the mode flag AND
    # the base file are unread), and an operator who only sees the first
    # would fix it, re-run, and meet the second. Collected here, emitted
    # after the --base-config block below.
    caller_error_lines: list[str] = []
    mode = _mode_of(args)
    for flag, why, remedy in _flags_this_mode_never_reads(args):
        caller_error_lines.append(f"{flag} is not read in {mode} mode: {why}.\n  {remedy}")
    namespace = args.namespace if args.namespace is not None else _DEFAULT_NAMESPACE
    configmap_name = args.configmap if args.configmap is not None else _DEFAULT_CONFIGMAP

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
        caller_error_lines.append(
            f"--base-config is not read in this mode: {why}.\n{remedy}")
    if caller_error_lines:
        die_caller_error("\n".join(caller_error_lines))

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
    # #1460: the tree form, not the tuple — the tuple has no record of the
    # files that did not parse, and that record decides whether anything
    # below is allowed to call itself a result.
    tree = load_tenant_tree(args.config_dir, strict_policies=args.strict)
    routing_configs, dedup_configs, schema_warnings, enforced_routing, metadata_configs = \
        tree.as_tuple()
    _refuse_unreadable_tenant_files(tree)
    _refuse_duplicate_tenants(tree)
    _refuse_routing_tree_errors(tree)

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

    # #2279: two generated receivers with one name. Blocking in EVERY mode and
    # with or without --strict — Alertmanager refuses such a config, and a
    # merge that de-duplicates by name would silently keep only one of them.
    # Decided before render / --apply's prompt / --output-configmap, so
    # nothing is written or applied.
    collisions = [w for w in all_warnings if is_receiver_name_collision(w)]
    if collisions:
        print(f"FAIL: {len(collisions)} duplicate receiver name(s) — nothing "
              "was written or applied:", file=sys.stderr)
        for e in collisions:
            print(safe_label(e), file=sys.stderr)
        sys.exit(EXIT_VIOLATION)

    # ADR-007 --strict outside --validate: abort before rendering/applying a
    # config that violates a domain policy ("--strict 模式：報錯終止").
    if args.strict:
        policy_errors = _policy_errors(all_warnings)
        if policy_errors:
            print(f"FAIL: {len(policy_errors)} blocking error(s) under "
                  "--strict (domain policy, unquoted matcher value, "
                  "invalid group_by entry, unreadable _routing / "
                  "_routing_defaults):",
                  file=sys.stderr)
            for e in policy_errors:
                print(e, file=sys.stderr)
            sys.exit(EXIT_VIOLATION)

    # Apply mode
    if args.apply:
        _apply_mode(routes, receivers, inhibit_rules, namespace,
                    configmap_name, args.yes, strict=args.strict)

    # Output-configmap mode
    if args.output_configmap:
        _output_configmap_mode(routes, receivers, inhibit_rules, base_config,
                              namespace, configmap_name, args.dry_run, args.output,
                              strict=args.strict)
        return

    # Default render mode
    _render_output_mode(routes, receivers, inhibit_rules, args.dry_run, args.output)


if __name__ == "__main__":
    main()
