#!/usr/bin/env python3
"""check_bilingual_content.py — 雙語內容一致性 lint

掃描英文文件偵測 CJK 字元比例，確保英文文件不含過多中文內容。
同時掃描中文文件偵測是否有純英文而無中文的情況。

支援兩種命名慣例（dual-mode；v2.8.0 S#101 policy lock：ZH primary）：
- ZH-primary (active): *.en.md = English, *.md (no suffix) = Chinese
- EN-primary (dormant future-option): *.md (no suffix) = English, *.zh.md = Chinese
S#101 policy lock：repo 現完全運作於 ZH-primary 模式；EN-primary 模式保留以
支援未來若 trigger 條件達成時的遷移（見 dev-rules.md §9b）。

Usage:
    python3 scripts/tools/lint/check_bilingual_content.py
    python3 scripts/tools/lint/check_bilingual_content.py --ci
    python3 scripts/tools/lint/check_bilingual_content.py --json
    python3 scripts/tools/lint/check_bilingual_content.py --threshold 0.15

Exit codes:
    0 = all checks passed, or nothing was compared (no document classified
        as a zh/en pair — the report says which)
    1 = errors found
    2 = caller error (docs dir is not a directory, or holds no markdown —
        nothing was measured)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

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


def _is_english_doc(filepath: Path) -> bool:
    """Determine if a file is an English document.

    Supports both legacy (.en.md) and new (bare .md with .zh.md sibling) patterns.
    """
    if filepath.name.endswith(".en.md"):
        return True
    # New pattern: bare .md file that has a .zh.md sibling
    if not filepath.name.endswith(".zh.md"):
        zh_sibling = filepath.parent / (filepath.stem + ".zh.md")
        if zh_sibling.is_file():
            return True
    return False


def _is_chinese_doc(filepath: Path) -> bool:
    """Determine if a file is a Chinese document.

    Supports both legacy (bare .md with .en.md sibling) and new (.zh.md) patterns.
    """
    if filepath.name.endswith(".zh.md"):
        return True
    # Legacy pattern: bare .md file that has an .en.md sibling
    if not filepath.name.endswith(".en.md"):
        en_sibling = filepath.parent / (filepath.stem + ".en.md")
        if en_sibling.is_file():
            return True
    return False


def english_docs(docs_dir: Path) -> list:
    """The English documents scan_en_docs compares, in scan order."""
    return [f for f in sorted(docs_dir.rglob("*.md")) if _is_english_doc(f)]


def chinese_docs(docs_dir: Path) -> list:
    """The Chinese documents scan_zh_docs compares, in scan order.

    Skips internal/generated files. The skip-set is judged on the segments
    below docs_dir, not on the absolute path (#1810).
    """
    return [f for f in sorted(docs_dir.rglob("*.md"))
            if _is_chinese_doc(f)
            and "includes" not in f.relative_to(docs_dir).parts]


def scan_en_docs(
    docs_dir: Path,
    threshold: float = DEFAULT_CJK_THRESHOLD,
    *,
    measured: set | None = None,
    unreadable: set | None = None,
) -> list:
    """Scan English docs for excessive CJK content.

    Detects both legacy (.en.md) and new pattern (bare .md with .zh.md sibling).
    Returns list of (severity, message, filepath, ratio) tuples.
    ``measured`` / ``unreadable`` collect the files that were read /
    could not be read (#1810): a selected file that fails to read is not
    compared, and the report must not count it as if it were.
    """
    findings = []
    for f in english_docs(docs_dir):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            if unreadable is not None:
                unreadable.add(f)
            continue
        if measured is not None:
            measured.add(f)
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
    *,
    measured: set | None = None,
    unreadable: set | None = None,
) -> list:
    """Scan Chinese docs for potentially untranslated content.

    Detects both legacy (bare .md with .en.md sibling) and new (.zh.md) patterns.
    Returns list of (severity, message, filepath, ratio) tuples.
    ``measured`` / ``unreadable`` as in :func:`scan_en_docs`.
    """
    findings = []
    for f in chinese_docs(docs_dir):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            if unreadable is not None:
                unreadable.add(f)
            continue
        if measured is not None:
            measured.add(f)
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
    *,
    measured: dict | None = None,
    unreadable: set | None = None,
) -> list:
    """Run all bilingual content checks.

    Returns list of (severity, message, filepath, ratio) tuples. When
    ``measured`` is given it is filled with ``{"english": set, "chinese":
    set}`` of the files each scan actually read (#1810).
    """
    en_seen: set = set()
    zh_seen: set = set()
    findings = []
    findings.extend(scan_en_docs(docs_dir, threshold=threshold,
                                 measured=en_seen, unreadable=unreadable))
    findings.extend(scan_zh_docs(docs_dir, min_threshold=min_threshold,
                                 measured=zh_seen, unreadable=unreadable))
    if measured is not None:
        measured["english"] = en_seen
        measured["chinese"] = zh_seen
    return findings


def format_text_report(findings: list, *, compared: tuple | None = None) -> str:
    """Format human-readable text report.

    ``compared`` is ``(english, chinese)`` document counts when the caller
    knows them (#1810): markdown present but no document classified as a
    zh/en pair means nothing was compared, and that must not read as the
    green "passed" — the wording is the only thing that tells the two apart
    (rc stays 0: being paired is per-file optional).
    """
    lines = []
    lines.append("=" * 60)
    lines.append("Bilingual Content Check")
    lines.append("=" * 60)
    if compared is not None:
        lines.append(f"Compared: {compared[0]} English, {compared[1]} Chinese document(s)")

    if not findings:
        if compared is not None and compared == (0, 0):
            lines.append("⚠ nothing was compared: markdown present but no document "
                         "is classified as a zh/en pair")
        else:
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


def format_json_report(findings: list, *, reason: str | None = None,
                       compared: tuple | None = None) -> str:
    """Format JSON report.

    ``status`` is one of ``pass | warn | caller_error``. ``reason`` is set
    only on the refused-empty-population path (#1810): ``--json`` stdout
    must still be exactly one JSON document there, so that path emits this
    shape zeroed out with ``status="caller_error"`` and the reason instead
    of prose. ``compared`` adds ``{"english": n, "chinese": m}`` when the
    caller knows the counts, so a consumer can tell "pass" from "nothing was
    compared".
    """
    report = {
        "check": "bilingual_content",
        "findings": [
            {"severity": s, "message": m, "file": p, "cjk_ratio": r}
            for s, m, p, r in findings
        ],
        "warning_count": sum(1 for s, *_ in findings if s == "warning"),
        "info_count": sum(1 for s, *_ in findings if s == "info"),
        "status": "pass" if not any(s == "warning" for s, *_ in findings)
                  else "warn",
    }
    if compared is not None:
        report["compared"] = {"english": compared[0], "chinese": compared[1]}
    if reason is not None:
        report["status"] = "caller_error"
        report["reason"] = reason
    return json.dumps(report, indent=2, ensure_ascii=False)


def empty_population_reason(docs_dir: Path) -> str | None:
    """Why nothing could be measured under ``docs_dir``, or None if it can.

    #1810: an empty population is a caller error, not a clean pass — a
    docs dir with no markdown printed "All bilingual content checks
    passed" and exited 0. Name WHICH empty shape: not a directory, or no
    markdown at all.
    """
    if not docs_dir.is_dir():
        return f"not a directory: {docs_dir}"
    if next(docs_dir.rglob("*.md"), None) is None:
        return f"no markdown files found under {docs_dir}"
    return None


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

    reason = empty_population_reason(DOCS_DIR)
    if reason is not None:
        msg = f"ERROR: {reason} — nothing was measured"
        print(msg, file=sys.stderr)
        if args.json:
            print(format_json_report([], reason=reason))
        else:
            # validate_all quotes only the last meaningful stdout line.
            print(msg)
        sys.exit(EXIT_CALLER_ERROR)

    measured: dict = {}
    unreadable: set = set()
    findings = run_all_checks(docs_dir=DOCS_DIR, threshold=args.threshold,
                              measured=measured, unreadable=unreadable)
    # Counts of documents actually READ (#1810), not merely selected.
    compared = (len(measured["english"]), len(measured["chinese"]))
    for f in sorted(unreadable):
        # A selected document that could not be read was not compared;
        # say so instead of folding it into a green "passed".
        print(f"⚠ not compared (unreadable or not UTF-8): {f}", file=sys.stderr)

    if args.json:
        print(format_json_report(findings, compared=compared))
    else:
        print(format_text_report(findings, compared=compared))

    has_warnings = any(s == "warning" for s, *_ in findings)
    if args.ci and has_warnings:
        sys.exit(EXIT_VIOLATION)


if __name__ == "__main__":
    main()
