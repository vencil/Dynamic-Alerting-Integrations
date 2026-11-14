#!/usr/bin/env python3
"""
generate_alertmanager_routes.py — Generate Alertmanager route + receiver + inhibit config from tenant YAML.

Reads all tenant YAML files from conf.d/, extracts _routing and _severity_dedup sections,
and merges the Alertmanager route tree + receivers + inhibit_rules they produce into a
SOURCE Alertmanager config — output = merge(source, conf.d), never fed back (#1211 gap 2).

Modes (exactly one; without one the run is a caller error, exit 2):
  --validate             judge the tree, write nothing
  --output-configmap     the merged config as a K8s ConfigMap (GitOps)
  --output-alertmanager  the merged config as a plain alertmanager.yml
  --apply                merge into the cluster's ConfigMap and reload

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
  python3 scripts/tools/ops/generate_alertmanager_routes.py --config-dir conf.d/ --output-configmap -o am-configmap.yaml
  python3 scripts/tools/ops/generate_alertmanager_routes.py --config-dir conf.d/ --output-alertmanager --base-config my-am.yml -o alertmanager.yml
  python3 scripts/tools/ops/generate_alertmanager_routes.py --config-dir conf.d/ --output-alertmanager --dry-run
  python3 scripts/tools/ops/generate_alertmanager_routes.py --config-dir conf.d/ --validate --findings-json findings.json

v2.8.0 PR-3a: This file is now a CLI facade. The 1645-line monolith was
split into 5 helper modules (_grar_validate / _grar_merge / _grar_parse /
_grar_routes / _grar_render) for testability and to break the god-file
pattern. All public + private symbols are re-exported below so existing
test imports keep working unchanged.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import textwrap
from types import MappingProxyType
from typing import Mapping, NamedTuple, NoReturn

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout

# ── Re-exports from _lib_python (kept for test backward-compat) ─────
# ⛔ Imported here and NOT added to the `_lib_python` block below: that block
# carries `# noqa: F401` because everything in it is a re-export kept for
# test backward-compat, and `test_grar_facade_reexports` pins that set. These
# two are USED, not re-exported.
from _lib_io import (  # noqa: E402  (#1538 output-layer escaping, #1789)
    OutputWriteError, safe_label, write_text_or_die,
)
# #2766: the --findings-json temp-file write. Module form, because the bare
# name is already a `_lib_python` re-export in the F401 block below.
import _lib_io  # noqa: E402
from _lib_validation import key_segment  # noqa: E402  (#1560 ⑤-b: keys into findings)
from _lib_compat import read_shipped_da_tools_version  # noqa: E402  (#2766 VERSION)
from _lib_python import (  # noqa: E402, F401
    write_text_secure,
    PLATFORM_DEFAULTS,
)
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, CallerError, die_caller_error,
    effective_crash_exit_code)
import _lib_input  # noqa: E402  (#3751: --config-dir is a declared input)

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
)

# ── Re-exports from _grar_merge ────────────────────────────────────
from _grar_merge import (  # noqa: E402, F401
    _apply_timing_params,
    _contains_tenant_placeholder,
    _substitute_tenant,
    build_receiver_config,
    merge_routing_with_defaults,
)
# Used here (#1560: the mount root, the policy's shared_secrets), not re-exported.
from _grar_merge import DEFAULT_MOUNT_ROOT, SecretBinding, SharedSecret  # noqa: E402
# #1560: the Secrets the emitted config reads (--secret-mounts-out).
from _grar_merge import MOUNT_LIST_SCHEMA, required_mounts  # noqa: E402

# ── Re-exports from _grar_parse ────────────────────────────────────
from _grar_parse import (  # noqa: E402, F401
    TenantTree,  # #1460 file accounting travels with the tree
    _merge_tenant_routing,
    _parse_config_files,
    _parse_platform_config,
    _parse_tenant_overrides,
    load_tenant_tree,
    print_reader_warnings,  # #2906: for the tools that do not print the stream
    credential_findings,  # #1560 ⑤-b: credentials written out
)

# Used here, not re-exported: which #2326 routing-tree findings refuse the
# tree (`tree_refusal`, the same filter as `TenantTree.routing_tree_errors`).
from _grar_parse import BLOCKING_TREE_KINDS  # noqa: E402
# #2906: the field-type refusal's itemised lines (`field_type_errors_refusal`).
from _grar_parse import field_type_findings  # noqa: E402

# ── Re-exports from _grar_routes ───────────────────────────────────
from _grar_routes import (  # noqa: E402, F401
    SlotVerdict,
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
    tenant_slot_verdicts,
)

# ── Re-exports from _grar_render ───────────────────────────────────
from _grar_render import (  # noqa: E402, F401
    BaseConfigInputError,
    _apply_merged_configmap,
    _read_existing_configmap,
    _reload_alertmanager,
    apply_to_configmap,
    assemble_alertmanager_yml,
    assemble_configmap,
    load_base_config,
    merge_generated,
)
# #2219: used here, not re-exported — hence no `# noqa: F401` (see the
# `_lib_io` block above for why that marker must stay off used imports).
from _grar_render import (  # noqa: E402
    AlertmanagerConfigInvariantViolated, AlertmanagerConfigRejected, BaseIsAnOutputError,
    alertmanager_check,
)
# #2660: the root-receiver-has-no-integration WARN (--output-configmap here;
# --apply emits it inside apply_to_configmap). Used, so no F401 marker.
from _grar_render import warn_if_root_receiver_without_integration  # noqa: E402
# #1211: served page-mode custom alerts that fall to the firehose receiver.
from _grar_render import (  # noqa: E402
    UndeliveredCustomAlerts, custom_firehose_findings, custom_firehose_has_integration,
    undelivered_custom_alert_tenants,
)
# #1211 gap 2: an assembled watchdog-heartbeat receiver that delivers nowhere.
from _grar_render import watchdog_receiver_findings  # noqa: E402
# #3206 (ADR-036 §12): the emitted ConfigMap read back the consumers' way.
from _grar_render import (  # noqa: E402
    ConsumerReadError, consumer_read_message, emitted_alertmanager_yml,
)
# #3257: the base config's global may not hand a credential to a receiver.
from _grar_render import assert_base_global_inherits_no_credentials  # noqa: E402
# #2311: the verdict on a generation result (`validate_tree`'s last step).
# Used, so no F401 marker.
from _grar_render import GeneratedConfigVerdict, evaluate_generated_config  # noqa: E402
from _grar_render import (  # noqa: E402, F401  (read as `gen.…` by validate_config)
    AM_ACCEPTED, AM_REJECTED, AM_UNUSABLE,
)
# #2279: the shared "is this generation result unusable" predicate (a tree
# with nothing to render asks it here; `evaluate_generated_config` otherwise).
from _grar_validate import blocking_generation_errors  # noqa: E402
from _grar_validate import is_receiver_name_collision  # noqa: E402
# #2315: the duplicate-tenant refusal, shared with validate-config's
# tenant_uniqueness row through `_lib_confd.tenant_declarations`.
from _grar_validate import duplicate_tenant_errors  # noqa: E402
# ADR-035 D3: the invalid-tenant-id refusal cites the rule's own words.
from _grar_validate import invalid_tenant_id_text, tenant_id_field  # noqa: E402
# #1533: a degraded probe set that cannot vouch for an operator-supplied
# tenant-triggered inhibit rule (rc 2), and the --validate summary line.
from _grar_validate import (  # noqa: E402
    PLATFORM_ALERT_IDENTITY_LABELS, PlatformProbeSetUnverifiable,
    platform_alert_identities, probe_set_degraded_reason, probe_set_is_degraded,
)
# #2766: structured findings (the refusal lines are built as them here).
from _grar_merge import Finding, as_finding, unclassified  # noqa: E402
# #2901 (ADR-036 §6): the policy's min_generator_version, read in the same
# parse as allowed_domains, and the comparison the generator container shares.
from _grar_validate import generator_version_problem, load_policy_document  # noqa: E402
import _lib_parse_cache  # noqa: E402
from _lib_parse_tree import ParseTreeError  # noqa: E402  (#3751: no usable da-guard)
from _grar_parse import parse_tree_failure_exit  # noqa: E402  (#3751: its own rc-2 line)
import _lib_consumer_read  # noqa: E402  (#3206: the platform rules as Prometheus reads them)
import _lib_key_verdict  # noqa: E402  (ADR-038 §5: the shared key verdict)
import _lib_tenant_values  # noqa: E402  (#1211: the custom alerts the exporter serves)
from _lib_confd import WARN_LIMIT  # noqa: E402


# ============================================================
# CLI Mode Handlers (--validate, --apply, --output-configmap, --output-alertmanager)
# ============================================================

def _policy_errors(all_warnings: list[str]) -> list[str]:
    """Extract blocking domain-policy ERROR lines (ADR-007 --strict).

    Only the strict domain-policy paths (check_domain_policies(strict=True)
    plus the fail-open closures in load_tenant_tree) emit
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
    reads as the tool having crashed. ``--apply`` reaches it too (#2506), with
    ``AlertmanagerConfigInvariantViolated`` from the merge into the cluster's
    config.
    """
    print(f"FAIL: the assembled Alertmanager config was refused — {refusing}:"
          f"\n  {safe_label(str(exc))}", file=sys.stderr)
    sys.exit(EXIT_VIOLATION)


def _unverifiable(exc: PlatformProbeSetUnverifiable, refusing: str) -> None:
    """#1533: the probe set degraded and an operator-supplied tenant-triggered
    inhibit rule could not be checked against it. No verdict on the config —
    rc 2, like Alertmanager giving no verdict — and nothing is written or applied."""
    print(f"ERROR: the assembled Alertmanager config could not be verified — "
          f"{refusing}:\n  {safe_label(str(exc))}", file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)


def _validate_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                   all_warnings: list[str], sink: list | None = None,
                   key_blocks: "list[str] | None" = None,
                   generated: "list | None" = None) -> NoReturn:
    """The last step of ``validate_tree``: judge the generation result
    (``evaluate_generated_config``), print the verdict and exit with it.

    ``--strict``'s ADR-007 policy errors are in *all_warnings* only under
    ``--strict`` (``load_tenant_tree(strict_policies=…)``) and are handed in
    as ``extra_errors``. *generated* receives the verdict, for
    ``ValidateOutcome.generated``.
    """
    verdict = evaluate_generated_config(
        routes, receivers, inhibit_rules, all_warnings,
        extra_errors=_policy_errors(all_warnings))
    if generated is not None:
        generated.append(verdict)
    # #2766: the lines only --validate prints join the findings. A verdict
    # error that is already one (a stream line, kept by identity) is not
    # added twice; the rest (the inhibit tripwire) block under --validate.
    recorded = {id(f) for f in sink} if sink is not None else set()
    _sink_extend(sink, [unclassified(w, blocks="never") for w in verdict.warnings])
    _sink_extend(sink, [unclassified(e, blocks="validate")
                        for e in verdict.errors if id(e) not in recorded])
    # Non-blocking lines the checks' helpers used to print directly (an
    # un-gated equal-label, a degraded probe set): same stream, same text.
    for w in verdict.warnings:
        print(w, file=sys.stderr)
    route_count = len(routes)
    inhibit_count = len(inhibit_rules)
    print(f"Validation: {route_count} route(s), {len(receivers)} receiver(s), "
          f"{inhibit_count} inhibit rule(s)")
    # #1533: the rc does not change (generated rules cannot reach a platform
    # alert — see the CLI reference), but the run must say what it checked.
    if probe_set_is_degraded():
        if platform_alert_identities() is PLATFORM_ALERT_IDENTITY_LABELS:
            print(f"Probe set: DEGRADED to the "
                  f"{len(PLATFORM_ALERT_IDENTITY_LABELS)}-entry built-in "
                  f"fallback of the shipped platform alert identities "
                  f"({probe_set_degraded_reason()})")
        else:
            print(f"Probe set: DEGRADED, shipped platform alert identities "
                  f"only partly read; built-in fallback added back "
                  f"({probe_set_degraded_reason()})")
    # #3016: the shared key verdict's blocking lines (`_report_key_verdict`,
    # already printed and recorded) fail the run with the generator's own.
    # A key verdict line carries a tenant's key name: printed as the first
    # print did (safe_label), never raw (#3016 review).
    errors = list(verdict.errors) + [safe_label(line) for line in key_blocks or []]
    if errors:
        print(f"FAIL: {len(errors)} error(s) found:", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(EXIT_VIOLATION)
    if verdict.assembly_error is not None:
        _assembly_failed(ValueError(verdict.assembly_error),
                         "validation cannot pass")
    # #2260 / #3689: Alertmanager, through the write paths' own gate:
    # accepted → OK, rejected → rc 1, no verdict → rc 2.
    print(verdict.alertmanager.message, file=sys.stderr)
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
                namespace: str, configmap_name: str, yes_flag: bool, strict: bool = False,
                judge_merged=None) -> None:
    """Handle --apply mode: merge into ConfigMap and reload. *judge_merged*:
    ``apply_to_configmap``'s (#1211)."""
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
                                     configmap_name, strict=strict,
                                     judge_merged=judge_merged)
    except AlertmanagerConfigRejected as exc:
        # #2219 / #3689: Alertmanager refused the merged config (or gave no
        # verdict) BEFORE kubectl was called.
        sys.exit(exc.exit_code)
    except AlertmanagerConfigInvariantViolated as exc:
        # #2506: the same `FAIL:` verdict #2260 gave --output-configmap and
        # --validate, instead of a traceback at rc 1.
        _assembly_failed(exc, "nothing was applied to the cluster")
    except PlatformProbeSetUnverifiable as exc:
        _unverifiable(exc, "nothing was applied to the cluster")
    # #1617: this was EXIT_VIOLATION (1) while docs/cli-reference.{md,en.md}
    # documented 2 — the code and the shipped table said opposite things.
    # `_lib_exitcodes` settles it: "cannot reach Prometheus / API" is
    # EXIT_CALLER_ERROR. Nothing that makes `apply_to_configmap` return False
    # is a CONFIG violation — the failures are the cluster being unreachable
    # or its ConfigMap being unusable — so the CODE was wrong, not the table.
    # #2219: a failed `/-/reload` IS one of those paths now — it used to be
    # warning-level (#1243) and exited 0 while the running Alertmanager stayed
    # on the old config. Unreachable is environment, so 2 fits; a reload the
    # new config made fail is prevented earlier by `alertmanager_check`.
    # ⛔ And it is NOT true that every
    # such path is a kubectl invocation failing: with kubectl returning 0,
    # a ConfigMap missing `alertmanager.yml` — or holding a value that is
    # not a mapping — also returns False. Measured; each of those now emits
    # a diagnostic (see `_read_existing_configmap`).
    sys.exit(EXIT_OK if success else EXIT_CALLER_ERROR)


