#!/usr/bin/env python3
"""generate_rule_pack_stats.py — Rule Pack 統計單一來源產生器

從 rule-packs/*.yaml 和 k8s/03-monitoring/configmap-rules-*.yaml
讀取實際規則數量，產生 Markdown include 片段供文件引用。

用法:
  python3 scripts/tools/generate_rule_pack_stats.py              # 印出統計
  python3 scripts/tools/generate_rule_pack_stats.py --check      # CI 模式 (exit 1 on drift)
  python3 scripts/tools/generate_rule_pack_stats.py --json       # JSON 輸出
  python3 scripts/tools/generate_rule_pack_stats.py --generate   # 產生 docs/includes/rule-pack-stats.md
"""
import argparse
import json
import os
import stat
import sys
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

RULE_PACKS_DIR = REPO_ROOT / "rule-packs"
K8S_RULES_DIR = REPO_ROOT / "k8s" / "03-monitoring"
INCLUDE_DIR = REPO_ROOT / "docs" / "includes"

# Exporter name mapping (pack name → display name + exporter)
PACK_META = {
    "mariadb": ("MariaDB", "mysqld_exporter (Percona)"),
    "postgresql": ("PostgreSQL", "postgres_exporter"),
    "kubernetes": ("Kubernetes", "cAdvisor + kube-state-metrics"),
    "redis": ("Redis", "redis_exporter"),
    "mongodb": ("MongoDB", "mongodb_exporter"),
    "elasticsearch": ("Elasticsearch", "elasticsearch_exporter"),
    "oracle": ("Oracle", "oracledb_exporter"),
    "db2": ("DB2", "db2_exporter"),
    "clickhouse": ("ClickHouse", "clickhouse_exporter"),
    "kafka": ("Kafka", "kafka_exporter"),
    "rabbitmq": ("RabbitMQ", "rabbitmq_exporter"),
    "jvm": ("JVM", "jmx_exporter"),
    "nginx": ("Nginx", "nginx-prometheus-exporter"),
    "operational": ("Operational", "threshold-exporter 運營模式"),
    "platform": ("Platform", "threshold-exporter 自監控"),
}

# Bilingual table headers / totals
LANG_STRINGS = {
    "zh": {
        "header": "| 規則包 | Exporter | Recording | Alert |",
        "sep": "|--------|----------|-----------|-------|",
        "total": "合計",
        "operational_exporter": "threshold-exporter 運營模式",
        "platform_exporter": "threshold-exporter 自監控",
    },
    "en": {
        "header": "| Rule Pack | Exporter | Recording | Alert |",
        "sep": "|-----------|----------|-----------|-------|",
        "total": "Total",
        "operational_exporter": "threshold-exporter operational mode",
        "platform_exporter": "threshold-exporter self-monitoring",
    },
}


def _get_stats_file(lang: str) -> Path:
    """Return output path for a given language."""
    if lang == "en":
        return INCLUDE_DIR / "rule-pack-stats.en.md"
    return INCLUDE_DIR / "rule-pack-stats.md"


def count_rules_in_yaml(filepath: Path) -> tuple:
    """Count recording and alert rules in a YAML file.

    Returns (recording_count, alert_count).
    """
    data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
    rec = alert = 0
    if data and "groups" in data:
        for g in data["groups"]:
            for r in g.get("rules", []):
                if "alert" in r:
                    alert += 1
                elif "record" in r:
                    rec += 1
    return rec, alert


def count_rules_in_configmap(filepath: Path) -> tuple:
    """Count rules inside a Kubernetes ConfigMap YAML.

    Returns (recording_count, alert_count).
    """
    data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
    rec = alert = 0
    if data and data.get("kind") == "ConfigMap":
        for _key, inner_yaml in data.get("data", {}).items():
            inner = yaml.safe_load(inner_yaml)
            if inner and "groups" in inner:
                for g in inner["groups"]:
                    for r in g.get("rules", []):
                        if "alert" in r:
                            alert += 1
                        elif "record" in r:
                            rec += 1
    return rec, alert


