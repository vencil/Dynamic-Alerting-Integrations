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

# Two committed derivations of the same SSOT (shape.py RECIPE_STATUS), one per
# consumer tree (go:embed can't reference "..", and the portal build imports from
# its own _common/data — so each consumer needs its copy in its own tree). Both
# are byte-identical and drift-gated against the SSOT.
OUT_RELS = (
    "components/tenant-api/internal/customalerts/recipe-status.json",          # Go go:embed
    "tools/portal/src/interactive/tools/_common/data/recipe-status.json",      # portal import
)

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
                        help="verify committed json copies match the SSOT; exit 1 on drift")
    args = parser.parse_args()

    repo = _repo_root()
    generated = render()
    targets = [(rel, repo / rel) for rel in OUT_RELS]

    if args.check:
        drift = False
        for rel, path in targets:
            if not path.exists():
                print(f"DRIFT: {rel} is missing; run `make recipe-status-json`", file=sys.stderr)
                drift = True
            elif path.read_text(encoding="utf-8") != generated:
                print(f"DRIFT: {rel} is stale vs shape.py RECIPE_STATUS; "
                      f"run `make recipe-status-json`", file=sys.stderr)
                drift = True
        if drift:
            return 1
        print(f"OK: recipe-status.json ({len(OUT_RELS)} copies) match RECIPE_STATUS "
              f"({len(_shape.RECIPES)} recipes).")
        return 0

    for rel, path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n": force LF on every platform. Without it, Path.write_text
        # uses os.linesep (CRLF on Windows), which the eol=lf gitattribute hides
        # from `git status` (renormalize-on-compare) AND from `--check` above
        # (read_text universal-newlines normalizes CRLF→LF) — but esbuild reads
        # the raw working-tree bytes, so a CRLF copy taints the portal bundle's
        # sourcemap and re-hashes the shared chunk → Portal Tests dist gate reds.
        path.write_text(generated, encoding="utf-8", newline="\n")
        print(f"WROTE: {rel} ({len(_shape.RECIPES)} recipes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
