#!/usr/bin/env python3
"""Check that Chinese and English include snippets stay in sync.

Compares docs/includes/X.md with docs/includes/X.en.md and reports
structural mismatches: code block count, table row count, URL count,
version string divergence.

Usage:
    check_includes_sync.py              # interactive report
    check_includes_sync.py --check      # CI mode (exit 1 on mismatch)
    check_includes_sync.py --verbose     # show per-file details
    check_includes_sync.py --fix         # create missing .en.md stubs
"""

import argparse
import os
import re
import stat
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
INCLUDES_DIR = REPO_ROOT / "docs" / "includes"

# ── Structural metrics ───────────────────────────────────────────────

def count_code_blocks(content: str) -> int:
    """Count fenced code blocks (``` ... ```)."""
    return len(re.findall(r"^```", content, re.MULTILINE))


def count_table_rows(content: str) -> int:
    """Count markdown table rows (lines starting with |)."""
    return len(re.findall(r"^\|", content, re.MULTILINE))


def count_urls(content: str) -> list:
    """Extract URLs from content."""
    return re.findall(r"https?://[^\s)>\"]+", content)


def extract_versions(content: str) -> list:
    """Extract semver-like version strings."""
    return re.findall(r"v?\d+\.\d+\.\d+", content)


def count_list_items(content: str) -> int:
    """Count markdown list items (- or * or numbered)."""
    return len(re.findall(r"^[\s]*[-*]\s|\d+\.\s", content, re.MULTILINE))


# ── Comparison ───────────────────────────────────────────────────────

def compare_pair(zh_path: Path, en_path: Path) -> list:
    """Compare a zh/en include pair. Returns list of issue strings."""
    issues = []

    if not en_path.exists():
        issues.append(f"English version missing: {en_path.name}")
        return issues

    zh = zh_path.read_text(encoding="utf-8")
    en = en_path.read_text(encoding="utf-8")

    # Code blocks
    zh_cb = count_code_blocks(zh)
    en_cb = count_code_blocks(en)
    if zh_cb != en_cb:
        issues.append(f"code blocks: ZH={zh_cb} vs EN={en_cb}")

    # Table rows
    zh_tr = count_table_rows(zh)
    en_tr = count_table_rows(en)
    if zh_tr != en_tr:
        issues.append(f"table rows: ZH={zh_tr} vs EN={en_tr}")

    # List items
    zh_li = count_list_items(zh)
    en_li = count_list_items(en)
    if zh_li != en_li:
        issues.append(f"list items: ZH={zh_li} vs EN={en_li}")

    # Version strings
    zh_ver = extract_versions(zh)
    en_ver = extract_versions(en)
    if zh_ver != en_ver:
        issues.append(f"versions: ZH={zh_ver} vs EN={en_ver}")

    # URLs
    zh_urls = count_urls(zh)
    en_urls = count_urls(en)
    if len(zh_urls) != len(en_urls):
        issues.append(f"URLs: ZH={len(zh_urls)} vs EN={len(en_urls)}")

    return issues


# ── Fix: generate missing English stubs ──────────────────────────────

# Common zh→en header translations for auto-generated includes
_ZH_EN_HEADERS = {
    "規則包": "Rule Pack",
    "合計": "Total",
    "運營模式": "operational mode",
    "自監控": "self-monitoring",
}


def _create_en_stub(zh_path: Path, en_path: Path) -> None:
    """Create an English stub from a Chinese include snippet.

    Copies the zh file and replaces known Chinese headers/keywords
    with English equivalents.
    """
    content = zh_path.read_text(encoding="utf-8")

    for zh_str, en_str in _ZH_EN_HEADERS.items():
        content = content.replace(zh_str, en_str)

    en_path.write_text(content, encoding="utf-8")
    os.chmod(en_path,
             stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Chinese/English include snippet sync"
    )
    parser.add_argument(
        "--check", action="store_true",
        help="CI mode: exit 1 if mismatches found",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show details for each file pair",
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="Create missing .en.md stubs from Chinese versions",
    )
    args = parser.parse_args()

    if not INCLUDES_DIR.exists():
        print(f"Includes directory not found: {INCLUDES_DIR}", file=sys.stderr)
        return 1

    # Find Chinese includes (*.md but not *.en.md and not abbreviations.md)
    zh_files = sorted(
        f for f in INCLUDES_DIR.glob("*.md")
        if not f.name.endswith(".en.md") and f.name != "abbreviations.md"
    )

    if not zh_files:
        print("No include snippets found.", file=sys.stderr)
        return 0

    total_pairs = 0
    total_issues = 0
    missing_en = 0
    fixed = 0

    for zh_path in zh_files:
        en_name = zh_path.stem + ".en.md"
        en_path = INCLUDES_DIR / en_name
        total_pairs += 1

        issues = compare_pair(zh_path, en_path)

        if not en_path.exists():
            missing_en += 1
            if args.fix:
                _create_en_stub(zh_path, en_path)
                print(f"  🔧 Created {en_name} from {zh_path.name}")
                fixed += 1
                continue

        if issues:
            total_issues += len(issues)
            print(f"❌ {zh_path.name} ↔ {en_name}:")
            for issue in issues:
                print(f"   {issue}")
        elif args.verbose:
            print(f"✅ {zh_path.name} ↔ {en_name}: in sync")

    # Summary
    print(f"\n{'─' * 40}")
    print(f"Pairs checked: {total_pairs}")
    print(f"Missing English: {missing_en}")
    print(f"Structural issues: {total_issues}")
    if fixed:
        print(f"Fixed (stubs created): {fixed}")

    if args.fix and fixed:
        print(f"\n🔧 Created {fixed} English stub(s). Review and refine.")
        return 0

    if total_issues > 0 or missing_en > 0:
        print(f"\n❌ {total_issues} issue(s) found across {total_pairs} pairs")
        return 1 if args.check else 0
    else:
        print(f"\n✅ All {total_pairs} include pairs are in sync")
        return 0


if __name__ == "__main__":
    sys.exit(main())