def gather_stats() -> dict:
    """Gather Rule Pack statistics from source YAML files.

    Returns dict with pack_count, recording, alert, total, per_pack.
    """
    packs = {}

    # rule-packs/ directory
    for f in sorted(RULE_PACKS_DIR.glob("rule-pack-*.yaml")):
        name = f.stem.replace("rule-pack-", "")
        rec, alert = count_rules_in_yaml(f)
        packs[name] = {"recording": rec, "alert": alert}

    # k8s ConfigMaps (may have additional alert rules)
    for f in sorted(K8S_RULES_DIR.glob("configmap-rules-*.yaml")):
        name = f.stem.replace("configmap-rules-", "")
        rec, alert = count_rules_in_configmap(f)
        if name not in packs:
            packs[name] = {"recording": rec, "alert": alert}
        else:
            packs[name]["recording"] = max(packs[name]["recording"], rec)
            packs[name]["alert"] = max(packs[name]["alert"], alert)

    total_rec = sum(p["recording"] for p in packs.values())
    total_alert = sum(p["alert"] for p in packs.values())

    return {
        "pack_count": len(packs),
        "recording": total_rec,
        "alert": total_alert,
        "total": total_rec + total_alert,
        "per_pack": packs,
    }


def generate_markdown_table(stats: dict, lang: str = "zh") -> str:
    """Generate a Markdown table from Rule Pack stats."""
    s = LANG_STRINGS[lang]
    lines = [
        "<!-- Auto-generated by generate_rule_pack_stats.py — DO NOT EDIT -->",
        "",
        s["header"],
        s["sep"],
    ]

    for name, counts in stats["per_pack"].items():
        display, exporter = PACK_META.get(name, (name, name))
        # Language-specific exporter names
        if name == "operational":
            exporter = s["operational_exporter"]
        elif name == "platform":
            exporter = s["platform_exporter"]
        lines.append(
            f"| {display.lower() if display == name else name} "
            f"| {exporter} "
            f"| {counts['recording']} "
            f"| {counts['alert']} |"
        )

    lines.append(
        f"| **{s['total']}** | "
        f"| **{stats['recording']}** "
        f"| **{stats['alert']}** |"
    )
    lines.append("")

    return "\n".join(lines)


def format_summary(stats: dict) -> str:
    """Format a badge-like one-line summary of Rule Pack statistics.

    Output: "15 packs | 139R 99A | 238 rules"
    """
    return (f"{stats['pack_count']} packs | "
            f"{stats['recording']}R {stats['alert']}A | "
            f"{stats['total']} rules")


def main():
    """CLI entry point: Rule Pack 統計單一來源產生器."""
    parser = argparse.ArgumentParser(
        description="Generate Rule Pack statistics from source YAML files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--check", action="store_true",
                        help="CI mode: exit 1 if include file is outdated")
    parser.add_argument("--json", action="store_true",
                        help="Output stats as JSON")
    parser.add_argument("--generate", action="store_true",
                        help="Generate docs/includes/rule-pack-stats[.en].md")
    parser.add_argument("--lang", choices=["zh", "en", "all"], default="zh",
                        help="Language: zh (default), en, or all")
    parser.add_argument("--format", choices=["table", "summary"],
                        default="table", dest="output_format",
                        help="Output format: table (default) or summary (one-line badge)")

    args = parser.parse_args()
    stats = gather_stats()
    langs = ["zh", "en"] if args.lang == "all" else [args.lang]

    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return

    if args.output_format == "summary":
        print(format_summary(stats))
        return

    if not args.json and not args.check and not args.generate:
        print(f"Rule Pack Stats (from source YAML):")
        print(f"  Packs:     {stats['pack_count']}")
        print(f"  Recording: {stats['recording']}")
        print(f"  Alert:     {stats['alert']}")
        print(f"  Total:     {stats['total']}")
        print()
        for name, counts in stats["per_pack"].items():
            print(f"  {name:20s} {counts['recording']:3d}R  {counts['alert']:3d}A")
        return

    has_drift = False

    for lang in langs:
        table = generate_markdown_table(stats, lang=lang)
        stats_file = _get_stats_file(lang)

        if args.generate:
            INCLUDE_DIR.mkdir(parents=True, exist_ok=True)
            stats_file.write_text(table, encoding="utf-8")
            os.chmod(stats_file,
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                     | stat.S_IROTH)
            print(f"✅ Generated {stats_file.relative_to(REPO_ROOT)}")

        elif args.check:
            if not stats_file.exists():
                print(f"❌ {stats_file.relative_to(REPO_ROOT)} does not exist. "
                      f"Run with --generate first.")
                has_drift = True
                continue

            existing = stats_file.read_text(encoding="utf-8")
            if existing.strip() != table.strip():
                print(f"❌ {stats_file.relative_to(REPO_ROOT)} is outdated. "
                      f"Run with --generate to update.")
                has_drift = True
            else:
                print(f"✅ {stats_file.relative_to(REPO_ROOT)} is up to date.")

    if args.check and has_drift:
        sys.exit(1)


if __name__ == "__main__":
    main()
