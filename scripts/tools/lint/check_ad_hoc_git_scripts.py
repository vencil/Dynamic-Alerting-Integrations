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

Exit:
  0 = no unapproved scripts found
  1 = at least one script outside the allowlist

Configuration:
  * ALLOWLIST_DIRS: path prefixes (repo-relative, POSIX-style) that may hold
    Windows shell scripts. If you truly need a new top-level tool dir, add it
    here + justify in PR body.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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


def scan(repo: Path) -> list[Path]:
    """Return list of offending files (repo-relative)."""
    offending: list[Path] = []
    for ext in EXTS:
        for candidate in repo.rglob(f"*{ext}"):
            parts = candidate.relative_to(repo).parts
            # Skip if ANY path segment is in skip list (covers nested node_modules).
            if any(p in SKIP_ANY for p in parts):
                continue
            rel_posix = "/".join(parts)
            if not is_allowlisted(rel_posix):
                offending.append(candidate.relative_to(repo))
    return sorted(offending)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reject Windows shell scripts (*.bat, *.ps1, *.cmd) outside "
            "allowlisted directories. Prevents ad-hoc `_commit.ps1` "
            "proliferation — use scripts/ops/win_git_escape.bat instead."
        )
    )
    parser.parse_args()

    repo = find_repo_root()
    offenders = scan(repo)
    if not offenders:
        return 0

    print(
        f"[check_ad_hoc_git_scripts] FAIL: {len(offenders)} Windows shell "
        f"script(s) outside allowlisted dirs:",
        file=sys.stderr,
    )
    for f in offenders:
        print(f"    {f}", file=sys.stderr)
    allowlist = "\n".join(f"    {p}" for p in ALLOWLIST_DIRS)
    print(
        "\n  Allowlisted dirs for *.bat / *.ps1 / *.cmd:\n"
        f"{allowlist}\n\n"
        "  What to do instead of writing a throw-away .bat / .ps1:\n"
        "    1. For git operations        -> scripts/ops/win_git_escape.bat\n"
        "    2. For gh (GitHub CLI) ops   -> scripts/ops/win_gh.bat\n"
        "    3. Missing subcommand?       -> extend the existing wrapper, DO\n"
        "                                    NOT write a sibling script.\n\n"
        "  See docs/internal/windows-mcp-playbook.md#修復層-c (Windows escape\n"
        "  hatch) for the full playbook.\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
