#!/usr/bin/env python3
"""Ad-hoc Windows shell script guard (L1 pre-commit hook).

Root cause chronicle:
  PR #39 session: agent wrote `_p39_commit.ps1` to work around FUSE git issues.
  PR #40 session: agent wrote `_p40_commit.ps1`, `_p40_pr.bat`, `_p40_checks.bat`,
                  `_p40_failog.bat`, `_p40_diag.bat` — FIVE ad-hoc scripts, all
                  reinventing what `scripts/ops/win_git_escape.bat` already does,
                  all because the agent didn't read the playbook before acting.

This hook makes the failure mode physically impossible:

  * **Whitelist mode** (not blacklist): every Windows shell script in the tree
    (`*.bat`, `*.ps1`, `*.cmd`) must live under an allowlisted path (`scripts/`,
    `tools/`, etc.). A throw-away `_foo.bat` anywhere else is rejected.
  * **Rationale for whitelist**: PR #40 taught us that a blacklist regex
    (`commit|push|git|tag|...`) is whack-a-mole — the agent always finds a
    new verb (check / failog / diag) that slips through.
  * **What to do instead**: if `scripts/ops/win_git_escape.bat` / `win_gh.bat`
    lack a subcommand, **extend them**, don't write a sibling script.

Lint class: (b) per docs/internal/lint-policy.md (negative pattern + path
allowlist as false-positive escape). Default scope: **diff-only** — only
shell scripts ADDED or MODIFIED in current diff are checked. Override
with --full-scan for periodic manual audit of the whole tree.

Usage:
    # Diff-only (default; CI sets LINT_DIFF_BASE / GITHUB_BASE_REF)
    python3 scripts/tools/lint/check_ad_hoc_git_scripts.py [--ci]

    # Full repo scan (manual audit)
    python3 scripts/tools/lint/check_ad_hoc_git_scripts.py --full-scan [--ci]

Bypass (per lint-policy.md §4):
    Add to PR description body:
        bypass-lint: ad-hoc-git-scripts
        reason: <≥30 words explaining why this case is legitimate>

Exit:
  0 = no offenders (or bypass matched)
  1 = at least one script outside the allowlist
  2 = diff base ref missing — fix CI workflow's fetch-depth or base ref

Configuration:
  * ALLOWLIST_DIRS: path prefixes (repo-relative, POSIX-style) that may hold
    Windows shell scripts. If you truly need a new top-level tool dir, add it
    here + justify in PR body.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Helpers from this lint family
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lint_helpers import (  # noqa: E402
    DiffBaseMissingError,
    parse_bypass_tag,
    resolve_diff_base,
)

EXTS = {".bat", ".ps1", ".cmd"}

# Repo-relative POSIX paths. Anything whose path starts with one of these
# prefixes is considered a sanctioned location for Windows shell scripts.
ALLOWLIST_DIRS = (
    "scripts/ops/",
    "scripts/tools/",
    "tools/",
    # Playbook / docs sample blocks live in .md files, not .bat — no allowlist
    # needed for them. If you add `docs/examples/` with runnable .bat later,
    # register it here + justify in PR body.
)


def find_repo_root() -> Path:
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return Path(__file__).resolve().parent.parent.parent.parent


def is_allowlisted(rel_posix: str) -> bool:
    return any(rel_posix.startswith(prefix) for prefix in ALLOWLIST_DIRS)


SKIP_ANY = {
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    ".pytest_cache", "dist", "build", ".mypy_cache", ".next",
}


def scan_full(repo: Path) -> list[Path]:
    """Full-tree scan: return list of offending files (repo-relative)."""
    offending: list[Path] = []
    for ext in EXTS:
        for candidate in repo.rglob(f"*{ext}"):
            parts = candidate.relative_to(repo).parts
            if any(p in SKIP_ANY for p in parts):
                continue
            rel_posix = "/".join(parts)
            if not is_allowlisted(rel_posix):
                offending.append(candidate.relative_to(repo))
    return sorted(offending)


def scan_diff(repo: Path, base: str) -> list[Path]:
    """Diff-only scan: return offenders newly added/modified in current diff vs base.

    Uses ``git diff --name-only --diff-filter=AM`` so deleted files don't
    trigger the lint (deleting an offender is the right move).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=AM", base],
            capture_output=True, text=True, cwd=str(repo),
            check=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []

    offending: list[Path] = []
    for rel in result.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        # Only check Windows shell scripts
        if not any(rel.endswith(ext) for ext in EXTS):
            continue
        # Skip sandbox / vendored dirs
        parts = rel.replace("\\", "/").split("/")
        if any(p in SKIP_ANY for p in parts):
            continue
        if is_allowlisted(rel.replace("\\", "/")):
            continue
        # File should still exist (filter=AM excludes deleted but be safe)
        full_path = repo / rel
        if not full_path.is_file():
            continue
        offending.append(Path(rel))
    return sorted(offending)