def _assembled_output_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                           base: dict, namespace: str, configmap_name: str,
                           dry_run: bool, output: str | None, strict: bool = False,
                           judge_emitted=None, *, as_configmap: bool,
                           mounts_out: str | None = None,
                           mount_root: str = DEFAULT_MOUNT_ROOT,
                           mount_lists: list[tuple[str, dict]] | None = None) -> None:
    """Handle --output-configmap (*as_configmap*) and --output-alertmanager:
    ``merge_generated`` on *base*, emitted as a ConfigMap or as a plain
    alertmanager.yml — one merge, two wrappers (#1211 gap 2).

    *judge_emitted* (#1211): called with the emitted config as Alertmanager
    reads it, before anything is written; the reasons it returns refuse it
    (rc 1).

    Takes the ALREADY-LOADED base config, not a path: #1616 requires the
    supplied-but-unusable check to happen before the tenant scan, and a
    parameter that cannot be a path is the structural way to keep it there.

    *mounts_out* (#1560, ``--secret-mounts-out``): also write the Secrets the
    emitted config reads under *mount_root* (``required_mounts``), read off
    the same reading of the emitted text the judges above saw — so the base
    config's own ``*_file`` paths are in it and nothing conf.d did not render
    is. Handed to *mount_lists* (``main`` writes it after the run, outside
    the stdout redirect a stdout document needs), else written here; either
    way after the config itself (the list describes an output that exists);
    a failed write of either is rc 2. Which documents may share stdout is
    decided in ``main`` before the run.
    """
    what = "the ConfigMap" if as_configmap else "the alertmanager.yml"
    am_what = "the ConfigMap's alertmanager.yml" if as_configmap else "the alertmanager.yml"
    target = safe_label(output) if (output and not dry_run) else "stdout"
    try:
        if as_configmap:
            content = assemble_configmap(
                base, routes, receivers, inhibit_rules,
                namespace=namespace, configmap_name=configmap_name, strict=strict)
        else:
            content = assemble_alertmanager_yml(base, routes, receivers,
                                                inhibit_rules, strict=strict)
    except ValueError as exc:
        _assembly_failed(exc, f"nothing was written to {target}")
    except PlatformProbeSetUnverifiable as exc:
        _unverifiable(exc, f"nothing was written to {target}")

    # #2219: validate what Alertmanager will LOAD from what this run EMITS.
    # For a ConfigMap the alertmanager.yml handed to Alertmanager is parsed back
    # OUT of the emitted text, not taken from the dict or the inner dump —
    # the outer dump is where a U+0085 in a value got folded into a line
    # break, so only a round trip of the emitted text sees what the cluster
    # will see. Gated before every emission (file, stdout, dry-run preview):
    # a rejected config is not written. ADR-036 §12 (#3206): the round trip
    # is Kubernetes' reading of the emitted text (da-crdecode), the config
    # Alertmanager's (da-read).
    if as_configmap:
        try:
            am_yml = emitted_alertmanager_yml(content)
        except ConsumerReadError as exc:
            print(f"ERROR: cannot read the emitted ConfigMap the way kubectl does — refusing "
                  f"to write it to {target} (nothing was written): "
                  f"{safe_label(consumer_read_message(exc))}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
    else:
        am_yml = content
    # #3689: the one gate --validate and --apply ask too, in its one order.
    check = alertmanager_check(am_yml, what=am_what,
                               refusing=f"write it to {target} (nothing was written)")
    print(check.message, file=sys.stderr)
    if check.exit_code is not None:
        sys.exit(check.exit_code)
    # #2660: the gate's reading of the emitted text, so a --base-config's own
    # root receiver is what is judged, not the built-in one.
    emitted = check.reading
    warn_if_root_receiver_without_integration(emitted)
    # #1211: the emitted firehose / watchdog-heartbeat receivers — a
    # --base-config's own, else the name-only placeholders — decide whether
    # those alerts notify anyone.
    reasons = judge_emitted(emitted) if judge_emitted is not None else []
    if reasons:
        print(f"FAIL: {'; '.join(reasons)} (the ERROR line(s) above, --strict) — "
              f"refusing to write {what} to {target} (nothing was written)",
              file=sys.stderr)
        sys.exit(EXIT_VIOLATION)

    route_count = len(routes)
    inhibit_count = len(inhibit_rules)

    if dry_run:
        print(f"\n--- DRY RUN: {'ConfigMap YAML' if as_configmap else 'alertmanager.yml'} ---")
        print(content)
        print(f"\n--- {route_count} route(s), {len(receivers)} receiver(s), "
              f"{inhibit_count} inhibit rule(s) ---")
        return

    if output:
        _write_output_or_die(output, content)
        print(f"Written to {output} ({route_count} routes, "
              f"{len(receivers)} receivers, {inhibit_count} inhibit rules)")
    else:
        print(content)
    if mounts_out is not None:
        doc = required_mounts(emitted, mount_root)
        if mount_lists is not None:
            mount_lists.append((mounts_out, doc))
        else:
            _write_mount_list(mounts_out, doc)


def _write_mount_list(path: str, doc: dict) -> None:
    """Write the ``--secret-mounts-out`` document (#1560) to *path*, or exit
    2. JSON, ``sort_keys``; ``-`` is stdout. One summary line on stderr:
    counts and the path only."""
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    _lib_io.write_operator_output_or_die(path, text, flag="--secret-mounts-out",
                                         exit_code=EXIT_CALLER_ERROR)
    n_mounted = len(doc["mounts"])
    n_keys = sum(len(m["keys"]) for m in doc["mounts"])
    n_other = len(doc["other_files"])
    print(f"Mount list: {n_mounted} Secret(s), {n_keys} key(s) under "
          f"{doc['mount_root']}, {n_other} other file path(s) -> {safe_label(path)}",
          file=sys.stderr)


def _refuse_unreadable_tenant_files(tree: TenantTree,
                                    sink: list | None = None) -> None:
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
    _sink_extend(sink, refusal)
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
        _refusal_line(
            f"FAIL: {len(tenant_file_errors)} config file(s) could not be read — "
            f"refusing to treat the remaining {files_read} as the whole tree:"),
        *(_refusal_line(f"  {safe_label(fname)}: {safe_label(reason)}",
                        kind="tenant_file_unreadable", file=fname)
          for fname, reason in tenant_file_errors),
        _refusal_line(
            "  ⛔ Every tenant in a skipped file is ABSENT from this run, so no "
            "verdict over the rest is a verdict over your conf.d. Repair the "
            "file (or remove it from conf.d) and re-run."),
    ]


