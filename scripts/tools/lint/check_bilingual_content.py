#!/usr/bin/env python3
"""check_bilingual_content.py — 雙語內容一致性 lint

掃描 docs/**/*.en.md 偵測 CJK 字元比例，確保英文文件不含過多中文內容。
同時掃描 docs/**/*.md (非 .en.md) 偵測是否有純英文而無中文的情況。

Usage:
    python3 scripts/tools/lint/check_bilingual_content.py
    python3 scripts/tools/lint/check_bilingual_content.py --ci
    python3 scripts/tools/lint/check_bilingual_content.py --json
    python3 scripts/tools/lint/check_bilingual_content.py --threshold 0.15

Exit codes:
    0 = all checks passed
    1 = errors found
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
DOCS_DIR = PROJECT_ROOT / "docs"

# CJK Unified Ideographs ranges
CJK_PATTERN = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
    r"\U00020000-\U0002a6df\U0002a700-\U0002b73f]"
)

# Default threshold: if >20% of non-whitespace chars are CJK in .en.md → warning
DEFAULT_CJK_THRESHOLD = 0.20
# If <5% CJK in a zh doc (non-.en.md) → warning (might be untranslated)
DEFAULT_ZH_MIN_THRESHOLD = 0.05


def count_cjk_ratio(text: str) -> float:
    """Count the ratio of CJK characters to total non-whitespace characters.

    Returns 0.0 if text is empty.
    """
    non_ws = re.sub(r"\s", "", text)
    if not non_ws:
        return 0.0
    cjk_count = len(CJK_PATTERN.findall(non_ws))
    return cjk_count / len(non_ws)


def scan_en_docs(
    docs_dir: Path,
    threshold: float = DEFAULT_CJK_THRESHOLD,
) -> list:
    """Scan .en.md files for excessive CJK content.

    Returns list of (severity, message, filepath, ratio) tuples.
    """
    findings = []
    for f in sorted(docs_dir.rglob("*.en.md")):
        text = f.read_text(encoding="utf-8")
        ratio = count_cjk_ratio(text)
        if ratio > threshold:
            rel = f.relative_to(PROJECT_ROOT)
            findings.append((
                "warning",
                f"{rel}: {ratio:.1%} CJK content in English doc "
                f"(threshold: {threshold:.0%})",
                str(rel),
                ratio,
            ))
    return findings


def scan_zh_docs(
    docs_dir: Path,
    min_threshold: float = DEFAULT_ZH_MIN_THRESHOLD,
) -> list:
    """Scan non-.en.md files for potentially untranslated content.

    Returns list of (severity, message, filepath, ratio) tuples.
    """
    findings = []
    for f in sorted(docs_dir.rglob("*.md")):
        if f.name.endswith(".en.md"):
            continue
        # Skip internal/generated files
        if "includes" in f.parts:
            continue
        text = f.read_text(encoding="utf-8")
        # Only check files with substantial content
        if len(text.strip()) < 200:
            continue
        ratio = count_cjk_ratio(text)
        if 0 < ratio < min_threshold:
            rel = f.relative_to(PROJECT_ROOT)
            findings.append((
                "info",
                f"{rel}: only {ratio:.1%} CJK content "
                f"(might need translation)",
                str(rel),
                ratio,
            ))
    return findings


def run_all_checks(
    docs_dir: Path = DOCS_DIR,
    threshold: float = DEFAULT_CJK_THRESHOLD,
    min_threshold: float = DEFAULT_ZH_MIN_THRESHOLD,
) -> list:
    """Run all bilingual content checks.

    Returns list of (severity, message, filepath, ratio) tuples.
    """
    findings = []
    findings.extend(scan_en_docs(docs_dir, threshold=threshold))
    findings.extend(scan_zh_docs(docs_dir, min_threshold=min_threshold))
    return findings


def format_text_report(findings: list) -> str:
    """Format human-readable text report."""
    lines = []
    lines.append("=" * 60)
    lines.append("Bilingual Content Check")
    lines.append("=" * 60)

    if not findings:
        lines.append("✓ All bilingual content checks passed.")
        return "\n".join(lines)

    warn_count = sum(1 for s, *_ in findings if s == "warning")
    info_count = sum(1 for s, *_ in findings if s == "info")

    for sev, msg, _path, _ratio in findings:
        sym = "⊘" if sev == "warning" else "ℹ"
        lines.append(f"  {sym} {msg}")

    lines.append("")
    lines.append(f"Result: {warn_count} warning(s), {info_count} info(s)")
    return "\n".join(lines)


def format_json_report(findings: list) -> str:
    """Format JSON report."""
    return json.dumps({
        "check": "bilingual_content",
        "findings": [
            {"severity": s, "message": m, "file": p, "cjk_ratio": r}
            for s, m, p, r in findings
        ],
        "warning_count": sum(1 for s, *_ in findings if s == "warning"),
        "info_count": sum(1 for s, *_ in findings if s == "info"),
        "status": "pass" if not any(s == "warning" for s, *_ in findings)
                  else "warn",
    }, indent=2, ensure_ascii=False)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Check bilingual content consistency in docs"
    )
    parser.add_argument("--ci", action="store_true",
                        help="CI mode: exit 1 on warnings")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--threshold", type=float,
                        default=DEFAULT_CJK_THRESHOLD,
                        help=f"CJK ratio threshold for .en.md files "
                             f"(default: {DEFAULT_CJK_THRESHOLD})")
    args = parser.parse_args()

    findings = run_all_checks(docs_dir=DOCS_DIR, threshold=args.threshold)

    if args.json:
        print(format_json_report(findings))
    else:
        print(format_text_report(findings))

    has_warnings = any(s == "warning" for s, *_ in findings)
    if args.ci and has_warnings:
        sys.exit(1)


if __name__ == "__main__":
    main()
