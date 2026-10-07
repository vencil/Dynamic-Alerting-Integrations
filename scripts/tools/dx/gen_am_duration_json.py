#!/usr/bin/env python3
"""Generate am-duration.json from the tenant-config schema (#2711).

The routing timing syntax (what Alertmanager's ``model.ParseDuration`` reads)
is written once, as ``definitions.duration`` of
docs/schemas/tenant-config.schema.json (#2490). Python reads it there at
runtime (``_lib_validation.duration_rule``) and Go calls ``model.ParseDuration``
itself; the portal bundle cannot read the repo's ``docs/`` at runtime, so this
script DERIVES one JSON copy for it (imported by
``_common/validation/yaml-parser.js``). Same shape as gen_tenant_id_json.py
(ADR-035 D2): one authored source, a derived copy, a drift gate.

    python gen_am_duration_json.py            # write the copy
    python gen_am_duration_json.py --check    # exit 1 if the committed copy drifted

Exit codes: 0 wrote / in-sync · 1 drift (--check) · 2 caller error (schema
missing or without a usable definitions.duration).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# cp950-console --help hardening (da-tools ROI r4 W7): import a root lib so
# stdout errors are reconfigured at import time, before argparse prints
# --help. scripts/tools is the parent of dx/.
sys.path.insert(0, os.path.dirname(_THIS_DIR))
import _lib_compat  # noqa: E402,F401  (import-time side effect; see _lib_compat)

SCHEMA_REL = "docs/schemas/tenant-config.schema.json"

# One committed derivation: the portal imports from its own _common/data.
# Go needs none (the exporter runs model.ParseDuration, the rule's origin).
OUT_RELS = (
    "tools/portal/src/interactive/tools/_common/data/am-duration.json",   # portal import
)

_HEADER = (
    f"GENERATED from {SCHEMA_REL} definitions.duration by "
    "gen_am_duration_json.py — DO NOT EDIT. Run `make am-duration-json`."
)


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return Path(_THIS_DIR).resolve().parents[2]


def render(schema: dict) -> str:
    """Deterministic JSON of definitions.duration's pattern, minLength and
    description.

    Raises ValueError when the definition is missing or not usable (the same
    conditions under which ``_lib_validation.duration_rule`` fails closed).
    """
    definition = schema.get("definitions", {}).get("duration")
    if not isinstance(definition, dict):
        raise ValueError("definitions.duration is missing")
    for key in ("pattern", "description"):
        if not isinstance(definition.get(key), str) or not definition[key]:
            raise ValueError(f"definitions.duration.{key} must be a non-empty string")
    min_length = definition.get("minLength")
    if not isinstance(min_length, int) or isinstance(min_length, bool):
        raise ValueError("definitions.duration.minLength must be an integer")
    doc = {
        "_generated": _HEADER,
        "pattern": definition["pattern"],
        "minLength": min_length,
        "description": definition["description"],
    }
    return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate am-duration.json from definitions.duration of the tenant-config schema")
    parser.add_argument("--check", action="store_true",
                        help="verify the committed json copy matches the schema; exit 1 on drift")
    args = parser.parse_args()

    repo = _repo_root()
    try:
        with open(repo / SCHEMA_REL, encoding="utf-8") as f:
            generated = render(json.load(f))
    except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
        print(f"ERROR: {SCHEMA_REL}: {exc}", file=sys.stderr)
        return 2
    targets = [(rel, repo / rel) for rel in OUT_RELS]

    if args.check:
        drift = False
        for rel, path in targets:
            if not path.exists():
                print(f"DRIFT: {rel} is missing; run `make am-duration-json`", file=sys.stderr)
                drift = True
            # Bytes, not read_text(): that would normalize a CRLF copy away.
            elif path.read_bytes() != generated.encode("utf-8"):
                print(f"DRIFT: {rel} is stale vs {SCHEMA_REL} definitions.duration; "
                      "run `make am-duration-json`", file=sys.stderr)
                drift = True
        if drift:
            return 1
        print(f"OK: am-duration.json ({len(OUT_RELS)} copy) matches "
              f"{SCHEMA_REL} definitions.duration.")
        return 0

    for rel, path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n": force LF on every platform (CRLF would taint the portal
        # bundle's sourcemap; see gen_recipe_status_json.py).
        path.write_text(generated, encoding="utf-8", newline="\n")
        print(f"WROTE: {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
