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
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml
except ImportError:
    yaml = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))

try:
    from _lib_python import detect_cli_lang, i18n_text, write_text_secure
except ImportError:
    detect_cli_lang = None
    i18n_text = None
    write_text_secure = None

from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402


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
        # Set/logical operators (defect i). When written un-parenthesised —
        # `metric_a or metric_b`, `x and on(...)` — the extractor's
        # `word(?=[{[\s])` capture treats the trailing-whitespace `or`/`and`
        # as a metric name. `unless`/`on`/`by`/`group_*`/`ignoring` were
        # already excused; `or`/`and` complete the binary-set-op set. (PromQL
        # FUNCTIONS — vector/label_replace/changes/absent/clamp_min/time/
        # avg_over_time — are always written `fn(`, i.e. followed by `(` not
        # `{[`/whitespace, so the regex never captures them; verified across
        # every rule-pack, so they need no listing here.)
        "or", "and",
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


# Metrics a CENTRAL rule may legitimately read that are NOT produced by the
# edge→central recording pipeline (defect ii — the validator used to compare
# central inputs against edge recording outputs ONLY, flagging every
# self-produced / federated-external input).
#
# ⚠ FEDERATION-ALIGNED SCOPE (ADR-004 pull-aggregate). The federation match[]
# selector is `{tenant!=""}` (docs/…/federation-integration.md §fed) — the central
# cluster federates ONLY series that CARRY A TENANT LABEL. That draws the decisive
# line for "may a central rule read this raw series":
#   • PER-TENANT exporter raw (one exporter per tenant; the scrape relabel stamps
#     `tenant`) IS tenant-labeled → federated → legal on central. This covers the
#     DB liveness / topology family every pack's *Down / *ExporterAbsent /
#     *NoPrimary alert depends on: `up` / `<x>_up`, and the db-exporter raws
#     (mysql_/mongodb_/pg_/redis_/oracledb_/kafka_/clickhouse_/rabbitmq_ …).
#   • CLUSTER-LEVEL kube-state-metrics / kubelet raw (`kube_*` / `kubelet_*`) is
#     NAMESPACE-labeled, NOT tenant-labeled (pack evidence: refs use
#     `{namespace=~"db-.+"}` and recordings do `label_replace(...,"tenant","$1",
#     "namespace",...)` to DERIVE tenant) → NOT federated → absent on central. A
#     central rule reading it is a genuine federation topology bug, so `kube_` /
#     `kubelet_` are the ONLY raw families NOT excused here (see
#     KNOWN_CENTRAL_RAW_EXEMPTIONS for the 2 pre-existing offenders).
#
# External inputs recognised:
#   1. Platform-synthesised config — threshold-exporter / conf.d injection
#      (ADR-024), never a rule-pack recording: user_* (user_threshold /
#      user_state_filter / user_severity_dedup / user_silent_mode),
#      tenant_metadata_info, tenant_expected_exporter, da_config_event.
#   2. Exporter liveness `up` / `<x>_up` (tenant-labeled, single 0/1 per target).
#   3. Per-tenant db-exporter raw families (tenant-labeled, federated).
#
# NARROW BY CONSTRUCTION — a recording-namespace metric (contains ':') is NEVER
# external: it must be pipeline-produced, so a dropped recording group is still
# caught (defect iii guard). A bare reference matching none of these (a typo
# `missing_metric`, OR namespace-only kube_*/kubelet_* on central) is reported.
_PLATFORM_CONFIG_METRICS = frozenset({
    "tenant_metadata_info",
    "tenant_expected_exporter",
    "da_config_event",
})
_PLATFORM_CONFIG_PREFIXES = ("user_",)
# Per-tenant exporter families (tenant-labeled → federated → legal on central).
# Deliberately EXCLUDES kube_/kubelet_ (namespace-labeled → not federated). A new
# per-tenant exporter that reads raw on central would be added here; a new
# kube_/kubelet_ on central is a bug caught by KNOWN_CENTRAL_RAW_EXEMPTIONS.
_FEDERATED_EXPORTER_PREFIXES = (
    "mysql_", "mongodb_", "pg_", "redis_", "oracledb_",
    "kafka_", "clickhouse_", "rabbitmq_", "db2_", "elasticsearch_",
    "jvm_", "nginx_",
)


