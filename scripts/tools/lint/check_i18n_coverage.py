#!/usr/bin/env python3
"""
check_i18n_coverage.py

Scans all three i18n layers and reports coverage:

  Layer 1: JSX tools (docs/interactive/tools/*.jsx)
    - Checks for window.__t() function usage
    - Reports t() call coverage per file

  Layer 2: Rule Pack annotations (rule-packs/*.yaml)
    - Checks for *_zh bilingual annotations
    - Uses existing check_bilingual_annotations.py logic

  Layer 3: Python CLI tools (scripts/tools/*.py + entrypoint.py)
    - Checks for detect_cli_lang() and bilingual _HELP dicts
    - Reports tool coverage

Usage:
  python3 check_i18n_coverage.py [--ci] [--json] [--verbose]

Flags:
  --ci       Exit code 1 if any layer < threshold (L1=50%, L2=10%, L3=5%)
  --json     Output as JSON for badge generation
  --verbose  Show per-file details
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


class JSXCoverageChecker:
    """Checks i18n coverage in JSX files."""

    def __init__(self, docs_dir: Path):
        self.docs_dir = docs_dir
        self.results: Dict[str, Dict] = {}

    def find_jsx_files(self) -> List[Path]:
        """Find all JSX files in docs directory."""
        return sorted(self.docs_dir.rglob("*.jsx"))

    def check_file(self, jsx_path: Path) -> Dict:
        """Check a single JSX file for i18n coverage."""
        with open(jsx_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check if file uses window.__t function
        has_t_function = "window.__t" in content

        # Count t() calls - look for patterns like t('zh', 'en') or t("zh", "en")
        # This is a simple regex that catches most common cases
        t_call_pattern = r"t\s*\(\s*['\"]"
        t_calls = len(re.findall(t_call_pattern, content))

        # Count all string literals in JSX (rough estimate)
        # Look for quoted strings inside JSX return blocks
        # This is a heuristic - counts all string literals in the file
        string_pattern = r'''['"]\w[^'"]*['"]'''
        all_strings = len(re.findall(string_pattern, content))

        # Conservative coverage estimate: t_calls / all_strings
        # If t_calls is 0, coverage is 0%
        if all_strings > 0:
            coverage_pct = min(100, int(100 * t_calls / max(all_strings, 1)))
        else:
            coverage_pct = 0 if t_calls == 0 else 100

        return {
            "has_t_function": has_t_function,
            "t_calls": t_calls,
            "estimated_strings": all_strings,
            "coverage_pct": coverage_pct,
        }

    def run(self) -> Dict:
        """Run JSX layer check."""
        jsx_files = self.find_jsx_files()

        if not jsx_files:
            return {
                "total_files": 0,
                "files_with_i18n": 0,
                "files": {},
                "coverage_pct": 0,
            }

        for jsx_path in jsx_files:
            rel_path = jsx_path.relative_to(self.docs_dir.parent)
            result = self.check_file(jsx_path)
            self.results[str(rel_path)] = result

        # Summary statistics
        files_with_i18n = sum(1 for r in self.results.values() if r["has_t_function"])
        total_files = len(self.results)

        coverage_pct = (
            int(100 * files_with_i18n / total_files) if total_files > 0 else 0
        )

        return {
            "total_files": total_files,
            "files_with_i18n": files_with_i18n,
            "files": self.results,
            "coverage_pct": coverage_pct,
        }


class RulePackCoverageChecker:
    """Checks bilingual annotation coverage in rule packs."""

    ANNOTATION_PAIRS = [
        ("summary", "summary_zh"),
        ("description", "description_zh"),
        ("platform_summary", "platform_summary_zh"),
    ]

    def __init__(self, rule_pack_dir: Path):
        self.rule_pack_dir = rule_pack_dir
        self.results: Dict[str, Dict] = {}

    def find_rule_packs(self) -> List[Path]:
        """Find all rule pack YAML files."""
        return sorted(self.rule_pack_dir.glob("rule-pack-*.yaml"))

    def check_alert(self, alert_name: str, alert_data: Dict) -> Dict[str, Tuple[bool, List[str]]]:
        """Check a single alert rule for bilingual annotations."""
        annotations = alert_data.get("annotations", {})
        if not annotations:
            return {}

        result = {}
        for eng_field, zh_field in self.ANNOTATION_PAIRS:
            has_eng = eng_field in annotations
            has_zh = zh_field in annotations

            if has_eng:
                if has_zh:
                    result[eng_field] = (True, [])
                else:
                    result[eng_field] = (False, [zh_field])

        return result

    def check_rule_pack(self, pack_path: Path) -> Dict:
        """Check all alerts in a rule pack file."""
        with open(pack_path, "r", encoding="utf-8") as f:
            try:
                data = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                return {"error": f"Failed to parse YAML: {e}"}

        groups = data.get("groups", [])
        pack_result = {
            "total_alerts": 0,
            "bilingual_alerts": 0,
            "monolingual_alerts": 0,
            "alerts": {}
        }

        for group in groups:
            rules = group.get("rules", [])
            for rule in rules:
                # Only check alert rules, not recording rules
                if "alert" not in rule:
                    continue

                alert_name = rule["alert"]
                pack_result["total_alerts"] += 1
                alert_check = self.check_alert(alert_name, rule)

                if alert_check:
                    # Count only if all English annotations have Chinese pairs
                    all_bilingual = all(
                        is_bilingual for is_bilingual, _ in alert_check.values()
                    )
                    if all_bilingual:
                        pack_result["bilingual_alerts"] += 1
                    else:
                        pack_result["monolingual_alerts"] += 1

                pack_result["alerts"][alert_name] = alert_check

        return pack_result

    def run(self) -> Dict:
        """Run rule pack layer check."""
        packs = self.find_rule_packs()

        if not packs:
            return {
                "total_packs": 0,
                "total_alerts": 0,
                "bilingual_alerts": 0,
                "packs": {},
                "coverage_pct": 0,
            }

        total_bilingual = 0
        total_alerts = 0

        for pack_path in packs:
            result = self.check_rule_pack(pack_path)
            self.results[pack_path.name] = result

            if "error" not in result:
                total_bilingual += result["bilingual_alerts"]
                total_alerts += result["total_alerts"]

        coverage_pct = (
            int(100 * total_bilingual / total_alerts) if total_alerts > 0 else 0
        )

        return {
            "total_packs": len(packs),
            "total_alerts": total_alerts,
            "bilingual_alerts": total_bilingual,
            "packs": self.results,
            "coverage_pct": coverage_pct,
        }


class PythonCLICoverageChecker:
    """Checks i18n coverage in Python CLI tools."""

    def __init__(self, scripts_dir: Path, components_dir: Path = None):
        self.scripts_dir = scripts_dir
        self.components_dir = components_dir
        self.results: Dict[str, Dict] = {}

    def find_python_files(self) -> List[Path]:
        """Find all Python files in scripts/tools and da-tools entrypoint."""
        files = list(self.scripts_dir.glob("*.py"))

        # Also check da-tools entrypoint
        if self.components_dir:
            entrypoint = self.components_dir / "da-tools" / "app" / "entrypoint.py"
            if entrypoint.exists():
                files.append(entrypoint)

        # Filter out private libraries
        files = [f for f in files if not f.name.startswith("_")]

        return sorted(files)

    def check_file(self, py_path: Path) -> Dict:
        """Check a single Python file for i18n support."""
        with open(py_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check for detect_cli_lang import and usage
        has_detect_lang = "detect_cli_lang" in content

        # Check for _HELP dict with bilingual structure
        has_help_dict = "_HELP" in content

        # Count bilingual help strings (entries with 'zh' and 'en' keys)
        help_pattern = r"['\"]zh['\"]:\s*['\"][^'\"]*['\"]"
        bilingual_helps = len(re.findall(help_pattern, content))

        return {
            "has_detect_lang": has_detect_lang,
            "has_help_dict": has_help_dict,
            "bilingual_help_strings": bilingual_helps,
            "is_bilingual": has_detect_lang and has_help_dict,
        }

    def run(self) -> Dict:
        """Run Python CLI layer check."""
        py_files = self.find_python_files()

        if not py_files:
            return {
                "total_tools": 0,
                "tools_with_i18n": 0,
                "tools": {},
                "coverage_pct": 0,
            }

        for py_path in py_files:
            rel_path = py_path.relative_to(py_path.parent.parent)
            if rel_path.parts[0] == "components":
                # For da-tools, use a nicer path
                rel_path = Path("da-tools/entrypoint.py")
            result = self.check_file(py_path)
            self.results[str(rel_path)] = result

        # Summary statistics
        tools_with_i18n = sum(1 for r in self.results.values() if r["is_bilingual"])
        total_tools = len(self.results)

        coverage_pct = (
            int(100 * tools_with_i18n / total_tools) if total_tools > 0 else 0
        )

        return {
            "total_tools": total_tools,
            "tools_with_i18n": tools_with_i18n,
            "tools": self.results,
            "coverage_pct": coverage_pct,
        }


def print_text_report(
    jsx_result: Dict,
    rule_pack_result: Dict,
    python_result: Dict,
    verbose: bool = False,
) -> int:
    """Print human-readable coverage report."""
    print("\n=== i18n Coverage Report ===\n")

    # Layer 1: JSX
    print("Layer 1: JSX Interactive Tools")
    if verbose:
        for file_name, file_result in jsx_result["files"].items():
            status = "✓" if file_result["has_t_function"] else "✗"
            if file_result["t_calls"] > 0:
                print(
                    f"  {status} {file_name:<45} "
                    f"{file_result['t_calls']:>3} t() calls"
                )
            else:
                print(f"  {status} {file_name:<45} (not started)")
    else:
        files_with_i18n = jsx_result["files_with_i18n"]
        total_files = jsx_result["total_files"]
        print(
            f"  {files_with_i18n}/{total_files} files with i18n "
            f"({jsx_result['coverage_pct']}%)"
        )

    print(f"  Coverage: {jsx_result['files_with_i18n']}/{jsx_result['total_files']} "
          f"files ({jsx_result['coverage_pct']}%)\n")

    # Layer 2: Rule Packs
    print("Layer 2: Rule Pack Bilingual Annotations")
    if verbose:
        for pack_name, pack_result in rule_pack_result["packs"].items():
            if "error" not in pack_result:
                bilingual = pack_result["bilingual_alerts"]
                total = pack_result["total_alerts"]
                if total > 0:
                    status = "✓" if bilingual == total else "✗"
                    pct = int(100 * bilingual / total)
                    print(
                        f"  {status} {pack_name:<40} "
                        f"{bilingual}/{total} rules ({pct}%)"
                    )
    else:
        total_alerts = rule_pack_result["total_alerts"]
        bilingual_alerts = rule_pack_result["bilingual_alerts"]
        if total_alerts > 0:
            print(
                f"  {bilingual_alerts}/{total_alerts} rules with full bilingual coverage "
                f"({rule_pack_result['coverage_pct']}%)"
            )

    print(f"  Coverage: {rule_pack_result['bilingual_alerts']}/{rule_pack_result['total_alerts']} "
          f"rules ({rule_pack_result['coverage_pct']}%)\n")

    # Layer 3: Python CLI
    print("Layer 3: Python CLI Help")
    if verbose:
        for tool_name, tool_result in python_result["tools"].items():
            if tool_result["is_bilingual"]:
                status = "✓"
                help_count = tool_result["bilingual_help_strings"]
                print(
                    f"  {status} {tool_name:<45} "
                    f"{help_count} help strings"
                )
            else:
                status = "✗"
                print(f"  {status} {tool_name:<45} (no i18n)")
    else:
        tools_with_i18n = python_result["tools_with_i18n"]
        total_tools = python_result["total_tools"]
        print(
            f"  {tools_with_i18n}/{total_tools} tools with i18n "
            f"({python_result['coverage_pct']}%)"
        )

    print(f"  Coverage: {python_result['tools_with_i18n']}/{python_result['total_tools']} "
          f"tools ({python_result['coverage_pct']}%)\n")

    # Overall
    total_coverage = (
        jsx_result["coverage_pct"]
        + rule_pack_result["coverage_pct"]
        + python_result["coverage_pct"]
    ) // 3
    print(f"Overall: {total_coverage}% average bilingual coverage\n")

    return 0


def print_json_report(
    jsx_result: Dict,
    rule_pack_result: Dict,
    python_result: Dict,
) -> int:
    """Print JSON report for badge generation."""
    report = {
        "timestamp": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "layers": {
            "jsx": {
                "files_with_i18n": jsx_result["files_with_i18n"],
                "total_files": jsx_result["total_files"],
                "coverage_pct": jsx_result["coverage_pct"],
            },
            "rule_packs": {
                "bilingual_alerts": rule_pack_result["bilingual_alerts"],
                "total_alerts": rule_pack_result["total_alerts"],
                "coverage_pct": rule_pack_result["coverage_pct"],
            },
            "python_cli": {
                "tools_with_i18n": python_result["tools_with_i18n"],
                "total_tools": python_result["total_tools"],
                "coverage_pct": python_result["coverage_pct"],
            },
        },
    }

    # Add overall average
    report["overall_pct"] = (
        jsx_result["coverage_pct"]
        + rule_pack_result["coverage_pct"]
        + python_result["coverage_pct"]
    ) // 3

    print(json.dumps(report, indent=2))
    return 0


def main():
    """CLI entry point: check_i18n_coverage.py."""
    parser = argparse.ArgumentParser(
        description="Check i18n coverage across JSX, Rule Packs, and Python CLI tools"
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: exit code 1 if any layer below threshold (L1=50%, L2=10%, L3=5%)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON for badge generation",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show per-file details",
    )

    args = parser.parse_args()

    # Determine base directories
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent.parent.parent
    docs_dir = repo_root / "docs"
    rule_pack_dir = repo_root / "rule-packs"
    scripts_dir = script_dir
    components_dir = repo_root / "components"

    # Check for required directories
    if not docs_dir.exists():
        print(f"Error: docs directory not found at {docs_dir}", file=sys.stderr)
        sys.exit(1)

    if not rule_pack_dir.exists():
        print(f"Error: rule-packs directory not found at {rule_pack_dir}", file=sys.stderr)
        sys.exit(1)

    if not scripts_dir.exists():
        print(f"Error: scripts/tools directory not found at {scripts_dir}", file=sys.stderr)
        sys.exit(1)

    # Run checks
    jsx_checker = JSXCoverageChecker(docs_dir)
    jsx_result = jsx_checker.run()

    rule_pack_checker = RulePackCoverageChecker(rule_pack_dir)
    rule_pack_result = rule_pack_checker.run()

    python_checker = PythonCLICoverageChecker(scripts_dir, components_dir)
    python_result = python_checker.run()

    # Output
    if args.json:
        exit_code = print_json_report(jsx_result, rule_pack_result, python_result)
    else:
        exit_code = print_text_report(
            jsx_result,
            rule_pack_result,
            python_result,
            verbose=args.verbose,
        )

    # CI mode: check thresholds
    if args.ci:
        thresholds = {
            "jsx": 50,
            "rule_packs": 10,
            "python_cli": 5,
        }

        if jsx_result["coverage_pct"] < thresholds["jsx"]:
            print(
                f"ERROR: JSX coverage {jsx_result['coverage_pct']}% < "
                f"threshold {thresholds['jsx']}%",
                file=sys.stderr,
            )
            exit_code = 1

        if rule_pack_result["coverage_pct"] < thresholds["rule_packs"]:
            print(
                f"ERROR: Rule Pack coverage {rule_pack_result['coverage_pct']}% < "
                f"threshold {thresholds['rule_packs']}%",
                file=sys.stderr,
            )
            exit_code = 1

        if python_result["coverage_pct"] < thresholds["python_cli"]:
            print(
                f"ERROR: Python CLI coverage {python_result['coverage_pct']}% < "
                f"threshold {thresholds['python_cli']}%",
                file=sys.stderr,
            )
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
