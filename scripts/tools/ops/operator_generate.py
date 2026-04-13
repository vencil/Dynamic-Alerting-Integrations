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
    da-tools operator-generate --receiver-template slack --secret-name da-slack --secret-key webhook-url
    da-tools operator-generate --receiver-template pagerduty --secret-name da-pd --secret-key routing-key
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

import re

_TENANT_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")

_RECEIVER_TEMPLATES = ("slack", "pagerduty", "email", "teams", "opsgenie", "webhook")

_HELP = {
    "zh": {
        "desc": "生成 Prometheus + Alertmanager CRD YAML（PrometheusRule、AlertmanagerConfig、ServiceMonitor）",
        "rule_packs_dir": "Rule packs 目錄（預設 rule-packs/）",
        "config_dir": "租户配置目錄（預設 conf.d/）",
        "output_dir": "輸出 CRD 目錄（預設 operator-manifests/）",
        "namespace": "目標命名空間（預設 monitoring）",
        "api_version": "AlertmanagerConfig API 版本（預設 v1beta1）",
        "gitops": "啟用 GitOps 模式（排序鍵、無時間戳）",
        "dry_run": "列印輸出而不寫入檔案",
        "json": "以 JSON 格式輸出結果報告",
        "components": "要生成的元件（all | rules | alertmanager | servicemonitor）",
        "receiver_template": f"Receiver 模板類型（{' | '.join(_RECEIVER_TEMPLATES)}）",
        "secret_name": "K8s Secret 名稱（receiver 機密引用）",
        "secret_key": "K8s Secret 中的 key 名稱（預設依 receiver 類型自動推斷）",
        "kustomize": "生成 kustomization.yaml，列出所有 CRD 檔案為資源",
    },
    "en": {
        "desc": "Generate Kubernetes CRD YAML for Prometheus + Alertmanager (PrometheusRule, AlertmanagerConfig, ServiceMonitor)",
        "rule_packs_dir": "Rule packs directory (default rule-packs/)",
        "config_dir": "Tenant config directory (default conf.d/)",
        "output_dir": "Output CRD directory (default operator-manifests/)",
        "namespace": "Target namespace (default monitoring)",
        "api_version": "AlertmanagerConfig API version (default v1beta1)",
        "gitops": "Enable GitOps mode (sorted keys, no timestamps)",
        "dry_run": "Print output instead of writing files",
        "json": "Output results as JSON report",
        "components": "Components to generate (all | rules | alertmanager | servicemonitor)",
        "receiver_template": f"Receiver template type ({' | '.join(_RECEIVER_TEMPLATES)})",
        "secret_name": "K8s Secret name (for receiver credential reference)",
        "secret_key": "Key within the K8s Secret (auto-inferred from receiver type if omitted)",
        "kustomize": "Generate kustomization.yaml listing all CRD files as resources",
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
                    "port": "http",
                    "interval": "15s",
                },
            ],
        },
    }


_DEFAULT_SECRET_KEYS = {
    "slack": "webhook-url",
    "pagerduty": "routing-key",
    "email": "smtp-password",
    "teams": "webhook-url",
    "opsgenie": "api-key",
    "webhook": "auth-token",
}


def _build_slack_config(tenant_name: str, secret_ref: dict) -> dict:
    """Build Slack receiver config."""
    return {
        "slackConfigs": [
            {
                "apiURL": {"secret": secret_ref},
                "channel": f"#alerts-{tenant_name}",
                "title": '{{ template "slack.default.title" . }}',
                "text": '{{ or .CommonAnnotations.summary_zh .CommonAnnotations.summary "Alert triggered" }}',
                "sendResolved": True,
            },
        ],
    }


def _build_pagerduty_config(tenant_name: str, secret_ref: dict) -> dict:
    """Build PagerDuty receiver config."""
    return {
        "pagerdutyConfigs": [
            {
                "routingKey": {"secret": secret_ref},
                "description": '{{ template "pagerduty.default.description" . }}',
                "severity": '{{ if eq .CommonLabels.severity "critical" }}critical{{ else }}warning{{ end }}',
                "sendResolved": True,
            },
        ],
    }


