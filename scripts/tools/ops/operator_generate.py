#!/usr/bin/env python3
"""operator-generate — Generate the Prometheus Operator CRD YAML of the dynamic alerting stack.

Reads the rule-packs/ directory and generates PrometheusRule and ServiceMonitor
CRDs. It reads no conf.d.

One PrometheusRule per declared rule pack. The platform pack
(`_lib_consumer_read.PLATFORM_PACKS`, #1595 D1b) is emitted without the rules
whose metric source a kube-prometheus-stack install does not have
(`_lib_metric_sources.operator_split`); each left-out rule is named on stderr
and in the PrometheusRule's `dynamic-alerting.io/operator-excluded-rules`
annotation.

⛔ No Alertmanager routing (#3572): `generate-routes` is the one Alertmanager
route renderer. AlertmanagerConfig CRDs cannot carry it — the Prometheus
Operator puts their routes first with `continue: true` and a namespace
matcher, so the platform routes (Watchdog, custom isolation, synthetic probe,
sentinel) could never come first. `--components alertmanager` and the flags
that only fed AlertmanagerConfig (`--config-dir`, `--api-version`,
`--receiver-template`, `--secret-name`, `--secret-key`) are refused (rc 2)
with how to render alertmanager.yaml and hand it to the Alertmanager as its
configSecret (`_lib_constants.OPERATOR_ROUTING_REMEDY_*`).

Usage:
    da-tools operator-generate | kubectl apply -f -   # CRDs to stdout; nothing written
    da-tools operator-generate --rule-packs-dir /path/to/rule-packs
    da-tools operator-generate --output-dir operator-crds/   # writing is opt-in (#1582)
    da-tools operator-generate --namespace monitoring --dry-run
    da-tools operator-generate --components rules
    da-tools operator-generate --gitops --json
    da-tools operator-generate --selector-label release=my-prom   # match a Prometheus selector (#2075)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))

try:
    import yaml
except ImportError:
    yaml = None

from _lib_python import detect_cli_lang, i18n_text  # noqa: E402
from _lib_exitcodes import Report, die_caller_error  # noqa: E402
from _lib_io import OutputWriteError, ensure_dir  # noqa: E402
import _lib_consumer_read  # noqa: E402  (#3206: rule packs as Prometheus reads them)
import _lib_metric_sources  # noqa: E402  (#1595 D1b: which platform rules operator mode can run)
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_yaml import _dict_to_yaml, write_yaml_crd  # noqa: E402
from _lib_constants import OPERATOR_ROUTING_REMEDY_EN, OPERATOR_ROUTING_REMEDY_ZH  # noqa: E402  (#3572)
from _lib_paths import supplied  # noqa: E402  (#3392)
import _lib_input  # noqa: E402  (#3462: what --rule-packs-dir selects)

# ---------------------------------------------------------------------------
# Constants and Help Text
# ---------------------------------------------------------------------------

# #3572: what --components may name. `alertmanager` is still parsed so that
# asking for it is refused with the remedy, not argparse's bare "invalid choice".
_COMPONENTS = ("all", "rules", "servicemonitor")
_REFUSED_COMPONENT = "alertmanager"

# #3572: the flags that only fed AlertmanagerConfig, as (flag, dest). Still
# parsed — hidden (`help=argparse.SUPPRESS`), `default=None`, no type /
# choices — so that giving one, with any value, is refused naming it with the
# remedy rather than as a bare "unrecognized arguments": the caller learns
# where routing went. They are declared one literal `add_argument` each in
# the parser builder (the conf.d population walks read flags statically).
_ROUTING_FLAGS = (
    ("--config-dir", "config_dir"),
    ("--api-version", "api_version"),
    ("--receiver-template", "receiver_template"),
    ("--secret-name", "secret_name"),
    ("--secret-key", "secret_key"),
)

_HELP = {
    "zh": {
        "desc": (
            "生成 Prometheus Operator CRD YAML（PrometheusRule、ServiceMonitor）。"
            "Alertmanager 路由不在此產生：用 generate-routes 產出 alertmanager.yaml，"
            "交給 Alertmanager 當 configSecret（#3572）"
        ),
        "rule_packs_dir": "Rule packs 目錄（預設 rule-packs；須至少有一個 rule pack）",
        "output_dir": "把 CRD 寫進這個目錄。不給、或帶 --dry-run，就改印到 stdout 而不寫任何檔案",
        "namespace": "目標命名空間（預設 monitoring）",
        "gitops": "啟用 GitOps 模式（排序鍵、無時間戳）",
        "dry_run": "列印輸出而不寫入檔案",
        "json": "以 JSON 格式輸出結果報告",
        "components": "要生成的元件（all | rules | servicemonitor；all = rules + servicemonitor）",
        "kustomize": "生成 kustomization.yaml，列出所有 CRD 檔案為資源",
        "selector_label": (
            "加在 PrometheusRule 與 ServiceMonitor 上的 label（key=value，可重複；"
            "同 key 覆寫預設）。預設已帶 release=kube-prometheus-stack，"
            "Helm release 名稱不同時用這個對上 Prometheus 的 selector"
        ),
    },
    "en": {
        "desc": (
            "Generate Prometheus Operator CRD YAML (PrometheusRule, ServiceMonitor). "
            "Alertmanager routing is not generated here: render alertmanager.yaml with "
            "generate-routes and give it to the Alertmanager as its configSecret (#3572)"
        ),
        "rule_packs_dir": "Rule packs directory (default rule-packs; must hold at least one rule pack)",
        "output_dir": "Write CRDs into this directory. Omit it, or pass --dry-run, and they go to stdout instead; nothing is written",
        "namespace": "Target namespace (default monitoring)",
        "gitops": "Enable GitOps mode (sorted keys, no timestamps)",
        "dry_run": "Print output instead of writing files",
        "json": "Output results as JSON report",
        "components": "Components to generate (all | rules | servicemonitor; all = rules + servicemonitor)",
        "kustomize": "Generate kustomization.yaml listing all CRD files as resources",
        "selector_label": (
            "Label added to the PrometheusRule and ServiceMonitor CRDs (key=value, repeatable; "
            "overrides a default of the same key). release=kube-prometheus-stack is set by default; "
            "use this when your Helm release name differs, so Prometheus's selector matches"
        ),
    },
}

_LANG = detect_cli_lang()

# Selector labels on the CRDs Prometheus has to SELECT (#2075). A Prometheus
# Operator only loads a PrometheusRule / ServiceMonitor whose labels match its
# ruleSelector / serviceMonitorSelector, and installs differ on which label
# that is (docs/integration/operator-prometheus-integration.md § ruleSelector
# 雙 Label 匹配). The docs promised both labels below on generated
# PrometheusRules while only `prometheus` was emitted.
# --selector-label adds / overrides per key, e.g. for a Helm release that is
# not named kube-prometheus-stack. #3427: the PrometheusRule pair is the one
# definition every PrometheusRule emitter shares (`_lib_consumer_read`).
_RULE_SELECTOR_LABELS = _lib_consumer_read.PROMETHEUS_RULE_SELECTOR_LABELS
_MONITOR_SELECTOR_LABELS = {
    "release": "kube-prometheus-stack",
}
# Identifies every CRD this tool emits (drift detection, cleanup) — a
# selector label must not be able to rewrite it.
_PART_OF_KEY = "app.kubernetes.io/part-of"
_PART_OF = {_PART_OF_KEY: "dynamic-alerting"}

# ---------------------------------------------------------------------------
# CRD Builders
# ---------------------------------------------------------------------------


def build_prometheus_rule(
    rule_pack_name: str,
    rule_pack_data: dict,
    namespace: str,
    selector_labels: Optional[Dict[str, str]] = None,
    excluded_rules: Optional[List[str]] = None,
) -> dict:
    """Build a PrometheusRule CRD from a rule pack YAML.

    Args:
        rule_pack_name: Name of the rule pack (e.g., 'mariadb')
        rule_pack_data: Parsed YAML dict with 'groups' key
        namespace: Target Kubernetes namespace
        selector_labels: Extra labels; each overrides the default of that key
        excluded_rules: Rules of the pack left out of *rule_pack_data*
            (#1595 D1b) — named in the EXCLUDED_RULES_ANNOTATION

    Returns:
        PrometheusRule CRD dict
    """
    groups = rule_pack_data.get("groups", [])
    metadata = {
        # #3471: the object name is the stem of the file it is written to
        # (`<name>.yaml`), both the declaration's (`OPERATOR_RULES`).
        "name": _lib_consumer_read.OPERATOR_RULES.file_stem(rule_pack_name),
        "namespace": namespace,
        "labels": {**_RULE_SELECTOR_LABELS, **(selector_labels or {}), **_PART_OF},
    }
    if excluded_rules:
        metadata["annotations"] = {EXCLUDED_RULES_ANNOTATION: ",".join(excluded_rules)}

    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": metadata,
        "spec": {
            "groups": groups,
        },
    }


#: The PrometheusRule annotation naming the rules of a platform pack left out
#: because a kube-prometheus-stack install has no source for a metric they
#: read (#1595 D1b) — comma-separated, in pack order.
EXCLUDED_RULES_ANNOTATION = "dynamic-alerting.io/operator-excluded-rules"


def platform_pack_for_operator(rule_pack_data: dict) -> "tuple[dict, list]":
    """The platform pack as operator mode can run it (#1595 D1b): the pack
    minus every rule ``_lib_metric_sources.operator_split`` finds a metric
    source missing for. → (pack data, [(rule name, reasons)])."""
    groups, dropped = _lib_metric_sources.operator_split(rule_pack_data.get("groups") or [])
    return {**rule_pack_data, "groups": groups}, dropped


def build_servicemonitor(
    namespace: str,
    selector_labels: Optional[Dict[str, str]] = None,
) -> dict:
    """Build a ServiceMonitor CRD for threshold-exporter.

    Args:
        namespace: Target Kubernetes namespace
        selector_labels: Extra labels; each overrides the default of that key

    Returns:
        ServiceMonitor CRD dict
    """
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {
            "name": "da-threshold-exporter",
            "namespace": namespace,
            "labels": {**_MONITOR_SELECTOR_LABELS, **(selector_labels or {}), **_PART_OF},
        },
        "spec": {
            "selector": {
                "matchLabels": {
                    "app": "threshold-exporter",
                },
            },
            "endpoints": [
                {
                    "port": "http",
                    "interval": "15s",
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main Logic
# ---------------------------------------------------------------------------


def generate_crds(
    rule_pack_files: List[Path],
    namespace: str,
    components: str,
    selector_labels: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Generate all CRDs from the rule-packs directory.

    Args:
        rule_pack_files: the rule packs to emit, as ``--rule-packs-dir``
            selected them (``_lib_input.selected``: every declared pack of
            ``_lib_consumer_read.RULE_PACKS``, the platform packs included
            since #1595 D1b as the subset ``platform_pack_for_operator``
            keeps)
        namespace: Target Kubernetes namespace
        components: One of ``_COMPONENTS`` (``all`` = rules + servicemonitor)
        selector_labels: Extra labels on the PrometheusRule / ServiceMonitor CRDs

    Returns:
        Dict with generated CRDs and metadata
    """
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "prometheus_rules": [],
        "service_monitor": None,
        # #1595 D1b: [{"pack", "rule", "reasons"}] left out of platform packs.
        "excluded_rules": [],
    }

    # PrometheusRule CRDs
    if components in ("all", "rules"):
        # #3206 (ADR-036 §12): the packs are read the way Prometheus'
        # rule loader reads them (`da-read prometheus-rules`, one run for
        # all). A pack the loader refuses raises `RuleFilesRefused` and
        # the generator stops (rc 2, naming it) — it used to be a WARNING
        # and a CRD set with the pack missing.
        rule_files = _lib_consumer_read.prometheus_rule_files(rule_pack_files)
        for rule_pack_file, rule_file in zip(rule_pack_files, rule_files):
            rule_pack_data = rule_file.documents[0]
            if "groups" in rule_pack_data:
                rule_pack_name = _lib_consumer_read.RULE_PACKS.member(rule_pack_file)
                dropped: list = []
                if _lib_consumer_read.is_platform_pack(rule_pack_name):
                    rule_pack_data, dropped = platform_pack_for_operator(rule_pack_data)
                for rule, reasons in dropped:
                    result["excluded_rules"].append(
                        {"pack": rule_pack_name, "rule": rule, "reasons": reasons})
                    print(i18n_text(
                        f"NOTE: {safe_label(rule_pack_file.name)} 的 {safe_label(rule)} "
                        f"未輸出（Prometheus Operator 模式沒有它讀的指標來源）："
                        f"{safe_label('; '.join(reasons))}",
                        f"NOTE: {safe_label(rule)} from {safe_label(rule_pack_file.name)} "
                        f"left out (no source for a metric it reads under the "
                        f"Prometheus Operator): {safe_label('; '.join(reasons))}",
                    ), file=sys.stderr)
                crd = build_prometheus_rule(
                    rule_pack_name, rule_pack_data, namespace, selector_labels,
                    [rule for rule, _ in dropped],
                )
                result["prometheus_rules"].append({
                    "name": crd["metadata"]["name"],
                    "file": rule_pack_file.name,
                    "crd": crd,
                })

    # ServiceMonitor CRD
    if components in ("all", "servicemonitor"):
        crd = build_servicemonitor(namespace, selector_labels)
        result["service_monitor"] = {
            "name": crd["metadata"]["name"],
            "crd": crd,
        }

    return result


def build_kustomization(crd_files: List[str], namespace: str) -> dict:
    """Build a kustomization.yaml dict.

    Args:
        crd_files: List of generated CRD filenames (without path)
        namespace: Target Kubernetes namespace

    Returns:
        Kustomization dict
    """
    resources = sorted(crd_files)
    return {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": resources,
        "commonLabels": {
            "app.kubernetes.io/part-of": "dynamic-alerting",
            "app.kubernetes.io/managed-by": "da-tools",
        },
        "namespace": namespace,
    }


# Kubernetes label syntax: an optional DNS-subdomain prefix (dot-separated
# DNS labels, at most 253 chars) and '/', then a name of at most 63 chars;
# a value of at most 63 chars (may be empty).
_LABEL_NAME_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?")
_DNS_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def _valid_label_key(key: str) -> bool:
    prefix, slash, name = key.rpartition("/")
    if slash and (len(prefix) > 253
                  or not all(_DNS_LABEL_RE.fullmatch(p) for p in prefix.split("."))):
        return False
    return bool(_LABEL_NAME_RE.fullmatch(name))


def _selector_label_arg(value: str) -> tuple:
    """argparse type for --selector-label: `key=value` with valid K8s syntax."""
    key, sep, val = value.partition("=")
    if not sep or not _valid_label_key(key) or not (val == "" or _LABEL_NAME_RE.fullmatch(val)):
        raise argparse.ArgumentTypeError(
            i18n_text(
                f"--selector-label 需為合法的 Kubernetes label key=value：{value!r}",
                f"--selector-label must be a valid Kubernetes label key=value: {value!r}",
            )
        )
    if key == _PART_OF_KEY:
        raise argparse.ArgumentTypeError(
            i18n_text(
                f"--selector-label 不可改寫 {_PART_OF_KEY}（它標示本工具產生的 CRD）",
                f"--selector-label cannot rewrite {_PART_OF_KEY} (it marks this tool's CRDs)",
            )
        )
    return key, val


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the operator-generate argument parser (construction only, no parse).

    Extracted from main() so the CLI surface is independently testable.
    """
    parser = argparse.ArgumentParser(
        description=i18n_text(_HELP["zh"]["desc"], _HELP["en"]["desc"]),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # #3462: the rule packs it emits are what this selects — the rule-pack
    # declaration (RULE_PACKS) of the directory's top level. None is no CRD
    # set (it was 0 PrometheusRules, rc 0), and the default `rule-packs`
    # nobody gave is judged as if given (owner, 2026-11-02).
    _lib_input.add_input(
        parser, "--rule-packs-dir", select=_lib_consumer_read.RULE_PACKS,
        empty=_lib_input.EMPTY_REFUSED, default="rule-packs",
        absent_default=_lib_input.ABSENT_REFUSED,
        help=i18n_text(_HELP["zh"]["rule_packs_dir"], _HELP["en"]["rule_packs_dir"]),
    )
    parser.add_argument(
        "--output-dir",
        type=_output_dir_arg,
        default=None,   # #1582：不給就不寫檔（寫入是 opt-in），見 main() 的分派
        help=i18n_text(_HELP["zh"]["output_dir"], _HELP["en"]["output_dir"]),
    )
    parser.add_argument(
        "--namespace",
        default="monitoring",
        help=i18n_text(_HELP["zh"]["namespace"], _HELP["en"]["namespace"]),
    )
    parser.add_argument(
        "--gitops",
        action="store_true",
        help=i18n_text(_HELP["zh"]["gitops"], _HELP["en"]["gitops"]),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=i18n_text(_HELP["zh"]["dry_run"], _HELP["en"]["dry_run"]),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=i18n_text(_HELP["zh"]["json"], _HELP["en"]["json"]),
    )
    parser.add_argument(
        "--components",
        choices=[*_COMPONENTS, _REFUSED_COMPONENT],
        metavar="{" + ",".join(_COMPONENTS) + "}",
        default="all",
        help=i18n_text(_HELP["zh"]["components"], _HELP["en"]["components"]),
    )
    # #3572: hidden and refused (`refuse_routing_flags`) — see _ROUTING_FLAGS.
    parser.add_argument("--config-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--api-version", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--receiver-template", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--secret-name", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--secret-key", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--kustomize",
        action="store_true",
        help=i18n_text(_HELP["zh"]["kustomize"], _HELP["en"]["kustomize"]),
    )
    parser.add_argument(
        "--selector-label",
        type=_selector_label_arg,
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=i18n_text(_HELP["zh"]["selector_label"], _HELP["en"]["selector_label"]),
    )
    return parser


def _ordered_crds(result: Dict[str, Any]) -> List[dict]:
    """CRD dicts in canonical emit order: PrometheusRules, ServiceMonitor."""
    crds = [item["crd"] for item in result["prometheus_rules"]]
    if result["service_monitor"]:
        crds.append(result["service_monitor"]["crd"])
    return crds


def _crd_filenames(result: Dict[str, Any]) -> List[str]:
    """`{name}.yaml` for every CRD, in the same order as _ordered_crds()."""
    return [f'{crd["metadata"]["name"]}.yaml' for crd in _ordered_crds(result)]


def _output_dir_arg(value: str) -> Path:
    """`--output-dir` 的 argparse type：拒絕空字串／純空白。

    ⛔ 不能靠 `type=Path` 之後在 main() 補判：`Path("")` 就是 `Path(".")`，
    到那時「使用者傳了空字串」與「使用者傳了 `.`」已經無法區分（實測：補判版本
    對 `--output-dir ""` 回 rc=0 並照樣寫檔）。判定必須發生在還看得見原始字串的
    這一層。
    ⚠️ 為什麼值得判：#1582 之後「要寫檔就必須打這個參數」，於是
    `--output-dir "$OUT"` 在 OUT 未設時會傳空字串、產物無聲落在呼叫目錄——
    命中面從零變成全部。`.` 仍然合法（明確要求寫進目前目錄）。
    """
    if not value.strip():
        raise argparse.ArgumentTypeError(i18n_text(
            "不能是空字串；要寫進目前目錄請明確傳 --output-dir .",
            "must not be empty; pass --output-dir . to write into the current directory",
        ))
    return Path(value)

def emit_dry_run(
    result: Dict[str, Any],
    kustomize: bool,
    namespace: str,
    as_json: bool,
    gitops: bool = False,
) -> tuple:
    """stdout output path: print CRD (and optional kustomization) YAML to stdout.

    Reached by ``--dry-run`` **and** by omitting ``--output-dir`` (#1582 — writing
    is opt-in). ``gitops`` is honoured here for the same reason it is in
    ``write_crds``: it is a *rendering* property (sorted keys), so dropping it on
    this path would silently ignore a flag the caller passed.

    In ``--json`` mode nothing is printed here — the documents are collected and
    returned so main() can emit the single #1112 envelope once at the end.

    Returns (json_crds, json_kustomization).
    """
    all_crds = _ordered_crds(result)
    json_crds: list = []
    json_kustomization = None

    if as_json:
        json_crds = all_crds
    else:
        if yaml:
            for i, crd in enumerate(all_crds):
                if i > 0:
                    print("---")
                print(yaml.dump(
                    crd, default_flow_style=False,
                    sort_keys=gitops, allow_unicode=True,
                ), end="")
        else:
            for crd in all_crds:
                print(_dict_to_yaml(crd))
                print("---")

    # Print kustomization.yaml if requested
    if kustomize:
        crd_files = _crd_filenames(result)
        kustomize_dict = build_kustomization(crd_files, namespace)
        if as_json:
            json_kustomization = kustomize_dict
        elif yaml:
            print("---")
            print(yaml.dump(
                kustomize_dict,
                default_flow_style=False,
                sort_keys=gitops,
                allow_unicode=True,
            ), end="")
        else:
            print("---")
            print(_dict_to_yaml(kustomize_dict))

    return json_crds, json_kustomization


def write_crds(
    result: Dict[str, Any],
    output_dir: Path,
    gitops: bool,
    kustomize: bool,
    namespace: str,
    as_json: bool,
) -> tuple:
    """Write CRD (and optional kustomization) files to output_dir.

    Emits one ``Generated: <path>`` line per file to stderr. Returns
    (json_crds, json_kustomization) for the #1112 envelope — the same stdout
    schema as the dry-run path (a report of what was generated / written).
    """
    # #1641: every path here descends from --output-dir. ensure_dir and the
    # shared write_yaml_crd RAISE OutputWriteError (library form); main()
    # catches it once around this call — rc=2 + one line naming the flag.
    ensure_dir(output_dir, flag="--output-dir")

    crd_files: List[str] = []

    for item in result["prometheus_rules"]:
        crd = item["crd"]
        name = crd["metadata"]["name"]
        output_path = output_dir / f"{name}.yaml"
        write_yaml_crd(output_path, crd, gitops=gitops, flag="--output-dir")
        print(f"Generated: {safe_label(output_path)}", file=sys.stderr)
        crd_files.append(f"{name}.yaml")

    if result["service_monitor"]:
        crd = result["service_monitor"]["crd"]
        name = crd["metadata"]["name"]
        output_path = output_dir / f"{name}.yaml"
        write_yaml_crd(output_path, crd, gitops=gitops, flag="--output-dir")
        print(f"Generated: {safe_label(output_path)}", file=sys.stderr)
        crd_files.append(f"{name}.yaml")

    # Write kustomization.yaml if requested
    json_kustomization = None
    if kustomize:
        kustomize_dict = build_kustomization(crd_files, namespace)
        kustomize_path = output_dir / "kustomization.yaml"
        write_yaml_crd(kustomize_path, kustomize_dict, gitops=gitops, flag="--output-dir")
        print(f"Generated: {safe_label(kustomize_path)}", file=sys.stderr)
        json_kustomization = kustomize_dict

    json_crds: list = []
    if as_json:
        # Same stdout schema as the dry-run path: the document reports what was
        # generated (and, here, also written to --output-dir).
        json_crds = _ordered_crds(result)

    return json_crds, json_kustomization


def build_summary(result: Dict[str, Any], kustomize: bool) -> Dict[str, int]:
    """Count generated artefacts — shared by the --json envelope and the stderr line."""
    base_total = (
        len(result["prometheus_rules"]) +
        (1 if result["service_monitor"] else 0)
    )
    kustomize_count = 1 if kustomize else 0
    return {
        "prometheus_rules": len(result["prometheus_rules"]),
        "service_monitor": 1 if result["service_monitor"] else 0,
        "kustomization": kustomize_count,
        "total": base_total + kustomize_count,
    }


def json_envelope(
    json_crds: list,
    json_kustomization,
    summary: Dict[str, int],
) -> dict:
    """#1112 single-document envelope: ``{"crds", "kustomization", "summary"}``
    (run() encodes it, #3216)."""
    return {
        "crds": json_crds,
        "kustomization": json_kustomization,
        "summary": summary,
    }


def _summary_message(summary: Dict[str, int], kustomize: bool) -> str:
    """Human-readable (stderr) success line — bilingual, kustomize-aware."""
    if kustomize:
        msg_zh = (
            f"\n✓ 已產出 {summary['total']} 個檔案: "
            f"{summary['prometheus_rules']} PrometheusRules, "
            f"{summary['service_monitor']} ServiceMonitor, "
            f"1 kustomization.yaml"
        )
        msg_en = (
            f"\n✓ Generated {summary['total']} files: "
            f"{summary['prometheus_rules']} PrometheusRules, "
            f"{summary['service_monitor']} ServiceMonitor, "
            f"1 kustomization.yaml"
        )
    else:
        msg_zh = (
            f"\n✓ 已產出 {summary['total']} 個 CRD: "
            f"{summary['prometheus_rules']} PrometheusRules, "
            f"{summary['service_monitor']} ServiceMonitor"
        )
        msg_en = (
            f"\n✓ Generated {summary['total']} CRDs: "
            f"{summary['prometheus_rules']} PrometheusRules, "
            f"{summary['service_monitor']} ServiceMonitor"
        )
    return i18n_text(msg_zh, msg_en)


def refuse_routing_flags(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """#3572: a flag that only fed AlertmanagerConfig is a caller error (rc 2,
    nothing on stdout, nothing written) that names it and says how routing
    is rendered now. It counts when it was given (`supplied`), whatever its
    value: an explicit `--config-dir conf.d` (the old default) is refused
    like any other."""
    given = [flag for flag, dest in _ROUTING_FLAGS if supplied(args, parser, dest)]
    if given:
        die_caller_error(
            f"ERROR: {', '.join(given)}: "
            f"{i18n_text(OPERATOR_ROUTING_REMEDY_ZH, OPERATOR_ROUTING_REMEDY_EN)}",
            reason="routing_not_generated")


def refuse_routing_component(args: argparse.Namespace) -> None:
    """#3572: `--components alertmanager` is a caller error (rc 2, nothing on
    stdout, nothing written) saying how routing is rendered now."""
    if args.components == _REFUSED_COMPONENT:
        die_caller_error(
            f"ERROR: --components {_REFUSED_COMPONENT}: "
            f"{i18n_text(OPERATOR_ROUTING_REMEDY_ZH, OPERATOR_ROUTING_REMEDY_EN)}",
            reason="routing_not_generated")


def main():
    """CLI entry point (thin orchestration: parse → validate → generate → emit → exit)."""
    parser = build_arg_parser()
    args = parser.parse_args()
    refuse_routing_flags(args, parser)
    refuse_routing_component(args)

    # #1582：沒給 --output-dir 就沒有寫入目標。⛔ 不要在這裡塞回一個預設值——
    # 那正是本票的缺陷形狀（輸入被重導、輸出沒被重導，仍寫進呼叫目錄）。
    output_dir = args.output_dir.resolve() if args.output_dir is not None else None

    # #1582：stdout 現在是**預設**的產物通道，所以它必須和寫檔通道等價。
    # ⛔ 不等價是量出來的，不是預防性的：在 cp950 主控台上直接跑本檔，PyYAML
    # 對目標編碼放不下的字元（例如 `≥`）會退成字面 `≥`，而 write_text_secure
    # 寫出的檔是真正的字元 —— 同一次比對 15 份相同、1 份語意不同、rc=0 無聲。
    # 換行同理：write_text_secure 明文強制 LF，而 print() 在 Windows 會譯成 CRLF。
    # `da-tools` dispatcher 已經做同一件事（entrypoint._configure_std_utf8），
    # 這裡補的是「直接 python 跑本檔」那條 entrypoint docstring 自承支援的路徑。
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, ValueError, OSError):  # 非 TextIOWrapper 的 stdout
        pass

    # Generate CRDs
    try:
        # #3392 / #3462: --rule-packs-dir — given or the default — that is not
        # there, no directory, cannot be listed or holds no rule pack is a
        # caller error (rc 2, `_lib_input`), named before anything is
        # generated or written: it was a WARNING and a CRD set without its
        # PrometheusRules, rc 0. Only when the chosen --components reads it.
        rule_pack_files = (
            [p.resolve() for p in _lib_input.selected(args, "rule_packs_dir", lang=_LANG)]
            if args.components in ("all", "rules") else [])
        result = generate_crds(
            rule_pack_files,
            args.namespace,
            args.components,
            selector_labels=dict(args.selector_label),
        )
    except _lib_consumer_read.ConsumerReadError as exc:
        # #3206: a rule pack Prometheus' rule loader refuses, or no da-read.
        die_caller_error(f"ERROR: rule packs: {safe_label(_lib_consumer_read.error_text(exc))}",
                         reason="rule_packs_unreadable")
    except Exception as exc:  # noqa: BLE001 — the tool's own "no result"
        # rc 2 and one line; nothing on stdout, --json or not (#3558).
        die_caller_error(f"ERROR: {safe_label(exc)}", reason="generate_failed")

    # Emit CRDs. No --output-dir (or --dry-run) prints YAML to stdout; the write path writes files and
    # logs `Generated:` lines to stderr. Both hand back the documents the #1112
    # single-document JSON envelope needs (empty list / None unless --json), so
    # in `--json` mode stdout stays exactly ONE document (emitted at the end).
    if args.dry_run or output_dir is None:
        json_crds, json_kustomization = emit_dry_run(
            result, args.kustomize, args.namespace, args.json,
            gitops=args.gitops,
        )
    else:
        try:
            json_crds, json_kustomization = write_crds(
                result, output_dir, args.gitops, args.kustomize,
                args.namespace, args.json,
            )
        except OutputWriteError as exc:
            # #1641: unusable --output-dir ⇒ rc=2 + one line, not a traceback
            # at rc=1 (which reads as "your config has a finding").
            die_caller_error(f"ERROR: {exc}", reason="output_unwritable")

    summary = build_summary(result, args.kustomize)

    if args.json:
        # ONE document, every mode (#1112): the CRDs that were generated, the
        # kustomization (null unless --kustomize), and the counts. `--json` is a
        # dry-run *report* flag (cli-reference §operator-generate) — the
        # applyable artefacts are the YAML/`--output-dir` paths, which are
        # untouched.
        return Report(json_envelope(json_crds, json_kustomization, summary))  # #3216
    print(_summary_message(summary, args.kustomize), file=sys.stderr)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
