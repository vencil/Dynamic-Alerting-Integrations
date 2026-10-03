#!/usr/bin/env python3
"""Generate tenant-id.json from the tenant-config schema (ADR-035 D2).

The tenant-id rule is written once, as ``definitions.tenantId`` of
docs/schemas/tenant-config.schema.json. Python reads it there at runtime
(``_lib_validation.tenant_id_rule``); a Go binary and the portal bundle cannot
read the repo's ``docs/`` at runtime, so this script DERIVES one JSON copy for
each of them (Go via ``go:embed``, the portal via import). One authored source,
derived everywhere — the same shape as gen_recipe_status_json.py.

    python gen_tenant_id_json.py            # write both copies
    python gen_tenant_id_json.py --check    # exit 1 if a committed copy drifted

Exit codes: 0 wrote / in-sync · 1 drift (--check) · 2 caller error (schema
missing or without a usable definitions.tenantId).
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

# Two committed derivations, one per consumer tree (go:embed can't reference
# "..", and the portal build imports from its own _common/data). Both are
# byte-identical and drift-gated against the schema.
OUT_RELS = (
    "components/threshold-exporter/app/pkg/tenantid/tenant_id.json",      # Go go:embed
    "tools/portal/src/interactive/tools/_common/data/tenant-id.json",     # portal import
)

_HEADER = (
    f"GENERATED from {SCHEMA_REL} definitions.tenantId by "
    "gen_tenant_id_json.py — DO NOT EDIT. Run `make tenant-id-json`."
)


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return Path(_THIS_DIR).resolve().parents[2]


def render(schema: dict) -> str:
    """Deterministic JSON of definitions.tenantId's pattern + description.

    Raises ValueError when the definition is missing or not usable.
    """
    definition = schema.get("definitions", {}).get("tenantId")
    if not isinstance(definition, dict):
        raise ValueError("definitions.tenantId is missing")
    for key in ("pattern", "description"):
        if not isinstance(definition.get(key), str) or not definition[key]:
            raise ValueError(f"definitions.tenantId.{key} must be a non-empty string")
    doc = {
        "_generated": _HEADER,
        "pattern": definition["pattern"],
        "description": definition["description"],
    }
    return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate tenant-id.json from definitions.tenantId of the tenant-config schema")
    parser.add_argument("--check", action="store_true",
                        help="verify committed json copies match the schema; exit 1 on drift")
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
                print(f"DRIFT: {rel} is missing; run `make tenant-id-json`", file=sys.stderr)
                drift = True
            # Bytes, not read_text(): that would normalize a CRLF copy away.
            elif path.read_bytes() != generated.encode("utf-8"):
                print(f"DRIFT: {rel} is stale vs {SCHEMA_REL} definitions.tenantId; "
                      "run `make tenant-id-json`", file=sys.stderr)
                drift = True
        if drift:
            return 1
        print(f"OK: tenant-id.json ({len(OUT_RELS)} copies) match "
              f"{SCHEMA_REL} definitions.tenantId.")
        return 0

    for rel, path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n": force LF on every platform (CRLF would taint the portal
        # bundle's sourcemap once the portal imports this file; see
        # gen_recipe_status_json.py).
        path.write_text(generated, encoding="utf-8", newline="\n")
        print(f"WROTE: {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