def _is_external_pipeline_input(metric: str) -> bool:
    """True if *metric* is a federated external input a central rule may read.

    Federation-aligned (ADR-004, match[]=`{tenant!=""}`): recording-namespace
    (excused elsewhere as pipeline-produced), platform-injected config, exporter
    liveness (`up`/`*_up`), and per-tenant db-exporter raw — all tenant-labeled
    and federated. Namespace-labeled kube_*/kubelet_* is NOT federated → NOT
    external here.
    """
    if ":" in metric:
        return False
    if metric == "up" or metric.endswith("_up"):
        return True
    if metric in _PLATFORM_CONFIG_METRICS:
        return True
    if metric.startswith(_PLATFORM_CONFIG_PREFIXES):
        return True
    if metric.startswith(_FEDERATED_EXPORTER_PREFIXES):
        return True
    return False


# ─ Known central-side raw-KSM references (pre-existing pack topology bugs) ─────
#
# Central ALERT rules that read a raw CLUSTER-LEVEL kube-state-metrics / kubelet
# series directly. Those series are namespace-labeled (not tenant-labeled), so
# federation's `match[]={tenant!=""}` does NOT pull them → they are absent on the
# central cluster → the alert is permanently inert there. This is a PACK-AUTHORING
# topology bug (the pack must add an EDGE normalisation recording that derives a
# tenant-labeled `tenant:*`/`:core` signal and federate THAT, as the container /
# node-health / ha-replicas groups already do). Grandfathered so the split tool
# reports exit 0 on the current repo while the fixes are tracked — every entry
# prints a fail-loud WARN, and a NEW (unlisted) kube_/kubelet_-on-central makes
# the tool exit 1 (audited ledger, NOT a blanket escape hatch).
#
# Scope note: per-tenant exporter raw (db liveness/topology: `up`, mysql_global_*,
# kafka_brokers, mongodb_mongod_replset_member_state, rabbitmq_identity_info, …)
# IS tenant-labeled → federated → legal on central (see _FEDERATED_EXPORTER_*),
# so those *Down/*ExporterAbsent/*NoPrimary alerts are NOT topology bugs. Only the
# 2 kube-state-metrics sentinels below genuinely reference non-federated raw.
# TODO(#1168: rule-pack KSM-federation topology audit): give each sentinel an
# edge normalisation recording (or platform-scoped alternative), then remove
# from this ledger.
#   file → { raw_metric: "alert + why it reads raw" }
KNOWN_CENTRAL_RAW_EXEMPTIONS: Dict[str, Dict[str, str]] = {
    "rule-pack-kubernetes.yaml": {
        "kube_pod_info": "VersionAwareThresholdInert: raw kube-state-metrics (namespace-labeled), not federated",
        "kube_pod_labels": "VersionAwareThresholdInert: raw kube-state-metrics (namespace-labeled), not federated",
        "kube_pod_status_phase": "CustomRecipeDiskInert: raw kube-state-metrics (namespace-labeled), not federated",
        "kubelet_volume_stats_available_bytes": "CustomRecipeDiskInert: raw kubelet volume-stats (namespace-labeled), not federated",
        "kubelet_volume_stats_capacity_bytes": "CustomRecipeDiskInert: raw kubelet volume-stats (namespace-labeled), not federated",
    },
}


def validate_central_references_edge(
    edge_outputs: Set[str],
    central_inputs: Set[str],
    filename: str,
    central_outputs: Optional[Set[str]] = None,
) -> Tuple[bool, List[str]]:
    """Check that every federated metric a central rule reads is produced upstream.

    A central input is a genuine dangling reference iff it is NONE of:
    (a) produced by an edge recording rule (``edge_outputs``);
    (b) produced by a central recording rule in the SAME bundle
        (``central_outputs`` — e.g. ``tenant_version:alert_threshold:*`` recorded
        by the ``-threshold-normalization`` group and consumed by that bundle's
        own ``-alerts`` group; defect ii-a);
    (c) a recognised external pipeline input — raw exporter / liveness /
        platform-injected config (defect ii-b; see _is_external_pipeline_input).

    Returns:
        (is_valid, missing_metrics)
    """
    available = set(edge_outputs)
    if central_outputs:
        available |= set(central_outputs)
    missing = sorted(
        m for m in central_inputs
        if m not in available and not _is_external_pipeline_input(m)
    )
    return (not missing), missing


# ─ Rule Pack Splitting ──────────────────────────────────────────────────


_CENTRAL_SUFFIXES = ("-threshold-normalization", "-alerts")
_EDGE_SUFFIX = "-normalization"


def _group_follows_suffix(name: str) -> bool:
    """True if *name* follows the -normalization / -threshold-normalization /
    -alerts naming convention (so it routes as a whole group by suffix).

    Non-suffix groups route per-rule by data-locality instead (see
    split_rule_pack) — used to drive the fail-loud routing WARN.
    """
    return name.endswith(_CENTRAL_SUFFIXES) or name.endswith(_EDGE_SUFFIX)


