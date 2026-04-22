#!/usr/bin/env python3
"""Bump `verified-at-version:` front-matter across the 4 operational playbooks.

Companion to `scripts/tools/lint/check_playbook_freshness.py`: that tool
*detects* drift, this one *writes* the new stamp during tag cut.

Scope is deliberately the same 4 files (`PLAYBOOK_PATHS`). `dev-rules.md`
also carries `verified-at-version:` but is governed separately.

Usage:
  python3 scripts/tools/dx/bump_playbook_versions.py --to v2.8.0
  python3 scripts/tools/dx/bump_playbook_versions.py --to v2.8.0 --check
  python3 scripts/tools/dx/bump_playbook_versions.py --to v2.8.0 --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Canonical list, mirrored from check_playbook_freshness.py. Kept as a literal
# (not imported) so this tool works when the lint module is not on sys.path
# (e.g. invoked via `python3 <path>` without conftest bootstrap).
PLAYBOOK_PATHS = [
    "docs/internal/testing-playbook.md",
    "docs/internal/benchmark-playbook.md",
    "docs/internal/windows-mcp-playbook.md",
    "docs/internal/github-release-playbook.md",
]

VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+$")
FRONTMATTER_LINE_RE = re.compile(
    r"^(?P<prefix>verified-at-version:\s*)(?P<value>\S+)\s*$"
)


def find_repo_root() -> Path:
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return Path(__file__).resolve().parents[3]


def _normalize_version(raw: str) -> str:
    raw = raw.strip()
    return raw if raw.startswith("v") else f"v{raw}"


def _read_frontmatter_block(text: str) -> tuple[int, int] | None:
    """Return (start_idx, end_idx_exclusive) of the YAML front-matter block.

    Returns None if the file has no front-matter. Indexes are line-based
    (0-based into splitlines(keepends=True) output).
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == "---":
            return (1, idx)
    return None


def apply_bump(filepath: Path, target: str, write: bool) -> tuple[str, str]:
    """Apply bump to one file.

    Returns (status, detail) where status is one of:
      - "UPDATED"   : value changed, written (or would-write in check/dry mode)
      - "OK"        : already at target value
      - "MISSING"   : no front-matter or no verified-at-version field
    """
    try:
        # Read bytes to preserve CRLF — read_text defaults to universal
        # newline translation which loses the original terminator.
        raw = filepath.read_bytes()
    except OSError as exc:
        return ("MISSING", f"read error: {exc}")

    try:
        original = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ("MISSING", f"decode error: {exc}")

    block = _read_frontmatter_block(original)
    if block is None:
        return ("MISSING", "no YAML front-matter")

    lines = original.splitlines(keepends=True)
    start, end = block
    found_idx: int | None = None
    current_value: str | None = None
    for idx in range(start, end):
        match = FRONTMATTER_LINE_RE.match(lines[idx].rstrip("\r\n"))
        if match:
            found_idx = idx
            current_value = match.group("value").strip().strip('"').strip("'")
            break

    if found_idx is None:
        return ("MISSING", "no verified-at-version field in front-matter")

    if _normalize_version(current_value or "") == target:
        return ("OK", current_value or "")

    old_line = lines[found_idx]
    newline = "\r\n" if old_line.endswith("\r\n") else "\n"
    new_line = f"verified-at-version: {target}{newline}"

    if write:
        lines[found_idx] = new_line
        # write_bytes to bypass universal-newline translation; terminators
        # in `lines` are already byte-faithful.
        filepath.write_bytes("".join(lines).encode("utf-8"))

    return ("UPDATED", f"{current_value} -> {target}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bump verified-at-version across the 4 operational playbooks."
        )
    )
    parser.add_argument(
        "--to",
        required=True,
        metavar="vX.Y.Z",
        help="Target version (e.g. v2.8.0). 'v' prefix optional.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if any playbook is not at --to value (no writes).",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without writing.",
    )
    args = parser.parse_args()

    if not VERSION_RE.match(args.to):
        print(
            f"error: --to must match vX.Y.Z (got {args.to!r})",
            file=sys.stderr,
        )
        return 2
    target = _normalize_version(args.to)

    repo_root = find_repo_root()
    write = not (args.check or args.dry_run)

    results: list[tuple[str, str, str]] = []
    for rel in PLAYBOOK_PATHS:
        filepath = repo_root / rel
        if not filepath.exists():
            results.append(("MISSING", rel, "file not found"))
            continue
        status, detail = apply_bump(filepath, target, write=write)
        results.append((status, rel, detail))

    updated = [r for r in results if r[0] == "UPDATED"]
    missing = [r for r in results if r[0] == "MISSING"]

    for status, rel, detail in results:
        marker = {"UPDATED": "~", "OK": "=", "MISSING": "!"}.get(status, "?")
        print(f"  {marker} {status:<8} {rel}  {detail}")

    if args.check:
        if updated:
            print(
                f"\n{len(updated)} playbook(s) need bump to {target}",
                file=sys.stderr,
            )
            return 1
        if missing:
            print(
                f"\n{len(missing)} playbook(s) missing field; investigate",
                file=sys.stderr,
            )
            return 1
        print(f"\nAll {len(results)} playbooks at {target}")
        return 0

    if args.dry_run:
        print(
            f"\n[dry-run] would update {len(updated)} "
            f"playbook(s) to {target}"
        )
        return 0

    if missing:
        print(
            f"\nwarning: {len(missing)} playbook(s) lack "
            "verified-at-version — skipped",
            file=sys.stderr,
        )
    print(f"\nBumped {len(updated)} playbook(s) to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
