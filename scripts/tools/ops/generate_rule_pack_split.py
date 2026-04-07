#!/usr/bin/env python3
"""Split Rule Packs into edge (Part 1) and central (Parts 2+3) YAML files.

Federation Scenario B: Edge clusters run Part 1 (raw metric normalization),
central cluster runs Parts 2+3 (threshold comparison + alerting).

Usage:
    generate_rule_pack_split.py --rule-packs-dir rule-packs/ --output-dir split-output/
    generate_rule_pack_split.py --output-dir split-output/ --operator --namespace monitoring
    generate_rule_pack_split.py --rule-packs-dir rule-packs/ --gitops --dry-run
    generate_rule_pack_split.py --rule-packs-dir rule-packs/ --json > report.json

Output structure:
    split-output/
      ├── edge-rules/
      │   └── rule-pack-*.yaml (Groups: *-normalization only)
      ├── central-rules/
      │   └── rule-pack-*.yaml (Groups: *-threshold-normalization + *-alerts)
      └── validation-report.json

Exit codes:
    0  Success
    1  Validation failure (metric mismatch)
    2  Error (file I/O, YAML parse)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

try:
    import yaml
except ImportError:
    yaml = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))

try:
    from _lib_python import detect_cli_lang, i18n_text, write_text_secure
except ImportError:
    detect_cli_lang = None
    i18n_text = None
    write_text_secure = None


# ─ I18n Fallback ────────────────────────────────────────────────────────


def get_lang():
    """Detect CLI language (zh_TW or en_US)."""
    if detect_cli_lang:
        return detect_cli_lang()
    return "en_US"


def t(zh: str, en: str) -> str:
    """Translate by detected language."""
    if i18n_text:
        return i18n_text(zh, en)
    lang = get_lang()
    return zh if lang.startswith("zh") else en


def _safe_write(path: str, content: str):
    """Write file, fallback to Path.write_text if _lib not available."""
    if write_text_secure:
        write_text_secure(path, content)
    else:
        Path(path).write_text(content, encoding='utf-8')


# ─ Metric extraction from PromQL expressions ────────────────────────────


def extract_metrics_from_expr(expr: str) -> Set[str]:
    """Extract metric names from PromQL expression.

    Regex: word followed by { or [ or whitespace. Captures built-in functions
    (rate, sum, max, etc.) but they are metric-adjacent, so we filter them.
    """
    if not expr or not isinstance(expr, str):
        return set()

    builtin_funcs = {
        "rate", "sum", "max", "min", "avg", "count", "topk", "bottomk",
        "histogram_quantile", "increase", "delta", "irate", "group",
        "on", "ignoring", "group_left", "group_right", "unless", "by",
    }

    # Match word followed by { or [ or whitespace
    pattern = r'\b([a-zA-Z_:][a-zA-Z0-9_:]*)\b(?=[{\[\s])'
    matches = re.findall(pattern, expr)

    # Filter out PromQL functions and labels
    metrics = set()
    for m in matches:
        if m not in builtin_funcs and not m[0].isupper():  # Skip labels like "tenant"
            metrics.add(m)

    return metrics


def extract_recording_outputs(rules: List[Dict[str, Any]]) -> Set[str]:
    """Extract all recording rule output metric names (record: field)."""
    outputs = set()
    for rule in rules:
        if "record" in rule:
            outputs.add(rule["record"])
    return outputs


# ─ Validation ───────────────────────────────────────────────────────────


def validate_central_references_edge(
    edge_outputs: Set[str],
    central_inputs: Set[str],
    filename: str,
) -> Tuple[bool, List[str]]:
    """Check that every metric in central Part 2 has corresponding edge output.

    Returns:
        (is_valid, missing_metrics)
    """
    missing = central_inputs - edge_outputs
    if missing:
        return False, sorted(missing)
    return True, []


# ─ Rule Pack Splitting ──────────────────────────────────────────────────


def split_rule_pack(groups: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split groups into edge and central.

    Rules:
    - Groups ending with '-normalization' (not '-threshold-normalization') → edge
    - Groups ending with '-threshold-normalization' or '-alerts' → central
    """
    edge_groups = []
    central_groups = []

    for group in groups:
        name = group.get("name", "")
        if name.endswith("-threshold-normalization") or name.endswith("-alerts"):
            central_groups.append(group)
        elif name.endswith("-normalization"):
            edge_groups.append(group)
        # else: unknown group, skip

    return edge_groups, central_groups


# ─ CRD Output ───────────────────────────────────────────────────────────


def to_prometheus_rule_crd(
    groups: List[Dict[str, Any]],
    filename: str,
    namespace: str = "monitoring",
) -> Dict[str, Any]:
    """Convert groups to PrometheusRule CRD.

    Metadata.name derived from filename (rule-pack-*.yaml → rule-pack-*)
    """
    # Extract base name: rule-pack-clickhouse.yaml → rule-pack-clickhouse
    base_name = Path(filename).stem

    crd = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": base_name,
            "namespace": namespace,
            "labels": {
                "prometheus": "kube-prometheus",
                "role": "alert-rules",
            },
        },
        "spec": {
            "groups": groups,
        },
    }
    return crd


