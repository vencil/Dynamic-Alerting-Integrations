#!/usr/bin/env python3
"""check_metric_dictionary.py — Metric Dictionary 自動驗證

交叉驗證 metric-dictionary.yaml 與 Rule Pack YAML 的實際 metric 使用:
  1. 字典中存在但 Rule Pack 不使用的 stale entry
  2. Rule Pack 使用但字典未收錄的 undocumented metric
  3. 契約（error，issue 1196）：`maps_to` 是租戶閾值 key，必須有 rule pack
     告警讀它；`golden_rule` 必須是存在的告警；兩者都有值時，該告警必須讀
     這個 key。migrate 把這兩個欄位原樣告訴使用者，錯了就是在教錯的設定。

v2.4.0 新增：DX Tooling Backlog 候選項目。
migrate_rule.py 依賴 metric-dictionary.yaml 做遷移映射，
stale 或 undocumented entry 會導致遷移建議不完整。

用法:
    python3 scripts/tools/lint/check_metric_dictionary.py [--ci] [--json]
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Set

import yaml
import os

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, Report  # noqa: E402
import _lib_consumer_read  # noqa: E402  (#3206: rule files as Prometheus reads them)
sys.path.insert(0, os.path.join(str(_THIS_DIR), "..", "ops"))
import _threshold_alerts  # noqa: E402  (rule pack 讀者索引，issue 1196)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

METRIC_DICT = REPO_ROOT / "scripts" / "tools" / "metric-dictionary.yaml"
# #3471: the directories and which files there are rule files are the one
# declaration (`_lib_consumer_read.RULE_SOURCES`).
RULE_PACKS_DIR = REPO_ROOT / _lib_consumer_read.RULE_PACKS.directory
K8S_RULES_DIR = REPO_ROOT / _lib_consumer_read.CONFIGMAP_RULES.directory


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
                               k8s_dir: Path,
                               refused: "List[str] | None" = None) -> Dict[str, Set[str]]:
    """Extract metric names used in Rule Pack YAML files.

    Returns dict mapping pack_name → set of metric names.
    Scans both rule-packs/ source and k8s/ ConfigMaps, each rule file read
    the way Prometheus' rule loader reads it (#3206, ADR-036 §12: one
    `da-read prometheus-rules` run for the packs, one for the ConfigMaps,
    whose outer layer is read the way kubectl reads it). A rule file the
    loader refuses is appended to *refused* (``<file>: <reason>``) — or,
    without that list, raises ``RuleFilesRefused``.
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

    results = []
    # rule-packs/ directory
    packs_src = _lib_consumer_read.RULE_PACKS
    if rule_packs_dir.is_dir():
        files = packs_src.files_in(rule_packs_dir)
        results += [(packs_src.member(f), rf) for f, rf in zip(
            files, _lib_consumer_read.prometheus_rule_file_results(files))]

    # k8s/ ConfigMaps
    cms_src = _lib_consumer_read.CONFIGMAP_RULES
    if k8s_dir.is_dir():
        files = cms_src.files_in(k8s_dir)
        results += [(cms_src.member(rf.manifest), rf)
                    for rf in _lib_consumer_read.manifest_rule_file_results(files)
                    if rf.kind == "ConfigMap"]

    bad = [rf for _name, rf in results if rf.problem is not None]
    if bad and refused is None:
        raise _lib_consumer_read.RuleFilesRefused([rf for _n, rf in results])
    for rf in bad:
        refused.append(f"{rf.path}: {rf.problem}")
    for name, rf in results:
        if rf.problem is None:
            _extract_from_rules(rf.documents[0], name)

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


def rule_pack_alert_names() -> Set[str]:
    """rule-packs/ 裡所有告警名。"""
    names: Set[str] = set()
    # #3471: the rule packs are the one declaration's; the registry beside
    # them is declared no rule file and holds no alert.
    paths = (_lib_consumer_read.RULE_PACKS.files_in(RULE_PACKS_DIR)
             if RULE_PACKS_DIR.is_dir() else [])
    for path in paths:
        names.update(re.findall(r"^\s*-\s*alert:\s*(\S+)",
                                path.read_text(encoding="utf-8"), re.M))
    return names


_READERS_INDEX: "Dict[tuple, dict]" = {}


