#!/usr/bin/env python3
"""Generate tenant-id.json from the tenant-config schema (ADR-035 §2).

The tenant-id rule is written once, as ``definitions.tenantId`` of
docs/schemas/tenant-config.schema.json. Python reads it there at runtime
(``_lib_validation.tenant_id_rule``); a Go binary and the portal bundle cannot
read the repo's ``docs/`` at runtime, so this script DERIVES one JSON copy for
each of them (Go via ``go:embed``, the portal via import). One authored source,
derived everywhere — the same shape as gen_recipe_contract.py.

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
from _lib_compat import require_project_root  # noqa: E402  (#2852)
# #3981 / #4223: the output entries without PyYAML — `--check` runs as a
# pre-commit hook in an environment that has none.
from _lib_output import (  # noqa: E402
    OutputWriteError, check_repo_artifact, ensure_dir, is_special_file, write_repo_artifact)

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
    """This tree's root, never an enclosing project's (#2852)."""
    return require_project_root(_THIS_DIR)


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

    # Every copy is judged before it is read or the first is written
    # (#3981): a FIFO there would block the read (`--check` too), a
    # directory is no file to compare or replace. Only a copy that will be
    # replaced must also not be this process's stdin / stdout / stderr:
    # comparing one is harmless (`--check < copy`).
    try:
        for _rel, path in targets:
            if args.check:
                if is_special_file(path):
                    raise OutputWriteError(path, OSError(None, "not a regular file"),
                                           action="read")
            else:
                check_repo_artifact(path)
    except OutputWriteError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

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

    try:
        # Judged above, before the first write; a write that fails later —
        # a directory that takes no new file — still leaves the copies
        # before it rewritten.
        for rel, path in targets:
            ensure_dir(path.parent)
            # The artifact entry writes LF on every platform (CRLF would
            # taint the portal bundle's sourcemap; see gen_recipe_contract.py).
            write_repo_artifact(path, generated)
            print(f"WROTE: {rel}")
    except OutputWriteError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