# ─ YAML I/O with GitOps mode ────────────────────────────────────────────


def load_rule_pack(path: str) -> Dict[str, Any]:
    """Load YAML rule pack."""
    if not yaml:
        raise RuntimeError(t(
            "YAML 模組不可用，請安裝 PyYAML",
            "YAML module not available, install PyYAML"
        ))

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        return data if data else {}
    except (OSError, yaml.YAMLError) as e:
        raise RuntimeError(f"{t('載入失敗', 'Failed to load')} {path}: {e}")


def dump_yaml(data: Dict[str, Any], gitops: bool = False) -> str:
    """Dump YAML with gitops determinism (sorted keys, no timestamps)."""
    if not yaml:
        raise RuntimeError("YAML module not available")

    return yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=gitops,
        explicit_start=False,
    )


# ─ Main Processing ─────────────────────────────────────────────────────


def process_rule_packs(
    rule_packs_dir: str,
    output_dir: str,
    operator: bool = False,
    namespace: str = "monitoring",
    gitops: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Process all rule packs in directory.

    Returns validation report.
    """
    rule_packs_path = Path(rule_packs_dir)
    output_path = Path(output_dir)
    edge_dir = output_path / "edge-rules"
    central_dir = output_path / "central-rules"

    if not dry_run:
        edge_dir.mkdir(parents=True, exist_ok=True)
        central_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "status": "success",
        "errors": [],
        "warnings": [],
        "processed_files": [],
        "validation": {
            "total_packs": 0,
            "edge_rules": 0,
            "central_rules": 0,
            "metric_mismatches": [],
        },
    }

    # Find all rule-pack-*.yaml files
    rule_files = sorted(rule_packs_path.glob("rule-pack-*.yaml"))
    if not rule_files:
        report["errors"].append(
            t(f"未找到規則包檔案: {rule_packs_dir}", f"No rule pack files found in {rule_packs_dir}")
        )
        report["status"] = "error"
        return report

    report["validation"]["total_packs"] = len(rule_files)

    for rule_file in rule_files:
        filename = rule_file.name
        try:
            data = load_rule_pack(str(rule_file))
            groups = data.get("groups", [])

            if not groups:
                report["warnings"].append(f"{filename}: no groups found")
                continue

            # Split into edge and central
            edge_groups, central_groups = split_rule_pack(groups)

            # Validation: extract metrics
            edge_outputs = set()
            central_inputs = set()

            for group in edge_groups:
                rules = group.get("rules", [])
                edge_outputs.update(extract_recording_outputs(rules))

            for group in central_groups:
                rules = group.get("rules", [])
                for rule in rules:
                    expr = rule.get("expr", "")
                    central_inputs.update(extract_metrics_from_expr(expr))

            # Check metric references
            is_valid, missing = validate_central_references_edge(
                edge_outputs, central_inputs, filename
            )

            if not is_valid:
                report["validation"]["metric_mismatches"].append({
                    "file": filename,
                    "missing_in_edge": missing,
                })
                report["warnings"].append(
                    f"{filename}: {t('中央規則缺少邊緣輸出', 'central rules reference missing edge outputs')}: {missing}"
                )

            # Write edge rules
            if edge_groups:
                edge_output = {"groups": edge_groups}
                if operator:
                    edge_output = to_prometheus_rule_crd(edge_groups, f"edge-{filename}", namespace)

                edge_yaml = dump_yaml(edge_output, gitops=gitops)
                edge_file = edge_dir / filename if not operator else edge_dir / f"edge-{filename}"

                if not dry_run:
                    _safe_write(str(edge_file), edge_yaml)

                report["validation"]["edge_rules"] += len(edge_groups)

            # Write central rules
            if central_groups:
                central_output = {"groups": central_groups}
                if operator:
                    central_output = to_prometheus_rule_crd(central_groups, f"central-{filename}", namespace)

                central_yaml = dump_yaml(central_output, gitops=gitops)
                central_file = central_dir / filename if not operator else central_dir / f"central-{filename}"

                if not dry_run:
                    _safe_write(str(central_file), central_yaml)

                report["validation"]["central_rules"] += len(central_groups)

            report["processed_files"].append({
                "file": filename,
                "edge_groups": len(edge_groups),
                "central_groups": len(central_groups),
                "valid": is_valid,
            })

        except Exception as e:
            report["status"] = "error"
            report["errors"].append(f"{filename}: {e}")

    # Write validation report
    if not dry_run:
        report_file = output_path / "validation-report.json"
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    return report


# ─ CLI ──────────────────────────────────────────────────────────────────


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description=t(
            "將規則包分割為邊緣和中央 YAML 檔案",
            "Split Rule Packs into edge and central YAML files"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--rule-packs-dir",
        default="rule-packs/",
        help=t(
            "規則包目錄（預設: rule-packs/）",
            "Directory with rule pack files (default: rule-packs/)"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="split-output/",
        help=t(
            "輸出目錄（預設: split-output/）",
            "Output directory (default: split-output/)"
        ),
    )
    parser.add_argument(
        "--operator",
        action="store_true",
        help=t(
            "輸出為 PrometheusRule CRD YAML",
            "Output as PrometheusRule CRD YAML"
        ),
    )
    parser.add_argument(
        "--namespace",
        default="monitoring",
        help=t(
            "CRD 命名空間（預設: monitoring）",
            "CRD namespace (default: monitoring)"
        ),
    )
    parser.add_argument(
        "--gitops",
        action="store_true",
        help=t(
            "GitOps 模式（排序鍵值、確定性輸出）",
            "GitOps mode (sorted keys, deterministic output)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=t(
            "預覽模式（不寫入檔案）",
            "Dry-run mode (don't write files)"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=t(
            "JSON 格式輸出報告",
            "Output report as JSON"
        ),
    )

    args = parser.parse_args()

    report = process_rule_packs(
        rule_packs_dir=args.rule_packs_dir,
        output_dir=args.output_dir,
        operator=args.operator,
        namespace=args.namespace,
        gitops=args.gitops,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        # Text output
        if report["status"] == "success":
            print(t("✓ 規則包分割成功", "✓ Rule packs split successfully"))
            print(f"  {t('邊緣規則組', 'Edge rule groups')}: {report['validation']['edge_rules']}")
            print(f"  {t('中央規則組', 'Central rule groups')}: {report['validation']['central_rules']}")
            print(f"  {t('已處理檔案', 'Files processed')}: {len(report['processed_files'])}")
        else:
            print(t("✗ 發生錯誤", "✗ Error occurred"))
            for error in report["errors"]:
                print(f"  ERROR: {error}")

        if report["warnings"]:
            for warning in report["warnings"]:
                print(f"  WARN: {warning}")

        if report["validation"]["metric_mismatches"]:
            print(t("\n⚠ 指標不匹配:", "\n⚠ Metric mismatches:"))
            for mismatch in report["validation"]["metric_mismatches"]:
                print(f"  {mismatch['file']}: {mismatch['missing_in_edge']}")

    # Exit codes
    if report["validation"]["metric_mismatches"]:
        sys.exit(1)
    if report["status"] == "error":
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
