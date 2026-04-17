#!/usr/bin/env python3
"""check_design_token_usage.py — JSX 設計 token 使用完整性 lint

掃描 JSX 工具檔案, 檢測:
  (a) hardcoded hex 色碼（應使用 var(--da-color-*) token）
  (b) hardcoded px 數值在 style object 中（應使用 --da-space-* 或 --da-font-size-* token）

例外規則:
  - 行末註解 /* token-exempt */ 豁免整行
  - // 單行註解中的 hex 不檢查
  - #fff 和 #000 過於常見，不檢查
  - 0px, 1px, 2px（邊框 / hairline）不檢查
  - design-tokens.css 本身不掃描（定義端）

用法:
    python3 scripts/tools/lint/check_design_token_usage.py [--ci]
    python3 scripts/tools/lint/check_design_token_usage.py --ci && echo "All clean"
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
JSX_TOOLS_DIR = REPO_ROOT / "docs" / "interactive" / "tools"
WIZARD_DIR = REPO_ROOT / "docs" / "getting-started"
DESIGN_TOKENS = REPO_ROOT / "docs" / "assets" / "design-tokens.css"


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------

def check_hardcoded_hex_colors(content: str, filename: str) -> List[Dict]:
    """Scan for hardcoded hex colors that should use --da-color-* tokens.

    Returns list of {line_num, hex, context}.
    """
    issues = []

    # Hex color pattern: exactly 3, 4, 6, or 8 hex digits after #
    # Use negative lookbehind for & (HTML entities like &#8987;)
    # Use word boundary or end-of-token to avoid matching #dba-alerts
    hex_pattern = re.compile(r'(?<!&)#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})(?![0-9a-fA-F\w-])')

    lines = content.splitlines()
    for i, line in enumerate(lines, 1):
        # Skip lines with exempt marker
        if "/* token-exempt */" in line:
            continue

        # Skip pure comment lines
        if line.strip().startswith("//") or line.strip().startswith("*"):
            continue

        # Extract code part (before //)
        code_part = line.split("//")[0]

        # Find hex patterns
        for m in hex_pattern.finditer(code_part):
            hex_val = "#" + m.group(1)

            # Exceptions: #fff, #000, #ffffff, #000000
            if hex_val.lower() in ("#fff", "#000", "#ffffff", "#000000"):
                continue

            issues.append({
                "line": i,
                "hex": hex_val,
                "context": line.strip()[:80],
            })

    return issues


def check_hardcoded_px_values(content: str, filename: str) -> List[Dict]:
    """Scan for hardcoded px values in style objects.

    Looks for patterns like: fontSize: '14px', padding: '12px', etc.
    Skips: 0px, 1px, 2px (borders/hairlines), className strings.

    Returns list of {line_num, px, context}.
    """
    issues = []

    # Pattern for style properties with px values
    # Matches: property: 'XXpx' or property: XXpx or property: 'XX' (numeric)
    style_pattern = re.compile(r''':\s*(?:['"])?(\d+)(?:px)?['"]?\s*[,}]''')

    lines = content.splitlines()
    for i, line in enumerate(lines, 1):
        # Skip lines with exempt marker
        if "/* token-exempt */" in line:
            continue

        # Skip pure comment lines
        if line.strip().startswith("//"):
            continue

        # Only check inside style object context (rough heuristic)
        # Look for style={ or style=` patterns
        if "style=" not in line and "style =" not in line:
            continue

        # Extract code part (before //)
        code_part = line.split("//")[0]

        # Find style property patterns
        # Match patterns like: fontSize: '14px', padding: 12, margin: "8px"
        prop_pattern = re.compile(
            r'(\w+)\s*:\s*[\'"]?(\d+)(?:px)?[\'"]?'
        )

        for m in prop_pattern.finditer(code_part):
            prop_name = m.group(1)
            px_value = m.group(2)

            # Exceptions: 0, 1, 2 (borders/hairlines)
            if int(px_value) in (0, 1, 2):
                continue

            # Reconstruct the matched text with px suffix
            matched_text = line[m.start():m.end()]
            if "px" not in matched_text:
                px_text = f"{px_value}px"
            else:
                px_text = f"{px_value}px"

            issues.append({
                "line": i,
                "px": px_text,
                "property": prop_name,
                "context": line.strip()[:80],
            })

    return issues


def scan_jsx_files() -> Tuple[Dict[str, List[Dict]], Dict[str, List[Dict]]]:
    """Scan all JSX files for design token violations.

    Returns (hex_issues_by_file, px_issues_by_file).
    """
    hex_issues = defaultdict(list)
    px_issues = defaultdict(list)

    jsx_dirs = [JSX_TOOLS_DIR, WIZARD_DIR]

    for jsx_dir in jsx_dirs:
        if not jsx_dir.is_dir():
            continue

        for jsx_file in sorted(jsx_dir.glob("**/*.jsx")):
            # Skip design-tokens.css itself
            if jsx_file.name == "design-tokens.css":
                continue

            try:
                content = jsx_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            rel_path = str(jsx_file.relative_to(REPO_ROOT))

            # Check hex colors
            hex_found = check_hardcoded_hex_colors(content, jsx_file.name)
            if hex_found:
                hex_issues[rel_path] = hex_found

            # Check px values
            px_found = check_hardcoded_px_values(content, jsx_file.name)
            if px_found:
                px_issues[rel_path] = px_found

    return dict(hex_issues), dict(px_issues)


# ---------------------------------------------------------------------------
# Main check logic
# ---------------------------------------------------------------------------

def main():
    """Run all design token usage checks."""
    parser = argparse.ArgumentParser(
        description="JSX 設計 token 使用完整性 lint"
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI 模式: 有違規時 exit 1"
    )
    args = parser.parse_args()

    hex_issues, px_issues = scan_jsx_files()

    all_files = set(hex_issues.keys()) | set(px_issues.keys())
    total_violations = 0

    if not all_files:
        print("✓ 設計 token 使用檢查通過。")
        sys.exit(0)

    # Print results grouped by file
    for filename in sorted(all_files):
        print(f"[{filename}]")

        # Hex color violations
        if filename in hex_issues:
            for issue in hex_issues[filename]:
                print(f"  L{issue['line']}: hardcoded hex {issue['hex']} "
                      f"(use --da-color-* token)")
                total_violations += 1

        # PX value violations
        if filename in px_issues:
            for issue in px_issues[filename]:
                print(f"  L{issue['line']}: hardcoded px '{issue['px']}' "
                      f"in {issue['property']} (use --da-space-* or --da-font-size-*)")
                total_violations += 1

        print()

    # Summary
    print(f"TOTAL: {total_violations} violation(s) in {len(all_files)} file(s)")

    # Exit with appropriate code
    if args.ci and total_violations > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
