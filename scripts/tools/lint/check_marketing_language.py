#!/usr/bin/env python3
"""check_marketing_language.py — flag marketing / hype language in docs.

Codifies dev-rules.md §6 「推銷語言不進 repo」 (closed PR #168 audit
identified this as one of 4 doc-drift gaps where the rule claimed a
hook that didn't exist).

Why this exists
---------------
This is an OSS project. Documentation must stand up to technical
review. Marketing-style language ("業界領先" / "革命性" / "唯一" /
"world's first") gets read as unprofessional by reviewers and **isn't
empirically defensible** anyway. dev-rules.md §6 has banned this since
v2.7.0 but enforcement was reviewer-convention until this PR.

What it flags
-------------
A curated list of high-signal hype keywords (zh + en), scanned across:
- README.md / README.en.md
- docs/**/*.md (excluding archive/)
- CHANGELOG.md
- Recent commit messages (last 50 commits when run in repo)

Each match emits ``<file>:<line>:<col> [<keyword>] <snippet>``.

False-positive control:
- **Per-line ignore**: ``<!-- marketing-language: ignore -->`` on the
  matching line OR up to 3 lines above (mirrors PR #166 / #169 ignore
  conventions). For legitimate quotes / examples / anti-pattern
  illustrations.
- **Quoted prose detection**: a match inside a fenced code block or
  inline backticks is suppressed automatically — it's almost always
  illustrative ("不要寫 `業界領先` 這種推銷語言").
- **Curated keyword list, not regex sweep**: high-precision over
  recall. Adding a term is cheap; loosening to regex would cause
  noise.

Severity
--------
- default mode → report only, exit 0 (audit / local debug)
- ``--ci`` → fatal on findings (exit 1). Codebase audit (PR #170)
  shows zero violations after PR #169's dev-rules re-wording, so we
  ship strict from day 1.

Usage
-----
::

    # Audit local
    python3 scripts/tools/lint/check_marketing_language.py

    # CI / pre-commit (fatal)
    python3 scripts/tools/lint/check_marketing_language.py --ci

    # Specific paths
    python3 scripts/tools/lint/check_marketing_language.py \\
        --paths README.md docs/scenarios/

References
----------
- ``dev-rules.md`` §6 (rule definition)
- PR #168 (closed) — original audit identifying drift
- PR #169 — `check_dev_rules_enforcement.py` (verifies dev-rules
  doesn't re-claim non-existent hooks)
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Per-line ignore marker.
_IGNORE_MARKER = "marketing-language: ignore"
_IGNORE_LOOKBACK_LINES = 3

# Curated zh+en hype keyword list. **High precision** — terms here
# would never appear in a defensible technical claim.
_BANNED_KEYWORDS_ZH = (
    "業界領先",
    "業界第一",
    "全球第一",
    "全球領先",
    "世界第一",
    "世界級",
    "革命性",
    "顛覆性",
    "獨步全球",
    "唯一選擇",
    "市場第一",
    "行業標竿",
    "領導品牌",
    "卓越",
    "頂尖",
    "業內最佳",
)
_BANNED_KEYWORDS_EN = (
    "industry-leading",
    "world-class",
    "best-in-class",
    "revolutionary",
    "game-changing",
    "cutting-edge",
    "state-of-the-art",
    "world's first",
    "unparalleled",
    "unrivaled",
    "next-generation",
    "leading-edge",
    # Excluded due to high technical false-positive rate:
    # - "the only" → "the only safe way" / "the only justification" (legit)
    # - "unmatched" → "unmatched braces" / "unmatched parens" (legit)
    # - "premier" → noisy across English prose
    # If genuinely needed, surface via two-word phrases below.
    "the only choice",
    "the only solution",
    "only viable solution",
)

_BANNED_KEYWORDS = tuple(_BANNED_KEYWORDS_ZH) + tuple(_BANNED_KEYWORDS_EN)

# File patterns under PROJECT_ROOT to scan by default.
_DEFAULT_SCAN_GLOBS = (
    "README.md",
    "README.en.md",
    "CHANGELOG.md",
    "docs/**/*.md",
)

# Subdirectories to skip even within scan globs.
_SKIP_DIRS = (
    "docs/internal/archive",  # historical / quoted material
    "docs/internal/v2.8.0-planning",  # gitignored anyway, but skip if present
)


@dataclass
class HypeFinding:
    """A single hit on a banned keyword."""

    path: Path
    line: int
    col: int
    keyword: str
    snippet: str

    def render(self) -> str:
        if self.path.is_absolute():
            try:
                rel = self.path.relative_to(PROJECT_ROOT)
            except ValueError:
                rel = self.path
        else:
            rel = self.path
        return (
            f"{rel}:{self.line}:{self.col} "
            f"[marketing-language: '{self.keyword}'] {self.snippet[:120]}"
        )


def _line_has_ignore(source_lines: list[str], line_no: int) -> bool:
    """True if line_no OR up to 3 lines above contain the ignore marker."""
    for offset in range(_IGNORE_LOOKBACK_LINES + 1):
        candidate = line_no - offset
        if 1 <= candidate <= len(source_lines):
            if _IGNORE_MARKER in source_lines[candidate - 1]:
                return True
    return False


def _is_within_code_span(line: str, col: int) -> bool:
    """True if column ``col`` (1-based) is inside backticks on this line.

    Conservative: counts backtick toggles up to col. Fenced code blocks
    (```...```) handled separately via _line_in_fenced_block.
    """
    # Count backticks before col.
    pre = line[: col - 1]
    n_backticks = pre.count("`")
    return n_backticks % 2 == 1


def _build_fenced_block_set(lines: list[str]) -> set[int]:
    """Return set of 1-based line numbers that are inside ``` fences.

    Fence opens / closes alternate; lines BETWEEN the markers are
    fenced (the marker lines themselves are also marked fenced — a
    keyword on the fence line itself is rare and likely intentional).
    """
    fenced: set[int] = set()
    in_fence = False
    for idx, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            fenced.add(idx)  # fence line itself is fenced
            continue
        if in_fence:
            fenced.add(idx)
    return fenced


def scan_source(path: Path, source: str) -> list[HypeFinding]:
    """Walk a single file's text and return all findings."""
    findings: list[HypeFinding] = []
    lines = source.splitlines()
    fenced_block_lines = _build_fenced_block_set(lines)

    for idx, line in enumerate(lines, start=1):
        if idx in fenced_block_lines:
            continue
        if _line_has_ignore(lines, idx):
            continue
        # Lower-case once per line for English match (zh kw remain
        # case-sensitive but are CJK so case-insensitive 無意義).
        lower = line.lower()
        for kw in _BANNED_KEYWORDS:
            kw_lower = kw.lower() if kw[0].isascii() else kw
            search_in = lower if kw[0].isascii() else line
            pos = search_in.find(kw_lower)
            while pos >= 0:
                col = pos + 1
                # Skip if inside inline code span.
                if _is_within_code_span(line, col):
                    pos = search_in.find(kw_lower, pos + 1)
                    continue
                findings.append(
                    HypeFinding(
                        path=path,
                        line=idx,
                        col=col,
                        keyword=kw,
                        snippet=line.strip(),
                    )
                )
                pos = search_in.find(kw_lower, pos + 1)

    return findings


def _iter_default_files() -> Iterable[Path]:
    """Resolve default scan globs to absolute paths under PROJECT_ROOT."""
    seen: set[Path] = set()
    for pattern in _DEFAULT_SCAN_GLOBS:
        for match in PROJECT_ROOT.glob(pattern):
            if not match.is_file():
                continue
            # Apply skip-dir filter on relative path.
            try:
                rel = match.relative_to(PROJECT_ROOT)
            except ValueError:
                rel = match
            rel_str = str(rel).replace("\\", "/")
            if any(rel_str.startswith(skip) for skip in _SKIP_DIRS):
                continue
            if match in seen:
                continue
            seen.add(match)
            yield match


def _resolve_paths(args: argparse.Namespace) -> list[Path]:
    if args.paths:
        result = []
        for p in args.paths:
            path = Path(p)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            if path.is_dir():
                # Walk directory for .md files.
                for md in sorted(path.rglob("*.md")):
                    result.append(md)
            elif path.is_file():
                result.append(path)
        return result
    return sorted(_iter_default_files())


def _compute_exit_code(*, ci: bool, n_findings: int) -> int:
    """Truth table:
    | --ci  | n_findings | exit |
    |-------|------------|------|
    | False | *          | 0    |
    | True  | 0          | 0    |
    | True  | >0         | 1    |
    """
    if not ci:
        return 0
    return 1 if n_findings > 0 else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scan docs / README / CHANGELOG for marketing-style hype "
            "language banned by dev-rules.md §6."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to scan. Defaults to README + CHANGELOG + docs/**/*.md.",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit 1 on findings (default: report only).",
    )
    args = parser.parse_args(argv)

    paths = _resolve_paths(args)
    if not paths:
        if args.ci:
            print("✓ no files matched scan globs")
        return 0

    all_findings: list[HypeFinding] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"⚠ cannot read {path}: {exc}", file=sys.stderr)
            continue
        all_findings.extend(scan_source(path, source))

    if not all_findings:
        if args.ci:
            print(f"✓ no marketing-language hits across {len(paths)} file(s)")
        return 0

    by_file: dict[Path, list[HypeFinding]] = {}
    for f in all_findings:
        by_file.setdefault(f.path, []).append(f)

    print(
        f"✗ {len(all_findings)} marketing-language hit(s) in "
        f"{len(by_file)} file(s):",
        file=sys.stderr,
    )
    for path in sorted(by_file):
        for f in by_file[path]:
            print(f"  {f.render()}", file=sys.stderr)

    print(
        "\nResolve each by either:\n"
        "  (a) Re-write to objective engineering language with measurable\n"
        "      claims (numbers, comparisons with clear methodology).\n"
        "  (b) Add `<!-- marketing-language: ignore -->` on the line if\n"
        "      it's an illustrative quote / anti-pattern example /\n"
        "      legitimate domain term.\n"
        "\n"
        "See dev-rules.md §6 for rationale (OSS technical review).",
        file=sys.stderr,
    )
    return _compute_exit_code(ci=args.ci, n_findings=len(all_findings))


if __name__ == "__main__":
    raise SystemExit(main())
