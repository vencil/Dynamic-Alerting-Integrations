#!/usr/bin/env python3
"""gen_adr_checks.py — an ADR's list of checks, generated from the code.

An ADR that states its decisions as checks numbers each clause of the section
that lists them (``- **3b** …`` under ``### 9. …``) and holds a generated
block for that section:

    <!-- BEGIN GENERATED: adr-checks section=9 -->
    <!-- END GENERATED: adr-checks -->

A test, script or CI step that implements a clause carries a marker comment
on the line above it (several clauses may share one line):

    // adr-check: ADR-027/9.3b
    # adr-check: ADR-027/9.6c ADR-027/9.6d

The block lists, per clause, the files carrying its marker and the test (or
CI step) right below each marker, and says "no check yet" for a clause no
marker names. Written by ``--write``; ``--check`` fails when a block differs
from what the markers give, so the ADR cannot claim a check that is not
there, or miss one that is.

Errors (exit 1): a marker naming an ADR, section or clause that does not
exist; a malformed marker; a duplicate clause id; an ADR file (either
language) of a marked section without the block. What a marked test actually
checks is not judged here — that is review's job.

Usage:
    python3 scripts/tools/dx/gen_adr_checks.py --check
    python3 scripts/tools/dx/gen_adr_checks.py --write
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
ADR_DIR = "docs/adr"

END = "<!-- END GENERATED: adr-checks -->"
_BEGIN = re.compile(r"<!-- BEGIN GENERATED: adr-checks section=(\d+) -->")

# Files that hold marker-shaped text that is not a marker.
SKIP_PATHS = {
    "scripts/tools/dx/gen_adr_checks.py": "this generator's own documentation",
    "tests/lint/test_gen_adr_checks.py": "this generator's fixtures",
}

# A marker is a comment line on its own — //, ///, #, /*, *, <!--, -- or a
# template comment {{/* — then exactly `adr-check:`.
_MARKER_LINE = re.compile(r"^\s*(?:/{2,}|#+|/\*+|\*|<!--|--|\{\{-?\s*/\*)\s*adr-check:(.*)$")
# Anything that looks like a marker but is not one is an error, never skipped:
# a typo would otherwise leave the clause silently unlisted.
_NEAR_MARKER = re.compile(r"(?i)adr[-_ ]?check\s*[:：]")
_ID = r"ADR-(\d{3})/(\d+)\.(\d+[a-z])"
_IDS = re.compile(r"^\s*(" + _ID + r"(?:[\s,]+" + _ID + r")*)\s*(?:\*/\s*-?\}\}|\*/|-->)?\s*$")
_ONE_ID = re.compile(_ID)

# What follows a marker: the first of these names it.
_NAMES = (
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)"),                         # Go
    re.compile(r"^\s*(?:async\s+)?def\s+(\w+)"),                            # Python
    re.compile(r"^\s*(?:it|test|describe)(?:\.\w+(?:\(.*?\))?)?\(\s*(['\"`])(.+?)\1"),  # Vitest
)
# A CI step's name, read only in workflow files: elsewhere `name:` is data.
_STEP_NAME = re.compile(r"^\s*-?\s*name:\s*[\"']?(.+?)[\"']?\s*$")
# Lines that may sit between a marker and what it marks (a blank line may not).
_SKIPPABLE = re.compile(r"^\s*(?://|#|@|/\*|\*|--)")
_LOOKAHEAD = 8

_CLAUSE = re.compile(r"^\s*-\s+\*\*(\d+[a-z])\*\*\s")
# Written like a clause but not in the clause's exact form: an error, so a
# clause cannot drop out of the list by being spelt differently.
_CLAUSE_LIKE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\*\*\d+[A-Za-z]\*\*")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^(#{2,4})\s+(\d+)\.\s")

_GAP = {"zh": "尚無檢查", "en": "no check yet"}


class AdrChecksError(Exception):
    """The markers or an ADR are inconsistent; the message says where."""


@dataclass(frozen=True)
class Marker:
    adr: str
    section: str
    clause: str
    path: str
    name: str | None


def begin_marker(section: str) -> str:
    return f"<!-- BEGIN GENERATED: adr-checks section={section} -->"


def parse_clauses(text: str, section: str) -> list[str]:
    """The clause ids under heading `section.` (to the next heading of the
    same or a higher level), in order."""
    out: list[str] = []
    level = None
    fence: str | None = None  # the opening fence while inside a code block
    for line in text.splitlines():
        f = _FENCE.match(line)
        if fence is not None:
            # CommonMark: closed only by the same character, at least as
            # long, with nothing but whitespace after it.
            if f and f.group(1)[0] == fence[0] and len(f.group(1)) >= len(fence) \
                    and not f.group(2).strip():
                fence = None
            continue
        if f:
            fence = f.group(1)
            continue
        h = _HEADING.match(line)
        if h or re.match(r"^#{1,4}\s", line):
            depth = len(line) - len(line.lstrip("#"))
            if level is not None and depth <= level:
                break
            if h and h.group(2) == section:
                level = len(h.group(1))
            continue
        if level is None:
            continue
        c = _CLAUSE.match(line)
        if c:
            if c.group(1) in out:
                raise AdrChecksError(f"section {section}: clause {c.group(1)} is declared twice")
            out.append(c.group(1))
        elif _CLAUSE_LIKE.match(line):
            raise AdrChecksError(
                f"section {section}: written like a clause but not as `- **1a** text`: {line.strip()}")
    return out


def _name_after(lines: list[str], i: int, rel: str) -> str | None:
    pats = _NAMES + ((_STEP_NAME,) if rel.startswith(".github/workflows/") else ())
    for line in lines[i + 1:i + 1 + _LOOKAHEAD]:
        for pat in pats:
            m = pat.match(line)
            if m:
                return m.group(m.lastindex)
        if not line.strip() or not _SKIPPABLE.match(line):
            return None
    return None


def collect_markers(root: Path, paths: list[str]) -> list[Marker]:
    out: list[Marker] = []
    for rel in paths:
        # Markers live in code, scripts and CI config; a Markdown file only
        # shows them as examples.
        if rel in SKIP_PATHS or rel.endswith(".md"):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        if not _NEAR_MARKER.search(text):
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not _NEAR_MARKER.search(line):
                continue
            m = _MARKER_LINE.match(line)
            ids = _IDS.match(m.group(1)) if m else None
            if not ids:
                raise AdrChecksError(
                    f"{rel}:{i + 1}: malformed marker (want a comment line "
                    f"`adr-check: ADR-NNN/S.C`): {line.strip()}")
            name = _name_after(lines, i, rel)
            for adr, section, clause in _ONE_ID.findall(ids.group(1)):
                out.append(Marker(adr, section, clause, rel, name))
    return out


def render(clauses: list[str], markers: list[Marker], lang: str) -> str:
    by_clause: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for m in markers:
        names = by_clause[m.clause][m.path]
        if m.name:
            names.add(m.name)
    colon, sep, open_, close, comma = (("：", "；", "（", "）", "、") if lang == "zh"
                                       else (": ", "; ", " (", ")", ", "))
    lines = []
    for c in clauses:
        files = by_clause.get(c)
        if not files:
            lines.append(f"- **{c}**{colon}{_GAP[lang]}")
            continue
        parts = []
        for path in sorted(files):
            names = sorted(files[path])
            parts.append(f"`{path}`" + (open_ + comma.join(f"`{n}`" for n in names) + close if names else ""))
        lines.append(f"- **{c}**{colon}" + sep.join(parts))
    return "\n".join(lines)


def _adr_files(root: Path) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = defaultdict(list)
    for p in sorted((root / ADR_DIR).glob("[0-9][0-9][0-9]-*.md")):
        out[p.name[:3]].append(p)
    return out


def run(root: Path, paths: list[str], write: bool) -> list[str]:
    """Regenerate every block; return the ADR files whose block differs (and,
    with write, was rewritten)."""
    markers = collect_markers(root, paths)
    adrs = _adr_files(root)
    marked = defaultdict(list)
    for m in markers:
        if m.adr not in adrs:
            raise AdrChecksError(f"{m.path}: marker names ADR-{m.adr}, which does not exist")
        marked[(m.adr, m.section)].append(m)

    changed: list[str] = []
    seen_blocks: set[tuple[str, str]] = set()
    declared: dict[tuple[str, str], tuple[str, list[str]]] = {}
    for adr, files in adrs.items():
        for path in files:
            text = path.read_text(encoding="utf-8")
            lang = "en" if path.name.endswith(".en.md") else "zh"
            rel = path.relative_to(root).as_posix()
            new = text
            for b in list(_BEGIN.finditer(text)):
                section = b.group(1)
                if (rel, section) in seen_blocks:
                    raise AdrChecksError(f"{rel}: block for section {section} appears twice")
                clauses = parse_clauses(text, section)
                first = declared.setdefault((adr, section), (rel, clauses))
                if first[1] != clauses:
                    raise AdrChecksError(
                        f"{rel} and {first[0]} declare different clauses for section {section}: "
                        f"{', '.join(clauses)} vs {', '.join(first[1])}")
                if not clauses:
                    raise AdrChecksError(f"{rel}: block for section {section}, which declares no clause")
                for m in marked.get((adr, section), []):
                    if m.clause not in clauses:
                        raise AdrChecksError(
                            f"{m.path}: marker names ADR-{adr}/{section}.{m.clause}; "
                            f"section {section} declares {', '.join(clauses)}")
                start = new.index(b.group(0)) + len(b.group(0))
                end = new.find(END, start)
                if end < 0:
                    raise AdrChecksError(f"{rel}: block for section {section} has no `{END}`")
                body = "\n" + render(clauses, marked.get((adr, section), []), lang) + "\n"
                new = new[:start] + body + new[end:]
                seen_blocks.add((rel, section))
            if new != text:
                changed.append(rel)
                if write:
                    path.write_text(new, encoding="utf-8", newline="\n")

    for (adr, section), ms in marked.items():
        for path in adrs[adr]:
            rel = path.relative_to(root).as_posix()
            if (rel, section) not in seen_blocks:
                raise AdrChecksError(
                    f"{rel}: markers name ADR-{adr} section {section} "
                    f"(e.g. {ms[0].path}) but the file has no `{begin_marker(section)}` block")
    return changed


def _tracked(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True, timeout=60)
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 if a block is out of date")
    mode.add_argument("--write", action="store_true", help="rewrite the blocks")
    args = ap.parse_args(argv)
    try:
        paths = _tracked(REPO_ROOT)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"gen_adr_checks: cannot list tracked files: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    try:
        changed = run(REPO_ROOT, paths, write=args.write)
    except AdrChecksError as e:
        print(f"❌ gen_adr_checks: {e}", file=sys.stderr)
        return EXIT_VIOLATION
    if args.write:
        for rel in changed:
            print(f"wrote {rel}")
        return EXIT_OK
    if changed:
        print("❌ ADR check lists are out of date (run "
              "`python3 scripts/tools/dx/gen_adr_checks.py --write`):", file=sys.stderr)
        for rel in changed:
            print(f"  {rel}", file=sys.stderr)
        return EXIT_VIOLATION
    print("OK every ADR check list matches the adr-check markers.")
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
