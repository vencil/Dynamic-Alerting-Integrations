#!/usr/bin/env python3
"""Unified validation entry point for all documentation and config validation tools."""

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

TOOLS = [
    ("links", "check_doc_links.py", [], "Link validation"),
    ("mermaid", "validate_mermaid.py", ["docs/", "rule-packs/"], "Mermaid diagram syntax"),
    ("translation", "check_translation.py", [], "Bilingual structure consistency"),
    ("glossary", "sync_glossary_abbr.py", ["--check"], "Glossary abbreviation sync"),
    ("schema", "sync_schema.py", ["--check"], "Go→JSON Schema drift"),
    ("alerts", "generate_alert_reference.py", ["--check"], "Alert reference drift"),
    ("rule_packs", "generate_rule_pack_readme.py", ["--check"], "Rule Pack README drift"),
    ("cheatsheet", "generate_cheat_sheet.py", ["--check"], "Cheat sheet drift"),
    ("freshness", "check_doc_freshness.py", [], "Dead doc detection"),
    ("includes", "check_includes_sync.py", ["--check"], "Include snippet zh/en sync"),
    ("changelog", "generate_changelog.py", ["--check"], "Conventional commit format"),
]


def run_tool(
    short_name: str,
    script_name: str,
    args: List[str],
    description: str,
    tools_dir: Path,
    verbose: bool,
) -> Tuple[str, float, str]:
    """
    Run a single validation tool and return status, timing, and detail.

    Returns:
        Tuple of (status, elapsed_seconds, detail_message)
        status: 'pass', 'fail', or 'error'
    """
    script_path = tools_dir / script_name
    if not script_path.exists():
        return "error", 0.0, f"Script not found: {script_name}"

    cmd = [sys.executable, str(script_path)] + args
    start = time.time()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        elapsed = time.time() - start

        if verbose:
            print(f"\n--- {short_name.upper()} ---")
            print(result.stdout)
            if result.stderr:
                print("STDERR:", result.stderr)

        if result.returncode == 0:
            # Extract detail from stdout if available
            detail = extract_detail(result.stdout)
            return "pass", elapsed, detail
        else:
            # Tool failed; extract error detail
            detail = result.stdout.split("\n")[0][:80] if result.stdout else "Exit code: " + str(result.returncode)
            return "fail", elapsed, detail

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return "error", elapsed, "Timeout after 60s"
    except Exception as e:
        elapsed = time.time() - start
        return "error", elapsed, str(e)[:80]


def extract_detail(output: str) -> str:
    """Extract a brief detail message from tool output."""
    lines = output.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        if line and not line.startswith("==="):
            # Return last non-empty line, truncated
            return line[:80]
    return ""


def format_status_symbol(status: str) -> str:
    """Return symbol for status."""
    if status == "pass":
        return "✓"
    elif status == "fail":
        return "✗"
    else:  # error or skipped
        return "⊘"


def format_time(elapsed: float) -> str:
    """Format elapsed time nicely."""
    if elapsed < 1.0:
        return f"{elapsed:.1f}s"
    return f"{elapsed:.1f}s"


def main():
    parser = argparse.ArgumentParser(
        description="Unified validation for documentation and configuration."
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit 1 on first failure (CI mode)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show each tool's full output",
    )
    parser.add_argument(
        "--skip",
        type=str,
        default="",
        help="Comma-separated list of tools to skip (e.g. links,mermaid)",
    )

    args = parser.parse_args()

    # Determine tools directory (scripts/tools/)
    tools_dir = Path(__file__).parent
    project_root = tools_dir.parent.parent

    # Change to project root so relative paths work
    import os
    os.chdir(project_root)

    skip_set = set(s.strip() for s in args.skip.split(",") if s.strip())
    results: Dict[str, Tuple[str, float, str]] = {}
    passed = 0
    failed = 0
    skipped = 0

    print("=" * 60)
    print("Documentation & Config Validation Report")
    print("=" * 60)
    print()

    for short_name, script_name, tool_args, description in TOOLS:
        if short_name in skip_set:
            print(f"{format_status_symbol('skip')} {short_name:20} ... skipped")
            results[short_name] = ("skip", 0.0, "")
            skipped += 1
            continue

        status, elapsed, detail = run_tool(
            short_name,
            script_name,
            tool_args,
            description,
            tools_dir,
            args.verbose,
        )

        results[short_name] = (status, elapsed, detail)

        # Format output line
        symbol = format_status_symbol(status)
        time_str = format_time(elapsed)
        detail_str = f" ({detail})" if detail else ""
        print(f"{symbol} {short_name:20} ... {time_str}{detail_str}")

        if status == "pass":
            passed += 1
        elif status == "fail":
            failed += 1
            if args.ci:
                print()
                print("=" * 60)
                print(f"CI mode: Stopping after first failure ({short_name})")
                print("=" * 60)
                sys.exit(1)
        elif status == "error":
            failed += 1
            if args.ci:
                print()
                print("=" * 60)
                print(f"CI mode: Stopping after error ({short_name})")
                print("=" * 60)
                sys.exit(1)

    print()
    print("=" * 60)
    total = len(TOOLS) - skipped
    if total == 0:
        print("Result: All tools skipped")
    else:
        print(
            f"Result: {passed}/{total} passed, {failed} failed, {skipped} skipped"
        )
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
