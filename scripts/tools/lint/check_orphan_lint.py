#!/usr/bin/env python3
"""check_orphan_lint.py — orphan / dead-lint detector (#717; supersedes #456 residue).

Why this exists
---------------
``scripts/tools/dx/generate_tool_map.py`` globs ``scripts/tools/lint/check_*.py``,
so a newly-added lint is guaranteed to be *recorded* into ``tool-map.{md,en.md}``
(the ``tool-map-check`` hard gate). But being documented is not being *run*: if a
new ``check_*.py`` is wired into no runner, it is a **dead lint** — it exists, it is
documented, and it never executes. #141 Track A had to *revive a dead exporter check
by hand*; dead-lint is a real, human-caught failure mode. This gate makes the
detection mechanical.

``check_structure.py`` already enforces script *placement* (ops/dx/lint buckets); it
does NOT check *registration*. This lint closes that residual gap.

What counts as "live"
---------------------
A ``check_*.py`` is **live** when its filename appears as an invocation in ANY
repo-internal runner (the "被任一 runner 引用" reference graph from #717's scope):

  - ``.pre-commit-config.yaml``        — an ``entry:`` hook
  - ``scripts/tools/validate_all.py``  — the ``TOOLS`` registry
  - ``Makefile``                       — a recipe target
  - ``.github/workflows/*.{yml,yaml}`` — a ``run:`` step
  - any sibling script under ``scripts/`` *outside* ``scripts/tools/lint/``
    (e.g. ``dx/pr_preflight.py`` runs ``check_pr_scope_drift``,
    ``dx/bump_playbook_versions.py`` runs ``check_playbook_freshness``,
    ``dx/scan_component_health.py`` runs ``check_tool_registry_jsx_parity``)

Design note — broader than the two runners the #717 body names
--------------------------------------------------------------
The issue body names only pre-commit + ``validate_all`` as runners, but its Scope
section says "只查『可執行 lint 是否被任一 runner 引用』的 repo-internal 引用圖".
Honouring the literal two-runner list would false-flag 8 checks that legitimately
run via Makefile / CI / a sibling script, forcing a static allowlist of 8 entries
that are not actually orphans — a fail-open design (if such a check were later
*removed* from CI, the allowlist would keep hiding it). The reference-graph model is
self-maintaining instead: a check is live iff some runner still invokes it, and a
later removal re-surfaces it. So this gate scans the full runner graph and the
allowlist stays reserved for *genuinely* runner-less checks.

Why ``scripts/tools/lint/`` is excluded from the referencer set
---------------------------------------------------------------
Lint files routinely mention *other* checks' filenames in their docstrings
(cross-references, design notes). Counting those prose mentions as "references"
would mask a dead lint behind a comment. The real cross-script invokers all live in
``scripts/tools/dx/`` / ``ops/``, so excluding ``lint/`` loses no genuine wiring.

Allowlist
---------
A ``check_*.py`` that is intentionally standalone (manual-only, invoked by a human
on demand, wired to no runner) may be listed in ``ALLOWLIST`` with a one-line
justification. Helper modules are ``_``-prefixed (``_lib*.py`` / ``_lint_helpers.py``)
and never match the ``check_*.py`` glob, so they need no allowlist entry. The
allowlist is currently empty: every executable lint is wired to a runner.

Known limitation (accepted, like check_lint_toolchain_fit.py): detection is a
filename-substring match against the runner corpus — a tripwire for the common case,
not an airtight proof. A runner that builds the script name dynamically from pieces
would slip through; a stray prose mention of ``check_foo.py`` inside a *runner* file
(not a lint file, which we already exclude) could falsely mark it live. Both are
rare; revisit if they ever happen in practice rather than over-engineering now.

Scope discipline (#717)
-----------------------
Repo-internal reference-graph only. This does NOT reimplement #456's staged-diff
heuristic (high false-positive; the wiring-triple body is already covered by
``tool-map-check`` / ``build-completeness-check`` / ``cli-coverage-check`` /
``doc-datools-cmds`` / ``check_changelog_no_tbd``).

Usage:
    python3 scripts/tools/lint/check_orphan_lint.py        # report mode
    python3 scripts/tools/lint/check_orphan_lint.py --ci   # exit 1 on orphans

Exit codes (see scripts/tools/_lib_exitcodes.py):
    0 = no orphan lints
    1 = orphan / dead lint(s) found (--ci mode)
    2 = caller/environment error (e.g. lint dir missing)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)                     # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# ── Config ──────────────────────────────────────────────────────────

# check_*.py files that are intentionally standalone (manual-only / human-invoked,
# wired to no runner). Each entry: basename -> one-line justification. Keep empty
# unless a check genuinely has no runner home; prefer wiring a check into a runner
# over allowlisting it.
ALLOWLIST: dict[str, str] = {
    # "check_example_manual.py": "manual-only forensic tool, run on demand (#NNN)",
}

# ── Reference-graph builders ────────────────────────────────────────

def find_check_lints(lint_dir: Path) -> list[str]:
    """Return sorted basenames of executable lints (check_*.py) in lint_dir."""
    return sorted(p.name for p in lint_dir.glob("check_*.py"))


def gather_referencers(project_root: Path, lint_dir: Path) -> list[Path]:
    """Collect every repo-internal runner file that may invoke a check_*.py.

    Excludes scripts/tools/lint/ itself (lint files cross-reference each other in
    prose; those mentions are not invocations — see module docstring).
    """
    referencers: list[Path] = []

    for rel in (".pre-commit-config.yaml", "scripts/tools/validate_all.py", "Makefile"):
        p = project_root / rel
        if p.exists():
            referencers.append(p)

    workflows = project_root / ".github" / "workflows"
    if workflows.is_dir():
        referencers.extend(sorted(workflows.glob("*.yml")))
        referencers.extend(sorted(workflows.glob("*.yaml")))

    # GitLab CI is a first-class runner too (the repo carries GitLab lineage,
    # e.g. .gitlab/ci/config-diff.gitlab-ci.yml). Include it so a lint wired
    # only into GitLab CI is not false-flagged as orphan.
    root_gitlab_ci = project_root / ".gitlab-ci.yml"
    if root_gitlab_ci.exists():
        referencers.append(root_gitlab_ci)
    gitlab_dir = project_root / ".gitlab"
    if gitlab_dir.is_dir():
        referencers.extend(sorted(gitlab_dir.glob("**/*.yml")))
        referencers.extend(sorted(gitlab_dir.glob("**/*.yaml")))

    scripts_dir = project_root / "scripts"
    if scripts_dir.is_dir():
        for pattern in ("**/*.py", "**/*.sh"):
            for p in scripts_dir.glob(pattern):
                if "__pycache__" in p.parts:
                    continue
                if p.parent == lint_dir:  # lint files are not referencers
                    continue
                referencers.append(p)

    return referencers


def read_corpus(referencers: list[Path]) -> str:
    """Concatenate the text of every referencer file (errors ignored)."""
    chunks: list[str] = []
    for f in referencers:
        try:
            chunks.append(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def find_orphans(
    check_lints: list[str],
    corpus: str,
    allowlist: dict[str, str] | None = None,
) -> list[str]:
    """Return check_*.py basenames not referenced in the runner corpus.

    Allowlisted basenames are never reported.
    """
    allow = set(allowlist or {})
    orphans = []
    for name in check_lints:
        if name in allow:
            continue
        if name not in corpus:
            orphans.append(name)
    return orphans


# ── Main ────────────────────────────────────────────────────────────

def main() -> int:
    """CLI entry point: orphan / dead-lint detector."""
    parser = argparse.ArgumentParser(
        description="Detect lint scripts wired into no runner (dead lints).",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="Exit with code 1 on orphans (for CI/pre-commit)",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[3]
    lint_dir = project_root / "scripts" / "tools" / "lint"
    if not lint_dir.is_dir():
        print(f"✗ lint dir not found: {lint_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    check_lints = find_check_lints(lint_dir)
    referencers = gather_referencers(project_root, lint_dir)
    corpus = read_corpus(referencers)
    orphans = find_orphans(check_lints, corpus, ALLOWLIST)

    if not orphans:
        allow_note = f" ({len(ALLOWLIST)} allowlisted)" if ALLOWLIST else ""
        print(f"✓ All {len(check_lints)} executable lints are wired to a "
              f"runner{allow_note}.")
        return EXIT_OK

    print("✗ Orphan / dead lint(s) — wired into no runner:")
    for name in orphans:
        print(f"  DEAD  scripts/tools/lint/{name}")
    print()
    print("Fix: wire each into a runner — a .pre-commit-config.yaml entry:, "
          "the validate_all.py TOOLS list, a Makefile recipe, a CI workflow "
          "run: step, or invoke it from a dx/ops sibling script. If it is")
    print("genuinely manual-only, add it to ALLOWLIST in this file with a "
          "justification.")

    return EXIT_VIOLATION if args.ci else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
