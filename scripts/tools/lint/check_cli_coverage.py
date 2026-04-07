#!/usr/bin/env python3
"""check_cli_coverage.py — CLI 命令覆蓋率檢查

從 entrypoint.py COMMAND_MAP (單一真相源) 反向驗證：
  1. cheat-sheet.md / cheat-sheet.en.md — 每個命令都出現在速查表中
  2. cli-reference.md / cli-reference.en.md — 每個命令都有詳解章節
  3. entrypoint.py help text — 每個命令都列在 help 文字中
  4. 雙語一致性 — zh/en 文件涵蓋相同的命令集

Usage:
    python3 scripts/tools/lint/check_cli_coverage.py [--ci] [--json]

Exit codes:
    0 = all checks passed
    1 = errors found (must fix)
"""

import argparse
import json
import re
import sys
from pathlib import Path

from _lint_helpers import parse_command_map_keys, REPO_ROOT, ENTRYPOINT_PATH

CHEAT_SHEET_ZH = REPO_ROOT / "docs" / "cheat-sheet.md"
CHEAT_SHEET_EN = REPO_ROOT / "docs" / "cheat-sheet.en.md"
CLI_REF_ZH = REPO_ROOT / "docs" / "cli-reference.md"
CLI_REF_EN = REPO_ROOT / "docs" / "cli-reference.en.md"


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_help_text_commands(path: Path) -> set:
    """Parse command names from the _build_help_text function.

    Extracts commands from triple-quoted help text blocks only.
    Matches indented lines like:  '    command-name   description'
    """
    commands = set()
    with open(path, encoding="utf-8") as f:
        content = f.read()

    # Non-command words that appear in the same indentation pattern
    skip_words = {"da", "da-tools", "Usage", "Commands", "Global",
                  "PROMETHEUS_URL", "DA_LANG"}

    # Extract content from triple-quoted strings only
    for block in re.findall(r'"""(.*?)"""', content, re.DOTALL):
        for m in re.finditer(r"^\s{4}([a-z][a-z0-9]+(?:-[a-z0-9]+)*)\s+\S",
                             block, re.MULTILINE):
            cmd = m.group(1)
            if cmd in skip_words:
                continue
            commands.add(cmd)
    return commands


def parse_cheat_sheet_commands(path: Path) -> set:
    """Parse commands from cheat-sheet markdown table.

    Matches rows: | `command-name` | description | flags | example |
    """
    commands = set()
    if not path.exists():
        return commands
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\|\s*`([a-z][a-z0-9-]+)`\s*\|", line.strip())
            if m:
                commands.add(m.group(1))
    return commands


def parse_cli_reference_commands(path: Path) -> set:
    """Parse commands from cli-reference markdown h4 sections.

    Matches: #### command-name
    """
    commands = set()
    if not path.exists():
        return commands
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^####\s+([a-z][a-z0-9-]+)\s*$", line.strip())
            if m:
                commands.add(m.group(1))
    return commands


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_coverage(command_map: set, doc_commands: set,
                   doc_name: str) -> list:
    """Compare COMMAND_MAP against a doc's command set.

    Returns list of (severity, message) tuples.
    """
    errors = []
    missing = command_map - doc_commands
    extra = doc_commands - command_map

    for cmd in sorted(missing):
        errors.append(("error", f"{doc_name}: missing command `{cmd}`"))
    for cmd in sorted(extra):
        errors.append(("warning",
                       f"{doc_name}: extra command `{cmd}` "
                       f"not in COMMAND_MAP"))
    return errors


def check_bilingual_consistency(zh_commands: set, en_commands: set,
                                doc_pair: str) -> list:
    """Check that zh and en versions cover the same commands."""
    errors = []
    zh_only = zh_commands - en_commands
    en_only = en_commands - zh_commands

    for cmd in sorted(zh_only):
        errors.append(("error",
                       f"{doc_pair}: `{cmd}` in zh but missing in en"))
    for cmd in sorted(en_only):
        errors.append(("error",
                       f"{doc_pair}: `{cmd}` in en but missing in zh"))
    return errors