def _build_email_config(tenant_name: str, secret_ref: dict) -> dict:
    """Build Email receiver config.

    NOTE: to/from/smarthost are placeholders — operators MUST override
    via AlertmanagerConfig overlay or Helm values before production use.
    """
    return {
        "emailConfigs": [
            {
                "to": f"alerts-{tenant_name}@example.com",
                "from": "dynamic-alerting@example.com",
                "smarthost": "smtp.example.com:587",
                "authUsername": f"da-alerts-{tenant_name}",
                "authPassword": {"secret": secret_ref},
                "requireTLS": True,
                "sendResolved": True,
            },
        ],
    }


def _build_teams_config(tenant_name: str, secret_ref: dict) -> dict:
    """Build Microsoft Teams receiver config."""
    return {
        "webhookConfigs": [
            {
                "url": "http://prometheus-msteams:2000/alertmanager",
                "httpConfig": {
                    "authorization": {
                        "credentials": {"secret": secret_ref},
                    },
                },
                "sendResolved": True,
            },
        ],
    }


def _build_opsgenie_config(tenant_name: str, secret_ref: dict) -> dict:
    """Build OpsGenie receiver config."""
    return {
        "opsgenieConfigs": [
            {
                "apiKey": {"secret": secret_ref},
                "message": '{{ .CommonAnnotations.summary | default "DA Alert" }}',
                "priority": '{{ if eq .CommonLabels.severity "critical" }}P1{{ else }}P3{{ end }}',
                "tags": f"dynamic-alerting,{tenant_name}",
                "sendResolved": True,
            },
        ],
    }


def _build_webhook_config(tenant_name: str, secret_ref: dict) -> dict:
    """Build Webhook receiver config."""
    return {
        "webhookConfigs": [
            {
                "url": f"http://alert-receiver:5001/webhook/{tenant_name}",
                "httpConfig": {
                    "authorization": {
                        "credentials": {"secret": secret_ref},
                    },
                },
                "sendResolved": True,
            },
        ],
    }


_RECEIVER_STRATEGIES = {
    "slack": _build_slack_config,
    "pagerduty": _build_pagerduty_config,
    "email": _build_email_config,
    "teams": _build_teams_config,
    "opsgenie": _build_opsgenie_config,
    "webhook": _build_webhook_config,
}