def _read_pr_body(pr_body_file: str | None) -> str | None:
    """Read PR body from --pr-body-file or $PR_BODY env var."""
    if pr_body_file:
        try:
            return Path(pr_body_file).read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError) as e:
            print(f"WARN: cannot read --pr-body-file {pr_body_file}: {e}", file=sys.stderr)
    return os.environ.get("PR_BODY") or None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reject Windows shell scripts (*.bat, *.ps1, *.cmd) outside "
            "allowlisted directories. Prevents ad-hoc `_commit.ps1` "
            "proliferation — use scripts/ops/win_git_escape.bat instead."
        )
    )
    parser.add_argument(
        "--full-scan", action="store_true",
        help="Scan entire repo (default is diff-only — recommended for CI).",
    )
    parser.add_argument(
        "--diff-base", default=None,
        help="Override diff base (default: $LINT_DIFF_BASE / $GITHUB_BASE_REF / origin/main).",
    )
    parser.add_argument(
        "--pr-body-file", default=None,
        help="Path to file containing PR body for bypass tag check.",
    )
    args = parser.parse_args()

    repo = find_repo_root()

    # Resolve scan mode
    if args.full_scan:
        scan_mode = "full-scan"
        offenders = scan_full(repo)
    else:
        try:
            base = args.diff_base or resolve_diff_base()
        except DiffBaseMissingError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        scan_mode = f"diff vs {base}"
        offenders = scan_diff(repo, base)

    if not offenders:
        print(
            f"[check_ad_hoc_git_scripts] OK no ad-hoc Windows shell scripts "
            f"(mode={scan_mode}).",
        )
        return EXIT_OK

    # Bypass check (lint-policy.md §4)
    pr_body = _read_pr_body(args.pr_body_file)
    bypass_reason = parse_bypass_tag(pr_body, "ad-hoc-git-scripts")

    print(
        f"[check_ad_hoc_git_scripts] FAIL: {len(offenders)} Windows shell "
        f"script(s) outside allowlisted dirs (mode={scan_mode}):",
        file=sys.stderr,
    )
    for f in offenders:
        print(f"    {f}", file=sys.stderr)

    if bypass_reason:
        print(
            f"\n⚠️  BYPASSED via PR body: {bypass_reason}\n"
            f"   {len(offenders)} finding(s) above are author-acknowledged.\n"
            f"   Reviewer must confirm bypass is justified.",
            file=sys.stderr,
        )
        return EXIT_OK

    allowlist = "\n".join(f"    {p}" for p in ALLOWLIST_DIRS)
    print(
        "\n  Allowlisted dirs for *.bat / *.ps1 / *.cmd:\n"
        f"{allowlist}\n\n"
        "  What to do instead of writing a throw-away .bat / .ps1:\n"
        "    1. For git operations        -> scripts/ops/win_git_escape.bat\n"
        "    2. For gh (GitHub CLI) ops   -> scripts/ops/win_gh.bat\n"
        "    3. Missing subcommand?       -> extend the existing wrapper, DO\n"
        "                                    NOT write a sibling script.\n\n"
        "  See docs/internal/windows-mcp-playbook.md for the full playbook.\n"
        "  Or add to PR description (per lint-policy.md §4):\n"
        "    bypass-lint: ad-hoc-git-scripts\n"
        "    reason: <≥30 words explaining why this case is legitimate>",
        file=sys.stderr,
    )
    return EXIT_VIOLATION


if __name__ == "__main__":
    sys.exit(main())
