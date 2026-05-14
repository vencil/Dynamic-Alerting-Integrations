#!/usr/bin/env python3
"""check_dist_source_consistency.py — Catch portal dist commits without matching source change (testing-playbook §LL §2, TRK-239).

Why this exists
---------------
TRK-233 (PR #270) traced a 20-spec regression in main back to PR-E
(#269) committing a regenerated `docs/assets/dist/` while only the
JSX source for ONE tool (`template-gallery.jsx`) had visibly changed.
The esbuild rebuild reshuffled chunk allocations across all 43 tools,
so the dist commit silently affected every tool — but reviewer
attention was on template-gallery only.

testing-playbook.md §LL "v2.8.0 — Portal E2E coverage push + ESM
dist regression" rule §2 codifies the lesson: "Don't treat build
artifacts as commit-time invariants — rebuild dist + run full E2E,
not just the changed spec."

This hook mechanically catches one half of the problem: a commit
that stages `docs/assets/dist/*.js` without staging any plausible
source-of-rebuild signal. It's the cheap defensive check before the
expensive E2E run.

Detection rule
--------------
If any staged file matches `docs/assets/dist/*.js`
(or its `.js.map` sibling), then at least one staged file MUST also
match one of:

  - tools/portal/entries/*.entry.jsx       (per-tool entry script)
  - tools/portal/build.mjs                 (esbuild config)
  - tools/portal/manifest.json             (entry list)
  - tools/portal/shims/*.js                (build-time shims)
  - docs/interactive/tools/**/*.{jsx,js}   (component sources)
  - docs/getting-started/**/*.{jsx,js}     (wizard sources)

Otherwise it's "dist drift without source intent" — the exact pattern
that broke main in PR-E. Block the commit.

Allowed (deliberately NOT flagged):
- dist-only changes that ARE legitimate (e.g. a CI bot regenerating
  artifacts) — escape via `BYPASS_DIST_CHECK=1` env var.
- Commits that touch source AND dist together — the canonical case.
- Commits that don't touch dist at all — out of scope.

Severity model
--------------
Auto-stage FATAL on findings.

Usage
-----
    pre-commit run dist-source-consistency-check
    BYPASS_DIST_CHECK=1 git commit ...   # explicit escape (rare)
    python3 scripts/tools/lint/check_dist_source_consistency.py --staged
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import PurePosixPath
from typing import List, Set

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402

# Source-of-rebuild signals — if a commit stages dist AND any of these,
# the rebuild was intentional.
#
# TRK-242 monorepo restructure: portal source moved from docs/* to
# tools/portal/src/*. Patterns updated accordingly.
SOURCE_PATTERNS = [
    # Entry scripts — adding / modifying entries triggers chunk rebuild
    lambda p: p.startswith("tools/portal/entries/") and p.endswith(".entry.jsx"),
    # Build config / manifest / shims
    lambda p: p == "tools/portal/build.mjs",
    lambda p: p == "tools/portal/manifest.json",
    lambda p: p.startswith("tools/portal/shims/") and p.endswith(".js"),
    # JSX/JS source files (post-TRK-242)
    lambda p: p.startswith("tools/portal/src/") and (p.endswith(".jsx") or p.endswith(".js")),
]

# Dist-side patterns — staging any of these is what triggers the check.
def is_dist(p: str) -> bool:
    return p.startswith("docs/assets/dist/") and (p.endswith(".js") or p.endswith(".js.map"))


def is_source(p: str) -> bool:
    return any(pred(p) for pred in SOURCE_PATTERNS)


def staged_files() -> List[str]:
    """Return list of files in the staging area, as POSIX-style paths."""
    # 10s timeout is generous — `git diff --cached --name-only` is near-instant
    # in any sane repo state. Cap protects against hung git processes (S#74).
    out = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRT"],
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    return [str(PurePosixPath(line.strip())) for line in out.splitlines() if line.strip()]


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Catch portal dist commits without matching source change."
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        default=True,
        help="Check staged files (default, used by pre-commit)",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Optional explicit file paths (overrides --staged; for testing only)",
    )
    args = parser.parse_args()

    if os.environ.get("BYPASS_DIST_CHECK") == "1":
        print("dist-source-consistency: ⏭ skipped (BYPASS_DIST_CHECK=1)")
        return 0

    if args.files:
        files = [str(PurePosixPath(f)) for f in args.files]
    else:
        try:
            files = staged_files()
        except subprocess.CalledProcessError as e:
            print(f"dist-source-consistency: ⚠ git diff failed: {e}")
            return 0  # don't block the commit on infra error

    dist_changes = [f for f in files if is_dist(f)]
    if not dist_changes:
        # No dist staged — nothing to check.
        return 0

    source_changes = [f for f in files if is_source(f)]
    if source_changes:
        print(
            f"dist-source-consistency: ✓ {len(dist_changes)} dist file(s) staged with "
            f"{len(source_changes)} source-of-rebuild signal(s)"
        )
        return 0

    # Dist changed without any source signal — block.
    print(
        "dist-source-consistency: ✗ dist files staged without any source-of-rebuild "
        "signal — testing-playbook §LL v2.8.0 §2"
    )
    print()
    print(f"  Staged dist files ({len(dist_changes)}):")
    for f in dist_changes[:8]:
        print(f"    - {f}")
    if len(dist_changes) > 8:
        print(f"    ... and {len(dist_changes) - 8} more")
    print()
    print("  Expected at least one staged file matching:")
    print("    - tools/portal/entries/*.entry.jsx")
    print("    - tools/portal/build.mjs")
    print("    - tools/portal/manifest.json")
    print("    - tools/portal/shims/*.js")
    print("    - docs/interactive/tools/**/*.{jsx,js}")
    print("    - docs/getting-started/**/*.{jsx,js}")
    print()
    print("  Why: PR-E (#269) committed regenerated dist on a chunk reshuffle")
    print("  triggered by ONE source change. Reviewer attention was on the")
    print("  visible source change; the hidden dist drift broke 20 specs in")
    print("  main (TRK-233). This hook catches that pattern at commit time.")
    print()
    print("  If this is a legitimate dist-only commit (rare — e.g. CI bot")
    print("  regenerating artifacts), escape with: BYPASS_DIST_CHECK=1 git commit")

    return 1


if __name__ == "__main__":
    sys.exit(main())