def split_rule_pack(groups: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split groups into edge (Part 1) and central (Parts 2+3).

    Data-locality rule (ADR-004): every rule runs on the plane where its inputs
    live. edge = recording rules that normalise RAW exporter series; central =
    threshold + alert rules over FEDERATED ``tenant:*`` / ``:core`` / ``up`` /
    injected series.

    - Groups ending ``-threshold-normalization`` / ``-alerts`` → central (whole).
    - Groups ending ``-normalization`` → edge (whole). (Checked first, since
      ``-threshold-normalization`` also ends with ``-normalization``.)
    - Groups following NO suffix convention → routed PER RULE (defect iii): a
      recording rule reads raw exporter series → edge; an alerting rule reads its
      own group's federated ``:core`` recording → central. A MIXED group
      (kubernetes-node-health / -ha-replicas: raw-reading recordings + an alert
      over their ``:core`` output) is thus split across BOTH planes. Nothing is
      dropped — the old ``# else: skip`` discarded whole groups (state-matching's
      ``tenant:container_waiting_reason:count`` recording, the node-health /
      ha-replicas / sentinel groups, and the ENTIRE liveness + operational packs).
    """
    edge_groups: List[Dict[str, Any]] = []
    central_groups: List[Dict[str, Any]] = []

    for group in groups:
        name = group.get("name", "")
        rules = group.get("rules", [])
        if name.endswith(_CENTRAL_SUFFIXES):
            central_groups.append(group)
        elif name.endswith(_EDGE_SUFFIX):
            edge_groups.append(group)
        else:
            # Non-suffix: per-rule data-locality. Alerts → central; everything
            # else (recording rules; also any malformed rule, so nothing drops)
            # → edge. A mixed group emits a subset-copy on each plane.
            central_rules = [r for r in rules if "alert" in r]
            edge_rules = [r for r in rules if "alert" not in r]
            if edge_rules:
                edge_groups.append({**group, "rules": edge_rules})
            if central_rules:
                central_groups.append({**group, "rules": central_rules})

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

            # Fail-loud (defect iii): surface any group that does not follow the
            # -normalization / -threshold-normalization / -alerts naming
            # convention. Such groups were previously SILENTLY DROPPED; they are
            # now routed PER RULE by data-locality — record the fact so the drift
            # is visible rather than data silently vanishing from the split.
            for group in groups:
                name = group.get("name", "")
                if not _group_follows_suffix(name):
                    report["warnings"].append(
                        f"{filename}: group '{name}' has no "
                        "-normalization/-threshold-normalization/-alerts suffix "
                        "→ routed per-rule by data-locality "
                        "(recordings→edge, alerts→central; previously silently dropped)"
                    )

            # Validation: extract metrics
            edge_outputs = set()
            central_outputs = set()
            central_inputs = set()

            for group in edge_groups:
                edge_outputs.update(extract_recording_outputs(group.get("rules", [])))

            for group in central_groups:
                rules = group.get("rules", [])
                # Central bundles produce their own recording outputs
                # (tenant_version:alert_threshold:* etc.) consumed by their own
                # alerts — count them as available (defect ii-a).
                central_outputs.update(extract_recording_outputs(rules))
                for rule in rules:
                    expr = rule.get("expr", "")
                    central_inputs.update(extract_metrics_from_expr(expr))

            # Check metric references (defect ii): available = edge ∪ central
            # recording outputs; federated external inputs (up/*_up, injected)
            # excused. Raw exporter series are NOT excused → land in `missing`.
            is_valid, missing = validate_central_references_edge(
                edge_outputs, central_inputs, filename, central_outputs
            )

            # Partition `missing` into (a) grandfathered raw-on-central pack
            # topology bugs — fail-loud WARN, do NOT fail the run; (b) genuine
            # violations (a new/unlisted raw-on-central, or a real dangling
            # reference) — recorded as a mismatch → exit 1.
            exemptions = KNOWN_CENTRAL_RAW_EXEMPTIONS.get(filename, {})
            real_missing = []
            for m in missing:
                if m in exemptions:
                    report["warnings"].append(
                        f"{filename}: KNOWN raw-on-central (federation topology bug, "
                        f"grandfathered): '{m}' — {exemptions[m]}"
                    )
                else:
                    real_missing.append(m)

            is_valid = not real_missing
            if real_missing:
                report["validation"]["metric_mismatches"].append({
                    "file": filename,
                    "missing_in_edge": real_missing,
                })
                report["warnings"].append(
                    f"{filename}: {t('中央規則缺少邊緣輸出', 'central rules reference missing edge outputs')}: {real_missing}"
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
    try_utf8_stdout()
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
        sys.exit(EXIT_VIOLATION)
    if report["status"] == "error":
        sys.exit(EXIT_CALLER_ERROR)
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
