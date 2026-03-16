#!/usr/bin/env python3
"""analyze_rule_pack_gaps.py — Rule Pack gap analysis for custom rules.

Analyzes custom_ rules in tenant configs and recommends official Rule Pack
substitutes by matching against the metric dictionary and rule pack catalog.

Usage:
  # Analyze a single tenant config
  python3 analyze_rule_pack_gaps.py --tenant-config conf.d/db-a.yaml

  # Analyze all tenant configs in a directory
  python3 analyze_rule_pack_gaps.py --config-dir conf.d/

  # Specify custom metric dictionary path
  python3 analyze_rule_pack_gaps.py --config-dir conf.d/ \
    --metric-dictionary scripts/tools/metric-dictionary.yaml

  # JSON output
  python3 analyze_rule_pack_gaps.py --config-dir conf.d/ --json

需求:
  - Tenant config YAML files (from threshold-config ConfigMap or local conf.d/)
  - metric-dictionary.yaml (bundled in da-tools container)
"""
import argparse
import glob
import json
import os
import re
import sys

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import load_yaml_file, write_json_secure  # noqa: E402

# ---------------------------------------------------------------------------
# Default paths (relative to script location for da-tools container)
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_METRIC_DICT = os.path.join(SCRIPT_DIR, "metric-dictionary.yaml")

# Rule pack prefixes mapped to pack names
RULE_PACK_PREFIXES = {
    "mariadb": ["mysql_", "mariadb_"],
    "postgresql": ["pg_", "postgres_"],
    "redis": ["redis_"],
    "mongodb": ["mongodb_"],
    "elasticsearch": ["elasticsearch_", "es_"],
    "oracle": ["oracle_", "oracledb_"],
    "db2": ["db2_"],
    "clickhouse": ["clickhouse_"],
    "kafka": ["kafka_"],
    "rabbitmq": ["rabbitmq_"],
    "kubernetes": ["kube_", "container_", "pod_", "node_"],
}


def load_tenant_configs(config_dir=None, tenant_config=None):
    """Load tenant configs from directory or single file.

    Returns dict: {tenant_name: {metric_key: value, ...}}
    """
    configs = {}

    if tenant_config:
        data = load_yaml_file(tenant_config, default={})
        name = os.path.basename(tenant_config).removesuffix(".yaml")
        configs[name] = data or {}
    elif config_dir:
        for path in sorted(glob.glob(os.path.join(config_dir, "*.yaml"))):
            basename = os.path.basename(path)
            if basename.startswith("_"):
                continue  # Skip _defaults.yaml etc.
            data = load_yaml_file(path, default={})
            name = basename.removesuffix(".yaml")
            configs[name] = data or {}

    return configs


def extract_custom_metrics(configs):
    """Extract metrics with custom_ prefix from tenant configs.

    Returns list of dicts: [{tenant, metric_key, value, original_metric}, ...]
    """
    custom_metrics = []
    for tenant, data in configs.items():
        for key, value in data.items():
            if key.startswith("_"):
                continue  # Skip reserved keys
            if key.startswith("custom_"):
                # Strip custom_ prefix to get original metric name
                original = key.removeprefix("custom_")
                custom_metrics.append({
                    "tenant": tenant,
                    "metric_key": key,
                    "value": value,
                    "original_metric": original,
                })
    return custom_metrics


def load_metric_dictionary(path):
    """Load metric dictionary for matching.

    Returns dict: {metric_name: {pack, description, ...}}
    """
    data = load_yaml_file(path, default={})
    if not data:
        return {}
    # The metric dictionary maps golden_name → {original, description, ...}
    # We need to build a reverse lookup
    lookup = {}
    for golden_name, info in data.items():
        if isinstance(info, dict):
            pack = info.get("rule_pack", "")
            lookup[golden_name] = {
                "pack": pack,
                "description": info.get("description", ""),
                "golden_name": golden_name,
            }
            # Also index the original metric name
            original = info.get("original_metric", "")
            if original and original != golden_name:
                lookup[original] = {
                    "pack": pack,
                    "description": info.get("description", ""),
                    "golden_name": golden_name,
                }
    return lookup


def match_by_prefix(metric_name):
    """Match a metric to a Rule Pack by prefix heuristic.

    Returns (pack_name, confidence) or (None, 0.0).
    """
    metric_lower = metric_name.lower()
    for pack, prefixes in RULE_PACK_PREFIXES.items():
        for prefix in prefixes:
            if metric_lower.startswith(prefix):
                return pack, 0.7
    return None, 0.0


def tokenize(name):
    """Split metric name into tokens for fuzzy matching."""
    # Split on _ and camelCase boundaries
    parts = re.split(r"[_\-]+", name.lower())
    return set(parts)


