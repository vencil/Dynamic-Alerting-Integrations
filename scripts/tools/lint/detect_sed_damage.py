#!/usr/bin/env python3
"""Detect sed -i damage on staged files.

Pre-commit hook that checks if staged files show signs of sed -i damage:
1. NUL bytes appeared in a file that didn't have them before
2. File was truncated (>50% size reduction compared to HEAD)

This is a DETECTION layer — the prevention layer is vibe-sed-guard.sh.
The REPAIR layer is fix_file_hygiene.py.

Allowlist for legitimate large refactors (PR-portal-6)
------------------------------------------------------
The truncation heuristic (>50% shrink) blocks legitimate refactors that
inline-shrink a file (e.g. converting an authoritative module into a
thin BC re-export shim). To allow such a refactor without disabling the
guard repo-wide, list the file path in `.sed-damage-allowlist` at the
repo root, one path per line, # for comments.

Allowlist semantics:
  - ONLY suppresses the truncation check, NOT the NUL-byte check
    (NUL bytes are always damage, never legitimate)
  - Path is matched verbatim against the staged path; no glob support
  - Empty / missing allowlist file → no exemption (default behaviour)

Usage:
    python3 detect_sed_damage.py [files...]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Repo root: this file is at <repo>/scripts/tools/lint/detect_sed_damage.py
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ALLOWLIST_FILE = _REPO_ROOT / ".sed-damage-allowlist"


def get_head_content(path: str) -> bytes | None:
    """Get file content from HEAD (returns None if file is new)."""
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def load_allowlist(path: Path = _ALLOWLIST_FILE) -> set[str]:
    """Load the truncation-check allowlist from `.sed-damage-allowlist`.

    Returns an empty set if the file is missing — the default behaviour
    is no exemption.

    Format: one path per line, `#` starts a comment, blank lines ignored.
    Trailing inline comments (after `#`) are supported.
    """
    if not path.exists():
        return set()
    allowed: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Strip inline trailing comment.
            if "#" in stripped:
                stripped = stripped.split("#", 1)[0].strip()
            if stripped:
                # Normalize separators so allowlist works on both
                # POSIX and Windows-style staged paths.
                allowed.add(stripped.replace("\\", "/"))
    return allowed


def check_file(path: str, allowlist: set[str] | None = None) -> list[str]:
    """Check a single file for sed -i damage. Returns list of issues."""
    if allowlist is None:
        allowlist = load_allowlist()

    issues = []

    try:
        current = open(path, "rb").read()
    except (OSError, IsADirectoryError):
        return issues

    head_content = get_head_content(path)

    # Check 1: NUL bytes appeared (didn't exist in HEAD).
    # NEVER suppressed by allowlist — NUL bytes are always damage.
    if b"\x00" in current:
        if head_content is None or b"\x00" not in head_content:
            issues.append(
                f"NUL bytes detected (not present in HEAD) — likely sed -i damage"
            )

    # Check 2: Significant truncation (file shrank >50%).
    # Suppressed for paths listed in `.sed-damage-allowlist`.
    if head_content is not None and len(head_content) > 100:
        normalized = path.replace("\\", "/")
        if normalized not in allowlist:
            ratio = len(current) / len(head_content)
            if ratio < 0.5:
                issues.append(
                    f"File truncated to {ratio:.0%} of HEAD size "
                    f"({len(head_content)}→{len(current)} bytes) — "
                    f"possible sed -i on file without EOF newline. "
                    f"If intentional refactor, add path to .sed-damage-allowlist."
                )

    return issues


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect sed -i damage on staged files",
    )
    parser.add_argument("files", nargs="*", help="Files to check")
    args = parser.parse_args()

    files = args.files
    if not files:
        return 0

    allowlist = load_allowlist()

    all_issues: dict[str, list[str]] = {}
    for f in files:
        issues = check_file(f, allowlist=allowlist)
        if issues:
            all_issues[f] = issues

    if not all_issues:
        return 0

    print("")
    print("⛔ sed-damage-guard: 偵測到可能的 sed -i 損壞：")
    print("")
    for path, issues in all_issues.items():
        for issue in issues:
            print(f"  {path}: {issue}")
    print("")
    print("修復方式：")
    print("  git checkout HEAD -- <file>    # 還原後用 Read+Edit 重做修改")
    print("  或: fix_file_hygiene.py <file>  # 移除 NUL bytes + 補 EOF newline")
    print("  或（重構縮減合法）: 把路徑加入 .sed-damage-allowlist")
    print("")
    return 1


if __name__ == "__main__":
    sys.exit(main())
