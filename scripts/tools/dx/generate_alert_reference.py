#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_alert_reference.py - Auto-generate ALERT-REFERENCE.md from Rule Pack YAML files.

Scans all Rule Pack YAML files (rule-pack-*.yaml), extracts alert rules, and generates
both Chinese and English ALERT-REFERENCE documents grouped by Rule Pack.

Usage:
    python3 generate_alert_reference.py [--dry-run] [--check] [--output-dir DIR]

Flags:
    --dry-run       Print to stdout instead of writing to file
    --check         Compare generated vs existing, exit 1 if different (CI drift detection)
    --output-dir    Directory to write output files (default: ./rule-packs)

SAST Rules:
    - File operations use absolute paths
    - YAML parsing uses safe_load
    - No shell execution
"""

import argparse
import os
import sys
import yaml
import difflib
from pathlib import Path
from typing import Dict, List, Tuple, Any

# Built-in mapping for recommended actions by alert pattern
RECOMMENDED_ACTIONS = {
    # Down/Absent patterns
    "Down": {
        "zh": "立即檢查伺服器狀態、網路連線；查看系統日誌",
        "en": "Immediately check server status and network connectivity; review system logs",
    },
    "Absent": {
        "zh": "確認相關元件已啟動、配置正確；檢查元件日誌",
        "en": "Verify component is running and configured correctly; check component logs",
    },
    # Connection patterns
    "HighConnections": {
        "zh": "檢查連線池配置、應用連線是否有洩漏；考慮增加最大連線數",
        "en": "Check connection pool configuration and potential leaks; consider increasing max connections",
    },
    "HighConnectionsCritical": {
        "zh": "立即介入，檢查活躍連線、殺掉閒置連線；考慮應用端限流",
        "en": "Immediate intervention required; check active connections, kill idle sessions; consider app-level throttling",
    },
    # Memory patterns
    "HighMemory": {
        "zh": "檢查資源消耗、優化配置；考慮增加記憶體或啟用壓縮",
        "en": "Check resource consumption and optimize configuration; consider increasing memory or enabling compression",
    },
    # Replication patterns
    "HighReplicationLag": {
        "zh": "檢查複寫狀態、網路連線；檢查複寫隊列堆積情況",
        "en": "Check replication status and network connectivity; inspect queue buildup",
    },
    "HighReplicationLagCritical": {
        "zh": "立即檢查副本健康狀態；考慮手動追趕或重新同步",
        "en": "Immediately check replica health; consider manual catch-up or resync",
    },
    # Query/Performance patterns
    "HighSlowQueries": {
        "zh": "檢查慢查詢日誌，找出優化候選；考慮調整相關參數",
        "en": "Check slow query logs, identify optimization candidates; consider parameter tuning",
    },
    "HighDeadlocks": {
        "zh": "分析死鎖查詢日誌、調整應用邏輯減少衝突；考慮增加鎖定超時時間",
        "en": "Analyze deadlock query logs, adjust application logic to reduce contention; consider increasing lock timeout",
    },
    # Default fallback
    "default": {
        "zh": "檢查告警指標、查看相關日誌；如需協助請聯絡平台團隊",
        "en": "Check alert metrics and review related logs; contact platform team for assistance if needed",
    },
}


def get_rule_pack_name(yaml_file: str) -> str:
    """Extract rule pack name from filename (e.g., rule-pack-mariadb.yaml -> mariadb)."""
    basename = os.path.basename(yaml_file)
    if basename.startswith("rule-pack-"):
        return basename.replace("rule-pack-", "").replace(".yaml", "")
    return basename.replace(".yaml", "")


def get_display_name(rule_pack: str) -> Dict[str, str]:
    """Get human-readable display names for rule packs."""
    names = {
        "mariadb": {"zh": "MariaDB Rule Pack", "en": "MariaDB Rule Pack"},
        "postgresql": {
            "zh": "PostgreSQL Rule Pack",
            "en": "PostgreSQL Rule Pack",
        },
        "redis": {"zh": "Redis Rule Pack", "en": "Redis Rule Pack"},
        "mongodb": {"zh": "MongoDB Rule Pack", "en": "MongoDB Rule Pack"},
        "elasticsearch": {
            "zh": "Elasticsearch Rule Pack",
            "en": "Elasticsearch Rule Pack",
        },
        "oracle": {"zh": "Oracle Database Rule Pack", "en": "Oracle Database Rule Pack"},
        "db2": {"zh": "DB2 Rule Pack", "en": "DB2 Rule Pack"},
        "clickhouse": {
            "zh": "ClickHouse Rule Pack",
            "en": "ClickHouse Rule Pack",
        },
        "kafka": {"zh": "Kafka Rule Pack", "en": "Kafka Rule Pack"},
        "rabbitmq": {"zh": "RabbitMQ Rule Pack", "en": "RabbitMQ Rule Pack"},
        "nginx": {"zh": "Nginx Rule Pack", "en": "Nginx Rule Pack"},
        "jvm": {"zh": "JVM Rule Pack", "en": "JVM Rule Pack"},
        "kubernetes": {
            "zh": "Kubernetes Rule Pack",
            "en": "Kubernetes Rule Pack",
        },
        "operational": {
            "zh": "Operational Rule Pack",
            "en": "Operational Rule Pack",
        },
    }
    return names.get(rule_pack, {"zh": rule_pack, "en": rule_pack})


def extract_alerts(yaml_content: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract alert rules from parsed YAML content."""
    alerts = []

    if "groups" not in yaml_content:
        return alerts

    for group in yaml_content["groups"]:
        if "rules" not in group:
            continue

        for rule in group["rules"]:
            # Skip recording rules, only process alert rules
            if "alert" not in rule:
                continue

            alert_name = rule["alert"]
            severity = rule.get("labels", {}).get("severity", "unknown")
            annotations = rule.get("annotations", {})

            # Extract description from annotations
            summary = annotations.get("summary", "")
            description = annotations.get("description", "")

            # Use platform_summary if available (for dual-perspective alerts)
            platform_summary = annotations.get("platform_summary", "")

            alerts.append(
                {
                    "name": alert_name,
                    "severity": severity,
                    "summary": summary,
                    "description": description,
                    "platform_summary": platform_summary,
                }
            )

    return alerts