def token_overlap_score(name_a, name_b):
    """Compute Jaccard similarity between tokenized metric names."""
    tokens_a = tokenize(name_a)
    tokens_b = tokenize(name_b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def analyze_gaps(custom_metrics, metric_dict):
    """Analyze each custom metric against official Rule Packs.

    Returns list of gap analysis results.
    """
    results = []

    for cm in custom_metrics:
        original = cm["original_metric"]
        best_match = None
        confidence = 0.0
        match_type = "none"

        # 1. Exact match in metric dictionary
        if original in metric_dict:
            best_match = metric_dict[original]
            confidence = 1.0
            match_type = "exact"
        else:
            # 2. Prefix match
            pack, conf = match_by_prefix(original)
            if pack:
                best_match = {"pack": pack, "golden_name": None, "description": ""}
                confidence = conf
                match_type = "prefix"

            # 3. Token overlap against dictionary entries (find best)
            if not best_match or confidence < 0.8:
                best_score = confidence
                for dict_name, info in metric_dict.items():
                    score = token_overlap_score(original, dict_name)
                    if score > best_score:
                        best_score = score
                        best_match = info
                        match_type = "fuzzy"
                confidence = max(confidence, best_score)

        # Build recommendation
        if best_match and confidence >= 0.7:
            recommendation = (
                f"Consider official Rule Pack '{best_match['pack']}'"
            )
            if best_match.get("golden_name"):
                recommendation += f" (golden metric: {best_match['golden_name']})"
        elif best_match and confidence >= 0.4:
            recommendation = (
                f"Possible match in '{best_match['pack']}' (low confidence)"
            )
        else:
            recommendation = "No official substitute found — keep as custom rule"

        results.append({
            "tenant": cm["tenant"],
            "custom_metric": cm["metric_key"],
            "original_metric": original,
            "current_value": cm["value"],
            "best_match_pack": best_match["pack"] if best_match else None,
            "golden_name": best_match.get("golden_name") if best_match else None,
            "confidence": round(confidence, 2),
            "match_type": match_type,
            "recommendation": recommendation,
        })

    return results


def print_report(results):
    """Print human-readable gap analysis report."""
    if not results:
        print("No custom_ metrics found in tenant configs.")
        return

    # Summary
    exact = sum(1 for r in results if r["match_type"] == "exact")
    prefix = sum(1 for r in results if r["match_type"] == "prefix")
    fuzzy = sum(1 for r in results if r["match_type"] == "fuzzy")
    no_match = sum(1 for r in results if r["match_type"] == "none")
    total = len(results)

    print()
    print("=" * 60)
    print("  Rule Pack Gap Analysis")
    print("=" * 60)
    print()
    print(f"  Total custom_ metrics: {total}")
    print(f"    Exact match:  {exact} (can migrate to official Rule Pack)")
    print(f"    Prefix match: {prefix} (likely covered by Rule Pack)")
    print(f"    Fuzzy match:  {fuzzy} (possible substitute)")
    print(f"    No match:     {no_match} (keep as custom)")
    print()

    # Grouped by pack
    by_pack = {}
    for r in results:
        pack = r["best_match_pack"] or "(no match)"
        by_pack.setdefault(pack, []).append(r)

    for pack, items in sorted(by_pack.items()):
        print(f"  [{pack}]")
        for item in items:
            conf = item["confidence"]
            mt = item["match_type"]
            print(f"    {item['custom_metric']} -> {item.get('golden_name', '?')} "
                  f"({mt}, {conf:.0%})")
        print()

    # Actionable recommendations
    migratable = [r for r in results if r["confidence"] >= 0.7]
    if migratable:
        print(f"  {len(migratable)} metric(s) can be replaced with official Rule Packs.")
        print("  Run 'da-tools scaffold --catalog' to see available packs.")
    print()


def main():
    """CLI entry point: Rule Pack gap analysis for custom rules."""
    parser = argparse.ArgumentParser(
        description="Analyze custom_ rules and recommend official Rule Pack substitutes",
    )
    parser.add_argument(
        "--config-dir",
        help="Directory with tenant config YAML files",
    )
    parser.add_argument(
        "--tenant-config",
        help="Single tenant config YAML file",
    )
    parser.add_argument(
        "--metric-dictionary", default=DEFAULT_METRIC_DICT,
        help=f"Path to metric-dictionary.yaml (default: {DEFAULT_METRIC_DICT})",
    )
    parser.add_argument(
        "--output", "-o",
        help="Write JSON report to file",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output JSON only",
    )
    args = parser.parse_args()

    if not args.config_dir and not args.tenant_config:
        print("ERROR: Specify --config-dir or --tenant-config", file=sys.stderr)
        sys.exit(1)

    # Load data
    configs = load_tenant_configs(
        config_dir=args.config_dir,
        tenant_config=args.tenant_config,
    )
    custom_metrics = extract_custom_metrics(configs)
    metric_dict = load_metric_dictionary(args.metric_dictionary)

    # Analyze
    results = analyze_gaps(custom_metrics, metric_dict)

    # Output
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print_report(results)

    if args.output:
        write_json_secure(args.output, results)
        if not args.json:
            print(f"  JSON report: {args.output}")


if __name__ == "__main__":
    main()
