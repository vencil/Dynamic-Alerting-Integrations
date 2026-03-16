#!/usr/bin/env python3
"""coverage_gap_analysis.py — Per-file coverage ranking report

Parses pytest-cov output (or .coverage database) to produce a ranked report
of per-file coverage percentages. Highlights modules below the target threshold
and suggests which files to prioritize for coverage improvements.

Usage:
  python3 scripts/tools/dx/coverage_gap_analysis.py
  python3 scripts/tools/dx/coverage_gap_analysis.py --target 70
  python3 scripts/tools/dx/coverage_gap_analysis.py --json
  python3 scripts/tools/dx/coverage_gap_analysis.py --ci --target 70
  python3 scripts/tools/dx/coverage_gap_analysis.py --coverage-file .coverage
"""
import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

DEFAULT_TARGET = 70
DEFAULT_SOURCE_DIRS = ["scripts/tools/ops", "scripts/tools/dx", "scripts/tools/lint"]


@dataclass
class FileCoverage:
    """Coverage data for a single source file."""
    file_path: str
    statements: int = 0
    missed: int = 0
    coverage_pct: float = 0.0
    missing_lines: str = ""

    def to_dict(self) -> Dict:
        return {
            "file": self.file_path,
            "statements": self.statements,
            "missed": self.missed,
            "coverage_pct": round(self.coverage_pct, 1),
            "missing_lines": self.missing_lines,
        }


@dataclass
class CoverageReport:
    """Aggregated coverage analysis report."""
    files: List[FileCoverage] = field(default_factory=list)
    target_pct: float = DEFAULT_TARGET
    total_statements: int = 0
    total_missed: int = 0
    overall_pct: float = 0.0

    @property
    def below_target_count(self) -> int:
        return sum(1 for f in self.files if f.coverage_pct < self.target_pct)

    @property
    def at_target_count(self) -> int:
        return sum(1 for f in self.files if f.coverage_pct >= self.target_pct)


def parse_coverage_output(text: str) -> List[FileCoverage]:
    """Parse pytest-cov text output into FileCoverage objects.

    Expected format (from pytest --cov --cov-report=term-missing):
    Name                  Stmts   Miss  Cover   Missing
    ---------------------------------------------------
    scripts/tools/ops/x    100     30    70%   12-15, 20
    """
    results = []
    # Match lines like: path/file.py  100  30  70%  12-15, 20
    pattern = re.compile(
        r"^(\S+\.py)\s+(\d+)\s+(\d+)\s+(\d+)%\s*(.*)?$"
    )

    for line in text.splitlines():
        line = line.strip()
        m = pattern.match(line)
        if m:
            file_path = m.group(1)
            stmts = int(m.group(2))
            missed = int(m.group(3))
            pct = float(m.group(4))
            missing = m.group(5).strip() if m.group(5) else ""
            results.append(FileCoverage(
                file_path=file_path,
                statements=stmts,
                missed=missed,
                coverage_pct=pct,
                missing_lines=missing,
            ))

    return results


def run_coverage(source_dirs: List[str], repo_root: Path) -> str:
    """Run pytest-cov and return the text output."""
    cov_sources = ",".join(source_dirs)
    cmd = [
        sys.executable, "-m", "pytest",
        f"--cov={cov_sources}",
        "--cov-report=term-missing",
        "--no-header", "-q",
        "tests/",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=300,
        )
        return result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return "ERROR: Coverage run timed out after 300s"
    except FileNotFoundError:
        return "ERROR: pytest not found"