def get_recommended_action(alert_name: str) -> Dict[str, str]:
    """Get recommended action based on alert name pattern matching."""
    for pattern in RECOMMENDED_ACTIONS:
        if pattern != "default" and pattern in alert_name:
            return RECOMMENDED_ACTIONS[pattern]

    return RECOMMENDED_ACTIONS["default"]


def get_metric_from_description(description: str) -> str:
    """Extract primary metric name from description if available."""
    # Simple extraction: look for metric patterns like "mysql_up", "pg_up", etc.
    import re

    match = re.search(r"\b([a-z_]+(?:_[a-z]+)*)\b", description)
    return match.group(1) if match else ""


def generate_markdown_zh(alerts_by_pack: Dict[str, List[Dict]]) -> str:
    """Generate Chinese ALERT-REFERENCE.md."""
    lines = [
        "---",
        'title: "Rule Pack 告警參考指南 (Alert Reference Guide)"',
        "tags: [alerts, reference, rule-packs]",
        "audience: [tenant, sre]",
        "version: v1.12.0",
        "lang: zh",
        "---",
        "# Rule Pack 告警參考指南 (Alert Reference Guide)",
        "",
        "本文件為租戶提供各 Rule Pack 中所有告警的統一參考，包括告警含義、觸發條件和建議動作。",
        "",
        "**注意**: 本指南僅涵蓋**使用者導向的閾值告警** (threshold alerts)。Operational Rule Pack 的 sentinel 告警為平台內部控制機制，不需要租戶操作。",
        "",
        "---",
        "",
    ]

    for pack_name in sorted(alerts_by_pack.keys()):
        display_name = get_display_name(pack_name)
        alerts = alerts_by_pack[pack_name]

        lines.append(f"## {display_name['zh']}")
        lines.append("")
        lines.append(
            "| 告警名稱 | 嚴重度 | 觸發條件 | 建議動作 | 相關指標 |"
        )
        lines.append("|---|---|---|---|---|")

        for alert in alerts:
            name = alert["name"]
            severity = alert["severity"]

            # Use platform_summary if available (more concise), else description
            trigger_condition = alert.get("platform_summary") or alert["description"]
            # Limit to first sentence or 100 chars
            if trigger_condition:
                sentences = trigger_condition.split("—")
                trigger_condition = sentences[0][:100]

            recommended_action = get_recommended_action(name)["zh"]

            # Extract metric from description
            metric = get_metric_from_description(alert["description"])
            if not metric:
                metric = ""

            lines.append(
                f"| {name} | {severity} | {trigger_condition} | {recommended_action} | {metric} |"
            )

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def generate_markdown_en(alerts_by_pack: Dict[str, List[Dict]]) -> str:
    """Generate English ALERT-REFERENCE.en.md."""
    lines = [
        "---",
        'title: "Rule Pack Alert Reference Guide"',
        "tags: [alerts, reference, rule-packs]",
        "audience: [tenant, sre]",
        "version: v1.12.0",
        "lang: en",
        "---",
        "# Rule Pack Alert Reference Guide",
        "",
        "This document provides tenants with a unified reference for all alerts across Rule Packs, including alert meanings, trigger conditions, and recommended actions.",
        "",
        "**Note**: This guide covers only **user-facing threshold alerts**. Sentinel alerts in the Operational Rule Pack are platform-internal control mechanisms and do not require tenant action.",
        "",
        "---",
        "",
    ]

    for pack_name in sorted(alerts_by_pack.keys()):
        display_name = get_display_name(pack_name)
        alerts = alerts_by_pack[pack_name]

        lines.append(f"## {display_name['en']}")
        lines.append("")
        lines.append("| Alert Name | Severity | Trigger Condition | Recommended Action | Related Metric |")
        lines.append("|---|---|---|---|---|")

        for alert in alerts:
            name = alert["name"]
            severity = alert["severity"]

            # Use platform_summary if available (more concise), else description
            trigger_condition = alert.get("platform_summary") or alert["description"]
            # Limit to first sentence or 100 chars
            if trigger_condition:
                sentences = trigger_condition.split("—")
                trigger_condition = sentences[0][:100]

            recommended_action = get_recommended_action(name)["en"]

            # Extract metric from description
            metric = get_metric_from_description(alert["description"])
            if not metric:
                metric = ""

            lines.append(
                f"| {name} | {severity} | {trigger_condition} | {recommended_action} | {metric} |"
            )

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def load_rule_packs(rule_packs_dir: str) -> Dict[str, List[Dict]]:
    """Load and parse all rule pack YAML files, return alerts grouped by pack."""
    alerts_by_pack = {}

    # Find all rule-pack-*.yaml files
    for yaml_file in sorted(Path(rule_packs_dir).glob("rule-pack-*.yaml")):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                content = yaml.safe_load(f)

            if content is None:
                continue

            pack_name = get_rule_pack_name(str(yaml_file))
            alerts = extract_alerts(content)

            if alerts:
                alerts_by_pack[pack_name] = alerts

        except yaml.YAMLError as e:
            print(f"Error parsing {yaml_file}: {e}", file=sys.stderr)
            sys.exit(1)
        except (OSError, yaml.YAMLError) as e:
            print(f"Error reading {yaml_file}: {e}", file=sys.stderr)
            sys.exit(1)

    return alerts_by_pack


