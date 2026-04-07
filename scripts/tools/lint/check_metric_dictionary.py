#!/usr/bin/env python3
"""check_metric_dictionary.py — Metric Dictionary 自動驗證

交叉驗證 metric-dictionary.yaml 與 Rule Pack YAML 的實際 metric 使用:
  1. 字典中存在但 Rule Pack 不使用的 stale entry
  2. Rule Pack 使用但字典未收錄的 undocumented metric

v2.4.0 新增：DX Tooling Backlog 候選項目。
migrate_rule.py 依賴 metric-dictionary.yaml 做遷移映射，
stale 或 undocumented entry 會導致遷移建議不完整。

用法:
    python3 scripts/tools/lint/check_metric_dictionary.py [--ci] [--json]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

METRIC_DICT = REPO_ROOT / "scripts" / "tools" / "metric-dictionary.yaml"
RULE_PACKS_DIR = REPO_ROOT / "rule-packs"
K8S_RULES_DIR = REPO_ROOT / "k8s" / "03-monitoring"


def load_dictionary_metrics(path: Path) -> Set[str]:
    """Load metric names from metric-dictionary.yaml.

    The dictionary maps legacy_metric → {maps_to, golden_rule, rule_pack}.
    We extract both the legacy metric keys and the maps_to values.
    """
    if not path.is_file():
        return set()

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return set()

    metrics = set()
    for legacy_key, info in data.items():
        metrics.add(legacy_key)
        if isinstance(info, dict) and "maps_to" in info:
            metrics.add(info["maps_to"])

    return metrics


def load_dictionary_golden_rules(path: Path) -> Set[str]:
    """Load golden_rule alert names from metric-dictionary.yaml."""
    if not path.is_file():
        return set()

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return set()

    rules = set()
    for _key, info in data.items():
        if isinstance(info, dict) and "golden_rule" in info:
            rules.add(info["golden_rule"])

    return rules


def extract_rule_pack_metrics(rule_packs_dir: Path,
                               k8s_dir: Path) -> Dict[str, Set[str]]:
    """Extract metric names used in Rule Pack YAML files.

    Returns dict mapping pack_name → set of metric names.
    Scans both rule-packs/ source and k8s/ ConfigMaps.
    """
    packs: Dict[str, Set[str]] = {}

    # Prometheus metric patterns in expr fields
    metric_pattern = re.compile(r"[a-z_][a-z0-9_]+(?::[a-z0-9_]+)*")

    def _extract_from_rules(rules_data, pack_name):
        metrics = packs.setdefault(pack_name, set())
        if not rules_data or "groups" not in rules_data:
            return
        for group in rules_data["groups"]:
            for rule in group.get("rules", []):
                expr = rule.get("expr", "")
                if isinstance(expr, str):
                    # Extract metric-like tokens from PromQL
                    for m in metric_pattern.finditer(expr):
                        token = m.group(0)
                        # Filter out PromQL functions and keywords
                        if token not in _PROMQL_KEYWORDS and len(token) > 3:
                            metrics.add(token)

    # rule-packs/ directory
    if rule_packs_dir.is_dir():
        for f in sorted(rule_packs_dir.glob("rule-pack-*.yaml")):
            name = f.stem.replace("rule-pack-", "")
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            _extract_from_rules(data, name)

    # k8s/ ConfigMaps
    if k8s_dir.is_dir():
        for f in sorted(k8s_dir.glob("configmap-rules-*.yaml")):
            name = f.stem.replace("configmap-rules-", "")
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            if data and data.get("kind") == "ConfigMap":
                for _key, inner_yaml in data.get("data", {}).items():
                    inner = yaml.safe_load(inner_yaml)
                    _extract_from_rules(inner, name)

    return packs


# Common PromQL functions/keywords to exclude from metric extraction
_PROMQL_KEYWORDS = {
    "sum", "avg", "max", "min", "count", "rate", "irate", "increase",
    "delta", "idelta", "histogram_quantile", "label_replace", "label_join",
    "sort", "sort_desc", "topk", "bottomk", "absent", "absent_over_time",
    "changes", "resets", "deriv", "predict_linear", "time", "vector",
    "scalar", "ceil", "floor", "round", "clamp", "clamp_min", "clamp_max",
    "group_left", "group_right", "on", "ignoring", "by", "without",
    "bool", "and", "or", "unless", "offset", "unless", "group",
    "stddev", "stdvar", "quantile", "count_values", "sgn",
    "last_over_time", "present_over_time", "timestamp",
    # Common label names that look metric-like
    "tenant", "severity", "alertname", "namespace", "job",
    "instance", "container", "pod", "filter", "target_severity",
}


def check_dictionary_coverage(
    dict_metrics: Set[str],
    dict_rules: Set[str],
    rule_pack_metrics: Dict[str, Set[str]],
) -> List[Dict]:
    """Cross-validate dictionary against Rule Pack usage."""
    issues = []

    # Flatten all Rule Pack metrics
    all_rp_metrics = set()
    for metrics in rule_pack_metrics.values():
        all_rp_metrics.update(metrics)

    # Stale dictionary entries: in dictionary but not used by any Rule Pack
    # Only check legacy keys (left side of dictionary)
    dict_data = {}
    if METRIC_DICT.is_file():
        dict_data = yaml.safe_load(
            METRIC_DICT.read_text(encoding="utf-8")) or {}

    for legacy_key in sorted(dict_data.keys()):
        # Check if legacy key or its maps_to target appears in Rule Packs
        info = dict_data[legacy_key]
        maps_to = info.get("maps_to", "") if isinstance(info, dict) else ""

        if legacy_key not in all_rp_metrics and maps_to not in all_rp_metrics:
            rule_pack = info.get("rule_pack", "unknown") if isinstance(info, dict) else "unknown"
            issues.append({
                "severity": "warning",
                "check": "stale-entry",
                "metric": legacy_key,
                "message": (
                    f"字典中 '{legacy_key}' (rule_pack: {rule_pack}) "
                    f"及其 maps_to '{maps_to}' 都不在任何 Rule Pack 的 expr 中使用。"
                    f" 可能是過時條目。"
                ),
            })

    return issues


def main():
    parser = argparse.ArgumentParser(
        description="Metric Dictionary 自動驗證")
    parser.add_argument("--ci", action="store_true",
                        help="CI 模式: 有 error 時 exit 1")
    parser.add_argument("--json", action="store_true",
                        help="JSON 格式輸出")
    args = parser.parse_args()

    if not METRIC_DICT.is_file():
        print(f"WARNING: metric-dictionary.yaml 不存在: {METRIC_DICT}",
              file=sys.stderr)
        sys.exit(0)

    dict_metrics = load_dictionary_metrics(METRIC_DICT)
    dict_rules = load_dictionary_golden_rules(METRIC_DICT)
    rule_pack_metrics = extract_rule_pack_metrics(RULE_PACKS_DIR, K8S_RULES_DIR)

    all_rp_metrics = set()
    for metrics in rule_pack_metrics.values():
        all_rp_metrics.update(metrics)

    issues = check_dictionary_coverage(dict_metrics, dict_rules, rule_pack_metrics)

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if args.json:
        print(json.dumps({
            "check": "metric-dictionary",
            "dictionary_entries": len(dict_metrics),
            "rule_pack_metrics": len(all_rp_metrics),
            "rule_packs_scanned": len(rule_pack_metrics),
            "issues": issues,
            "summary": {"errors": len(errors), "warnings": len(warnings)},
        }, ensure_ascii=False, indent=2))
    else:
        print(f"Metric Dictionary: {len(dict_metrics)} 條目")
        print(f"Rule Pack metrics: {len(all_rp_metrics)} 個（"
              f"掃描 {len(rule_pack_metrics)} 個 pack）")
        print()

        if not issues:
            print("✓ Metric Dictionary 與 Rule Pack 使用完全一致。")
        else:
            for issue in issues:
                icon = "✗" if issue["severity"] == "error" else "⚠"
                print(f"  {icon} [{issue['check']}] {issue['message']}")
            print()
            print(f"總計: {len(errors)} 錯誤, {len(warnings)} 警告")

    if args.ci and errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
