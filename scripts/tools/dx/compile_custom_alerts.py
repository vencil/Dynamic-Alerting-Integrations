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


def build_pack(config_dir: Path,
               max_custom_recipes: int = _loader.MAX_CUSTOM_RECIPES_DEFAULT) -> dict:
    """Build the rule-pack dict (groups) from a conf.d tree."""
    shapes, per_tenant = _loader.build_shapes(config_dir, max_custom_recipes=max_custom_recipes)

    recording: List[dict] = []
    alerts: List[dict] = []
    for shape in shapes:
        rec, alr = _recipes.emit_shape(shape)
        recording.extend(rec)
        alerts.extend(alr)

    groups: List[dict] = []
    if recording:
        groups.append({
            "name": "custom-alerts-normalization",
            "interval": "15s",
            "rules": recording,
        })
    if alerts:
        groups.append({"name": "custom-alerts", "rules": alerts})

    return {
        "groups": groups,
        "_meta": {"shapes": len(shapes), "per_tenant_counts": per_tenant},
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
    print(f"✅ Compiled {meta['shapes']} shape(s) → {out_path.relative_to(repo)}")
    if meta["per_tenant_counts"]:
        worst = max(meta["per_tenant_counts"].values())
        print(f"   per-tenant EFFECTIVE recipe counts (own + inherited): "
              f"{meta['per_tenant_counts']} (max={worst}). OWN-recipe cap "
              f"{args.max_custom_recipes} enforced at compile (inherited policy "
              f"is vectorized + uncapped).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
