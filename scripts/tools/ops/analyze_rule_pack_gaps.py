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
import os
import re
import sys

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_io import find_metric_dictionary, safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_io import load_yaml_file_strict_exporter_keys  # noqa: E402  (#2231)
from _lib_python import (  # noqa: E402
    exit_on_yaml_file_error,
    format_json_report,
    load_yaml_file,
    write_json_or_die,
)
from _lib_tenant_values import (  # noqa: E402  (#2115)
    exit_on_served_values_error,
    load_served_tree,
    print_load_warnings,
)
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402

# ---------------------------------------------------------------------------
# Default paths (relative to script location for da-tools container)
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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

    - Directory branch (#2115): the threshold keys the exporter's /metrics
      serves per tenant, with the served value (``da-guard served-values``).
      A value inherited from ``defaults:``, a platform ``tenants:`` block or
      a subtree counts, and a nested tenant is seen; a key switched off
      (``disable``) or with no row on /metrics (e.g. no default for it) does
      not. A file the load serves no tenant from (no ``tenants:`` mapping)
      is named on stderr, not read as a tenant; whatever da-guard prints on
      stderr is passed on line by line, each behind ``DA_GUARD_PREFIX``.
      da-guard missing or failing, or a file the load cannot decode, raises
      (the CLI exits 2).
    - Single-file branch: unchanged — the file as written. It unwraps the
      ``tenants:`` wrapper, and derives the flat-format tenant name via
      ``splitext`` (so ``x.yml`` → ``x``). It is not switched (owner, #2115):
      one file is not a tree /metrics serves.

    Returns dict: {tenant_name: {metric_key: value, ...}}
    """
    configs = {}

    if tenant_config:
        # #2114: tenant ids as source text, as the directory branch reads them.
        # #2231: and strictly, as it does — a key written twice is a file the
        # exporter drops, so it exits 2 like bad syntax.
        data = load_yaml_file_strict_exporter_keys(tenant_config, default={})
        if isinstance(data, dict) and isinstance(data.get("tenants"), dict):
            # Multi-tenant wrapper format (mirrors _lib_io.load_tenant_configs)
            for t_name, t_data in data["tenants"].items():
                if isinstance(t_data, dict):
                    configs[t_name] = t_data
        else:
            name = os.path.splitext(os.path.basename(tenant_config))[0]
            # Non-dict yaml (list/scalar) → {}, matching the dir branch
            # (_lib_io skips non-dict); extract_custom_metrics needs .items().
            configs[name] = data if isinstance(data, dict) else {}
    elif config_dir:
        tree = load_served_tree(config_dir)
        print_load_warnings(tree)
        configs = {t: {k: served.values[k] for k in served.severities}
                   for t, served in tree.tenants.items()}

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
        print(f"  [{safe_label(pack)}]")
        for item in items:
            conf = item["confidence"]
            mt = item["match_type"]
            print(f"    {safe_label(item['custom_metric'])} -> "
                  f"{safe_label(item.get('golden_name', '?'))} "
                  f"({safe_label(mt)}, {conf:.0%})")
        print()

    # Actionable recommendations
    migratable = [r for r in results if r["confidence"] >= 0.7]
    if migratable:
        print(f"  {len(migratable)} metric(s) can be replaced with official Rule Packs.")
        print("  Run 'da-tools scaffold --catalog' to see available packs.")
    print()


@exit_on_yaml_file_error  # #1654: unreadable tenant/dictionary file → rc 2, named
@exit_on_served_values_error  # #2115: da-guard missing or failing → rc 2, named
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
        "--metric-dictionary",
        help="Path to metric-dictionary.yaml (default: the one shipped beside "
             "the tool, or one level up in the repository layout)",
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
        sys.exit(EXIT_CALLER_ERROR)
    # A path that does not exist used to read as "no custom_ metrics found"
    # at rc=0 — indistinguishable from a real, clean scan (issue 1513).
    for flag, path, exists in (("--config-dir", args.config_dir, os.path.isdir),
                               ("--tenant-config", args.tenant_config, os.path.isfile),
                               ("--metric-dictionary", args.metric_dictionary,
                                os.path.isfile)):
        if path and not exists(path):
            kind = "directory" if flag == "--config-dir" else "file"
            print(f"ERROR: {flag} {safe_label(path)}: no such {kind}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
    dictionary_path = args.metric_dictionary or find_metric_dictionary(SCRIPT_DIR)
    if dictionary_path is None:
        print("WARN: metric-dictionary.yaml not found beside the tool or one level "
              "up; matches fall back to prefix and token heuristics. "
              "Pass --metric-dictionary to use one.", file=sys.stderr)

    # Load data
    configs = load_tenant_configs(
        config_dir=args.config_dir,
        tenant_config=args.tenant_config,
    )
    custom_metrics = extract_custom_metrics(configs)
    metric_dict = load_metric_dictionary(dictionary_path) if dictionary_path else {}

    # Analyze
    results = analyze_gaps(custom_metrics, metric_dict)

    # Output
    if args.json:
        print(format_json_report(results))
    else:
        print_report(results)

    if args.output:
        # #1641: an unwritable -o is rc=2 + one line, not a traceback at rc=1.
        write_json_or_die(args.output, results, flag="-o/--output")
        if not args.json:
            print(f"  JSON report: {args.output}")


if __name__ == "__main__":
    main()
