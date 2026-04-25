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

# Match "#NN" or "| #NN |" at the start of a row to extract the session id.
SESSION_ID_RE = re.compile(r"^\|\s*#?(\d+)\s*\|")
# Match "## §S#NN" headers in archive files to find existing archive sections.
ARCHIVE_SECTION_RE = re.compile(r"^##\s+§S#(\d+)\b")


def find_offending_rows(path: Path, limit: int) -> list[tuple[int, int, str, str]]:
    """Return list of (line_no, char_count, preview, full_line) tuples for
    over-limit rows. `full_line` is needed by --auto-archive-suggest to
    extract session_id and date from the original row.
    """
    in_ledger = False
    offenders: list[tuple[int, int, str, str]] = []
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
            offenders.append((lineno, len(line), preview, line))
    return offenders


def find_archived_sessions(archive_path: Path) -> set[str]:
    """Scan archive doc for ## §S#NN headers, return set of session IDs as strings."""
    if not archive_path.exists():
        return set()
    try:
        text = archive_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {m.group(1) for line in text.splitlines() if (m := ARCHIVE_SECTION_RE.match(line))}


def derive_archive_path(planning_path: Path) -> Path:
    """Convention: v2.8.0-planning.md ↔ v2.8.0-planning-archive.md (sibling)."""
    name = planning_path.name
    if name.endswith("-planning.md"):
        archive_name = name[: -len(".md")] + "-archive.md"
        return planning_path.parent / archive_name
    return planning_path.parent / (planning_path.stem + "-archive.md")


def suggest_slim_pointer(full_line: str, archive_path: Path) -> str | None:
    """Generate a slim-pointer replacement for an over-bloat row IFF the
    archive contains a matching §S#NN section. Returns None if no archive
    match (caller would have to manually archive first).

    Format follows existing slim pointers in the codebase:
        | #NN | DATE | <one-line title> | **PR #X merged** … 詳 archive §S#NN | <status> | <next> |
    """
    m = SESSION_ID_RE.match(full_line)
    if not m:
        return None
    session_id = m.group(1)
    if session_id not in find_archived_sessions(archive_path):
        return None
    # Parse the existing row to extract: id, date, title (3rd cell), status (5th cell), next (6th cell).
    # Markdown tables: split on '|' but discard leading/trailing empties.
    cells = [c.strip() for c in full_line.split("|")]
    cells = cells[1:-1] if cells and cells[0] == "" else cells
    if len(cells) < 6:
        return None
    sid, date, title, body, status, nxt = cells[0], cells[1], cells[2], cells[3], cells[4], cells[5]
    # Truncate title to avoid pulling huge content; keep ≤ 80 chars.
    short_title = title if len(title) <= 80 else title[:77] + "…"
    # Body should reduce to a one-liner pointing at archive.
    suggested_body = f"**Archived** — full detail in archive §S#{session_id}"
    return f"| {sid} | {date} | {short_title} | {suggested_body} | {status[:80]} | {nxt[:80]} |"


def emit_archive_suggestions(
    offenders_by_path: dict[Path, list[tuple[int, int, str, str]]],
) -> None:
    """Print --auto-archive-suggest output to stdout: one block per offender,
    showing the original row and a suggested slim-pointer replacement.
    """
    any_suggestion = False
    for planning_path, offenders in offenders_by_path.items():
        if not offenders:
            continue
        archive_path = derive_archive_path(planning_path)
        archived = find_archived_sessions(archive_path)
        for lineno, n, preview, full_line in offenders:
            sid_match = SESSION_ID_RE.match(full_line)
            sid = sid_match.group(1) if sid_match else "?"
            print(f"\n=== {_display_path(planning_path)}:L{lineno} (S#{sid}, {n} chars) ===")
            if sid not in archived:
                print(f"  No matching §S#{sid} in {_display_path(archive_path)}.")
                print(f"  → MANUAL: write archive §S#{sid} first, then re-run with --auto-archive-suggest.")
                continue
            suggested = suggest_slim_pointer(full_line, archive_path)
            if suggested is None:
                print(f"  Could not parse row structure (need 6 cells); manual trim required.")
                continue
            any_suggestion = True
            print(f"  ORIGINAL (excerpt): {preview}…")
            print(f"  SUGGESTED replacement (paste over L{lineno}):")
            print(f"  {suggested}")
    if not any_suggestion:
        print(
            "\nNo auto-suggestions generated. For each over-bloat row, ensure the matching\n"
            "§S#NN section exists in the archive doc, then re-run.",
            file=sys.stderr,
        )


def resolve_targets(args_paths: list[str], glob: str) -> list[Path]:
    """Resolve CLI paths or fall back to repo-root glob."""
    if args_paths:
        return [Path(p) for p in args_paths]
    return sorted(REPO_ROOT.glob(glob))


def _display_path(path: Path) -> Path:
    """Return a repo-relative path when possible, else the path as-is.

    Using `Path.relative_to` directly crashes if the path is absolute but
    lives outside REPO_ROOT (e.g. test fixture under tmp_path); we fall
    back to the original path in that case instead of aborting the report.
    """
    if not path.is_absolute():
        return path
    try:
        return path.relative_to(REPO_ROOT)
    except ValueError:
        return path


def report(
    offenders_by_path: dict[Path, list[tuple[int, int, str, str]]], limit: int
) -> None:
    for path, offenders in offenders_by_path.items():
        if not offenders:
            continue
        print(f"\n{_display_path(path)}: "
              f"{len(offenders)} Session row(s) exceed {limit} chars")
        for lineno, n, preview, _full in offenders:
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
    parser.add_argument(
        "--auto-archive-suggest",
        action="store_true",
        help="For each over-bloat row, if a matching §S#NN section exists in "
             "the sibling -archive.md doc, emit a suggested slim-pointer "
             "replacement to stdout (manual paste). No file is modified.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    targets = resolve_targets(args.paths, args.glob)
    if not targets:
        print(f"no planning docs matched (glob={args.glob}); nothing to check",
              file=sys.stderr)
        return 0

    offenders_by_path: dict[Path, list[tuple[int, int, str, str]]] = {}
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
    if args.auto_archive_suggest:
        emit_archive_suggestions(offenders_by_path)
    print(
        "\nHint: fold bloated rows to:",
        "\n  • CHANGELOG.md  — durable user-facing entry (PR/commit landed)",
        "\n  • <playbook>.md — Lesson Learned for env / FUSE / CI traps",
        "\n  • v*-planning-archive.md  — concluded session detail",
        "\nSee dev-rules.md §A6 (v2.9.0+ planning doc 不再保留 Session Ledger).",
        "\nRun with --auto-archive-suggest to generate slim-pointer replacements"
        " for rows that already have an archive §S#NN section.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