def alerts_reading_key(key: str):
    """讀 ``key`` 的告警，掃的是本 lint 的 RULE_PACKS_DIR（含經由 recording rule
    間接讀，規則同 `_threshold_alerts.build_index`）。沒有 rule pack 時回 None。

    索引對同一組 pack 只建一次：rule pack 經 da-read 讀（#3206），一次讀全部，
    不是每個 key 一個子行程。"""
    packs = (tuple(_lib_consumer_read.RULE_PACKS.files_in(RULE_PACKS_DIR))
             if RULE_PACKS_DIR.is_dir() else ())
    if not packs:
        return None
    if packs not in _READERS_INDEX:
        _READERS_INDEX[packs] = _threshold_alerts.build_index(list(packs))
    return _READERS_INDEX[packs].get(key, ())


def check_dictionary_contract(dict_data, alert_names, readers_of) -> List[Dict]:
    """maps_to／golden_rule 的契約，違反一律是 error。"""
    issues: List[Dict] = []
    if not isinstance(dict_data, dict):
        return issues
    for metric, info in sorted(dict_data.items()):
        if not isinstance(info, dict):
            continue
        key = info.get("maps_to")
        golden = info.get("golden_rule")
        readers = None
        if key:
            readers = readers_of(key)
            if readers is None:
                issues.append({"severity": "error", "check": "readers-unknown",
                               "metric": metric,
                               "message": f"'{metric}': 找不到 rule pack，無法判定 maps_to '{key}' 的讀者"})
                continue
            if not readers:
                issues.append({"severity": "error", "check": "unread-key", "metric": metric,
                               "message": (f"'{metric}' 的 maps_to '{key}' 沒有任何 rule pack 告警讀"
                                           f"（照設等於設了不生效的 key）；固定判斷的黃金規則請寫 maps_to: null")})
        if golden and golden not in alert_names:
            issues.append({"severity": "error", "check": "missing-golden-rule", "metric": metric,
                           "message": f"'{metric}' 的 golden_rule '{golden}' 不是 rule pack 裡存在的告警"})
        elif golden and readers and golden not in readers:
            issues.append({"severity": "error", "check": "golden-rule-ignores-key",
                           "metric": metric,
                           "message": (f"'{metric}' 的 golden_rule '{golden}' 不讀 maps_to '{key}'"
                                       f"（讀它的是 {', '.join(readers)}）")})
    return issues


def main():
    try_utf8_stdout()
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
        sys.exit(EXIT_OK)

    dict_metrics = load_dictionary_metrics(METRIC_DICT)
    dict_rules = load_dictionary_golden_rules(METRIC_DICT)
    refused: List[str] = []
    try:
        rule_pack_metrics = extract_rule_pack_metrics(RULE_PACKS_DIR, K8S_RULES_DIR, refused)
        issues = []
        # #3206: a rule file Prometheus' rule loader refuses is an error that
        # names it — its metrics are not counted around it.
        for line in refused:
            issues.append({
                "severity": "error",
                "check": "rule-file-refused",
                "message": f"Prometheus' rule loader refuses {line}",
            })
        all_rp_metrics = set()
        for metrics in rule_pack_metrics.values():
            all_rp_metrics.update(metrics)

        issues += check_dictionary_coverage(dict_metrics, dict_rules, rule_pack_metrics)
        issues += check_dictionary_contract(
            yaml.safe_load(METRIC_DICT.read_text(encoding="utf-8")) or {},
            rule_pack_alert_names(), alerts_reading_key)
    except _lib_consumer_read.ConsumerReadError as exc:
        # no da-read / da-crdecode (or a pack refused while indexing readers)
        print(f"ERROR: rule files: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    status = EXIT_VIOLATION if args.ci and errors else EXIT_OK
    if args.json:
        return Report({
            "check": "metric-dictionary",
            "dictionary_entries": len(dict_metrics),
            "rule_pack_metrics": len(all_rp_metrics),
            "rule_packs_scanned": len(rule_pack_metrics),
            "issues": issues,
            "summary": {"errors": len(errors), "warnings": len(warnings)},
        }, status)  # #3216: run() encodes it
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

    sys.exit(status)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)  # #3216: run() encodes the --json document
