#!/usr/bin/env python3
"""
check_bilingual_annotations.py

Validates bilingual annotations in Prometheus rule pack YAML files.
Checks for presence of Chinese (*_zh) annotation fields alongside English annotations.

Usage:
  python check_bilingual_annotations.py --check
  python check_bilingual_annotations.py --coverage
  python check_bilingual_annotations.py --check --only rule-pack-mariadb,rule-pack-postgresql
  python check_bilingual_annotations.py --ci
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


class BilingualAnnotationChecker:
    """Checks bilingual annotations in rule packs."""

    # Annotation pairs to check (English -> Chinese)
    ANNOTATION_PAIRS = [
        ("summary", "summary_zh"),
        ("description", "description_zh"),
        ("platform_summary", "platform_summary_zh"),
    ]

    def __init__(self, rule_pack_dir: Path):
        self.rule_pack_dir = rule_pack_dir
        self.results: Dict[str, Dict] = {}

    def find_rule_packs(self, only_packs: List[str] = None) -> List[Path]:
        """Find all rule pack YAML files."""
        packs = list(self.rule_pack_dir.glob("rule-pack-*.yaml"))

        if only_packs:
            # Filter by specified pack names
            filter_set = set(only_packs)
            packs = [p for p in packs if p.stem in filter_set]

        return sorted(packs)

    def check_alert(
        self, alert_name: str, alert_data: Dict
    ) -> Dict[str, Tuple[bool, List[str]]]:
        """
        Check a single alert rule for bilingual annotations.

        Returns dict: {annotation_pair: (has_chinese, missing_fields)}
        """
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

    def run_check(self, only_packs: List[str] = None) -> int:
        """Run check mode and return exit code."""
        packs = self.find_rule_packs(only_packs)

        if not packs:
            print("Error: No rule packs found", file=sys.stderr)
            return 1

        exit_code = 0
        for pack_path in packs:
            result = self.check_rule_pack(pack_path)
            self.results[pack_path.name] = result

            if "error" in result:
                print(f"ERROR in {pack_path.name}: {result['error']}", file=sys.stderr)
                exit_code = 1
                continue

            # Check for missing bilingual annotations
            has_missing = False
            for alert_name, alert_check in result["alerts"].items():
                for field, (is_bilingual, missing) in alert_check.items():
                    if not is_bilingual and missing:
                        if not has_missing:
                            print(f"\n{pack_path.name}:")
                            has_missing = True
                        print(f"  {alert_name}: missing {missing}")
                        exit_code = 1

        return exit_code

    def print_coverage(self, only_packs: List[str] = None):
        """Print coverage summary."""
        packs = self.find_rule_packs(only_packs)

        if not packs:
            print("Error: No rule packs found", file=sys.stderr)
            return

        print("\nBilingual Annotation Coverage Report\n")
        print(f"{'Pack Name':<40} {'Bilingual':<15} {'Coverage':<15}")
        print("-" * 70)

        total_bilingual = 0
        total_alerts = 0

        for pack_path in packs:
            result = self.check_rule_pack(pack_path)
            self.results[pack_path.name] = result

            if "error" in result:
                print(f"{pack_path.name:<40} ERROR", file=sys.stderr)
                continue

            bilingual = result["bilingual_alerts"]
            total = result["total_alerts"]
            total_bilingual += bilingual
            total_alerts += total

            if total > 0:
                coverage = f"{bilingual}/{total}"
                percent = f"{(bilingual/total*100):.1f}%"
            else:
                coverage = "0/0"
                percent = "N/A"

            status = "✓" if bilingual == total else " "
            print(f"{status} {pack_path.name:<38} {coverage:<15} {percent:<15}")

        print("-" * 70)
        if total_alerts > 0:
            overall_percent = f"{(total_bilingual/total_alerts*100):.1f}%"
        else:
            overall_percent = "N/A"
        print(f"{'TOTAL':<40} {total_bilingual}/{total_alerts:<13} {overall_percent:<15}")

    def run_ci_mode(self, only_packs: List[str] = None) -> int:
        """Run in CI mode: check + coverage."""
        exit_code = self.run_check(only_packs)
        print()
        self.print_coverage(only_packs)
        return exit_code


def main():
    parser = argparse.ArgumentParser(
        description="Check bilingual annotations in Prometheus rule packs"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check mode: fail if any rule pack is missing *_zh annotations"
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Coverage mode: print summary table of bilingual annotation coverage"
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: run both check and coverage"
    )
    parser.add_argument(
        "--only",
        type=str,
        help="Check only specified packs (comma-separated, e.g., rule-pack-mariadb,rule-pack-postgresql)"
    )

    args = parser.parse_args()

    # Determine rule pack directory
    script_dir = Path(__file__).parent
    rule_pack_dir = script_dir.parent.parent / "rule-packs"

    if not rule_pack_dir.exists():
        print(f"Error: rule-packs directory not found at {rule_pack_dir}", file=sys.stderr)
        sys.exit(1)

    only_packs = None
    if args.only:
        only_packs = [p.strip() for p in args.only.split(",")]

    checker = BilingualAnnotationChecker(rule_pack_dir)

    if args.check:
        return checker.run_check(only_packs)
    elif args.coverage:
        checker.print_coverage(only_packs)
        return 0
    elif args.ci:
        return checker.run_ci_mode(only_packs)
    else:
        # Default to coverage if no mode specified
        checker.print_coverage(only_packs)
        return 0


if __name__ == "__main__":
    sys.exit(main())