def main():
    """CLI entry point: generate_alert_reference.py - Auto-generate ALERT-REFERENCE.md from Rule Pack YAML files."""
    parser = argparse.ArgumentParser(
        description="Auto-generate ALERT-REFERENCE.md from Rule Pack YAML files"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print to stdout instead of writing to file",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare generated vs existing, exit 1 if different (CI drift detection)",
    )
    parser.add_argument(
        "--output-dir",
        default="./rule-packs",
        help="Directory containing rule pack YAML files (default: ./rule-packs)",
    )

    args = parser.parse_args()

    # Resolve to absolute path
    rule_packs_dir = os.path.abspath(args.output_dir)

    if not os.path.isdir(rule_packs_dir):
        print(f"Error: {rule_packs_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Load rule packs
    alerts_by_pack = load_rule_packs(rule_packs_dir)

    if not alerts_by_pack:
        print("Error: No alerts found in rule packs", file=sys.stderr)
        sys.exit(1)

    # Generate markdown
    content_zh = generate_markdown_zh(alerts_by_pack)
    content_en = generate_markdown_en(alerts_by_pack)

    if args.dry_run:
        print("=== ALERT-REFERENCE.md (Chinese) ===")
        print(content_zh)
        print("\n\n=== ALERT-REFERENCE.en.md (English) ===")
        print(content_en)
        return

    # Define output paths
    file_zh = os.path.join(rule_packs_dir, "ALERT-REFERENCE.md")
    file_en = os.path.join(rule_packs_dir, "ALERT-REFERENCE.en.md")

    if args.check:
        # Check mode: compare generated vs existing
        differences = []

        # Check Chinese version
        if os.path.exists(file_zh):
            with open(file_zh, "r", encoding="utf-8") as f:
                existing_zh = f.read()
            if existing_zh != content_zh:
                diff = list(
                    difflib.unified_diff(
                        existing_zh.splitlines(keepends=True),
                        content_zh.splitlines(keepends=True),
                        fromfile="existing ALERT-REFERENCE.md",
                        tofile="generated ALERT-REFERENCE.md",
                    )
                )
                differences.extend(diff)
        else:
            print(
                f"Warning: {file_zh} does not exist, skipping Chinese version check",
                file=sys.stderr,
            )

        # Check English version
        if os.path.exists(file_en):
            with open(file_en, "r", encoding="utf-8") as f:
                existing_en = f.read()
            if existing_en != content_en:
                diff = list(
                    difflib.unified_diff(
                        existing_en.splitlines(keepends=True),
                        content_en.splitlines(keepends=True),
                        fromfile="existing ALERT-REFERENCE.en.md",
                        tofile="generated ALERT-REFERENCE.en.md",
                    )
                )
                differences.extend(diff)
        else:
            print(
                f"Warning: {file_en} does not exist, skipping English version check",
                file=sys.stderr,
            )

        if differences:
            print("ALERT-REFERENCE files are out of sync with rule packs:", file=sys.stderr)
            print("".join(differences), file=sys.stderr)
            sys.exit(1)
        else:
            print(
                "OK: ALERT-REFERENCE files are synchronized with rule packs",
                file=sys.stdout,
            )
            sys.exit(0)

    # Write mode: generate and write
    try:
        with open(file_zh, "w", encoding="utf-8") as f:
            f.write(content_zh)
        os.chmod(file_zh, 0o644)
        print(f"Generated {file_zh}")

        with open(file_en, "w", encoding="utf-8") as f:
            f.write(content_en)
        os.chmod(file_en, 0o644)
        print(f"Generated {file_en}")

    except IOError as e:
        print(f"Error writing output files: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