def run_all_checks() -> list:
    """Run all CLI coverage checks.

    Returns list of (severity, message) tuples.
    """
    all_errors = []

    # Parse source of truth
    if not ENTRYPOINT_PATH.exists():
        all_errors.append(("error",
                           f"entrypoint.py not found: {ENTRYPOINT_PATH}"))
        return all_errors

    command_map = parse_command_map_keys(ENTRYPOINT_PATH)
    if not command_map:
        all_errors.append(("error", "COMMAND_MAP is empty or unparseable"))
        return all_errors

    help_commands = parse_help_text_commands(ENTRYPOINT_PATH)

    # Parse docs
    cs_zh = parse_cheat_sheet_commands(CHEAT_SHEET_ZH)
    cs_en = parse_cheat_sheet_commands(CHEAT_SHEET_EN)
    cr_zh = parse_cli_reference_commands(CLI_REF_ZH)
    cr_en = parse_cli_reference_commands(CLI_REF_EN)

    # 1. Help text coverage
    help_missing = command_map - help_commands
    for cmd in sorted(help_missing):
        all_errors.append(("error",
                           f"entrypoint help text: missing `{cmd}`"))

    # 2. Cheat sheet coverage
    all_errors.extend(check_coverage(command_map, cs_zh, "cheat-sheet.md"))
    all_errors.extend(check_coverage(command_map, cs_en, "cheat-sheet.en.md"))

    # 3. CLI reference coverage
    all_errors.extend(check_coverage(command_map, cr_zh, "cli-reference.md"))
    all_errors.extend(check_coverage(command_map, cr_en,
                                     "cli-reference.en.md"))

    # 4. Bilingual consistency
    all_errors.extend(check_bilingual_consistency(
        cs_zh, cs_en, "cheat-sheet zh/en"))
    all_errors.extend(check_bilingual_consistency(
        cr_zh, cr_en, "cli-reference zh/en"))

    return all_errors


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_text_report(errors: list, command_map: set) -> str:
    """Format human-readable text report."""
    lines = []
    lines.append("=" * 60)
    lines.append("CLI Command Coverage Check")
    lines.append("=" * 60)
    lines.append(f"COMMAND_MAP commands: {len(command_map)}")
    lines.append("")

    if not errors:
        lines.append("✓ All commands covered in all docs.")
        return "\n".join(lines)

    error_count = sum(1 for sev, _ in errors if sev == "error")
    warn_count = sum(1 for sev, _ in errors if sev == "warning")

    for sev, msg in errors:
        sym = "✗" if sev == "error" else "⊘"
        lines.append(f"  {sym} {msg}")

    lines.append("")
    lines.append(f"Result: {error_count} error(s), {warn_count} warning(s)")
    return "\n".join(lines)


def format_json_report(errors: list, command_map: set) -> str:
    """Format JSON report."""
    return json.dumps({
        "check": "cli_coverage",
        "command_count": len(command_map),
        "errors": [{"severity": s, "message": m} for s, m in errors],
        "error_count": sum(1 for s, _ in errors if s == "error"),
        "warning_count": sum(1 for s, _ in errors if s == "warning"),
        "status": "pass" if not any(s == "error" for s, _ in errors)
                  else "fail",
    }, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Check CLI command coverage across docs"
    )
    parser.add_argument("--ci", action="store_true",
                        help="CI mode: exit 1 on any error")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    args = parser.parse_args()

    command_map = set()
    if ENTRYPOINT_PATH.exists():
        command_map = parse_command_map_keys(ENTRYPOINT_PATH)

    errors = run_all_checks()

    if args.json:
        print(format_json_report(errors, command_map))
    else:
        print(format_text_report(errors, command_map))

    has_errors = any(s == "error" for s, _ in errors)
    if args.ci and has_errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
