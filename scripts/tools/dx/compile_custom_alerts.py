#!/usr/bin/env python3
"""Compile Custom Alert recipes → rule-packs/rule-pack-custom-alerts.yaml.

ADR-024 Capability B (#741 S1+S2). Reads `_custom_alerts` declarations from a
conf.d tree, groups them by shape signature, and emits ONE vectorised
`group_left` rule per shape (rule count = shape count, NOT per-tenant fan-out —
preserves the rule-pack O(M) invariant). Tenants never write PromQL.

The generated pack flows through the EXISTING fan-out unchanged (both glob
`rule-pack-*.yaml`): generate_rulepack_configmaps.py → configmap, and
operator_generate.py → PrometheusRule CRD.

Source of declarations (`--config-dir`): defaults to the committed example tree
`rule-packs/recipes/examples/conf.d/`. S3 (exporter `_custom_alerts` support)
will switch the production source to the live conf.d; until then the live
exporter cannot parse the new key, so the example tree is the safe S1+S2 source.

`--check` regenerates in memory and SEMANTICALLY compares against the committed
pack (via check_rulepack_sync), so a stale / hand-edited pack is a hard failure.

Exit codes:
    0  wrote file (default) / in sync (--check)
    1  drift detected (--check)
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
DEFAULT_CONFIG_REL = "rule-packs/recipes/examples/conf.d"
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
    """
    return {
        "alert": "CustomRecipeSilent",
        "expr": 'max by(tenant, name) (user_threshold{component="custom", mode="silent"})',
        "labels": {
            "severity": "none",
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
    """Strip control chars (incl newline / ANSI ESC) from a tenant-controlled value before
    printing a quarantine line to the CI log — a malformed tenant id / origin / exception
    text must not inject forged log lines or terminal escapes (#1008 fail-soft observability).
    PyYAML already rejects most control chars at parse, but newline/tab pass through."""
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(value))


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

    if args.check:
        gen_map = sync._extract(groups)
        committed = sync._extract(sync._groups_from_rulepack(out_path)) if out_path.exists() else {}
        findings = sync._diff_maps(gen_map, committed)
        if findings:
            print(f"  ❌ {OUT_REL} drifted from custom-alert source:")
            for f in findings:
                print(f"       {f}")
            print(f"\n❌ custom-alerts rule pack out of sync. "
                  f"Run `make custom-alerts-compile` to regenerate.", file=sys.stderr)
            return EXIT_VIOLATION
        print(f"✅ custom-alerts rule pack matches source "
              f"({meta['shapes']} shape(s)).")
        return EXIT_OK

    out_path.write_text(_render(groups), encoding="utf-8")
    # Display repo-relative when the out path is inside the repo, else show it as
    # given. `relative_to` raises ValueError for an --out outside the repo (or on a
    # different drive on Windows) — without this guard the success line would crash
    # AFTER the file was already written, turning a successful compile into a
    # traceback + nonzero exit (false failure).
    try:
        shown = out_path.relative_to(repo)
    except ValueError:
        shown = out_path
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
