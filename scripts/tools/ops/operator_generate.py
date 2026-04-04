#!/usr/bin/env python3
"""operator-generate — Generate Kubernetes CRD YAML for Prometheus + Alertmanager.

Reads rule-packs/ and conf.d/ directories, generates PrometheusRule,
AlertmanagerConfig, and ServiceMonitor CRDs for dynamic alerting stack.

Usage:
    da-tools operator-generate
    da-tools operator-generate --rule-packs-dir /path/to/rule-packs
    da-tools operator-generate --output-dir operator-crds/
    da-tools operator-generate --namespace monitoring --dry-run
    da-tools operator-generate --components rules,alertmanager
    da-tools operator-generate --gitops --json
"""
from __future__ import annotations

import argparse
import json
import os
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
from _lib_io import load_yaml_file, write_text_secure  # noqa: E402

# ---------------------------------------------------------------------------
# Constants and Help Text
# ---------------------------------------------------------------------------

_HELP = {
    "zh": {
        "desc": "生成 Prometheus + Alertmanager CRD YAML（PrometheusRule、AlertmanagerConfig、ServiceMonitor）",
        "rule_packs_dir": "Rule packs 目錄（預設 rule-packs/）",
        "config_dir": "租户配置目錄（預設 conf.d/）",
        "output_dir": "輸出 CRD 目錄（預設 operator-output/）",
        "namespace": "目標命名空間（預設 monitoring）",
        "api_version": "AlertmanagerConfig API 版本（預設 v1beta1）",
        "gitops": "啟用 GitOps 模式（排序鍵、無時間戳）",
        "dry_run": "列印輸出而不寫入檔案",
        "json": "以 JSON 格式輸出結果報告",
        "components": "要生成的元件（all | rules | alertmanager | servicemonitor）",
    },
    "en": {
        "desc": "Generate Kubernetes CRD YAML for Prometheus + Alertmanager (PrometheusRule, AlertmanagerConfig, ServiceMonitor)",
        "rule_packs_dir": "Rule packs directory (default rule-packs/)",
        "config_dir": "Tenant config directory (default conf.d/)",
        "output_dir": "Output CRD directory (default operator-output/)",
        "namespace": "Target namespace (default monitoring)",
        "api_version": "AlertmanagerConfig API version (default v1beta1)",
        "gitops": "Enable GitOps mode (sorted keys, no timestamps)",
        "dry_run": "Print output instead of writing files",
        "json": "Output results as JSON report",
        "components": "Components to generate (all | rules | alertmanager | servicemonitor)",
    },
}

_LANG = detect_cli_lang()

# ---------------------------------------------------------------------------
# CRD Builders
# ---------------------------------------------------------------------------


def build_prometheus_rule(rule_pack_name: str, rule_pack_data: dict, namespace: str) -> dict:
    """Build a PrometheusRule CRD from a rule pack YAML.

    Args:
        rule_pack_name: Name of the rule pack (e.g., 'mariadb')
        rule_pack_data: Parsed YAML dict with 'groups' key
        namespace: Target Kubernetes namespace

    Returns:
        PrometheusRule CRD dict
    """
    groups = rule_pack_data.get("groups", [])

    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": f"da-rule-pack-{rule_pack_name}",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/part-of": "dynamic-alerting",
                "prometheus": "kube-prometheus",
            },
        },
        "spec": {
            "groups": groups,
        },
    }


def build_servicemonitor(namespace: str) -> dict:
    """Build a ServiceMonitor CRD for threshold-exporter.

    Args:
        namespace: Target Kubernetes namespace

    Returns:
        ServiceMonitor CRD dict
    """
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {
            "name": "da-threshold-exporter",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/part-of": "dynamic-alerting",
            },
        },
        "spec": {
            "selector": {
                "matchLabels": {
                    "app": "threshold-exporter",
                },
            },
            "endpoints": [
                {
                    "port": "metrics",
                    "interval": "15s",
                },
            ],
        },
    }


