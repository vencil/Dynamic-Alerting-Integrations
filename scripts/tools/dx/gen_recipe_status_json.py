#!/usr/bin/env python3
"""Generate recipe-status.json from the compiler SSOT (ADR-024 §8, #741 #6 / A1).

The recipe lifecycle status SSOT is `custom_alerts/shape.py::RECIPE_STATUS`
(Python compiler). The tenant-api (Go) needs the same status to enforce the
eol-expansion write guard, and the portal needs it for the deprecated/eol badge.
Rather than hand-author a second Go map (the split-brain trap Gemini flagged),
this script DERIVES a `recipe-status.json` that the Go side consumes via
`go:embed`. One authored source, derived everywhere.

    python gen_recipe_status_json.py            # write the json
    python gen_recipe_status_json.py --check     # exit 1 if the committed json drifted

Exit codes: 0 wrote / in-sync · 1 drift (--check) · 2 caller error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
from custom_alerts import shape as _shape  # noqa: E402

# go:embed can only reference files in/below its own package dir, so the json
# lives inside the customalerts Go package (not a shared top-level location).
OUT_REL = "components/tenant-api/internal/customalerts/recipe-status.json"

_HEADER = (
    "GENERATED from scripts/tools/dx/custom_alerts/shape.py RECIPE_STATUS by "
    "gen_recipe_status_json.py — DO NOT EDIT. Run `make recipe-status-json`."
)


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return Path(_THIS_DIR).resolve().parents[2]


def render() -> str:
    """Deterministic JSON: sorted recipe keys + trailing newline."""
    doc = {
        "_generated": _HEADER,
        "statuses": {r: _shape.recipe_status(r) for r in sorted(_shape.RECIPES)},
    }
    return json.dumps(doc, indent=2, sort_keys=False, ensure_ascii=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate recipe-status.json from RECIPE_STATUS")
    parser.add_argument("--check", action="store_true",
                        help="verify committed json matches the SSOT; exit 1 on drift")
    parser.add_argument("--out", default=None, help=f"output path (default: {OUT_REL})")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else _repo_root() / OUT_REL
    generated = render()

    if args.check:
        if not out_path.exists():
            print(f"DRIFT: {OUT_REL} is missing; run `make recipe-status-json`", file=sys.stderr)
            return 1
        committed = out_path.read_text(encoding="utf-8")
        if committed != generated:
            print(f"DRIFT: {OUT_REL} is stale vs shape.py RECIPE_STATUS; "
                  f"run `make recipe-status-json`", file=sys.stderr)
            return 1
        print(f"OK: {OUT_REL} matches RECIPE_STATUS ({len(_shape.RECIPES)} recipes).")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(generated, encoding="utf-8")
    print(f"WROTE: {OUT_REL} ({len(_shape.RECIPES)} recipes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
