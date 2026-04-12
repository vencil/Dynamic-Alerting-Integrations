#!/usr/bin/env python3
"""Detect sed -i damage on staged files.

Pre-commit hook that checks if staged files show signs of sed -i damage:
1. NUL bytes appeared in a file that didn't have them before
2. File was truncated (>50% size reduction compared to HEAD)

This is a DETECTION layer — the prevention layer is vibe-sed-guard.sh.
The REPAIR layer is fix_file_hygiene.py.

Usage:
    python3 detect_sed_damage.py [files...]
"""
import subprocess
import sys


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


def check_file(path: str) -> list[str]:
    """Check a single file for sed -i damage. Returns list of issues."""
    issues = []

    try:
        current = open(path, "rb").read()
    except (OSError, IsADirectoryError):
        return issues

    head_content = get_head_content(path)

    # Check 1: NUL bytes appeared (didn't exist in HEAD)
    if b"\x00" in current:
        if head_content is None or b"\x00" not in head_content:
            issues.append(
                f"NUL bytes detected (not present in HEAD) — likely sed -i damage"
            )

    # Check 2: Significant truncation (file shrank >50%)
    if head_content is not None and len(head_content) > 100:
        ratio = len(current) / len(head_content)
        if ratio < 0.5:
            issues.append(
                f"File truncated to {ratio:.0%} of HEAD size "
                f"({len(head_content)}→{len(current)} bytes) — "
                f"possible sed -i on file without EOF newline"
            )

    return issues


def main() -> int:
    files = [f for f in sys.argv[1:] if not f.startswith("--")]
    if not files:
        return 0

    all_issues: dict[str, list[str]] = {}
    for f in files:
        issues = check_file(f)
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
    print("")
    return 1


if __name__ == "__main__":
    sys.exit(main())
