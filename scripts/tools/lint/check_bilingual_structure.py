#!/usr/bin/env python3
"""check_bilingual_structure.py — ZH/EN 文件結構同步 lint

對每組 *.md / *.en.md pair 提取 ## / ### / #### 標題骨架，
比對章節結構是否一致。允許翻譯差異但 section count 和 heading hierarchy
必須匹配。同時檢查雙語導航連結對稱性。

v2.4.0 新增：解決 v2.3.0 release 過程中 cli-reference.en.md 缺少整個
"Operator + Federation" 章節和 opa-evaluate 指令的問題。
現有 check_doc_links.py 只檢查檔案存在性，validate_docs_versions.py
只檢查 frontmatter 版號和計數，兩者都抓不到章節級內容漂移。

用法:
    python3 scripts/tools/lint/check_bilingual_structure.py [--ci] [--json] [--verbose]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# 掃描範圍
SCAN_DIRS = [
    REPO_ROOT / "docs",
    REPO_ROOT / "rule-packs",
]
SCAN_ROOT_FILES = [
    REPO_ROOT / "README.md",
]

# 不檢查結構差異的檔案（例如 CHANGELOG 會有不同段落）
SKIP_STRUCTURE_CHECK = {
    "CHANGELOG.md",
}


# ---------------------------------------------------------------------------
# Heading extraction
# ---------------------------------------------------------------------------

def extract_headings(filepath: Path) -> List[Tuple[int, int, str]]:
    """Extract markdown headings from file.

    Returns list of (line_num, level, heading_text).
    Skips headings inside code blocks.
    """
    headings = []
    in_code = False

    try:
        content = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return headings

    in_frontmatter = False
    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()

        # Skip YAML frontmatter
        if i == 1 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue

        # Track code blocks
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue

        # Match heading
        m = re.match(r"^(#{2,4})\s+(.+)", line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            headings.append((i, level, text))

    return headings


def heading_skeleton(headings: List[Tuple[int, int, str]]) -> List[Tuple[int, str]]:
    """Convert headings to structural skeleton: (level, normalized_key).

    The normalized key strips CJK text but preserves technical terms,
    numbers, and code patterns (e.g. function names, CLI commands).
    """
    skeleton = []
    for _line, level, text in headings:
        # Normalize: lowercase, strip formatting
        key = text.lower()
        key = re.sub(r"[*_`]", "", key)
        # Remove markdown links but keep link text
        key = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", key)
        # Condense whitespace
        key = re.sub(r"\s+", " ", key).strip()
        skeleton.append((level, key))
    return skeleton


# ---------------------------------------------------------------------------
# Navigation link check
# ---------------------------------------------------------------------------

def check_nav_links(zh_path: Path, en_path: Path) -> List[str]:
    """Check bidirectional navigation links between zh/en pairs.

    Each file's first 20 lines should contain a link to its counterpart.
    """
    issues = []

    def _has_link_to(filepath: Path, target_name: str) -> bool:
        try:
            lines = filepath.read_text(encoding="utf-8").splitlines()[:20]
        except (UnicodeDecodeError, OSError):
            return True  # Can't read → skip
        for line in lines:
            if target_name in line:
                return True
        return False

    rel_zh = zh_path.relative_to(REPO_ROOT)
    rel_en = en_path.relative_to(REPO_ROOT)

    if not _has_link_to(zh_path, en_path.name):
        issues.append(
            f"{rel_zh}: 前 20 行缺少指向 {en_path.name} 的導航連結"
        )
    if not _has_link_to(en_path, zh_path.name):
        issues.append(
            f"{rel_en}: 前 20 行缺少指向 {zh_path.name} 的導航連結"
        )

    return issues


# ---------------------------------------------------------------------------
# Structure comparison
# ---------------------------------------------------------------------------

def compare_structure(
    zh_path: Path,
    en_path: Path,
) -> List[Dict]:
    """Compare heading structure between zh and en files.

    Returns list of issue dicts with keys: file, severity, message.
    """
    issues = []
    rel_zh = str(zh_path.relative_to(REPO_ROOT))
    rel_en = str(en_path.relative_to(REPO_ROOT))

    zh_headings = extract_headings(zh_path)
    en_headings = extract_headings(en_path)

    zh_skel = heading_skeleton(zh_headings)
    en_skel = heading_skeleton(en_headings)

    # Compare heading counts per level
    zh_level_counts = {}
    en_level_counts = {}
    for level, _ in zh_skel:
        zh_level_counts[level] = zh_level_counts.get(level, 0) + 1
    for level, _ in en_skel:
        en_level_counts[level] = en_level_counts.get(level, 0) + 1

    all_levels = sorted(set(list(zh_level_counts.keys()) + list(en_level_counts.keys())))
    for level in all_levels:
        zh_count = zh_level_counts.get(level, 0)
        en_count = en_level_counts.get(level, 0)
        if zh_count != en_count:
            hashes = "#" * level
            issues.append({
                "file": f"{rel_zh} ↔ {rel_en}",
                "severity": "error",
                "message": (
                    f"h{level} ({hashes}) 數量不一致: "
                    f"ZH={zh_count}, EN={en_count} (差 {abs(zh_count - en_count)})"
                ),
            })

    # Total heading count comparison
    if len(zh_skel) != len(en_skel):
        issues.append({
            "file": f"{rel_zh} ↔ {rel_en}",
            "severity": "error",
            "message": (
                f"總標題數不一致: ZH={len(zh_skel)}, EN={len(en_skel)}"
            ),
        })

    # Detect missing technical sections
    # Only flag headings that contain CLI-like patterns (da-tools, --flag, file.py)
    # or version patterns (v2.x.x) — these are language-independent identifiers
    # that MUST appear in both versions. Pure prose headings differ by translation.
    cli_pattern = re.compile(
        r"(da-tools|--[a-z]|\.py\b|\.yaml\b|\.jsx\b|v\d+\.\d+|"
        r"configmap|prometheus|alertmanager|helm|kubectl|opa|crd|api)"
    )

    zh_cli = {k for _, k in zh_skel if cli_pattern.search(k)}
    en_cli = {k for _, k in en_skel if cli_pattern.search(k)}

    # Only flag if one side has CLI-specific headings the other doesn't
    zh_only_cli = zh_cli - en_cli
    en_only_cli = en_cli - zh_cli

    if zh_only_cli:
        samples = sorted(zh_only_cli)[:5]
        issues.append({
            "file": rel_en,
            "severity": "error",
            "message": (
                f"EN 版本缺少 {len(zh_only_cli)} 個技術標題（含 CLI/工具關鍵字）: "
                + ", ".join(f'"{s}"' for s in samples)
            ),
        })
    if en_only_cli:
        samples = sorted(en_only_cli)[:5]
        issues.append({
            "file": rel_zh,
            "severity": "warning",
            "message": (
                f"ZH 版本缺少 {len(en_only_cli)} 個技術標題（含 CLI/工具關鍵字）: "
                + ", ".join(f'"{s}"' for s in samples)
            ),
        })

    return issues


# ---------------------------------------------------------------------------
# Discovery and main
# ---------------------------------------------------------------------------

def discover_bilingual_pairs() -> List[Tuple[Path, Path]]:
    """Find all zh/en markdown file pairs."""
    pairs = []

    # docs/ and rule-packs/
    for scan_dir in SCAN_DIRS:
        if not scan_dir.is_dir():
            continue
        for en_file in sorted(scan_dir.rglob("*.en.md")):
            zh_file = en_file.parent / en_file.name.replace(".en.md", ".md")
            if zh_file.is_file():
                pairs.append((zh_file, en_file))

    # Root READMEs
    for zh_file in SCAN_ROOT_FILES:
        if zh_file.is_file():
            en_name = zh_file.stem + ".en" + zh_file.suffix
            en_file = zh_file.parent / en_name
            if en_file.is_file():
                pairs.append((zh_file, en_file))

    return pairs


def main():
    parser = argparse.ArgumentParser(
        description="ZH/EN 文件結構同步 lint")
    parser.add_argument("--ci", action="store_true",
                        help="CI 模式: 有 error 時 exit 1")
    parser.add_argument("--json", action="store_true",
                        help="JSON 格式輸出")
    parser.add_argument("--verbose", action="store_true",
                        help="顯示每對檔案的比較細節")
    args = parser.parse_args()

    pairs = discover_bilingual_pairs()

    all_issues = []
    nav_issues = []

    for zh_path, en_path in pairs:
        basename = zh_path.name
        if basename in SKIP_STRUCTURE_CHECK:
            continue

        # Structure check
        issues = compare_structure(zh_path, en_path)
        all_issues.extend(issues)

        # Navigation link check
        nav = check_nav_links(zh_path, en_path)
        nav_issues.extend(nav)

    errors = [i for i in all_issues if i["severity"] == "error"]
    warnings = [i for i in all_issues if i["severity"] == "warning"]

    if args.json:
        print(json.dumps({
            "pairs_checked": len(pairs),
            "structure_issues": all_issues,
            "nav_issues": nav_issues,
            "summary": {
                "errors": len(errors),
                "warnings": len(warnings),
                "nav_issues": len(nav_issues),
            },
        }, ensure_ascii=False, indent=2))
    else:
        print(f"掃描 {len(pairs)} 對雙語文件\n")

        if args.verbose:
            for zh_path, en_path in pairs:
                rel = zh_path.relative_to(REPO_ROOT)
                zh_h = extract_headings(zh_path)
                en_h = extract_headings(en_path)
                print(f"  {rel}: ZH {len(zh_h)} headings, EN {len(en_h)} headings")

        if all_issues:
            print("結構差異:")
            for issue in all_issues:
                icon = "✗" if issue["severity"] == "error" else "⚠"
                print(f"  {icon} [{issue['file']}] {issue['message']}")
            print()

        if nav_issues:
            print("導航連結問題:")
            for msg in nav_issues:
                print(f"  ⚠ {msg}")
            print()

        if not all_issues and not nav_issues:
            print("✓ 所有雙語文件結構一致，導航連結完整。")
        else:
            total = len(errors) + len(warnings) + len(nav_issues)
            print(f"總計: {len(errors)} 錯誤, {len(warnings)} 警告, "
                  f"{len(nav_issues)} 導航問題")

    if args.ci and errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
