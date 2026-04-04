#!/usr/bin/env python3
"""check_portal_i18n.py — Portal JSX i18n hardcoded string detector

Scans .jsx files in docs/interactive/tools/ and docs/getting-started/ for
hardcoded English strings that should be internationalized with window.__t(zh, en).

Usage:
    python3 scripts/tools/lint/check_portal_i18n.py
    python3 scripts/tools/lint/check_portal_i18n.py --ci
    python3 scripts/tools/lint/check_portal_i18n.py --json
    python3 scripts/tools/lint/check_portal_i18n.py --fix-hint

Exit codes:
    0 = no i18n issues found
    1 = i18n issues found (with --ci flag)
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
DOCS_DIR = PROJECT_ROOT / "docs"

# Directories to scan
SCAN_DIRS = [
    DOCS_DIR / "interactive" / "tools",
    DOCS_DIR / "getting-started",
]

# UI keywords indicating user-visible text
UI_KEYWORDS = {
    "Click", "Enter", "Select", "Submit", "Cancel", "Save", "Delete",
    "Edit", "Add", "Remove", "View", "Show", "Hide", "Search", "Filter",
    "Sort", "Next", "Previous", "Back", "Close", "Open", "Loading",
    "Error", "Warning", "Success", "Info", "Help", "Apply", "Reset",
    "Copy", "Paste", "Undo", "Redo", "Confirm", "Continue", "Done",
    "Please", "Invalid", "Required", "Optional", "No", "Yes", "Name",
    "Label", "Description", "Value", "Result", "Results", "Status",
}


def extract_strings_from_jsx(content: str) -> list:
    """Extract potential UI strings from JSX content.

    Returns list of (line_num, string, context) tuples.
    Heuristic-based; false positives acceptable.
    """
    findings = []
    lines = content.split("\n")

    # Simple regex patterns for string literals in JSX
    # Match: "string", 'string' but not in comments/imports/frontmatter
    string_pattern = re.compile(r'(["\'])([^"\'\n]{3,})(["\'])')

    for line_num, line in enumerate(lines, 1):
        # Skip frontmatter (YAML)
        if line.strip().startswith("---"):
            break
        if line.strip().startswith("//") or line.strip().startswith("/*"):
            continue

        # Skip import statements
        if "import " in line and "from" in line:
            continue

        # Skip lines with common exclusions
        if any(skip in line for skip in [
            "className=",
            "style=",
            "key=",
            "data-",
            "http://",
            "https://",
            ".png",
            ".jpg",
            ".svg",
            "\.jsx",
            "\.js",
            ".css",
            ".md",
            "const ",
        ]):
            continue

        # Skip if already using t() or __t()
        if re.search(r't\s*\(\s*["\']', line) or re.search(r'__t\s*\(\s*["\']', line):
            continue

        # Find all string literals
        for match in string_pattern.finditer(line):
            quote = match.group(1)
            string_val = match.group(2)

            # Filter: must be >3 chars, contain space or start with uppercase
            if len(string_val) > 3:
                looks_like_ui = (
                    " " in string_val or
                    (string_val and string_val[0].isupper()) or
                    any(kw in string_val for kw in UI_KEYWORDS)
                )

                if looks_like_ui:
                    # Exclude all-caps (likely constant), metric keys, etc.
                    if not (string_val.isupper() and "_" in string_val):
                        findings.append((line_num, string_val, line.strip()))

    return findings


def scan_jsx_files(base_dirs: list) -> list:
    """Scan all .jsx files for potential i18n issues.

    Returns list of (file_path, issues) tuples.
    issues = [(line_num, string, context), ...]
    """
    results = []

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue

        for jsx_file in sorted(base_dir.rglob("*.jsx")):
            try:
                content = jsx_file.read_text(encoding="utf-8")
                issues = extract_strings_from_jsx(content)

                if issues:
                    rel_path = jsx_file.relative_to(PROJECT_ROOT)
                    results.append((str(rel_path), issues))
            except Exception as e:
                print(f"Warning: Failed to scan {jsx_file}: {e}", file=sys.stderr)

    return results


def format_text_report(findings: list, fix_hint: bool = False) -> str:
    """Format human-readable text report."""
    lines = []
    lines.append("=" * 70)
    lines.append("Portal JSX i18n Check")
    lines.append("=" * 70)

    if not findings:
        lines.append("✓ All JSX files passed i18n check.")
        return "\n".join(lines)

    total_files = len(findings)
    total_issues = sum(len(issues) for _, issues in findings)

    for filepath, issues in findings:
        lines.append(f"\n{filepath}:")
        for line_num, string_val, context in issues:
            lines.append(f"  Line {line_num}: \"{string_val}\"")
            if fix_hint:
                zh_placeholder = "[Chinese translation here]"
                lines.append(
                    f"  → t('{zh_placeholder}', '{string_val}')"
                )
            lines.append(f"    Context: {context[:60]}")

    lines.append("")
    lines.append(f"Result: {total_files} file(s), {total_issues} potential issue(s)")
    return "\n".join(lines)


def format_json_report(findings: list) -> str:
    """Format JSON report."""
    issues_flat = []
    for filepath, issues in findings:
        for line_num, string_val, context in issues:
            issues_flat.append({
                "file": filepath,
                "line": line_num,
                "string": string_val,
                "context": context[:80],
            })

    return json.dumps({
        "check": "portal_i18n",
        "files_scanned": len(SCAN_DIRS),
        "files_with_issues": len(findings),
        "total_issues": sum(len(issues) for _, issues in findings),
        "issues": issues_flat,
        "status": "pass" if not findings else "warn",
    }, indent=2, ensure_ascii=False)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Check Portal JSX files for hardcoded English strings"
    )
    parser.add_argument("--ci", action="store_true",
                        help="CI mode: exit 1 if issues found")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--fix-hint", action="store_true",
                        help="Show suggested t() replacement pattern")
    args = parser.parse_args()

    findings = scan_jsx_files(SCAN_DIRS)

    if args.json:
        print(format_json_report(findings))
    else:
        print(format_text_report(findings, fix_hint=args.fix_hint))

    if args.ci and findings:
        sys.exit(1)


if __name__ == "__main__":
    main()
