#!/usr/bin/env python3
"""coverage_delta.py — Per-file + total coverage delta between two runs.

Gap 5 (testing-quality memory roadmap) — first PR. Builds the
foundation for both per-PR delta gating ("coverage went down by N%
in this file") and weekly trend aggregation. Both consume this
script's structured output.

What this is NOT
----------------

This is the COMPARISON layer, not the storage layer. CI already
uploads `coverage.xml` as an artifact (see `.github/workflows/ci.yml`,
`coverage-py${{ matrix.python }}` step). A future PR will wire this
script into a workflow step that:

  1. Downloads the previous run's `coverage.xml` (e.g., from
     main-branch latest, or the user's PR base SHA), and
  2. Runs this script to compute the delta, posting a comment.

Output (text mode)
------------------

  Coverage delta: 84.5% → 86.2% (+1.7%)

  Improved (3 files):
    + scripts/tools/dx/foo.py        72.0% → 88.0% (+16.0%)
    + scripts/tools/lint/bar.py      45.0% →  60.0% (+15.0%)
    + ...

  Regressed (1 file):
    - scripts/tools/ops/qux.py       95.0% →  82.0% (-13.0%)

  Newly tracked (2 files):
    * scripts/tools/dx/new_a.py      80.0%
    * ...

  Removed (1 file):
    ! scripts/tools/dx/gone.py       (was 100.0%)

Output (--json mode)
--------------------

Emits a structured payload suitable for a PR-comment bot:

  {
    "total": {"before": 84.5, "after": 86.2, "delta": 1.7},
    "improved": [{"file": "...", "before": 72.0, "after": 88.0,
                   "delta": 16.0}, ...],
    "regressed": [...],
    "added": [...],
    "removed": [...],
    "unchanged_count": 47
  }

Usage
-----

  # Compare two coverage.xml files
  python3 scripts/tools/dx/coverage_delta.py BEFORE.xml AFTER.xml

  # JSON output for downstream consumers
  python3 scripts/tools/dx/coverage_delta.py BEFORE.xml AFTER.xml --json

  # Fail on regression > N% (for CI gating)
  python3 scripts/tools/dx/coverage_delta.py BEFORE.xml AFTER.xml \\
      --max-total-regression 1.0 --max-file-regression 5.0

Exit codes
----------

  0  no regression, or regression within thresholds
  1  regression beyond a configured threshold
  2  configuration error (file missing, malformed XML, etc.)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402


@dataclass(frozen=True)
class FileCov:
    """Per-file coverage extracted from a Cobertura XML report."""
    filename: str
    line_rate: float  # 0.0 - 1.0
    lines_covered: int
    lines_valid: int

    @property
    def percent(self) -> float:
        """Coverage as a 0-100 percentage."""
        return round(self.line_rate * 100, 1)


@dataclass(frozen=True)
class CoverageReport:
    """Parsed Cobertura XML report — total + per-file breakdown."""
    total_line_rate: float  # 0.0 - 1.0
    total_lines_covered: int
    total_lines_valid: int
    files: dict  # filename → FileCov

    @property
    def total_percent(self) -> float:
        return round(self.total_line_rate * 100, 1)


def parse_cobertura(path: Path) -> CoverageReport:
    """Parse a Cobertura XML coverage report.

    Cobertura's structure (relevant subset):

      <coverage line-rate="0.85" lines-covered="850" lines-valid="1000">
        <packages>
          <package name="...">
            <classes>
              <class filename="..." line-rate="0.9"
                     lines-covered="..." lines-valid="...">
                <lines>...</lines>
              </class>
            </classes>
          </package>
        </packages>
      </coverage>

    pytest-cov omits `lines-covered` / `lines-valid` from `<class>` —
    we synthesize them from the `<line>` children when missing.
    """
    if not path.is_file():
        raise FileNotFoundError(f"coverage report not found: {path}")
    try:
        tree = ET.parse(path)  # nosec B314  #input is pytest-cov-generated Cobertura XML on local FS, not external/untrusted (defusedxml dep avoided)
    except ET.ParseError as e:
        raise ValueError(f"malformed Cobertura XML at {path}: {e}") from e
    root = tree.getroot()
    if root.tag != "coverage":
        raise ValueError(
            f"unexpected root element {root.tag!r} (expected 'coverage') in {path}"
        )

    files: dict = {}
    for cls in root.iter("class"):
        filename = cls.attrib.get("filename")
        if not filename:
            continue
        line_rate = float(cls.attrib.get("line-rate", 0.0))
        # pytest-cov omits these on <class>; recompute from <line> hits.
        lines = list(cls.iter("line"))
        lines_valid = int(cls.attrib.get("lines-valid", len(lines)))
        if "lines-covered" in cls.attrib:
            lines_covered = int(cls.attrib["lines-covered"])
        else:
            lines_covered = sum(
                1 for ln in lines if int(ln.attrib.get("hits", 0)) > 0
            )
        # If a file appears in multiple <package> sections, take the
        # union of covered lines (max per file). Cobertura producers
        # don't typically duplicate, but be defensive.
        existing = files.get(filename)
        if existing is None or lines_valid > existing.lines_valid:
            files[filename] = FileCov(
                filename=filename,
                line_rate=line_rate,
                lines_covered=lines_covered,
                lines_valid=lines_valid,
            )

    total_line_rate = float(root.attrib.get("line-rate", 0.0))
    total_lines_covered = int(root.attrib.get("lines-covered", 0))
    total_lines_valid = int(root.attrib.get("lines-valid", 0))

    return CoverageReport(
        total_line_rate=total_line_rate,
        total_lines_covered=total_lines_covered,
        total_lines_valid=total_lines_valid,
        files=files,
    )


@dataclass
class FileDelta:
    """Per-file delta between before / after."""
    filename: str
    before: float  # percentage
    after: float
    delta: float


@dataclass
class DeltaReport:
    """Full delta result — total + four buckets of file changes."""
    total_before: float
    total_after: float
    total_delta: float
    improved: list  # list[FileDelta]
    regressed: list  # list[FileDelta]
    added: list  # list[FileDelta] (after-only; before=0)
    removed: list  # list[FileDelta] (before-only; after=0)
    unchanged_count: int

    def to_dict(self) -> dict:
        def _serialize(items):
            return [asdict(d) for d in items]

        return {
            "total": {
                "before": self.total_before,
                "after": self.total_after,
                "delta": round(self.total_delta, 2),
            },
            "improved": _serialize(self.improved),
            "regressed": _serialize(self.regressed),
            "added": _serialize(self.added),
            "removed": _serialize(self.removed),
            "unchanged_count": self.unchanged_count,
        }


def compute_delta(before: CoverageReport, after: CoverageReport) -> DeltaReport:
    """Compute per-file + total coverage delta."""
    improved: list = []
    regressed: list = []
    added: list = []
    removed: list = []
    unchanged = 0

    before_files = before.files
    after_files = after.files
    all_filenames = set(before_files) | set(after_files)

    for fn in sorted(all_filenames):
        b = before_files.get(fn)
        a = after_files.get(fn)
        if a is None:  # file was removed (or excluded) in after
            removed.append(FileDelta(
                filename=fn, before=b.percent, after=0.0, delta=-b.percent,
            ))
            continue
        if b is None:  # file is new in after
            added.append(FileDelta(
                filename=fn, before=0.0, after=a.percent, delta=a.percent,
            ))
            continue
        diff = round(a.percent - b.percent, 1)
        if diff > 0:
            improved.append(FileDelta(fn, b.percent, a.percent, diff))
        elif diff < 0:
            regressed.append(FileDelta(fn, b.percent, a.percent, diff))
        else:
            unchanged += 1

    total_delta = round(after.total_percent - before.total_percent, 2)
    return DeltaReport(
        total_before=before.total_percent,
        total_after=after.total_percent,
        total_delta=total_delta,
        improved=improved,
        regressed=regressed,
        added=added,
        removed=removed,
        unchanged_count=unchanged,
    )


def format_text_report(report: DeltaReport, top_n: int = 10) -> str:
    """Format DeltaReport as a human-readable text block.

    Limits each non-empty bucket to the top *top_n* entries by absolute
    delta (so a CI comment doesn't get hundreds of lines).
    """
    lines: list = []
    sign = "+" if report.total_delta >= 0 else ""
    lines.append(
        f"Coverage delta: {report.total_before:.1f}% → "
        f"{report.total_after:.1f}% ({sign}{report.total_delta:.2f}%)"
    )
    lines.append("")

    def _section(title: str, items, marker: str, sort_key=None):
        if not items:
            return
        if sort_key is None:
            sort_key = lambda d: -abs(d.delta)
        sorted_items = sorted(items, key=sort_key)[:top_n]
        suffix = (
            f" (showing top {top_n} of {len(items)})"
            if len(items) > top_n else f" ({len(items)} files)"
        )
        lines.append(f"{title}{suffix}:")
        for d in sorted_items:
            sign_d = "+" if d.delta >= 0 else ""
            lines.append(
                f"  {marker} {d.filename}  "
                f"{d.before:5.1f}% → {d.after:5.1f}% "
                f"({sign_d}{d.delta:.1f}%)"
            )
        lines.append("")

    _section("Improved", report.improved, "+")
    _section("Regressed", report.regressed, "-")
    _section("Newly tracked", report.added, "*")
    _section("Removed", report.removed, "!")

    if report.unchanged_count:
        lines.append(f"Unchanged: {report.unchanged_count} files")

    return "\n".join(lines).rstrip() + "\n"


def format_markdown_report(report: DeltaReport, top_n: int = 10) -> str:
    """Format DeltaReport as a Markdown block suitable for a PR comment.

    Distinct from format_text_report: uses Markdown tables + headings
    instead of plain indented lines, and includes a stable
    `<!-- coverage-delta-bot -->` HTML comment marker so the workflow
    can find-and-update an existing comment instead of accumulating
    one comment per push.
    """
    sign = "+" if report.total_delta >= 0 else ""
    direction = "📈" if report.total_delta > 0 else (
        "📉" if report.total_delta < 0 else "➡️"
    )

    lines: list = [
        "<!-- coverage-delta-bot -->",
        "## Coverage delta",
        "",
        f"{direction} **{report.total_before:.1f}%** → "
        f"**{report.total_after:.1f}%** ({sign}{report.total_delta:.2f}%)",
        "",
    ]

    def _table(title: str, items, marker_emoji: str):
        if not items:
            return
        sorted_items = sorted(items, key=lambda d: -abs(d.delta))[:top_n]
        suffix = (
            f" (top {top_n} of {len(items)})"
            if len(items) > top_n else f" ({len(items)})"
        )
        lines.append(f"### {marker_emoji} {title}{suffix}")
        lines.append("")
        lines.append("| File | Before | After | Δ |")
        lines.append("|---|---:|---:|---:|")
        for d in sorted_items:
            sd = "+" if d.delta >= 0 else ""
            lines.append(
                f"| `{d.filename}` | {d.before:.1f}% | "
                f"{d.after:.1f}% | {sd}{d.delta:.1f}% |"
            )
        lines.append("")

    _table("Improved", report.improved, "✅")
    _table("Regressed", report.regressed, "⚠️")
    _table("Newly tracked", report.added, "🆕")
    _table("Removed", report.removed, "🗑")

    if report.unchanged_count:
        lines.append(f"_Unchanged: {report.unchanged_count} files._")
        lines.append("")

    if not (report.improved or report.regressed
            or report.added or report.removed):
        lines.append("_No per-file coverage changes detected._")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# Stable marker the PR-comment workflow uses to find-and-update an
# existing comment rather than accumulate one per push. Keep in sync
# with the literal in format_markdown_report.
PR_COMMENT_MARKER = "<!-- coverage-delta-bot -->"


def evaluate_thresholds(
    report: DeltaReport,
    *,
    max_total_regression: Optional[float] = None,
    max_file_regression: Optional[float] = None,
) -> list:
    """Evaluate threshold violations against the delta report.

    Args:
        max_total_regression: Fail if total coverage drops by more than
            this percentage (e.g. ``1.0`` blocks a -1.5% drop).
        max_file_regression: Fail if any single file drops by more than
            this percentage.

    Returns:
        List of human-readable violation strings (empty = OK).
    """
    violations: list = []
    if max_total_regression is not None and report.total_delta < -max_total_regression:
        violations.append(
            f"total coverage dropped by {-report.total_delta:.2f}% "
            f"(threshold: {max_total_regression:.2f}%)"
        )
    if max_file_regression is not None:
        for d in report.regressed:
            if -d.delta > max_file_regression:
                violations.append(
                    f"{d.filename} dropped by {-d.delta:.1f}% "
                    f"(threshold: {max_file_regression:.1f}%)"
                )
    return violations


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compute per-file + total coverage delta between two "
            "Cobertura XML reports."
        ),
    )
    parser.add_argument("before", help="Path to BEFORE coverage.xml")
    parser.add_argument("after", help="Path to AFTER coverage.xml")
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of human-readable text",
    )
    parser.add_argument(
        "--markdown", action="store_true",
        help="Emit Markdown PR-comment-ready output instead of text",
    )
    parser.add_argument(
        "--top", type=int, default=10,
        help="Top N files per bucket to show in text/markdown mode (default: 10)",
    )
    parser.add_argument(
        "--max-total-regression", type=float, default=None,
        help="Fail if total coverage drops by more than N%% (default: no gate)",
    )
    parser.add_argument(
        "--max-file-regression", type=float, default=None,
        help="Fail if any single file drops by more than N%% (default: no gate)",
    )
    args = parser.parse_args(argv)

    try:
        before = parse_cobertura(Path(args.before))
        after = parse_cobertura(Path(args.after))
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    report = compute_delta(before, after)

    if args.json and args.markdown:
        print("ERROR: --json and --markdown are mutually exclusive",
              file=sys.stderr)
        return EXIT_CALLER_ERROR

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif args.markdown:
        print(format_markdown_report(report, top_n=args.top))
    else:
        print(format_text_report(report, top_n=args.top))

    violations = evaluate_thresholds(
        report,
        max_total_regression=args.max_total_regression,
        max_file_regression=args.max_file_regression,
    )
    if violations:
        print("Threshold violations:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