def build_alertmanager_config(
    tenant_name: str,
    namespace: str,
    api_version: str = "v1beta1",
) -> dict:
    """Build an AlertmanagerConfig CRD (template for tenant).

    Args:
        tenant_name: Tenant identifier
        namespace: Target Kubernetes namespace
        api_version: AlertmanagerConfig API version (v1alpha1 or v1beta1)

    Returns:
        AlertmanagerConfig CRD dict
    """
    return {
        "apiVersion": f"monitoring.coreos.com/{api_version}",
        "kind": "AlertmanagerConfig",
        "metadata": {
            "name": f"da-tenant-{tenant_name}",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/part-of": "dynamic-alerting",
                "tenant": tenant_name,
            },
        },
        "spec": {
            "route": {
                "receiver": f"{tenant_name}-receiver",
                "groupBy": ["alertname", "instance"],
                "groupWait": "5s",
                "groupInterval": "5m",
                "repeatInterval": "12h",
            },
            "receivers": [
                {
                    "name": f"{tenant_name}-receiver",
                    "webhookConfigs": [
                        {
                            "url": "http://localhost:5001/webhook",
                        },
                    ],
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def discover_rule_packs(rule_packs_dir: Path) -> List[Path]:
    """Discover all rule-pack-*.yaml files.

    Args:
        rule_packs_dir: Directory containing rule packs

    Returns:
        Sorted list of rule pack file paths

    Raises:
        FileNotFoundError: If directory does not exist
    """
    if not rule_packs_dir.is_dir():
        raise FileNotFoundError(f"rule-packs directory not found: {rule_packs_dir}")
    return sorted(rule_packs_dir.glob("rule-pack-*.yaml"))


def discover_tenant_configs(config_dir: Path) -> List[str]:
    """Discover tenant names from conf.d/*.yaml files.

    Args:
        config_dir: Directory containing tenant configs

    Returns:
        Sorted list of tenant names

    Raises:
        FileNotFoundError: If directory does not exist
    """
    if not config_dir.is_dir():
        raise FileNotFoundError(f"config directory not found: {config_dir}")

    tenants = []
    for yaml_file in config_dir.glob("*.yaml"):
        if not yaml_file.name.startswith("_"):
            tenant = yaml_file.stem
            tenants.append(tenant)
    return sorted(tenants)


def write_yaml_crd(
    output_path: Path,
    crd: dict,
    gitops: bool = False,
) -> None:
    """Write CRD to YAML file.

    Args:
        output_path: Output file path
        crd: CRD dict to serialize
        gitops: If True, use sorted keys and exclude timestamps
    """
    if yaml:
        # Use yaml module if available
        yaml_str = yaml.dump(
            crd,
            default_flow_style=False,
            sort_keys=gitops,
            allow_unicode=True,
        )
    else:
        # Fallback: minimal YAML serialization
        yaml_str = _dict_to_yaml(crd)

    write_text_secure(str(output_path), yaml_str)


def _dict_to_yaml(obj: Any, indent: int = 0) -> str:
    """Minimal YAML serialization (fallback when yaml module unavailable)."""
    if isinstance(obj, dict):
        lines = []
        for k, v in obj.items():
            val_str = _dict_to_yaml(v, indent + 2)
            if "\n" in val_str:
                lines.append(f"{' ' * indent}{k}:\n{val_str}")
            else:
                lines.append(f"{' ' * indent}{k}: {val_str}")
        return "\n".join(lines)
    elif isinstance(obj, list):
        lines = []
        for item in obj:
            item_str = _dict_to_yaml(item, indent + 2)
            if "\n" in item_str:
                lines.append(f"{' ' * indent}-\n{item_str}")
            else:
                lines.append(f"{' ' * indent}- {item_str}")
        return "\n".join(lines)
    elif isinstance(obj, bool):
        return "true" if obj else "false"
    elif isinstance(obj, str):
        # Quote strings that need it
        if any(c in obj for c in ":[]{},'\""):
            return f'"{obj}"'
        return obj
    else:
        return str(obj)


# ---------------------------------------------------------------------------
# Main Logic
# ---------------------------------------------------------------------------


def generate_crds(
    rule_packs_dir: Path,
    config_dir: Path,
    namespace: str,
    api_version: str,
    components: str,
) -> Dict[str, Any]:
    """Generate all CRDs from input directories.

    Args:
        rule_packs_dir: Path to rule-packs directory
        config_dir: Path to conf.d directory
        namespace: Target Kubernetes namespace
        api_version: AlertmanagerConfig API version
        components: Comma-separated components to generate

    Returns:
        Dict with generated CRDs and metadata
    """
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "prometheus_rules": [],
        "alertmanager_configs": [],
        "service_monitor": None,
    }

    # PrometheusRule CRDs
    if components in ("all", "rules"):
        try:
            rule_pack_files = discover_rule_packs(rule_packs_dir)
            for rule_pack_file in rule_pack_files:
                try:
                    rule_pack_data = load_yaml_file(str(rule_pack_file))
                    if rule_pack_data and "groups" in rule_pack_data:
                        rule_pack_name = rule_pack_file.stem.replace("rule-pack-", "")
                        crd = build_prometheus_rule(rule_pack_name, rule_pack_data, namespace)
                        result["prometheus_rules"].append({
                            "name": crd["metadata"]["name"],
                            "file": rule_pack_file.name,
                            "crd": crd,
                        })
                except Exception as exc:
                    print(
                        f"WARNING: Failed to load {rule_pack_file.name}: {exc}",
                        file=sys.stderr,
                    )
        except FileNotFoundError as exc:
            print(f"WARNING: {exc}", file=sys.stderr)

    # AlertmanagerConfig CRDs
    if components in ("all", "alertmanager"):
        try:
            tenants = discover_tenant_configs(config_dir)
            for tenant in tenants:
                crd = build_alertmanager_config(tenant, namespace, api_version)
                result["alertmanager_configs"].append({
                    "name": crd["metadata"]["name"],
                    "tenant": tenant,
                    "crd": crd,
                })
        except FileNotFoundError as exc:
            print(f"WARNING: {exc}", file=sys.stderr)

    # ServiceMonitor CRD
    if components in ("all", "servicemonitor"):
        crd = build_servicemonitor(namespace)
        result["service_monitor"] = {
            "name": crd["metadata"]["name"],
            "crd": crd,
        }

    return result


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=i18n_text(_HELP["zh"]["desc"], _HELP["en"]["desc"]),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--rule-packs-dir",
        type=Path,
        default=Path("rule-packs"),
        help=i18n_text(_HELP["zh"]["rule_packs_dir"], _HELP["en"]["rule_packs_dir"]),
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("conf.d"),
        help=i18n_text(_HELP["zh"]["config_dir"], _HELP["en"]["config_dir"]),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("operator-output"),
        help=i18n_text(_HELP["zh"]["output_dir"], _HELP["en"]["output_dir"]),
    )
    parser.add_argument(
        "--namespace",
        default="monitoring",
        help=i18n_text(_HELP["zh"]["namespace"], _HELP["en"]["namespace"]),
    )
    parser.add_argument(
        "--api-version",
        choices=["v1alpha1", "v1beta1"],
        default="v1beta1",
        help=i18n_text(_HELP["zh"]["api_version"], _HELP["en"]["api_version"]),
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
        choices=["all", "rules", "alertmanager", "servicemonitor"],
        default="all",
        help=i18n_text(_HELP["zh"]["components"], _HELP["en"]["components"]),
    )

    args = parser.parse_args()

    # Resolve paths relative to current directory
    rule_packs_dir = args.rule_packs_dir.resolve()
    config_dir = args.config_dir.resolve()
    output_dir = args.output_dir.resolve()

    # Generate CRDs
    try:
        result = generate_crds(
            rule_packs_dir,
            config_dir,
            args.namespace,
            args.api_version,
            args.components,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # Write or print CRDs
    if args.dry_run:
        # Print YAML to stdout
        all_crds = []
        for item in result["prometheus_rules"]:
            all_crds.append(item["crd"])
        for item in result["alertmanager_configs"]:
            all_crds.append(item["crd"])
        if result["service_monitor"]:
            all_crds.append(result["service_monitor"]["crd"])

        if args.json:
            print(json.dumps(all_crds, indent=2, ensure_ascii=False))
        else:
            if yaml:
                for i, crd in enumerate(all_crds):
                    if i > 0:
                        print("---")
                    print(yaml.dump(crd, default_flow_style=False, allow_unicode=True), end="")
            else:
                for crd in all_crds:
                    print(_dict_to_yaml(crd))
                    print("---")
    else:
        # Write CRD files
        output_dir.mkdir(parents=True, exist_ok=True)

        count = 0
        for item in result["prometheus_rules"]:
            crd = item["crd"]
            name = crd["metadata"]["name"]
            output_path = output_dir / f"{name}.yaml"
            write_yaml_crd(output_path, crd, gitops=args.gitops)
            print(f"Generated: {output_path}", file=sys.stderr)
            count += 1

        for item in result["alertmanager_configs"]:
            crd = item["crd"]
            name = crd["metadata"]["name"]
            output_path = output_dir / f"{name}.yaml"
            write_yaml_crd(output_path, crd, gitops=args.gitops)
            print(f"Generated: {output_path}", file=sys.stderr)
            count += 1

        if result["service_monitor"]:
            crd = result["service_monitor"]["crd"]
            name = crd["metadata"]["name"]
            output_path = output_dir / f"{name}.yaml"
            write_yaml_crd(output_path, crd, gitops=args.gitops)
            print(f"Generated: {output_path}", file=sys.stderr)
            count += 1

    # Summary
    summary = {
        "prometheus_rules": len(result["prometheus_rules"]),
        "alertmanager_configs": len(result["alertmanager_configs"]),
        "service_monitor": 1 if result["service_monitor"] else 0,
        "total": len(result["prometheus_rules"]) + len(result["alertmanager_configs"]) + (1 if result["service_monitor"] else 0),
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(
            f"\n✓ Generated {summary['total']} CRDs: "
            f"{summary['prometheus_rules']} PrometheusRules, "
            f"{summary['alertmanager_configs']} AlertmanagerConfigs, "
            f"{summary['service_monitor']} ServiceMonitor",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