def _refusal_line(text: str, *, kind: str = "refusal_summary",
                  **fields: str | None) -> Finding:
    """#2766: one line of a refusal (blocking in every mode) as a Finding.

    A refusal's framing lines (``FAIL: …``, the ⛔ advice) are
    ``refusal_summary``; the itemised lines carry their own kind.
    """
    return Finding(text, kind=kind, severity="error", blocks="always",
                   **fields)


def _refuse_duplicate_tenants(tree: TenantTree,
                              sink: list | None = None) -> None:
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
    _sink_extend(sink, refusal)
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
    return [_refusal_line(f"FAIL: {len(dups)} tenant(s) declared in more than one file — "
                          "nothing was written or applied:"),
            *(e.with_text(safe_label(e)) for e in dups)]


def _refuse_routing_tree_errors(tree: TenantTree,
                                sink: list | None = None) -> None:
    """#2326: refuse a conf.d tree the routing plane cannot route as one.

    ADR-017 Decision 9 makes the routing plane hierarchical and
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
    _sink_extend(sink, refusal)
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
    return [_refusal_line(f"ERROR: {len(errors)} routing-tree error(s) — nothing was "
                          f"generated, written or applied (ADR-017 Decision 9):"),
            *(_refusal_line(f"  {safe_label(msg)}", kind=kind, file=fname,
                            field=field)
              for kind, fname, field, msg in errors)]


def _refuse_invalid_tenant_ids(tree: TenantTree,
                               sink: list | None = None) -> None:
    """ADR-035 D3: a declared tenant id the rule refuses — refused in EVERY mode.

    Render, --dry-run, --apply, --output-configmap, --validate, with or
    without --strict: rc 1 and nothing written or applied. ⛔ Deliberately
    NOT the wave-2 1A rule (WARN, skip the entry, render rc 0): skipping a
    whole tenant ships an Alertmanager config without its routes, and its
    alerts fall through to the root receiver — the shipped `default` is an
    empty receiver, so they are silently dropped. A red deploy keeps the
    config already in the cluster instead. EXIT_VIOLATION like the duplicate
    tenant refusal (#2315): a verdict on the config, not on the invocation.
    """
    refusal = invalid_tenant_ids_refusal(tree.invalid_tenant_ids, tree.invalid_tenant_id_files)
    if not refusal:
        return
    _sink_extend(sink, refusal)
    for msg in refusal:
        print(msg, file=sys.stderr)
    sys.exit(EXIT_VIOLATION)


def invalid_tenant_ids_refusal(invalid_ids: list[str],
                               files: "dict[str, list[str]] | None" = None) -> list[str]:
    """The stderr lines of the ADR-035 D3 refusal; [] = not refused.

    The judgment `_refuse_invalid_tenant_ids` acts on, as data (see
    `unreadable_tenant_files_refusal`). *files*: id → the tenant files
    declaring it (`TenantTree.invalid_tenant_id_files`); each line and
    finding names the first (#3703: an id that is not printed is found by
    its file).
    """
    if not invalid_ids:
        return []
    files = files or {}
    return [_refusal_line(f"FAIL: {len(invalid_ids)} invalid tenant id(s) — nothing was "
                          "generated, written or applied; rename the tenant(s):"),
            *(_refusal_line(f"  {safe_label(invalid_tenant_id_text(t, files.get(t, ())))}",
                            kind="invalid_tenant_id", tenant=tenant_id_field(t),
                            file=(files.get(t) or [None])[0])
              for t in invalid_ids)]


def _refuse_field_type_errors(tree: TenantTree,
                              sink: list | None = None) -> None:
    """#2906: a boolean / enum field of the wrong type or with a value the
    generator does not know — refused in EVERY mode.

    Render, --dry-run, --apply, --output-configmap, --validate, with or
    without --strict: rc 1 and nothing written or applied. The fields are
    judged by type and value alone (ADR-036 §2), never by what a string
    spells: `_routing_enforced.enabled: "true"` used to be a `WARN …
    skipping` line and a config with no NOC route at rc 0; a quoted
    `require_critical_escalation: "true"` switched the constraint off unless
    --strict; `_severity_dedup: "no"` turned dedup ON; `send_resolved:
    "yes"` was written out as `true`. Once the generator reads yaml.v3
    (ADR-036 step 4) an unquoted `yes` / `no` / `on` / `off` is such a
    string as well, so each of those would change meaning in silence.
    EXIT_VIOLATION, like the invalid tenant id refusal: a verdict on the
    config, not on the invocation.
    """
    refusal = field_type_errors_refusal(tree.field_type_errors)
    if not refusal:
        return
    _sink_extend(sink, refusal)
    for msg in refusal:
        print(msg, file=sys.stderr)
    sys.exit(EXIT_VIOLATION)


def field_type_errors_refusal(errors: list) -> list[str]:
    """The stderr lines of the #2906 / #3116 refusal; [] = not refused.

    The judgment `_refuse_field_type_errors` acts on, as data (see
    `unreadable_tenant_files_refusal`).
    """
    if not errors:
        return []
    # #3116: one header for every kind it carries — a boolean / enum field
    # of the wrong type (#2906), a domain policy that cannot be enforced as
    # written, a policy file that cannot be used.
    return [_refusal_line(f"FAIL: {len(errors)} config problem(s) — nothing was "
                          "generated, written or applied:"),
            *(f.with_text(safe_label(f)) for f in field_type_findings(errors))]


def tree_refusal(files_read: int, tenant_file_errors: list[tuple[str, str]],
                 duplicates: dict[str, list[str]],
                 routing_tree_problems: list[tuple[str, str, str, str]],
                 invalid_tenant_ids: list[str] | None = None,
                 field_type_errors: list | None = None,
                 *, invalid_tenant_id_files: "dict[str, list[str]] | None" = None,
                 ) -> tuple[int, list[str]]:
    """What `main()` refuses a scanned tree for: ``(rc, stderr lines)``.

    ``(EXIT_OK, [])`` = not refused. The order is `main()`'s — unreadable
    tenant file (#1460, rc 1), duplicate tenant (#2315, rc 1), routing-tree
    error (#2326, rc 2), invalid tenant id (ADR-035 D3, rc 1), field type
    (#2906, rc 1) — and `main()` exits on the first refusal, so only that
    one's lines and rc.
    *routing_tree_problems* is every routing-tree finding; only the blocking
    kinds refuse, the same filter as `TenantTree.routing_tree_errors`.
    """
    blocking = [p for p in routing_tree_problems
                if p[0] in BLOCKING_TREE_KINDS]
    for rc, lines in (
            (EXIT_VIOLATION, unreadable_tenant_files_refusal(
                files_read, tenant_file_errors)),
            (EXIT_VIOLATION, duplicate_tenants_refusal(duplicates)),
            (EXIT_CALLER_ERROR, routing_tree_errors_refusal(blocking)),
            (EXIT_VIOLATION, invalid_tenant_ids_refusal(
                sorted(set(invalid_tenant_ids or []), key=str), invalid_tenant_id_files)),
            (EXIT_VIOLATION, field_type_errors_refusal(field_type_errors or []))):
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
    "apply", "output_configmap", "output_alertmanager", "mount_list_out",
})
FLAGS_READ_IN_EVERY_MODE = frozenset({"config_dir", "validate", "strict", "policy",
                                      "findings_json",
                                      # #1560: judged (exit 2) beside --policy
                                      "mount_root"})
FLAGS_GUARDED_BY_BASE_CONFIG_CHECK = frozenset({"base_config"})


#: The modes that write or apply a config, by flag (argparse dest → flag).
OUTPUT_MODE_FLAGS = {"apply": "--apply", "output_configmap": "--output-configmap",
                     "output_alertmanager": "--output-alertmanager"}
#: What a run without a mode is told (#1211 gap 2).
NO_MODE_MESSAGE = (
    "no mode given: pass one of --validate, --output-configmap, "
    "--output-alertmanager or --apply.\n"
    "  The routing-fragment mode (no mode flag) is gone (#1211): a fragment "
    "pasted after your own routes loses the platform routes that must come "
    "first. --output-alertmanager --base-config <your alertmanager.yml> "
    "renders your config (the source, kept as it is) with the generated "
    "routing merged in; --output-configmap does the same as a ConfigMap.\n"
    "  ⛔ Without --base-config those modes assemble on a built-in placeholder "
    "base (its `global:` and empty root receiver are not yours).")


def _mode_of(args: argparse.Namespace) -> str:
    """The ONE mode this invocation runs, by the precedence main() applies.

    `--validate` wins over everything: `_validate_mode` exits before the
    output branches are reached, so under `--validate` those flags are as
    unread as any other (#1616 measured the `--validate --output-configmap
    --base-config` shape). The three output modes are mutually exclusive by
    argparse. No mode at all is "none" — a caller error (#1211 gap 2).
    """
    if args.validate:
        return "--validate"
    for dest, flag in OUTPUT_MODE_FLAGS.items():
        if getattr(args, dest):
            return flag
    return "none"


def _flags_this_mode_never_reads(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """#1650: every supplied flag the current mode will never look at.

    Returns ``(flag, why, remedy)`` triples. "Supplied" is "not the argparse
    default": ``is not None`` for the path / string flags (which is why
    ``--namespace`` / ``--configmap`` default to ``None`` and are resolved
    after this check), ``True`` for the store_true ones.

    The read sites, from main() and the mode handlers (the matrix is what
    matters; a run with no mode is refused before this is consulted):

        flag                 --validate  --apply  --output-configmap  --output-alertmanager
        -o/--output          never       never    unless --dry-run    unless --dry-run
        --secret-mounts-out  never       never    unless --dry-run    unless --dry-run
        --dry-run            never       never    read                read
        --namespace          never       read     read                never
        --configmap          never       read     read                never
        --yes                never       read     never               never

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
    if validating and args.output_alertmanager:
        found.append((
            "--output-alertmanager",
            "--validate returns before the config is assembled, so no "
            "alertmanager.yml is emitted",
            "Drop one of them: keep --validate to check, or keep "
            "--output-alertmanager to emit the alertmanager.yml."))

    if args.output is not None:
        if validating:
            found.append((
                "-o/--output",
                "--validate returns before anything is written, so the file "
                "at -o is never created (not even checked for writability)",
                "Drop -o, or replace --validate with --output-alertmanager "
                "or --output-configmap (keeping -o) if you meant to write "
                "the file — validate in one run and write in another."))
        elif applying:
            found.append((
                "-o/--output",
                "--apply writes into the cluster's ConfigMap, not to a file",
                "Drop -o. (To write the merged config to a file instead, "
                "use --output-configmap or --output-alertmanager with -o "
                "<file>; neither can be combined with --apply.)"))
        elif args.dry_run:
            found.append((
                "-o/--output",
                "--dry-run prints the preview to stdout and never writes a file",
                "Drop --dry-run to write the file, or drop -o to preview."))

    # #1560: the mount list is read off the config an output mode emits.
    if args.mount_list_out is not None:
        if validating:
            found.append((
                "--secret-mounts-out",
                "--validate returns before the config is assembled, so there "
                "is no emitted config to read the Secret mounts from",
                "Drop --secret-mounts-out, or replace --validate with "
                "--output-alertmanager or --output-configmap (keeping "
                "--secret-mounts-out)."))
        elif applying:
            found.append((
                "--secret-mounts-out",
                "--apply writes into the cluster's ConfigMap and writes no "
                "file",
                "Drop --secret-mounts-out. (To write the list, use "
                "--output-alertmanager or --output-configmap with "
                "--secret-mounts-out <file>; neither can be combined with "
                "--apply.)"))
        elif args.dry_run:
            found.append((
                "--secret-mounts-out",
                "--dry-run prints the preview to stdout and never writes a file",
                "Drop --dry-run to write the list, or drop "
                "--secret-mounts-out to preview."))

    if args.dry_run:
        if validating:
            found.append((
                "--dry-run",
                "--validate returns before the render step, so there is no "
                "preview to print — and --validate never writes a file anyway",
                "Drop --dry-run, or replace --validate with "
                "--output-alertmanager or --output-configmap (keeping "
                "--dry-run) if you meant to preview the output."))
        elif applying:
            found.append((
                "--dry-run",
                "--apply has no preview: it merges into the cluster or it "
                "does nothing",
                "Drop --dry-run. To preview the merged YAML without touching "
                "the cluster, use --output-configmap --dry-run or "
                "--output-alertmanager --dry-run (neither can be combined "
                "with --apply)."))

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
                "--output-alertmanager writes a plain alertmanager.yml, which "
                "has no ConfigMap to name",
                f"Drop {flag}, or use --output-configmap instead of "
                f"--output-alertmanager to wrap the config in a ConfigMap."))

    if args.yes and not applying:
        if validating:
            found.append((
                "--yes",
                "--validate never prompts, and never applies",
                "Drop --yes. To apply instead, drop --validate AND add "
                "--apply (keeping --yes)."))
        else:
            found.append((
                "--yes",
                f"{mode} writes YAML and never touches the cluster, so there "
                "is nothing to confirm",
                f"Drop --yes. (--apply, the mode that prompts, cannot be "
                f"combined with {mode}.)"))
    return found


def _build_parser() -> argparse.ArgumentParser:
    """The CLI parser, separately so tests can enumerate its dests (#1650)."""
    parser = argparse.ArgumentParser(
        description="Generate Alertmanager route + receiver config from tenant YAML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Exactly one mode is required: --validate, --output-configmap,
            --output-alertmanager or --apply. The output modes render
            output = merge(your source --base-config, conf.d); the output is
            never a base (a base holding generated receivers is refused).

            Examples:
              %(prog)s --config-dir conf.d/ --validate --strict
              %(prog)s --config-dir conf.d/ --output-alertmanager --base-config my-am.yml -o alertmanager.yml
              %(prog)s --config-dir conf.d/ --output-configmap --base-config my-am.yml -o am-configmap.yaml
              %(prog)s --config-dir conf.d/ --output-alertmanager --dry-run
        """),
    )
    # #3751: a conf.d with no config file is a tree with no tenant (the
    # mode's own answer), as before; the path and a root da-guard cannot
    # list are refused naming the flag.
    _lib_input.add_input(parser, "--config-dir", select=_lib_input.DA_GUARD_LISTS,
                         empty=_lib_input.EMPTY_ALLOWED, required=True,
                         help="Directory containing tenant YAML configs (conf.d/)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output file path for --output-configmap / "
                             "--output-alertmanager (default: stdout)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview output without writing file")
    parser.add_argument("--validate", action="store_true",
                        help="Validate generated config (exit 0 if valid, 1 if errors)")
    parser.add_argument("--strict", action="store_true",
                        help="Escalate to ERROR and fail (exit 1): domain-policy "
                             "violations (ADR-007), an unquoted routes[].match value "
                             "or overrides[].alertname / metric_group that YAML "
                             "reads as a non-string (8, true, ~, 2024-01-02; #2431 — "
                             "quote it), a group_by entry that is not a non-empty string, "
                             "repeats a label or mixes '...' with labels (#2503), "
                             "a tenant _routing that is neither a mapping nor a "
                             "disabling string, a _routing_defaults that is not "
                             "a mapping (#2341) "
                             "and, on the --apply/--output-configmap/"
                             "--output-alertmanager "
                             "merge, any inhibit rule with an ungated `equal:` label "
                             "(#1132), a tenant (no _routing; an explicit "
                             "`_routing: disable` stays WARN) whose served page-mode "
                             "custom alerts fall to a custom-alerts-firehose receiver "
                             "with no integration (#1211), and a watchdog-heartbeat "
                             "receiver with no integration in your own base "
                             "(--base-config, or the cluster's on --apply; #1211). "
                             "Without --strict these "
                             "surface as WARN (an "
                             "unquoted override value is rendered via str() "
                             "instead; a bad group_by entry is dropped). CI runs "
                             "--strict. NOT included: a credential written out "
                             "(credential_plaintext / credential_unjudged, #1560) — "
                             "only --validate (or validate-config) refuses it; the "
                             "output modes and --apply print it and go on, with "
                             "--strict too, so gate CI / BYO on --validate.")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--apply", action="store_true",
                            help="Apply: render from the cluster ConfigMap's "
                                 "source (alertmanager.base.yml; on the first "
                                 "run its alertmanager.yml), write the output "
                                 "and the source back + reload")
    mode_group.add_argument("--output-configmap", action="store_true",
                            help="Output the merged Alertmanager config as a "
                                 "ConfigMap (for GitOps PR flow)")
    mode_group.add_argument("--output-alertmanager", action="store_true",
                            help="Output the merged Alertmanager config as a "
                                 "plain alertmanager.yml (BYO Alertmanager, or "
                                 "the Prometheus Operator's configSecret)")
    parser.add_argument("--base-config", default=None,
                        help="Your Alertmanager YAML for --output-configmap / "
                             "--output-alertmanager — the SOURCE the generated "
                             "routing is merged into, never an earlier output "
                             "(default: a built-in placeholder)")
    # #1650: default=None so "supplied" is decidable; the value is resolved
    # to _DEFAULT_NAMESPACE / _DEFAULT_CONFIGMAP after the mode check below.
    parser.add_argument("--namespace", default=None,
                        help=f"K8s namespace for --apply/--output-configmap "
                             f"(default: {_DEFAULT_NAMESPACE})")
    parser.add_argument("--configmap", default=None,
                        help=f"ConfigMap name for --apply/--output-configmap "
                             f"(default: {_DEFAULT_CONFIGMAP})")
    add_policy_argument(parser)
    parser.add_argument("--secret-mount-root", dest="mount_root", default=DEFAULT_MOUNT_ROOT,
                        metavar="PATH",
                        help="Where the Alertmanager container mounts the "
                             "Secrets a receiver's Secret reference names "
                             "(#1560): a reference renders as "
                             "<field>_file: <PATH>/<secret>/<key>, the tenant's "
                             "own Secret being da-<tenant> and a shared one the "
                             "--policy shared_secrets entry's (default: "
                             f"{DEFAULT_MOUNT_ROOT}, Prometheus Operator's "
                             "spec.secrets layout)")
    parser.add_argument("--secret-mounts-out", dest="mount_list_out", default=None, metavar="PATH",
                        help="With --output-alertmanager / --output-configmap: "
                             f"also write, as JSON ({MOUNT_LIST_SCHEMA}), "
                             "the Secrets the emitted config reads under "
                             "--secret-mount-root — each Secret name with its "
                             "keys (names only, never values), the base "
                             "config's own *_file paths included; paths "
                             "outside the root are listed as other_files. "
                             "The list spec.secrets (Prometheus Operator) or "
                             "your own mounts must match; operator-check "
                             "--secret-mounts compares it with the "
                             "Alertmanager CR (#1560)")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt for --apply")
    parser.add_argument("--findings-json", default=None, metavar="PATH",
                        help="Also write the findings this run prints (its "
                             "warning-stream and refusal lines, each with "
                             "kind / severity / blocks / tenant / policy / "
                             f"file / field) as JSON ({FINDINGS_SCHEMA}) to "
                             "PATH, atomically, on every exit (usage errors "
                             "and exceptions included); stdout, stderr and "
                             "the exit code are unchanged")
    return parser


# ── #2766 (ADR-036 §4): --findings-json ────────────────────────────────
FINDINGS_SCHEMA = "da-tools.findings/v1"


def _sink_extend(sink: list | None, findings: list) -> None:
    """Record *findings* for --findings-json (no-op without a sink)."""
    if sink is not None:
        sink.extend(findings)


def da_tools_version() -> str | None:
    """The da-tools release this generator ships in, or None if unreadable.

    The image's flat layout puts ``VERSION`` beside this file
    (``/opt/da-tools``); the repo keeps it at ``components/da-tools/app``.
    Shared with `da-tools init`, which pins the image it writes to the same
    value (#2900), so the two cannot disagree about which release they are.
    """
    return read_shipped_da_tools_version(_THIS_DIR)


# ── The deployment policy, one resolver for every tool that takes it (#3142)
#
# generate-routes, validate-config, test-notification and explain-route judge
# receivers by the same `--policy` file: the flag, its reading
# (`load_policy_document`), the generator-version gate (ADR-036 §6) and the
# words they print are defined here once, so no tool reads it its own way.
# generator-service is not a caller: it reads the file per judgement and
# answers 503 (ADR-036 §5).

# The words (help, NOTICE, remedy) and the refusal's kind live beside
# `load_policy_document` in _grar_validate, which validate-config imports
# without loading this module.
from _grar_validate import (  # noqa: E402,F401
    POLICY_HELP, POLICY_NOT_APPLIED_NOTICE, POLICY_REFUSAL_KIND, POLICY_REFUSAL_REMEDY,
    POLICY_VERSION_REMEDY, PolicyVersionError,
)


class ResolvedPolicy(NamedTuple):
    """The deployment policy a run judges under. ``path`` None = no
    ``--policy``: nothing is applied (``allowed_domains`` None)."""

    path: str | None
    allowed_domains: list[str] | None
    min_generator_version: str | None
    #: #1560: the policy's ``shared_secrets`` (empty: none, or no policy).
    shared_secrets: Mapping[str, SharedSecret] = MappingProxyType({})

    @property
    def applied(self) -> bool:
        return self.path is not None

    def secret_binding(self, mount_root: str = DEFAULT_MOUNT_ROOT) -> SecretBinding:
        """#1560: what the route builders bind Secret references with —
        this policy's ``shared_secrets`` and *mount_root*."""
        return SecretBinding(mount_root, self.shared_secrets)


def mount_root_value_problem(value: str) -> str | None:
    """#1560: why *value* cannot be ``--secret-mount-root``, or None. It is
    written into every rendered ``<field>_file`` path, which Alertmanager
    opens inside its own container: an absolute path, one line, no ``..``
    segment."""
    if (not value.startswith("/") or "\n" in value or "\r" in value
            or ".." in value.split("/")):
        return (f"--secret-mount-root {value!r} must be an absolute path on one "
                "line without '..' segments (where the Secrets are mounted in "
                f"the Alertmanager container, default {DEFAULT_MOUNT_ROOT})")
    return None


def add_policy_argument(parser: argparse.ArgumentParser, *, lang: str = "en") -> None:
    """``--policy PATH``, the same flag on every tool that judges receivers."""
    parser.add_argument("--policy", default=None, metavar="PATH",
                        help=POLICY_HELP.get(lang, POLICY_HELP["en"]))


def resolve_policy(policy_path: str | None) -> ResolvedPolicy:
    """The deployment policy at *policy_path* (None: not given).

    Raises ``PolicyInputError`` — the caller's exit 2 — when the file is
    unusable (``load_policy_document``) or names a ``min_generator_version``
    this generator does not meet (ADR-036 §6): judging under it would use
    rules its author did not mean.
    """
    if policy_path is None:   # ⛔ not truthiness: "" is supplied (#1616)
        return ResolvedPolicy(None, None, None)
    doc = load_policy_document(policy_path)
    too_old = generator_version_problem(da_tools_version(), doc.min_generator_version)
    if too_old is not None:
        raise PolicyVersionError(f"--policy: {too_old}")
    return ResolvedPolicy(policy_path, doc.allowed_domains, doc.min_generator_version,
                          doc.shared_secrets)


def is_policy_refusal(finding: object) -> bool:
    """Whether *finding* is the allowlist dropping a receiver."""
    return getattr(finding, "kind", None) == POLICY_REFUSAL_KIND


def in_force_blocks(*, validate: bool, strict: bool) -> set[str]:
    """The ``blocks`` values that make a run in this mode fail: ``always``,
    plus ``validate`` under --validate and ``strict`` under --strict. A
    reader of a ``--findings-json`` document takes the mode from its
    ``validate`` / ``strict`` fields."""
    return {"always"} | ({"validate"} if validate else set()) \
        | ({"strict"} if strict else set())


def findings_document(findings: list[str], *, config_dir: str,
                      exit_code: int, validate: bool = False,
                      strict: bool = False) -> dict:
    """The ``--findings-json`` document (schema ``FINDINGS_SCHEMA``).

    ``findings`` keeps the run's order; each record is
    ``Finding.as_record()`` — ``message`` is the line's exact text with only
    its leading indentation stripped.
    """
    records = [as_finding(f) for f in findings]
    # #2766: a non-zero exit with no finding that blocks in this run's mode
    # (an Alertmanager rejection, an assembly refusal, "no valid routes", a caller
    # error) must not read as a pass to a consumer that looks only at the
    # findings — the document is never looser than the run's own exit code.
    in_force = in_force_blocks(validate=validate, strict=strict)
    if exit_code != EXIT_OK and not any(f.blocks in in_force for f in records):
        records.append(Finding(
            f"exit code {exit_code}: the run failed for a reason not itemised "
            "here — read the run's stderr",
            kind="run_failed", severity="error", blocks="always"))
    return {
        "schema": FINDINGS_SCHEMA,
        "generator_version": da_tools_version(),
        "config_dir": config_dir,
        "exit_code": exit_code,
        "validate": validate,
        "strict": strict,
        "findings": [f.as_record() for f in records],
    }


def write_findings_json(path: str, doc: dict) -> None:
    """Write *doc* to *path* as an operator output (#3107 v2), or exit 2.

    UTF-8, ``ensure_ascii=False``, ``sort_keys``, ``allow_nan=False``. The
    kernel opens *path* (``-`` is stdout); a failed write leaves a regular
    file empty, never half a document (owner 2026-10-24: gar is written
    directly, not replaced atomically).
    """
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, indent=2) + "\n"
    _lib_io.write_operator_output_or_die(path, text, flag="--findings-json",
                                         exit_code=EXIT_CALLER_ERROR)


def _exit_code_of(exc: SystemExit) -> int:
    """The process exit status ``sys.exit(code)`` produces."""
    if exc.code is None:
        return EXIT_OK
    if isinstance(exc.code, int):
        return exc.code
    return 1  # a message: Python prints it and exits 1


def main(argv: list[str] | None = None, *, key_verdict: bool = True,
         credential_scope: "frozenset[str] | None" = None) -> None:
    """CLI entry point: Generate Alertmanager route + receiver + inhibit config from tenant YAML.

    *argv* defaults to ``sys.argv[1:]``; the generator container (#2901)
    passes its own, in the judgement process. *key_verdict*: whether
    ``--validate`` also reports the shared key verdict (ADR-038 §5). The
    container passes False: it judges the keys itself, after the generator
    passes the write (``_gsvc_child.key_verdict``), so running it here too
    would judge every write twice. *credential_scope* (#1560 ⑤-b): the
    files a credential written out refuses the run for (``credential_findings``'
    *scope*); the container passes the files the write changes, so tenant-api
    refuses a write by the files it writes. None: every file (every CLI run)."""
    parser = _build_parser()
    # #2766: every finding the run prints, in the order it was determined.
    # --findings-json writes it on EVERY exit — a refusal, a verdict, a caller
    # error, a usage error, an exception — so a document a previous run left
    # at PATH is never read as this run's. Its path is read before the full
    # parse for exactly that: a usage error (rc 2) still replaces it.
    early = _early_findings_args(argv)
    findings: list[str] = []
    args: argparse.Namespace | None = None
    # #3090: what an exception other than SystemExit ends the process with.
    code = effective_crash_exit_code()
    try:
        args = parser.parse_args(argv)
        # #3107 v2: `--findings-json -` (or the file stdout already is) owns
        # stdout. Without -o the routes go there too — refused before any is
        # printed; with -o the run's own stdout lines move to stderr, so
        # stdout carries the findings document alone. #1560: the same for
        # `--secret-mounts-out -` in the modes that read it; the two together
        # on stdout are two documents on one stream (claim_stdout: rc 2).
        # #1560 (CodeRabbit on #3615): every destination this run writes is
        # judged by ONE function, before anything is written.
        receipts = contextlib.nullcontext()
        if refuse_clashing_outputs(args):
            receipts = contextlib.redirect_stdout(sys.stderr)
        # #1560: the mount list is written HERE, after the redirect ends, so
        # `--secret-mounts-out -` reaches the real stdout.
        mount_lists: list[tuple[str, dict]] = []
        # ADR-036 step 4 (#2906): one `da-guard parse-tree` reading per run.
        # A judge (#3206): it refuses a tree Go names paths on as its own
        # finding, so the reader's fail-closed exit stands aside.
        with receipts, _lib_parse_cache.session(), _lib_parse_cache.judging():
            _run(args, findings, key_verdict=key_verdict, mount_lists=mount_lists,
                 credential_scope=credential_scope)
        for path, doc in mount_lists:
            _write_mount_list(path, doc)
        code = EXIT_OK
    except SystemExit as exc:
        code = _exit_code_of(exc)
        _write_findings_if_asked(early, args, findings, code)
        raise
    except BaseException:
        # An exception the run did not turn into an exit: write the failing
        # document, but a PATH that cannot be written must not replace the
        # exception (its traceback is the diagnosis) — the ERROR line is
        # printed and the exception propagates.
        _write_findings_if_asked(early, args, findings, code,
                                 keep_exception=True)
        raise
    _write_findings_if_asked(early, args, findings, code)


# The shared verdict's finding kinds → this generator's (FINDING_KINDS).
_KEY_VERDICT_KINDS = {
    "unread": "key_unread",
    "disable_noop": "key_disable_noop",
    "disable_cascade": "key_disable_cascade",
    "profile_not_judged": "key_profile_not_judged",
    "value_unread": "key_value_unread",
    "dead_entry": "key_dead_entry",
    "schedule_unread": "key_schedule_unread",
}

# The exporter's key checks (`_lib_key_verdict.key_check_lines`) → FINDING_KINDS.
_KEY_CHECK_KINDS = {
    _lib_key_verdict.KEY_CHECK_VERSION_LABEL: "key_version_label",
    _lib_key_verdict.KEY_CHECK_RENAMED: "key_renamed",
}


def _report_key_verdict(config_dir: str, findings: list[str]) -> list[str]:
    """The shared verdict on keys no reader reads (``_lib_key_verdict``,
    ADR-038 §5), under ``--validate``: the same lines validate-config's
    ``unread_keys`` row prints, on stderr. Returns the lines that block the
    run (#3016), each a ``blocks: validate`` finding: the findings the
    verdict says block (an error), the undecidable line when a threshold key
    could not be judged (decision 7), and the one line saying no verdict was
    given — a tree no verdict was given for does not pass, as tenant-api
    refuses a write it has none for (decision 5). Warnings and notices are
    ``blocks: never``.

    The verdict is ``da-guard key-verdict``'s own run; the custom-alert
    delivery check (#1211) reads served-values separately, so a failing
    verdict leaves that check as it is."""
    try:
        verdict = _lib_key_verdict.judge_tree(config_dir)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — "not judged": no pass
        line = Finding(
            "  ERROR: key verdict incomplete: the keys no reader reads were not "
            f"judged ({type(exc).__name__}: {' '.join(str(exc).split())})",
            kind="key_verdict_incomplete", blocks="validate")
        print(safe_label(line), file=sys.stderr)
        findings.append(line)
        # The da-guard binary's own fault (missing, stale, broken) is no
        # verdict on the config: rc 2 as validate-config's unread_keys, and
        # ahead of any violation (#3016 review). A tree da-guard refuses to
        # judge is the tree's: a blocking line, rc 1.
        tv = _lib_tenant_values
        if isinstance(exc, tv.DaGuardNotFoundError) or (
                isinstance(exc, tv.DaGuardError) and exc.binary_fault):
            sys.exit(EXIT_CALLER_ERROR)
        return [line]
    lines = []
    for f in verdict.findings:
        level = ("ERROR" if f.blocks else
                 "NOTICE" if f.severity == _lib_key_verdict.NOTICE else "WARN")
        lines.append(Finding(
            f"  {level}: key verdict: {_lib_key_verdict.finding_line(f, WARN_LIMIT)}",
            kind=_KEY_VERDICT_KINDS[f.kind],
            blocks="validate" if f.blocks else "never",
            tenant=f.tenants[0] if len(f.tenants) == 1 else None,
            file=f.file, field=key_segment(f.key)))
    undecidable = _lib_key_verdict.undecidable_line(verdict)
    if undecidable is not None:
        stops = verdict.undecidable_blocks
        lines.append(Finding(f"  {'ERROR' if stops else 'NOTICE'}: key verdict: {undecidable}",
                             kind="key_undecidable",
                             blocks="validate" if stops else "never"))
    # ADR-038 §11: the exporter's own key checks (a `version` label, a
    # retired spelling), printed as it wrote them — this tool no longer
    # judges keys itself.
    for kind, tenant, msg in _lib_key_verdict.key_check_lines(verdict):
        lines.append(Finding(f"  {msg}", kind=_KEY_CHECK_KINDS[kind],
                             blocks="never", tenant=tenant))
    for line in lines:
        print(safe_label(line), file=sys.stderr)
    findings.extend(lines)
    return [line for line in lines if line.blocks == "validate"]


# ── #1211: served page-mode custom alerts that reach no one ─────────

def _load_served_tree(config_dir: str, *, disable_effects: bool,
                      ) -> "_lib_tenant_values.ServedTree | BaseException":
    """The run's ONE da-guard served-values reading of *config_dir*, or the
    exception that stopped it (each reader reports that its own way: never a
    refusal — a broken da-guard must not fail a run the generator passed)."""
    try:
        return _lib_tenant_values.load_served_tree(config_dir,
                                                   disable_effects=disable_effects)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — "not checked", never a refusal
        return exc


def _served_page_custom_alerts(served: "_lib_tenant_values.ServedTree",
                               ) -> dict[str, tuple[str, ...]]:
    """tenant -> the names of its custom alerts the exporter SERVES in page
    mode (``mode`` unset or ``page``), declaration order. The exporter is the
    authority on which custom alerts a tenant has (#3042): a rejected,
    duplicate or switched-off entry is not one."""
    out = {}
    for tenant, values in served.tenants.items():
        names = tuple(str(e.spec.get("name") or f"_custom_alerts[{e.index}]")
                      for e in values.custom_alerts or ()
                      if e.status == "served" and e.spec.get("mode") != "silent")
        if names:
            out[tenant] = names
    return out


def _undelivered_custom_alerts(served: "_lib_tenant_values.ServedTree | BaseException",
                               routes: list[dict], tree: TenantTree,
                               findings: list[str]) -> list[UndeliveredCustomAlerts]:
    """The tenants whose served page-mode custom alerts get no delivery child
    under the custom route. A served-values reading that failed is one
    NOTICE (``blocks: never``) and no tenant."""
    if isinstance(served, BaseException):
        line = Finding(
            "NOTICE: custom-alert delivery not checked: the tenants' served custom "
            f"alerts could not be read ({type(served).__name__}: "
            f"{' '.join(str(served).split())})",
            kind="custom_alert_delivery_unchecked", blocks="never")
        print(safe_label(line), file=sys.stderr)
        findings.append(line)
        return []
    return undelivered_custom_alert_tenants(_served_page_custom_alerts(served),
                                            routes, tree.routing_disabled)


def _emit_custom_firehose_findings(undelivered: list[UndeliveredCustomAlerts],
                                   firehose_has_integration: bool | None, *,
                                   strict: bool, findings: list[str]) -> bool:
    """Print and record the #1211 findings; True when one blocks under
    *strict* (an ERROR line was printed)."""
    lines = custom_firehose_findings(undelivered, firehose_has_integration,
                                     strict=strict)
    for line in lines:
        print(safe_label(line), file=sys.stderr)
    findings.extend(lines)
    return strict and any(f.blocks == "strict" for f in lines)


def _assembled_judge(undelivered: list[UndeliveredCustomAlerts], *, strict: bool,
                     findings: list[str], real_base: bool):
    """The check the output modes and ``--apply`` run on the config they are
    about to write / apply, as Alertmanager reads it: only there are the
    firehose and watchdog-heartbeat receivers known. The judge prints and
    records its findings and returns the reasons to refuse (rc 1) — empty
    unless one blocks under *strict*."""
    def judge(am: dict) -> list[str]:
        reasons = []
        if undelivered and _emit_custom_firehose_findings(
                undelivered, custom_firehose_has_integration(am), strict=strict,
                findings=findings):
            reasons.append("page-mode custom alerts would notify no one")
        lines = watchdog_receiver_findings(am, strict=strict, real_base=real_base)
        for line in lines:
            print(safe_label(line), file=sys.stderr)
        findings.extend(lines)
        if strict and any(f.blocks == "strict" for f in lines):
            reasons.append("the Watchdog heartbeat would reach no dead-man's switch")
        return reasons
    return judge


# #1560 ⑤-a: the one "do these two outputs land in one file" judgement,
# shared with migrate-credentials (``_lib_io.same_file``).
_is_special_file = _lib_io.is_special_file
_same_file = _lib_io.same_file


def _names_the_source(dest: str | None, base_config: str | None) -> bool:
    """Whether writing *dest* would overwrite the --base-config file (the
    source, #1211 gap 2): ``_same_file``."""
    return _same_file(dest, base_config)


#: The stdout owner the routes output is named as (#3107 v2).
ROUTES_ON_STDOUT = "the routes output (no -o)"


def output_destinations(args: argparse.Namespace) -> tuple[list[tuple[str, str]], list[str]]:
    """What this run writes, in the mode it runs (#1560): ``(files, stdout)``.

    *files*: ``(flag, path)`` of each FILE written — ``-o`` in an output mode
    without ``--dry-run``, ``--findings-json`` (every mode, every exit),
    ``--secret-mounts-out`` where it is read. *stdout*: the owners of
    standard output — the routes output when an output mode prints it (no
    ``-o``, or an ``-o`` that is the file / pipe fd 1 already is), and each
    operator output given ``-`` or fd 1's file (``targets_stdout``). A flag
    the mode never reads is not a destination: ``_flags_this_mode_never_reads``
    refuses it by name.

    ⛔ ``-o -`` is a file named ``-`` (the routes writer is not an operator
    output), so it is a file here, never stdout.
    """
    assembles = ((args.output_alertmanager or args.output_configmap)
                 and not args.validate)
    files: list[tuple[str, str]] = []
    stdout: list[str] = []
    if assembles and not args.dry_run:
        if args.output is None:
            stdout.append(ROUTES_ON_STDOUT)
        elif args.output != _lib_io.STDOUT_PATH and _lib_io.targets_stdout(args.output):
            stdout.append(f"-o/--output {args.output}")
        else:
            files.append(("-o/--output", args.output))
    elif args.output is None and args.findings_json is not None:
        # #3107 v2 as it stood: without -o this run's stdout is the routes
        # output's (or its report) — `--findings-json -` is a second writer.
        stdout.append(ROUTES_ON_STDOUT)
    for flag, value in (("--findings-json", args.findings_json),
                        ("--secret-mounts-out",
                         args.mount_list_out if assembles and not args.dry_run else None)):
        if value is None:
            continue
        if _lib_io.targets_stdout(value):
            stdout.append(flag)
        else:
            files.append((flag, value))
    return files, stdout


def output_clashes(args: argparse.Namespace) -> list[tuple[frozenset[str], str]]:
    """Every pair of this run's outputs that land in one place (#1560), as
    ``(flags, line)``: the flags whose destination is taken — none of them
    may be written — and the line that says why. Two files that are one file
    (``_same_file``: a symlink or a hard link to it included), a file that
    is the ``--base-config`` source (in the modes that read it), more than
    one writer of stdout. The ONE judgement: ``refuse_clashing_outputs``
    refuses the run by it, ``_write_findings_if_asked`` skips the document
    by it (the document is written on every exit, a refusal included)."""
    files, stdout = output_destinations(args)
    found: list[tuple[frozenset[str], str]] = []
    reads_base = ((args.output_alertmanager or args.output_configmap)
                  and not args.validate and args.base_config is not None)
    for flag, path in files:
        if reads_base and _names_the_source(path, args.base_config):
            found.append((frozenset({flag}),
                f"{flag} names the --base-config file itself: the base is your "
                "source config and the output is rendered from it, so writing over "
                "it would destroy the source (refused).\n"
                "  Write it to another path and keep the source as it is."))
    for i, (flag_a, path_a) in enumerate(files):
        for flag_b, path_b in files[i + 1:]:
            if _same_file(path_a, path_b):
                found.append((frozenset({flag_a, flag_b}),
                    f"{flag_b} names the same file as {flag_a}: one would replace "
                    "the other (refused).\n  Write each to its own path."))
    if len(stdout) > 1:
        found.append((frozenset(stdout),
            f"standard output was asked for by: {', '.join(stdout)} — only one of "
            "them may write there (refused)."))
    return found


def refuse_clashing_outputs(args: argparse.Namespace) -> bool:
    """Refuse (rc 2, before anything is written) a run with an
    ``output_clashes`` entry. Returns whether an operator output owns
    stdout (the caller moves its own receipts to stderr); the stdout owners
    are claimed (``_lib_io.claim_stdout``)."""
    clashes = output_clashes(args)
    if clashes:
        die_caller_error("ERROR: " + "\nERROR: ".join(line for _, line in clashes))
    _, stdout = output_destinations(args)
    for owner in stdout:
        _lib_io.claim_stdout(owner)
    return any(owner != ROUTES_ON_STDOUT and not owner.startswith("-o/") for owner in stdout)


#: #1560: the flags whose value names a file on THIS host (argparse dest →
#: flag), read from the raw argv as well as from the parse: the
#: --findings-json document never lands on any of them. Not listed:
#: --config-dir (a directory — the document's write to it fails anyway),
#: --secret-mount-root (a path inside the Alertmanager container, never
#: opened here); the rest of the flags name no path.
FINDINGS_MUST_NOT_REPLACE = {"output": "-o/--output", "mount_list_out": "--secret-mounts-out",
                             "base_config": "--base-config", "policy": "--policy"}
#: Of those, the operator outputs, for which `-` is stdout, not a file.
_STDOUT_DASH_FLAGS = frozenset({"--secret-mounts-out"})


def findings_path_taken_by(findings: str | None, early: argparse.Namespace,
                           args: argparse.Namespace | None) -> str | None:
    """The flag (of ``FINDINGS_MUST_NOT_REPLACE``) whose file *findings*
    would replace, or None. Its values come from the raw-argv scan
    (``early.path_values``, every spelling argparse accepts) and, when the
    parse succeeded, from *args* — independent of the mode and of whether
    the run was refused. ``_same_file`` judges (a path that is not a
    regular file is never taken). A findings path that is stdout is not a
    file: stdout has its own owners (``claim_stdout``)."""
    if findings is None or _lib_io.targets_stdout(findings):
        return None
    values = list(getattr(early, "path_values", []))
    if args is not None:
        values += [(flag, getattr(args, dest, None))
                   for dest, flag in FINDINGS_MUST_NOT_REPLACE.items()]
    for flag, value in values:
        if value is None or (value == _lib_io.STDOUT_PATH and flag in _STDOUT_DASH_FLAGS):
            continue
        if _same_file(findings, value):
            return flag
    return None


def _write_findings_if_asked(early: argparse.Namespace,
                             args: argparse.Namespace | None,
                             findings: list[str], code: int, *,
                             keep_exception: bool = False) -> None:
    """Write the --findings-json document, if one was asked for (#2766).

    With *keep_exception* (an exception is propagating), a failed write
    prints its ERROR line but does not exit, so the exception survives."""
    src = args if args is not None else early
    if src.findings_json is None:
        return
    # #1560 (#1211 gap 2 for the source): the document is written on every
    # exit — a usage error and a refusal included — so it must never land on
    # a file another path flag of this command line names, whatever the mode
    # and whether the parse succeeded. ONE invariant, not the run's verdict.
    taken = findings_path_taken_by(src.findings_json, early, args)
    if taken is not None:
        what = ("the source is kept as it is" if taken == "--base-config"
                else "the file is kept as it is")
        print(f"ERROR: --findings-json names the same file as {taken} — "
              f"not written, {what}", file=sys.stderr)
        return
    doc = findings_document(findings, config_dir=src.config_dir,
                            exit_code=code, validate=src.validate,
                            strict=src.strict)
    if getattr(src, "output", None) is None:
        # #3107 v2: without -o the routes go to stdout; `--findings-json -`
        # would interleave a second document there.
        _lib_io.claim_stdout("the routes output (no -o)")
    if not keep_exception:
        write_findings_json(src.findings_json, doc)
        return
    with contextlib.suppress(SystemExit):
        write_findings_json(src.findings_json, doc)


def _path_flag_values(argv: list[str]) -> list[tuple[str, str]]:
    """``(flag, value)`` of every ``FINDINGS_MUST_NOT_REPLACE`` flag on the
    raw *argv* (#1560), in every spelling argparse accepts — ``--flag V``,
    ``--flag=V``, an abbreviation (``--secret-mounts-o``), ``-o V``, ``-oV``.
    An abbreviation that could be more than one flag is read as each: this
    list only ever STOPS a write, so reading too much is safe and reading
    too little overwrote a file (review of #3615)."""
    longs = [f.split("/")[-1] if f.startswith("-o/") else f
             for f in FINDINGS_MUST_NOT_REPLACE.values()]
    found: list[tuple[str, str]] = []
    for i, tok in enumerate(argv):
        if tok == "--":
            break
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        follows = nxt if nxt is not None and (nxt == "-" or not nxt.startswith("-")) else None
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            for long in longs:
                if len(name) >= 3 and long.startswith(name):
                    flag = "-o/--output" if long == "--output" else long
                    v = value if eq else follows
                    if v is not None:
                        found.append((flag, v))
        elif len(tok) > 1 and "o" in tok[1:]:
            # Short flags, argparse's way: this tool's are `-h` (no value)
            # and `-o`, which may follow others in one token (`-ho V`,
            # `-hoV`). After the `o`, the rest of the token is the value
            # (`-oV`); an `o` at the end takes the next token. `-o=V` is read
            # both ways (argparse's reading of the `=` has changed between
            # versions), which is safe here.
            rest = tok[tok.index("o", 1) + 1:]
            if not rest:
                if follows is not None:
                    found.append(("-o/--output", follows))
            else:
                found.append(("-o/--output", rest))
                if rest.startswith("="):
                    found.append(("-o/--output", rest[1:]))
    return found


def _early_findings_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The flags the --findings-json document needs, read from argv before
    the full parse (#2766), so a usage error still replaces a stale document
    — and ``--base-config``, so it never replaces the source (#1211 gap 2). A plain scan, not a second parser (the CLI contract check reads
    this file's parser): the exact spellings ``--flag VALUE`` /
    ``--flag=VALUE`` only — an abbreviated flag is found by the full parse
    alone."""
    argv = sys.argv[1:] if argv is None else argv
    early = argparse.Namespace(findings_json=None, config_dir=None, base_config=None,
                               validate="--validate" in argv,
                               strict="--strict" in argv,
                               path_values=_path_flag_values(argv))
    for i, tok in enumerate(argv):
        if tok == "--":
            break
        for flag, dest in (("--findings-json", "findings_json"),
                           ("--config-dir", "config_dir")):
            if (tok == flag and i + 1 < len(argv)
                    and not argv[i + 1].startswith("-")):
                setattr(early, dest, argv[i + 1])
            elif tok.startswith(flag + "="):
                setattr(early, dest, tok[len(flag) + 1:])
        # ⛔ --base-config is read in every spelling argparse accepts — an
        # abbreviation too (`--base`; no other flag starts with `--b`): it
        # only ever STOPS a write (`_names_the_source`), so reading too much
        # is safe and reading too little overwrote the source (review F4').
        name, eq, value = tok.partition("=")
        if len(name) >= 3 and "--base-config".startswith(name):
            if eq:
                early.base_config = value
            elif i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                early.base_config = argv[i + 1]
    return early


def _resolve_inputs(policy_path: str | None, mount_root: str) -> ResolvedPolicy:
    """What every mode settles before the tenant scan: the deployment policy,
    ``--secret-mount-root`` and the shipped platform probe set. Raises
    ``PolicyInputError`` (the caller's exit 2); the other two exit 2 here."""
    # Load policy (webhook domain allowlist).
    # #1556: a supplied-but-unusable --policy is a caller error, not "no
    # policy". Exiting 2 rather than proceeding with an empty allowlist
    # is the whole point — the previous behaviour generated routes with SSRF
    # domain checking off and said nothing.
    # #2901 (ADR-036 §6): a policy may name the oldest generator it accepts;
    # an older one would judge by rules the policy's author did not mean.
    policy = resolve_policy(policy_path)
    # #1560: written into every rendered `<field>_file` path.
    mount_root_problem = mount_root_value_problem(mount_root)
    if mount_root_problem is not None:
        die_caller_error(f"ERROR: {mount_root_problem}")
    # #3206 (ADR-036 §12): the platform probe set is read from the shipped
    # rules ConfigMap through da-crdecode and `da-read prometheus-rules`.
    # Without them that is a named rc 2 here, before any work — not a crash
    # in the first guard that asks, and never another reading.
    try:
        platform_alert_identities()
    except _lib_consumer_read.ConsumerReadError as exc:
        die_caller_error(
            "ERROR: cannot read the shipped platform rules — their ConfigMap is "
            "read the way kubectl does (da-crdecode) and its rule files the way "
            "Prometheus' rule loader does (da-read prometheus-rules); stopping "
            "before any work (nothing was written): "
            + _lib_consumer_read.error_text(exc))
    if policy.allowed_domains:
        print(f"Policy: {len(policy.allowed_domains)} allowed domain pattern(s) loaded")
    return policy


def _scan_and_generate(config_dir: str, findings: list[str], *,
                       policy: ResolvedPolicy, mount_root: str, strict: bool,
                       validate: bool, assembles_file: bool,
                       key_verdict: bool = True,
                       credential_scope: "frozenset[str] | None" = None,
                       key_blocks: "list[str] | None" = None,
                       ) -> tuple[TenantTree, list[dict], list[dict], list[dict], list[str]]:
    """Scan *config_dir*, refuse what every mode refuses, generate:
    ``(tree, routes, receivers, inhibit_rules, all_warnings)``. Exits where
    the run ends short of a generation result. *validate*: this is
    ``validate_tree``'s run (the shared key verdict's blocking lines go to
    *key_blocks*); *assembles_file*: an output mode that writes the render
    (``--output-configmap`` / ``--output-alertmanager``)."""
    allowed_domains = policy.allowed_domains or []
    # Load tenant configs (routing + dedup + schema warnings + enforced routing)
    # #1460: the tree carries the record of the files that did not parse, and
    # that record decides whether anything below may call itself a result.
    tree = load_tenant_tree(config_dir, strict_policies=strict,
                            secrets=policy.secret_binding(mount_root),
                            allowed_domains=allowed_domains)
    routing_configs = tree.routing_configs
    dedup_configs = tree.dedup_configs
    schema_warnings = tree.schema_warnings
    enforced_routing = tree.enforced_routing
    # #2766: --findings-json holds exactly the findings this run PRINTS — a
    # refusal prints its own lines and not the schema stream, so the stream
    # joins only once no refusal has stopped the run.
    _refuse_unreadable_tenant_files(tree, findings)
    _refuse_duplicate_tenants(tree, findings)
    _refuse_routing_tree_errors(tree, findings)
    _refuse_invalid_tenant_ids(tree, findings)
    _refuse_field_type_errors(tree, findings)
    findings.extend(schema_warnings)
    # #1560 ⑤-b: a credential a file writes out is judged on that file's own
    # value, before any layer is merged — after the refusals (a refused tree
    # is judged by nothing else), before any early exit (a tree with nothing
    # to render, an unbound routing profile, is judged too). Blocking under
    # --validate (blocking_generation_errors) and printed in every mode.
    credential_lines = credential_findings(tree, scope=credential_scope)
    findings.extend(credential_lines)
    # ADR-038 §5: after the refusals (a refused tree is judged by nothing
    # else), before the exits below that end the run short of
    # `_validate_mode` ("No tenants found", "No valid routes …"): a tree with
    # nothing to render is judged too.
    key_blocks = [] if key_blocks is None else key_blocks
    if validate and key_verdict:
        key_blocks.extend(_report_key_verdict(config_dir, findings))

    has_routing = bool(routing_configs)
    has_dedup = bool(dedup_configs)

    if not has_routing and not has_dedup and not enforced_routing:
        # #2341: "nothing to render" can be the result of refusing every
        # tenant (an unreadable `_routing`; an invalid tenant id is refused
        # above, ADR-035 D3): those lines are blocking here as everywhere else — `--validate` and
        # `--strict` fail on them, render mode prints them and exits 0.
        for w in schema_warnings + credential_lines:
            print(safe_label(w), file=sys.stderr)
        errors = ((blocking_generation_errors(schema_warnings + credential_lines)
                   if validate else [])
                  + (_policy_errors(schema_warnings) if strict else [])
                  + key_blocks)
        if errors:
            print(f"FAIL: {len(errors)} error(s) found and nothing to render:",
                  file=sys.stderr)
            for e in errors:
                print(safe_label(e), file=sys.stderr)
            sys.exit(EXIT_VIOLATION)
        if not assembles_file:
            print("No tenants found in config directory.")
            sys.exit(EXIT_OK)
        # #1211 gap 2: an output mode still writes the render — the output is
        # always merge(source, conf.d), here the source plus the platform
        # routes. --apply
        # keeps the early exit: an empty tree is more often a wrong
        # --config-dir than an intent to strip the cluster's routing.
        print("No tenants found in config directory; the output keeps the "
              "base's own content and the platform routes only.")

    _print_config_summary(routing_configs, dedup_configs, enforced_routing)

    # Generate routes + receivers (enforced route inserted first)
    routes, receivers, route_warnings = generate_routes(
        routing_configs, allowed_domains=allowed_domains,
        enforced_routing=enforced_routing, tenants=dedup_configs,
        secrets=policy.secret_binding(mount_root))

    # Generate per-tenant severity dedup inhibit rules
    inhibit_rules, dedup_warnings = generate_inhibit_rules(dedup_configs)

    # Collect all warnings
    all_warnings = schema_warnings + credential_lines + route_warnings + dedup_warnings
    findings.extend(route_warnings + dedup_warnings)
    for w in all_warnings:
        # #1538: escape here, not in `all_warnings` — the same list is handed
        # to --validate / --json consumers, which must stay raw.
        print(safe_label(w), file=sys.stderr)

    # A tree WITH tenants that renders nothing is refused in every mode (a
    # tenant whose only receiver is unusable must not vanish from the
    # output). Only a tree with no tenant at all reaches an output mode with
    # nothing generated (#1211 gap 2: the base loses the earlier run's part).
    if not routes and not inhibit_rules and (
            dedup_configs or not (assembles_file)):
        print("No valid routes or inhibit rules generated.")
        sys.exit(EXIT_VIOLATION)
    return tree, routes, receivers, inhibit_rules, all_warnings


class ValidateOutcome(NamedTuple):
    """What ``validate_tree`` decided."""

    #: The exit code ``generate-routes --validate`` ends with.
    exit_code: int
    #: Every finding the run determined, in order (``--findings-json``'s).
    findings: list
    #: ``evaluate_generated_config``'s verdict; None when the run ended
    #: before a generation result was judged.
    generated: "GeneratedConfigVerdict | None" = None
    #: The unusable ``--policy`` that ended the run (exit 2), if one did.
    policy_error: "PolicyInputError | None" = None


def validate_tree(config_dir: str, *, strict: bool = False,
                  policy: str | None = None,
                  mount_root: str = DEFAULT_MOUNT_ROOT,
                  key_verdict: bool = True,
                  credential_scope: "frozenset[str] | None" = None,
                  findings: list | None = None) -> ValidateOutcome:
    """The whole ``--validate`` judgement of the tree at *config_dir*.

    ``generate-routes --validate`` exits with this function's ``exit_code``
    and validate-config's ``routes`` row is this function's outcome: neither
    decides anything of its own (#3788). It prints what ``--validate``
    prints (stdout and stderr) and never exits. *strict*, *policy*,
    *mount_root*: the flags; *key_verdict*, *credential_scope*: ``main``'s;
    *findings*: the list to record into (``--findings-json``).

    The caller holds the run's parse-tree session and ``judging()``
    (``_lib_parse_cache``) and has judged *config_dir* as its declared input
    (``declared_trees``): those belong to the command line, not to the tree.
    """
    findings = [] if findings is None else findings
    generated: list = []
    policy_error: PolicyInputError | None = None
    try:
        try:
            resolved = _resolve_inputs(policy, mount_root)
        except PolicyInputError as exc:
            policy_error = exc
            die_caller_error(str(exc))
        key_blocks: list[str] = []
        tree, routes, receivers, inhibit_rules, all_warnings = _scan_and_generate(
            config_dir, findings, policy=resolved, mount_root=mount_root,
            strict=strict, validate=True, assembles_file=False,
            key_verdict=key_verdict, credential_scope=credential_scope,
            key_blocks=key_blocks)
        # #1211: a tenant's served page-mode custom alerts with no delivery
        # child under the custom route fall to the firehose receiver, which
        # only a mode that assembles the config it writes knows: --validate
        # says it could not check.
        served = _load_served_tree(config_dir, disable_effects=False)
        undelivered = _undelivered_custom_alerts(served, routes, tree, findings)
        if undelivered:
            _emit_custom_firehose_findings(undelivered, None, strict=strict,
                                           findings=findings)
        _validate_mode(routes, receivers, inhibit_rules, all_warnings, findings,
                       key_blocks, generated)
    except SystemExit as exc:
        code = _exit_code_of(exc)
    return ValidateOutcome(code, findings, generated[0] if generated else None,
                           policy_error)


def _run(args: argparse.Namespace, findings: list[str], *,
         key_verdict: bool = True,
         mount_lists: list[tuple[str, dict]] | None = None,
         credential_scope: "frozenset[str] | None" = None) -> None:
    """``main`` after argument parsing; appends each finding to *findings*
    (#2766) as soon as the run has determined it. *key_verdict*,
    *credential_scope*: ``main``'s.
    *mount_lists* (#1560): receives ``(path, document)`` for
    ``--secret-mounts-out``, which ``main`` writes once the run is done."""

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
    # #1211 gap 2: no mode, no output — never a silent fragment or a
    # built-in base the operator did not ask for. Before any work.
    if mode == "none":
        die_caller_error(NO_MODE_MESSAGE)
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
    writes_a_file_mode = args.output_configmap or args.output_alertmanager
    honours_base_config = writes_a_file_mode and not args.validate
    if args.base_config is not None and not honours_base_config:
        if args.apply:
            why, remedy = (
                "--apply merges into the ConfigMap already in the cluster, so "
                "a base file has no effect",
                "  Drop --base-config. (--output-configmap / "
                "--output-alertmanager, which do read it, cannot be combined "
                "with --apply.)")
        else:
            why, remedy = (
                "--validate returns before the config is assembled, so the "
                "base file is never read — adding --output-configmap or "
                "--output-alertmanager does NOT change that",
                "  Drop --base-config, or drop --validate if you meant to "
                "produce a config.")
        caller_error_lines.append(
            f"--base-config is not read in this mode: {why}.\n{remedy}")
    if caller_error_lines:
        die_caller_error("\n".join(caller_error_lines))

    # #3751: --config-dir judged once, before anything is read — the path,
    # a root da-guard cannot list — each refusal naming the flag. Inside
    # `main`'s session and `judging()`: the tree's reader is the one every
    # later read takes (`load_tenant_tree`), and a tree Go names paths on
    # stays this judge's own finding. The refusal leaves as every caller
    # error of this run does (`die_caller_error`: rc 2, one line), so
    # --findings-json records exit 2, not the crash code an exception
    # through `main` gets.
    try:
        _lib_parse_cache.declared_trees(args, "config_dir", lang="en")
    except CallerError as exc:
        die_caller_error(exc.error_line, exc.reason)
    except ParseTreeError as exc:
        parse_tree_failure_exit(exc, args.config_dir)

    # --validate is one function, shared with validate-config's routes row
    # (#3788): everything below this line that a --validate run does is in
    # it, and nothing of a --validate run is decided after it.
    if args.validate:
        sys.exit(validate_tree(
            args.config_dir, strict=args.strict, policy=args.policy,
            mount_root=args.mount_root, key_verdict=key_verdict,
            credential_scope=credential_scope, findings=findings).exit_code)

    try:
        policy = _resolve_inputs(args.policy, args.mount_root)
    except PolicyInputError as exc:
        die_caller_error(str(exc))

    # #1616: load the base config HERE, not inside _assembled_output_mode.
    # ⛔ Measured with the load left at its original site (after the tenant
    # scan): a typo'd --base-config against a tree that yields no routes still
    # exited 0 in silence, because main() returns at "No tenants found in
    # config directory." before anything looks at the flag. The original defect
    # survived the fix in a narrower shape, and it was a NEW test that caught
    # it, not review. "Supplied but unusable" is a property of the INVOCATION,
    # so it has to be decided before any work — exactly like --policy above.
    base_config: dict | None = None
    # #1211 gap 2 (owner decision S): the base is the operator's SOURCE and
    # the output is rendered from it — writing the output over the source
    # would turn the next run's base into an output, which it refuses.
    # ⛔ Every path this run writes, not just -o: --findings-json over the
    # source destroys it just the same (blind review F4).
    # Every path this run writes against the source and against each other
    # — and who writes stdout — is `refuse_clashing_outputs`, which main()
    # runs before this.
    if writes_a_file_mode:
        try:
            base_config = load_base_config(args.base_config)
        except BaseIsAnOutputError as exc:
            _assembly_failed(exc, "nothing was generated or written")
        except BaseConfigInputError as exc:
            die_caller_error(str(exc))
        # #3257: a credential in the base's global reaches every conf.d
        # receiver that leaves the field out. A verdict on the CONFIG (rc 1),
        # decided here, before the tenant scan, so a tree with no routes
        # cannot pass it in silence (the #1616 lesson above).
        try:
            assert_base_global_inherits_no_credentials(base_config)
        except ValueError as exc:
            _assembly_failed(exc, "nothing was generated or written")

    generated = _scan_and_generate(
        args.config_dir, findings, policy=policy, mount_root=args.mount_root,
        strict=args.strict, validate=False, assembles_file=bool(writes_a_file_mode),
        credential_scope=credential_scope)
    tree, routes, receivers, inhibit_rules, all_warnings = generated

    # #1211: a tenant's served page-mode custom alerts with no delivery child
    # under the custom route fall to the firehose receiver. Only a mode that
    # assembles the config it writes (--output-configmap, --output-alertmanager,
    # --apply) knows that receiver — it judges there. The same modes judge
    # the watchdog-heartbeat receiver.
    served = _load_served_tree(args.config_dir, disable_effects=False)
    undelivered = _undelivered_custom_alerts(served, routes, tree, findings)
    # A real base: the operator's --base-config, or the cluster's on --apply.
    assembled_judge = _assembled_judge(
        undelivered, strict=args.strict, findings=findings,
        real_base=args.apply or args.base_config is not None)

    # #2279: two generated receivers with one name. Blocking in EVERY mode and
    # with or without --strict — Alertmanager refuses such a config, and a
    # merge that de-duplicates by name would silently keep only one of them.
    # Decided before --apply's prompt / the output modes, so
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
                    configmap_name, args.yes, strict=args.strict,
                    judge_merged=assembled_judge)

    # --output-configmap / --output-alertmanager: one merge, two wrappers.
    _assembled_output_mode(routes, receivers, inhibit_rules, base_config,
                           namespace, configmap_name, args.dry_run, args.output,
                           strict=args.strict, judge_emitted=assembled_judge,
                           as_configmap=bool(args.output_configmap),
                           mounts_out=args.mount_list_out,
                           mount_root=args.mount_root,
                           mount_lists=mount_lists)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
