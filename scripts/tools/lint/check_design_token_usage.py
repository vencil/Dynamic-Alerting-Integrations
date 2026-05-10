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

Lint class & scope (lint-policy.md §3)
--------------------------------------
Class **(b)** — negative pattern + token-exempt allowlist.
Default scope: **diff-only** — only lines ADDED in current diff vs base
emit findings. Override with --full-scan for periodic manual audit.

Bypass (per lint-policy.md §4):
    Add to PR description body:
        bypass-lint: design-token-usage
        reason: <≥30 words explaining why this case is legitimate>

用法:
    # Diff-only (default; CI sets LINT_DIFF_BASE / GITHUB_BASE_REF)
    python3 scripts/tools/lint/check_design_token_usage.py [--ci]

    # Full-scan (manual audit)
    python3 scripts/tools/lint/check_design_token_usage.py --full-scan [--ci]
"""

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

# Helpers from this lint family
sys.path.insert(0, str(Path(__file__).parent))
from _lint_helpers import (  # noqa: E402
    DiffBaseMissingError,
    get_diff_added_lines,
    parse_bypass_tag,
    resolve_diff_base,
)

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


def scan_jsx_files(diff_base: str | None = None) -> Tuple[Dict[str, List[Dict]], Dict[str, List[Dict]]]:
    """Scan JSX files for design token violations.

    If ``diff_base`` is None: full-scan all JSX files in JSX_TOOLS_DIR + WIZARD_DIR.
    If ``diff_base`` is a ref: only flag findings on lines ADDED in the current
    diff vs that base. Existing pre-existing violations are not re-emitted.

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

            # Run full scan to get all findings
            hex_found = check_hardcoded_hex_colors(content, jsx_file.name)
            px_found = check_hardcoded_px_values(content, jsx_file.name)

            # If diff-only, filter findings to lines actually added in current diff
            if diff_base is not None:
                try:
                    added_lines = {ln for ln, _ in get_diff_added_lines(jsx_file, diff_base)}
                except subprocess.CalledProcessError:
                    # Git error — keep all findings (safer than silent suppress)
                    added_lines = None
                if added_lines is not None:
                    hex_found = [h for h in hex_found if h["line"] in added_lines]
                    px_found = [h for h in px_found if h["line"] in added_lines]

            if hex_found:
                hex_issues[rel_path] = hex_found
            if px_found:
                px_issues[rel_path] = px_found

    return dict(hex_issues), dict(px_issues)


def _read_pr_body(pr_body_file: str | None) -> str | None:
    """Read PR body from --pr-body-file or $PR_BODY env var."""
    if pr_body_file:
        try:
            return Path(pr_body_file).read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError) as e:
            print(f"WARN: cannot read --pr-body-file {pr_body_file}: {e}", file=sys.stderr)
    return os.environ.get("PR_BODY") or None


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
    parser.add_argument(
        "--full-scan", action="store_true",
        help="Scan ALL existing violations (default: diff-only — only added lines).",
    )
    parser.add_argument(
        "--diff-base", default=None,
        help="Override diff base (default: $LINT_DIFF_BASE / $GITHUB_BASE_REF / origin/main).",
    )
    parser.add_argument(
        "--pr-body-file", default=None,
        help="Path to file containing PR body for bypass tag check.",
    )
    args = parser.parse_args()

    # Resolve scan mode
    if args.full_scan:
        scan_mode = "full-scan"
        diff_base = None
    else:
        try:
            diff_base = args.diff_base or resolve_diff_base()
        except DiffBaseMissingError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        scan_mode = f"diff vs {diff_base}"

    hex_issues, px_issues = scan_jsx_files(diff_base=diff_base)

    all_files = set(hex_issues.keys()) | set(px_issues.keys())
    total_violations = 0

    if not all_files:
        print(f"✓ 設計 token 使用檢查通過 (mode={scan_mode})。")
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
    print(f"TOTAL: {total_violations} violation(s) in {len(all_files)} file(s) (mode={scan_mode})")

    # Bypass check (lint-policy.md §4)
    pr_body = _read_pr_body(args.pr_body_file)
    bypass_reason = parse_bypass_tag(pr_body, "design-token-usage")
    if bypass_reason:
        print(
            f"\n⚠️  BYPASSED via PR body: {bypass_reason}\n"
            f"   {total_violations} finding(s) above are author-acknowledged.\n"
            f"   Reviewer must confirm bypass is justified."
        )
        sys.exit(0)

    # Exit with appropriate code
    if args.ci and total_violations > 0:
        print(
            "\nFix: replace hardcoded values with --da-* tokens, OR add\n"
            "  /* token-exempt */ on the line if intentional.\n"
            "Or add to PR description (per lint-policy.md §4):\n"
            "  bypass-lint: design-token-usage\n"
            "  reason: <≥30 words explaining why this is legitimate>",
        )
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
