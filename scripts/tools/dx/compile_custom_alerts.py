#!/usr/bin/env python3
"""Compile Custom Alert recipes → rule-packs/rule-pack-custom-alerts.yaml.

ADR-024 Capability B (#741 S1+S2). Reads `_custom_alerts` declarations from a
conf.d tree, groups them by shape signature, and emits ONE vectorised
`group_left` rule per shape (rule count = shape count, NOT per-tenant fan-out —
preserves the rule-pack O(M) invariant). Tenants never write PromQL.

The generated pack flows through the EXISTING fan-out unchanged (both glob
`rule-pack-*.yaml`): generate_rulepack_configmaps.py → configmap, and
operator_generate.py → PrometheusRule CRD.

Source of declarations (`--config-dir`): defaults to the LIVE exporter tree
`components/threshold-exporter/config/conf.d` — the same canonical source every
gate (Makefile `custom-alerts-compile*` / pre-commit / CI) pins explicitly, and
the tree the committed pack is compiled from since S3b. The source MUST be the
conf.d the exporter serves, or recipe_id will not match emit. The docs example
tree (`rule-packs/recipes/examples/conf.d/`) stays reachable via `--config-dir`.

`--check` regenerates in memory and SEMANTICALLY compares against the committed
pack (via check_rulepack_sync), so a stale / hand-edited pack is a hard failure.

⛔ A compile that produces NOTHING will not overwrite a pack that has rules
(`--allow-empty` to override). A source file the compiler stops seeing — renamed,
moved, or a name its discovery does not match — is indistinguishable from "the
last recipe was removed" at this layer, and the write is irreversible in the
working tree. See `_erases_committed_rules`.

Exit codes:
    0  wrote file (default) / in sync (--check)
    1  drift detected (--check) / refused to erase the committed pack (write)
    2  error (invalid declaration tree, missing source, …)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "lint"))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
import check_rulepack_sync as sync  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)

try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass

# import the compiler package (scripts/tools/dx/custom_alerts/)
sys.path.insert(0, _THIS_DIR)
from custom_alerts import loader as _loader  # noqa: E402
from custom_alerts import recipes as _recipes  # noqa: E402
from custom_alerts import shape as _shape  # noqa: E402
from custom_alerts.loader import CustomAlertConfigError  # noqa: E402


PACK_NAME = "custom-alerts"
DEFAULT_CONFIG_REL = "components/threshold-exporter/config/conf.d"
OUT_REL = "rule-packs/rule-pack-custom-alerts.yaml"


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parent.parent.parent


class _BlockDumper(yaml.SafeDumper):
    """Emit multiline strings as `|` block scalars for readable PromQL exprs."""


def _str_representer(dumper: yaml.Dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_BlockDumper.add_representer(str, _str_representer)


def _silent_sentinel() -> dict:
    """The SINGLE global (tenant-agnostic) silent-mode sentinel (#741 S7/S8).

    Fires once per (tenant, name) whose recipe declares mode=silent, derived
    straight from the exporter's user_threshold series — so it is injected ONCE
    regardless of shape count (the query already spans every silent recipe in
    the cluster). Alertmanager uses it as an inhibit SOURCE (equal:[tenant,name])
    to suppress that recipe's notification while Prometheus keeps evaluating it
    as an ALERTS series for dashboards. This rides the platform's established
    ADR-003 Sentinel+Inhibit silent paradigm (mirrors TenantSilentWarning/
    Critical) instead of an Alertmanager route-to-null receiver — more
    observable in the AM UI and consistent with the tenant-level tri-state.

    Like every platform sentinel it carries the static component="sentinel"
    routing discriminator (#1095): the sentinel is an inhibit source + AM-UI/
    dashboard surface, NOT a notification, and it still carries a `tenant`
    label — without the discriminator it would fall through to the tenant main
    route (and a matcher-less enforced NOC route) and notify humans. The
    platform-static sentinel-sinkhole route (continue:false, ahead of enforced/
    tenant routes) swallows it; inhibition is unaffected (Alertmanager matches
    inhibit sources against all active alerts regardless of routing).
    """
    return {
        "alert": "CustomRecipeSilent",
        "expr": 'max by(tenant, name) (user_threshold{component="custom", mode="silent"})',
        "labels": {
            "severity": "none",
            "component": "sentinel",
            "tenant": "{{ $labels.tenant }}",
            "name": "{{ $labels.name }}",
        },
        "annotations": {
            "summary": "Custom recipe [{{ $labels.name }}] is silent for {{ $labels.tenant }}",
            "summary_zh": "{{ $labels.tenant }} 的自訂告警 [{{ $labels.name }}] 處於靜默模式",
            "description": ("The recipe is still evaluated and visible as an ALERTS "
                           "series (dashboard-only); its notifications are suppressed "
                           "via inhibit, not deleted."),
            "description_zh": ("此自訂告警仍會評估並可於監控面板（ALERTS series）查看，"
                              "通知經 inhibit 抑制而非刪除。"),
        },
    }


# Platform-authored template actions the compiler ITSELF emits into custom-alert
# rule labels/annotations — the ONLY {{ … }} allowed in a generated custom-alert
# rule. Anything else means tenant-controlled data became Go-template code (the F2
# annotation-injection class), regardless of WHICH field leaked it. Emit-time
# INVARIANT gate (A+ defence-in-depth): shape.py's boundary reject stops the known
# selector-value vector; this catches ANY future field reaching a template context
# without its own guard. Allowed: `{{ $value | printf "%.Nf" }}` and `{{ $labels.X }}`.
_ALLOWED_TEMPLATE_ACTION = re.compile(
    r'\{\{\s*(?:\$value\s*\|\s*printf\s+"%\.[0-9]+f"|\$labels\.[a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}'
)


def _assert_annotations_template_safe(groups: List[dict]) -> None:
    """Fail the compile if any generated label/annotation carries a Go-template
    action OR backtick beyond the platform allowlist. Catches the F2 injection class
    at emit time no matter which field carried it. MUST be kept in lockstep with the
    platform annotations emitted by recipes.py / _silent_sentinel."""
    for g in groups:
        for r in g.get("rules", []):
            fields = dict(r.get("labels") or {})
            fields.update(r.get("annotations") or {})
            for name, val in fields.items():
                residual = _ALLOWED_TEMPLATE_ACTION.sub("", str(val))
                if "{{" in residual or "}}" in residual or "`" in residual:
                    ident = r.get("alert") or r.get("record") or "<rule>"
                    raise CustomAlertConfigError(
                        f"emit-time invariant violation: {name!r} in {ident!r} contains a "
                        f"non-platform Go-template action or backtick — tenant-controlled data "
                        f"must never become template code (F2 injection class). Value: {val!r}"
                    )


def _safe_log(value) -> str:
    """Deprecated alias for :func:`_lib_io.safe_label` (#1538).

    This function WAS the rule — added for #1008 so a malformed tenant id /
    origin / exception text could not inject forged log lines or terminal
    escapes into a quarantine line. #1538 found the same defect in 18 more
    tools and promoted this implementation to the shared output layer.

    ⚠️ The character class DID change in the promotion, and an earlier wording
    here said it did not: the shared rule adds **C1** (``\\x80``–``\\x9f``), so a
    value that reaches this function with ``\\x85`` now becomes ``?`` where the
    pre-#1538 body passed it through. Behaviour change, deliberate, disclosed. The name is kept because callers and
    ``tests/dx/test_compile_custom_alerts.py`` reference it; the body is now a
    delegate so there is exactly ONE definition of the escaping rule in the
    repo rather than a copy per tool.
    """
    return safe_label(value)


def build_pack(config_dir: Path,
               max_custom_recipes: int = _loader.MAX_CUSTOM_RECIPES_DEFAULT) -> dict:
    """Build the rule-pack dict (groups) from a conf.d tree."""
    shapes, per_tenant, skipped = _loader.build_shapes(config_dir, max_custom_recipes=max_custom_recipes)

    recording: List[dict] = []
    alerts: List[dict] = []
    info: List[dict] = []
    for shape in shapes:
        rec, alr = _recipes.emit_shape(shape)
        recording.extend(rec)
        alerts.extend(alr)
        # D1 (ADR-024 §8): a static lifecycle-info series per shape, so SRE can join
        # recipe USAGE to recipe STATUS for a tech-debt burn-down:
        #   count by(recipe_id)(user_threshold{component="custom"})
        #     * on(recipe_id) group_left(recipe, status) custom_recipe_info
        # user_threshold carries recipe_id but not recipe/status, and deriving the
        # recipe type from recipe_id needs a fragile label_replace — this info series
        # supplies recipe + status keyed by the same recipe_id instead.
        info.append({
            "record": "custom_recipe_info",
            "expr": "vector(1)",
            "labels": {
                "recipe_id": shape["recipe_id"],
                "recipe": shape["recipe"],
                "status": _shape.recipe_status(shape["recipe"]),
            },
        })

    groups: List[dict] = []
    if recording:
        groups.append({
            "name": "custom-alerts-normalization",
            "interval": "15s",
            "rules": recording,
        })
    if info:
        # Static metadata (never changes between compiles) → its own group on a
        # slow 1m interval, kept out of the 15s normalize cadence.
        groups.append({
            "name": "custom-alerts-info",
            "interval": "1m",
            "rules": info,
        })
    if alerts:
        # Inject the single global silent sentinel ONCE, ahead of the shape
        # alerts (S7/S8). It is tenant-agnostic — never per-recipe.
        groups.append({"name": "custom-alerts", "rules": [_silent_sentinel()] + alerts})

    # A+ emit-time invariant gate: no tenant-controlled data may have become a
    # Go-template action in any generated label/annotation (F2 defence-in-depth).
    _assert_annotations_template_safe(groups)
    return {
        "groups": groups,
        "_meta": {"shapes": len(shapes), "info": len(info), "per_tenant_counts": per_tenant,
                  "skipped": skipped},
    }


def _render(groups: List[dict]) -> str:
    header = (
        "# ============================================================\n"
        "# custom-alerts Rule Pack — Tenant-authored declarative alerts\n"
        "# GENERATED from _custom_alerts declarations by\n"
        "# scripts/tools/dx/compile_custom_alerts.py — DO NOT EDIT.\n"
        "# Run `make custom-alerts-compile` after editing recipes/conf.d.\n"
        "# Rule count = SHAPE count (vectorized, not per-tenant; ADR-024 §2b).\n"
        "# ============================================================\n"
    )
    body = yaml.dump(
        {"groups": groups},
        Dumper=_BlockDumper,
        sort_keys=False, default_flow_style=False, allow_unicode=True, width=10_000,
    )
    return header + body


def _committed_rules(out_path: Path) -> dict:
    """Rules already in the pack at *out_path*, in the same identity form `--check` uses.

    ⚠️ An unreadable / malformed pack answers {} ON PURPOSE, not by accident. This
    function exists to protect content that is there; a pack that cannot be parsed
    has no content to protect, and regenerating it is the repair. Raising here would
    turn "your committed pack is corrupt" into "the compiler no longer runs", i.e. it
    would take away the only way out.
    """
    if not out_path.exists():
        return {}
    try:
        return sync._extract(sync._groups_from_rulepack(out_path))
    except Exception:
        return {}


def _erases_committed_rules(produced: dict, committed: dict) -> bool:
    """True when writing *produced* would leave the pack with none of its rules.

    ⛔ THE PREDICATE IS "PRODUCED NOTHING", NOT "PRODUCED FEWER". Losing SOME rules
    is a legitimate everyday edit (a tenant retires one recipe), so gating on any
    shrink would fail-red on ordinary work and the guard would be removed. Losing
    ALL of them is the shape that means the compiler saw an empty source — which a
    rename, a move, or a filename its discovery does not match produces just as
    readily as a genuine "the last recipe is gone".

    ⚠️ NOT GUARDED, on purpose and measured: partial loss. Renaming one of three
    tenant files still drops that tenant's alerts silently through this path. The
    `--check` gate reports it as drift; this function does not look at it.
    """
    return bool(committed) and not produced


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description="Compile custom-alert recipes → rule pack")
    parser.add_argument("--check", action="store_true",
                        help="verify committed pack matches source (semantic); exit 1 on drift")
    parser.add_argument("--config-dir", default=None,
                        help=f"conf.d tree with _custom_alerts (default: {DEFAULT_CONFIG_REL})")
    parser.add_argument("--out", default=None,
                        help=f"output rule pack path (default: {OUT_REL})")
    parser.add_argument("--max-custom-recipes", type=int,
                        default=_loader.MAX_CUSTOM_RECIPES_DEFAULT,
                        help=f"per-tenant cap on OWN recipes (default: "
                             f"{_loader.MAX_CUSTOM_RECIPES_DEFAULT}; inherited policy uncapped)")
    parser.add_argument("--allow-empty", action="store_true",
                        help="write a pack with no rules over one that has rules "
                             "(refused by default; only when removing the last recipe is intended)")
    args = parser.parse_args()

    repo = _repo_root()
    config_dir = Path(args.config_dir) if args.config_dir else repo / DEFAULT_CONFIG_REL
    out_path = Path(args.out) if args.out else repo / OUT_REL

    if not config_dir.exists():
        print(f"ERROR: config dir not found: {config_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        pack = build_pack(config_dir, max_custom_recipes=args.max_custom_recipes)
    except CustomAlertConfigError as e:
        # surface compile-time validation errors as caller errors (exit 2)
        print(f"ERROR: invalid custom-alert declarations: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    groups = pack["groups"]
    meta = pack["_meta"]

    # #1008 Part B: surface quarantined (fail-soft) recipes LOUDLY. A shared compiler
    # gate must not abort the whole compile on one bad recipe (that blocks every
    # tenant's PR merge — a cross-tenant DoS), so an invalid recipe is dropped and the
    # rest compile. Report each drop to stderr + the pack _meta so it is never silent
    # (a quarantined recipe does not deploy). NOT a hard failure by design.
    skipped = meta.get("skipped", [])
    for s in skipped:
        print(f"  ⚠ custom-alert QUARANTINED (fail-soft, #1008): tenant={_safe_log(s['tenant'])} "
              f"name={s['name']!r} ({_safe_log(s['origin'])}): {_safe_log(s['reason'])}", file=sys.stderr)
    if skipped:
        print(f"  ⚠ {len(skipped)} custom-alert recipe(s) quarantined — compiled the rest. "
              f"A quarantined recipe does NOT deploy; fix it (or it stays dropped).",
              file=sys.stderr)

    # Non-fatal recipe-lifecycle notices (ADR-024 #6): deprecated/eol recipes in
    # use still compile (no silent alert loss); surface them to stderr so a GitOps
    # PR / CI log flags the migration debt without breaking the build.
    for notice in _loader.collect_lifecycle_notices(config_dir):
        print(f"  ⚠ recipe-lifecycle: {notice}", file=sys.stderr)

    # Prerequisite notice (#692 P0③ W3): disk-fill recipes (kubelet_volume_stats_*)
    # depend on cluster-side plumbing the compiler CANNOT verify — a CSI driver
    # implementing NodeGetVolumeStats, a kubelet volume-stats scrape job, and a
    # namespace→tenant relabel (ADR-006 §Addendum). Surface it at author-time so the
    # GitOps author wires it (honest: we INFORM, we do not assert the prereq is met).
    # byo_check.py verifies the live flow; CustomRecipeDiskInert is the runtime backstop.
    if any("kubelet_volume_stats_" in r.get("expr", "")
           for g in groups for r in g.get("rules", [])):
        print("  ⚠ disk-recipe prerequisite: a disk-fill recipe (kubelet_volume_stats_*) "
              "compiled — it only fires if the cluster has CSI NodeGetVolumeStats, a kubelet "
              "volume-stats scrape job, and a namespace→tenant relabel. Verify the live flow "
              "with `byo_check.py prometheus`.", file=sys.stderr)

    # Prerequisite notice (#692 P0④): disk-IOPS recipes (container_fs_*) depend on a
    # cadvisor container_fs scrape + namespace→tenant relabel AND on the storage exposing
    # I/O to cgroup blkio — network volumes (NFS/EFS) bypass blkio and emit 0. It is
    # PER-CONTAINER, not per-PVC. We INFORM at author-time; byo_check.py is the live
    # fidelity gate (no runtime sentinel — an inert IOPS recipe is always platform-side).
    if any("container_fs_" in r.get("expr", "")
           for g in groups for r in g.get("rules", [])):
        print("  ⚠ disk-IOPS-recipe prerequisite: a disk-IOPS recipe (container_fs_*) compiled "
              "— it only fires if cadvisor scrapes container_fs with a namespace→tenant relabel "
              "AND the storage exposes I/O to cgroup blkio (network volumes like NFS/EFS bypass "
              "it → 0). Per-CONTAINER, not per-PVC. Verify the live flow with `byo_check.py "
              "prometheus` after a representative load test.", file=sys.stderr)

    produced = sync._extract(groups)
    committed = _committed_rules(out_path)

    if args.check:
        findings = sync._diff_maps(produced, committed)
        if findings:
            print(f"  ❌ {OUT_REL} drifted from custom-alert source:")
            for f in findings:
                print(f"       {f}")
            # ⛔ THE REMEDY LINE IS BRANCHED, AND THAT IS THE POINT. "Regenerate" is
            # the right answer to ordinary drift and the WRONG answer to this shape:
            # when the source compiled to nothing, regenerating is what deletes the
            # pack. Measured: renaming one tenant file to `.yml` turns this gate red,
            # and following the instruction this line used to print unconditionally
            # emptied the shipped pack (7016 -> 447 bytes) and turned the gate green.
            if _erases_committed_rules(produced, committed):
                print(f"\n❌ custom-alerts rule pack out of sync — and the source now "
                      f"compiles to NOTHING while the committed pack has "
                      f"{len(committed)} rule(s).\n"
                      f"   The compiler read: {config_dir}\n"
                      f"   ⛔ Do not regenerate to clear this. Regenerating here deletes "
                      f"every custom alert in the pack.\n"
                      f"   Find the declarations the compiler stopped seeing first "
                      f"(renamed / moved file, or a filename its discovery does not match).",
                      file=sys.stderr)
            else:
                print(f"\n❌ custom-alerts rule pack out of sync. "
                      f"Run `make custom-alerts-compile` to regenerate.", file=sys.stderr)
            return EXIT_VIOLATION
        print(f"✅ custom-alerts rule pack matches source "
              f"({meta['shapes']} shape(s)).")
        return EXIT_OK

    # Display repo-relative when the out path is inside the repo, else show it as
    # given. `relative_to` raises ValueError for an --out outside the repo (or on a
    # different drive on Windows) — without this guard the success line would crash
    # AFTER the file was already written, turning a successful compile into a
    # traceback + nonzero exit (false failure).
    #
    # ⛔ COMPUTED BEFORE THE WRITE, so the refusal below names the pack the same way
    # the success line does. A refusal that prints an absolute path while every other
    # line prints a repo-relative one reads like it is talking about a different file.
    try:
        shown = out_path.relative_to(repo)
    except ValueError:
        shown = out_path

    if _erases_committed_rules(produced, committed) and not args.allow_empty:
        print(f"❌ refusing to write: this compile produced no rules, and "
              f"{shown} currently has {len(committed)}.\n"
              f"   Writing would remove every custom alert from the pack, and the "
              f"working-tree copy is gone once it is written.\n"
              f"   The compiler read: {config_dir}\n"
              f"   Check first whether a declaration stopped being visible to it — a "
              f"renamed, moved or deleted source file, or a filename its discovery "
              f"does not match — because that is indistinguishable from an intended "
              f"removal at this layer.\n"
              f"   If removing the last recipe is what you meant, re-run with "
              f"--allow-empty.", file=sys.stderr)
        return EXIT_VIOLATION

    out_path.write_text(_render(groups), encoding="utf-8", newline="\n")
    print(f"✅ Compiled {meta['shapes']} shape(s) → {shown}")
    if meta["per_tenant_counts"]:
        worst = max(meta["per_tenant_counts"].values())
        print(f"   per-tenant EFFECTIVE recipe counts (own + inherited): "
              f"{meta['per_tenant_counts']} (max={worst}). OWN-recipe cap "
              f"{args.max_custom_recipes} enforced at compile (inherited policy "
              f"is vectorized + uncapped).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