def _build_receiver_config(
    tenant_name: str,
    receiver_type: str,
    secret_name: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> dict:
    """Build a receiver config block for the given template type.

    All credential fields use secretKeyRef — plaintext values are NEVER
    written into the YAML output (enterprise audit requirement).

    Args:
        tenant_name: Tenant identifier
        receiver_type: One of _RECEIVER_TEMPLATES
        secret_name: K8s Secret name; defaults to 'da-{tenant}-{type}'
        secret_key: Key within Secret; defaults to _DEFAULT_SECRET_KEYS[type]

    Returns:
        Receiver config dict for embedding in AlertmanagerConfig spec
    """
    effective_secret = secret_name or f"da-{tenant_name}-{receiver_type}"
    effective_key = secret_key or _DEFAULT_SECRET_KEYS.get(receiver_type, "token")
    secret_ref = {
        "name": effective_secret,
        "key": effective_key,
    }

    receiver: Dict[str, Any] = {"name": f"{tenant_name}-{receiver_type}"}

    # Dispatch to strategy function
    strategy = _RECEIVER_STRATEGIES.get(receiver_type)
    if strategy:
        receiver.update(strategy(tenant_name, secret_ref))
    else:
        # Fallback: generic webhook (no secret)
        receiver["webhookConfigs"] = [
            {"url": "http://localhost:5001/webhook"},
        ]

    return receiver


def _build_inhibit_rules_crd(tenant_name: str) -> list:
    """Build CRD-format inhibit rules for severity dedup + silent/maintenance modes.

    Returns:
        List of inhibitRules for AlertmanagerConfig spec
    """
    return [
        # Severity dedup: critical suppresses warning
        {
            "sourceMatch": [
                {"name": "tenant", "value": tenant_name},
                {"name": "severity", "value": "critical"},
            ],
            "targetMatch": [
                {"name": "tenant", "value": tenant_name},
                {"name": "severity", "value": "warning"},
            ],
            "equal": ["alertname", "instance"],
        },
        # Silent mode: sentinel suppresses warning alerts
        {
            "sourceMatch": [
                {"name": "tenant", "value": tenant_name},
                {"name": "alertname", "value": "TenantSilentWarning"},
            ],
            "targetMatch": [
                {"name": "tenant", "value": tenant_name},
                {"name": "severity", "value": "warning"},
            ],
            "equal": ["tenant"],
        },
        # Silent mode: sentinel suppresses critical alerts
        {
            "sourceMatch": [
                {"name": "tenant", "value": tenant_name},
                {"name": "alertname", "value": "TenantSilentCritical"},
            ],
            "targetMatch": [
                {"name": "tenant", "value": tenant_name},
                {"name": "severity", "value": "critical"},
            ],
            "equal": ["tenant"],
        },
        # Maintenance mode: sentinel suppresses ALL severity levels
        {
            "sourceMatch": [
                {"name": "tenant", "value": tenant_name},
                {"name": "alertname", "value": "TenantMaintenanceMode"},
            ],
            "targetMatch": [
                {"name": "tenant", "value": tenant_name},
            ],
            "equal": ["tenant"],
        },
    ]


def build_alertmanager_config(
    tenant_name: str,
    namespace: str,
    api_version: str = "v1beta1",
    receiver_template: Optional[str] = None,
    secret_name: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> dict:
    """Build an AlertmanagerConfig CRD for a tenant.

    Args:
        tenant_name: Tenant identifier
        namespace: Target Kubernetes namespace
        api_version: AlertmanagerConfig API version (v1alpha1 or v1beta1)
        receiver_template: Receiver type (slack/pagerduty/email/teams/opsgenie/webhook)
        secret_name: K8s Secret name for credential reference
        secret_key: Key within the K8s Secret

    Returns:
        AlertmanagerConfig CRD dict
    """
    # Build receiver
    if receiver_template and receiver_template in _RECEIVER_TEMPLATES:
        receiver = _build_receiver_config(
            tenant_name, receiver_template, secret_name, secret_key,
        )
        receiver_name = receiver["name"]
    else:
        receiver_name = f"{tenant_name}-receiver"
        receiver = {
            "name": receiver_name,
            "webhookConfigs": [
                {"url": "http://localhost:5001/webhook"},
            ],
        }

    # Build inhibit rules (severity dedup + silent/maintenance modes)
    inhibit_rules = _build_inhibit_rules_crd(tenant_name)

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
                "receiver": receiver_name,
                "groupBy": ["alertname", "instance"],
                "groupWait": "5s",
                "groupInterval": "5m",
                "repeatInterval": "12h",
                "matchers": [
                    {"name": "tenant", "value": tenant_name},
                ],
            },
            "receivers": [receiver],
            "inhibitRules": inhibit_rules,
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
        raise FileNotFoundError(
            i18n_text(
                f"rule-packs 目錄不存在: {rule_packs_dir}",
                f"rule-packs directory not found: {rule_packs_dir}",
            )
        )
    return sorted(rule_packs_dir.glob("rule-pack-*.yaml"))


def validate_tenant_name(name: str) -> bool:
    """Validate tenant name against K8s label value rules (RFC 1123).

    Must be lowercase alphanumeric + hyphens, max 63 chars, start/end with
    alphanumeric.

    Args:
        name: Tenant name to validate

    Returns:
        True if valid
    """
    return bool(_TENANT_NAME_RE.match(name))


def discover_tenant_configs(config_dir: Path) -> List[str]:
    """Discover tenant names from conf.d/*.yaml files.

    Args:
        config_dir: Directory containing tenant configs

    Returns:
        Sorted list of valid tenant names

    Raises:
        FileNotFoundError: If directory does not exist
    """
    if not config_dir.is_dir():
        raise FileNotFoundError(
            i18n_text(
                f"配置目錄不存在: {config_dir}",
                f"config directory not found: {config_dir}",
            )
        )

    tenants = []
    for yaml_file in config_dir.glob("*.yaml"):
        if not yaml_file.name.startswith("_"):
            tenant = yaml_file.stem
            if validate_tenant_name(tenant):
                tenants.append(tenant)
            else:
                print(
                    i18n_text(
                        f"WARNING: 略過無效的租戶名稱 '{tenant}'（不符合 RFC 1123）",
                        f"WARNING: Skipping invalid tenant name '{tenant}' (not RFC 1123 compliant)",
                    ),
                    file=sys.stderr,
                )
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
    receiver_template: Optional[str] = None,
    secret_name: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate all CRDs from input directories.

    Args:
        rule_packs_dir: Path to rule-packs directory
        config_dir: Path to conf.d directory
        namespace: Target Kubernetes namespace
        api_version: AlertmanagerConfig API version
        components: Comma-separated components to generate
        receiver_template: Receiver type for AlertmanagerConfig
        secret_name: K8s Secret name for credential reference
        secret_key: Key within the K8s Secret

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
                        i18n_text(
                            f"WARNING: 載入 {rule_pack_file.name} 失敗: {exc}",
                            f"WARNING: Failed to load {rule_pack_file.name}: {exc}",
                        ),
                        file=sys.stderr,
                    )
        except FileNotFoundError as exc:
            print(f"WARNING: {exc}", file=sys.stderr)

    # AlertmanagerConfig CRDs
    if components in ("all", "alertmanager"):
        try:
            tenants = discover_tenant_configs(config_dir)
            for tenant in tenants:
                crd = build_alertmanager_config(
                    tenant, namespace, api_version,
                    receiver_template, secret_name, secret_key,
                )
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
        default=Path("operator-manifests"),
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
    parser.add_argument(
        "--receiver-template",
        choices=list(_RECEIVER_TEMPLATES),
        default=None,
        help=i18n_text(
            _HELP["zh"]["receiver_template"],
            _HELP["en"]["receiver_template"],
        ),
    )
    parser.add_argument(
        "--secret-name",
        default=None,
        help=i18n_text(_HELP["zh"]["secret_name"], _HELP["en"]["secret_name"]),
    )
    parser.add_argument(
        "--secret-key",
        default=None,
        help=i18n_text(_HELP["zh"]["secret_key"], _HELP["en"]["secret_key"]),
    )
    parser.add_argument(
        "--kustomize",
        action="store_true",
        help=i18n_text(_HELP["zh"]["kustomize"], _HELP["en"]["kustomize"]),
    )

    args = parser.parse_args()

    # Resolve paths relative to current directory
    rule_packs_dir = args.rule_packs_dir.resolve()
    config_dir = args.config_dir.resolve()
    output_dir = args.output_dir.resolve()

    # Validate: --secret-name requires --receiver-template
    if args.secret_name and not args.receiver_template:
        parser.error(
            i18n_text(
                "--secret-name 需搭配 --receiver-template 使用",
                "--secret-name requires --receiver-template",
            )
        )

    # Generate CRDs
    try:
        result = generate_crds(
            rule_packs_dir,
            config_dir,
            args.namespace,
            args.api_version,
            args.components,
            receiver_template=args.receiver_template,
            secret_name=args.secret_name,
            secret_key=args.secret_key,
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

        # Print kustomization.yaml if requested
        if args.kustomize:
            crd_files = []
            for item in result["prometheus_rules"]:
                crd = item["crd"]
                name = crd["metadata"]["name"]
                crd_files.append(f"{name}.yaml")
            for item in result["alertmanager_configs"]:
                crd = item["crd"]
                name = crd["metadata"]["name"]
                crd_files.append(f"{name}.yaml")
            if result["service_monitor"]:
                crd = result["service_monitor"]["crd"]
                name = crd["metadata"]["name"]
                crd_files.append(f"{name}.yaml")

            kustomize_dict = build_kustomization(crd_files, args.namespace)
            print("---")
            if yaml:
                print(yaml.dump(
                    kustomize_dict,
                    default_flow_style=False,
                    allow_unicode=True,
                ), end="")
            else:
                print(_dict_to_yaml(kustomize_dict))
    else:
        # Write CRD files
        output_dir.mkdir(parents=True, exist_ok=True)

        count = 0
        crd_files = []

        for item in result["prometheus_rules"]:
            crd = item["crd"]
            name = crd["metadata"]["name"]
            output_path = output_dir / f"{name}.yaml"
            write_yaml_crd(output_path, crd, gitops=args.gitops)
            print(f"Generated: {output_path}", file=sys.stderr)
            crd_files.append(f"{name}.yaml")
            count += 1

        for item in result["alertmanager_configs"]:
            crd = item["crd"]
            name = crd["metadata"]["name"]
            output_path = output_dir / f"{name}.yaml"
            write_yaml_crd(output_path, crd, gitops=args.gitops)
            print(f"Generated: {output_path}", file=sys.stderr)
            crd_files.append(f"{name}.yaml")
            count += 1

        if result["service_monitor"]:
            crd = result["service_monitor"]["crd"]
            name = crd["metadata"]["name"]
            output_path = output_dir / f"{name}.yaml"
            write_yaml_crd(output_path, crd, gitops=args.gitops)
            print(f"Generated: {output_path}", file=sys.stderr)
            crd_files.append(f"{name}.yaml")
            count += 1

        # Write kustomization.yaml if requested
        if args.kustomize:
            kustomize_dict = build_kustomization(crd_files, args.namespace)
            kustomize_path = output_dir / "kustomization.yaml"
            write_yaml_crd(kustomize_path, kustomize_dict, gitops=args.gitops)
            print(f"Generated: {kustomize_path}", file=sys.stderr)
            count += 1

    # Summary
    base_total = (
        len(result["prometheus_rules"]) +
        len(result["alertmanager_configs"]) +
        (1 if result["service_monitor"] else 0)
    )
    kustomize_count = 1 if args.kustomize else 0
    total = base_total + kustomize_count

    summary = {
        "prometheus_rules": len(result["prometheus_rules"]),
        "alertmanager_configs": len(result["alertmanager_configs"]),
        "service_monitor": 1 if result["service_monitor"] else 0,
        "kustomization": kustomize_count,
        "total": total,
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        if args.kustomize:
            msg_zh = (
                f"\n✓ 已產出 {summary['total']} 個檔案: "
                f"{summary['prometheus_rules']} PrometheusRules, "
                f"{summary['alertmanager_configs']} AlertmanagerConfigs, "
                f"{summary['service_monitor']} ServiceMonitor, "
                f"1 kustomization.yaml"
            )
            msg_en = (
                f"\n✓ Generated {summary['total']} files: "
                f"{summary['prometheus_rules']} PrometheusRules, "
                f"{summary['alertmanager_configs']} AlertmanagerConfigs, "
                f"{summary['service_monitor']} ServiceMonitor, "
                f"1 kustomization.yaml"
            )
        else:
            msg_zh = (
                f"\n✓ 已產出 {summary['total']} 個 CRD: "
                f"{summary['prometheus_rules']} PrometheusRules, "
                f"{summary['alertmanager_configs']} AlertmanagerConfigs, "
                f"{summary['service_monitor']} ServiceMonitor"
            )
            msg_en = (
                f"\n✓ Generated {summary['total']} CRDs: "
                f"{summary['prometheus_rules']} PrometheusRules, "
                f"{summary['alertmanager_configs']} AlertmanagerConfigs, "
                f"{summary['service_monitor']} ServiceMonitor"
            )
        print(i18n_text(msg_zh, msg_en), file=sys.stderr)


if __name__ == "__main__":
    main()
