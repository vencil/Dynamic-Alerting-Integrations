#!/usr/bin/env python3
"""Detect bloated §12.1 Session Ledger rows in versioned planning docs.

Background
----------
v2.8.0 Phase .a Session #18 root-caused chronic context-compact pressure
during the v2.8.0 cycle to a single anti-pattern: the §12.1 Session
Ledger table is append-only, and per-session rows had grown to 2-4 KB
of free-form prose. Reading the planning doc each session re-loaded the
entire ledger, burning ~50K context tokens.

Codified rule (dev-rules.md §A6, v2.8.0 Phase .a):

    v2.9.0+ planning doc 不再保留 §12.1 Session Ledger.
    Active-cycle planning may keep one, but each session row must stay
    summary-shaped (≤ ~2 KB / ≤ ~5 visible lines). Anything longer
    belongs in:
      - CHANGELOG.md (durable user-facing entry)
      - the matching playbook (Lesson Learned)
      - v*-planning-archive.md (concluded session detail)

This script flags Session Ledger rows that exceed the char threshold and
prints a hint pointing the maintainer at the recommended fold target.

Usage
-----
  python3 scripts/tools/lint/validate_planning_session_row.py
  python3 scripts/tools/lint/validate_planning_session_row.py --limit 1500
  python3 scripts/tools/lint/validate_planning_session_row.py path/to/v2.9.0-planning.md

Exit
----
  0 — no offending rows (or no planning docs found, treated as no-op)
  1 — at least one row exceeds --limit

Notes
-----
- Planning docs are L2 gitignored (dev-rules.md §A1) so this hook is
  manual-stage only — pre-commit cannot stage gitignored files.
- Wire via `make check-planning-bloat` or the `manual` pre-commit stage.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GLOB = "docs/internal/v*-planning.md"
DEFAULT_ROW_CHAR_LIMIT = 2000

# §12.1 header (matches "### 12.1 Session Ledger" / "### 12.1 Session Ledger（Working Log）" / etc.)
LEDGER_HEADER_RE = re.compile(r"^###\s+12\.1\b.*Session\s+Ledger", re.IGNORECASE)
# Any other ### header closes the ledger scope.
ANY_SUBHEADER_RE = re.compile(r"^###\s+")
# Markdown table row: starts with `|` and the first non-pipe non-space cell
# either looks like a session number/id or starts with a #-prefixed identifier.
TABLE_ROW_RE = re.compile(r"^\|\s*[#\d]")
TABLE_DIVIDER_RE = re.compile(r"^\|[\s\-:|]+\|\s*$")


def find_offending_rows(path: Path, limit: int) -> list[tuple[int, int, str]]:
    """Return list of (line_no, char_count, preview) tuples for over-limit rows."""
    in_ledger = False
    offenders: list[tuple[int, int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
        return offenders

    for lineno, line in enumerate(text.splitlines(), start=1):
        if ANY_SUBHEADER_RE.match(line):
            in_ledger = bool(LEDGER_HEADER_RE.match(line))
            continue
        if not in_ledger:
            continue
        if TABLE_DIVIDER_RE.match(line):
            continue
        if TABLE_ROW_RE.match(line) and len(line) > limit:
            preview = line[:120].replace("\n", " ")
            offenders.append((lineno, len(line), preview))
    return offenders


def resolve_targets(args_paths: list[str], glob: str) -> list[Path]:
    """Resolve CLI paths or fall back to repo-root glob."""
    if args_paths:
        return [Path(p) for p in args_paths]
    return sorted(REPO_ROOT.glob(glob))


def report(offenders_by_path: dict[Path, list[tuple[int, int, str]]], limit: int) -> None:
    for path, offenders in offenders_by_path.items():
        if not offenders:
            continue
        print(f"\n{path.relative_to(REPO_ROOT) if path.is_absolute() else path}: "
              f"{len(offenders)} Session row(s) exceed {limit} chars")
        for lineno, n, preview in offenders:
            print(f"  L{lineno}: {n} chars — {preview}…")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Planning doc(s) to check. Defaults to glob 'docs/internal/v*-planning.md'.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_ROW_CHAR_LIMIT,
        help=f"Char limit per Session row (default {DEFAULT_ROW_CHAR_LIMIT}).",
    )
    parser.add_argument(
        "--glob",
        default=DEFAULT_GLOB,
        help=f"Glob pattern when paths omitted (default {DEFAULT_GLOB!r}).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    targets = resolve_targets(args.paths, args.glob)
    if not targets:
        print(f"no planning docs matched (glob={args.glob}); nothing to check",
              file=sys.stderr)
        return 0

    offenders_by_path: dict[Path, list[tuple[int, int, str]]] = {}
    any_bad = False
    for path in targets:
        if not path.exists():
            continue
        offenders = find_offending_rows(path, args.limit)
        offenders_by_path[path] = offenders
        if offenders:
            any_bad = True

    if not any_bad:
        scanned = sum(1 for p in targets if p.exists())
        print(f"OK: scanned {scanned} planning doc(s); no §12.1 row exceeds {args.limit} chars.")
        return 0

    report(offenders_by_path, args.limit)
    print(
        "\nHint: fold bloated rows to:",
        "\n  • CHANGELOG.md  — durable user-facing entry (PR/commit landed)",
        "\n  • <playbook>.md — Lesson Learned for env / FUSE / CI traps",
        "\n  • v*-planning-archive.md  — concluded session detail",
        "\nSee dev-rules.md §A6 (v2.9.0+ planning doc 不再保留 Session Ledger).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