def parse_coverage_file(coverage_path: Path, repo_root: Path) -> str:
    """Use coverage CLI to produce a text report from a .coverage file."""
    cmd = [
        sys.executable, "-m", "coverage", "report",
        "--show-missing",
        f"--data-file={coverage_path}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=60,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def build_report(files: List[FileCoverage], target: float) -> CoverageReport:
    """Build an aggregated coverage report."""
    report = CoverageReport(files=files, target_pct=target)
    report.total_statements = sum(f.statements for f in files)
    report.total_missed = sum(f.missed for f in files)
    if report.total_statements > 0:
        report.overall_pct = round(
            (1 - report.total_missed / report.total_statements) * 100, 1
        )
    return report


def format_text_report(report: CoverageReport) -> str:
    """Format a human-readable ranked coverage report."""
    lines = []
    lines.append(f"Coverage Gap Analysis (target: {report.target_pct}%)")
    lines.append(f"Overall: {report.overall_pct}% "
                 f"({report.total_statements} stmts, {report.total_missed} missed)")
    lines.append(f"Files: {len(report.files)} total, "
                 f"{report.at_target_count} at target, "
                 f"{report.below_target_count} below target")
    lines.append("")

    # Sort by coverage ascending (worst first)
    sorted_files = sorted(report.files, key=lambda f: f.coverage_pct)

    if report.below_target_count > 0:
        lines.append(f"Below {report.target_pct}% (prioritize these):")
        lines.append(f"  {'File':<50} {'Stmts':>6} {'Miss':>6} {'Cover':>6}")
        lines.append(f"  {'-'*50} {'-'*6} {'-'*6} {'-'*6}")
        for f in sorted_files:
            if f.coverage_pct >= report.target_pct:
                break
            icon = "🔴" if f.coverage_pct < 50 else "🟡"
            lines.append(
                f"  {icon} {f.file_path:<48} {f.statements:>6} "
                f"{f.missed:>6} {f.coverage_pct:>5.1f}%"
            )
        lines.append("")

    if report.at_target_count > 0:
        lines.append(f"At or above {report.target_pct}%:")
        at_target = [f for f in sorted_files if f.coverage_pct >= report.target_pct]
        for f in reversed(at_target):  # Best first
            lines.append(
                f"  🟢 {f.file_path:<48} {f.statements:>6} "
                f"{f.missed:>6} {f.coverage_pct:>5.1f}%"
            )

    return "\n".join(lines)


def format_json_report(report: CoverageReport) -> str:
    """Format a JSON coverage report."""
    sorted_files = sorted(report.files, key=lambda f: f.coverage_pct)
    result = {
        "target_pct": report.target_pct,
        "overall_pct": report.overall_pct,
        "total_statements": report.total_statements,
        "total_missed": report.total_missed,
        "files_total": len(report.files),
        "files_at_target": report.at_target_count,
        "files_below_target": report.below_target_count,
        "files": [f.to_dict() for f in sorted_files],
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Per-file coverage ranking report with gap analysis",
    )
    parser.add_argument("--target", type=float, default=DEFAULT_TARGET,
                        help=f"Target coverage percentage (default: {DEFAULT_TARGET})")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON report")
    parser.add_argument("--ci", action="store_true",
                        help="Exit 1 if any file is below target")
    parser.add_argument("--coverage-file", type=str, default=None,
                        help="Path to existing .coverage file (skip pytest run)")
    parser.add_argument("--coverage-text", type=str, default=None,
                        help="Path to saved coverage text output (skip pytest run)")
    parser.add_argument("--source-dirs", type=str, default=None,
                        help="Comma-separated source directories to cover")
    args = parser.parse_args(argv)

    source_dirs = (
        args.source_dirs.split(",") if args.source_dirs
        else DEFAULT_SOURCE_DIRS
    )

    # Get coverage data
    if args.coverage_text:
        text = Path(args.coverage_text).read_text(encoding="utf-8")
    elif args.coverage_file:
        text = parse_coverage_file(Path(args.coverage_file), REPO_ROOT)
    else:
        text = run_coverage(source_dirs, REPO_ROOT)

    if text.startswith("ERROR:"):
        print(text, file=sys.stderr)
        sys.exit(1)

    files = parse_coverage_output(text)
    if not files:
        print("No coverage data found. Ensure pytest-cov is installed "
              "and tests are discoverable.", file=sys.stderr)
        sys.exit(1)

    report = build_report(files, args.target)

    if args.json:
        print(format_json_report(report))
    else:
        print(format_text_report(report))

    if args.ci and report.below_target_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
