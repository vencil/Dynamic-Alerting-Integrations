#!/usr/bin/env python3
"""migrate-to-operator — Migrate ConfigMap-based rules to Operator CRD format.

Reads existing Prometheus ConfigMap rules (from configmaps/ or live cluster export)
and conf.d/ tenant configs, then produces equivalent PrometheusRule + AlertmanagerConfig
CRDs along with a step-by-step migration checklist.

Usage:
    da-tools migrate-to-operator --source-dir configmaps/ --config-dir conf.d/
    da-tools migrate-to-operator --source-dir configmaps/ --output-dir migration-output/
    da-tools migrate-to-operator --source-dir configmaps/ --dry-run
    da-tools migrate-to-operator --source-dir configmaps/ --checklist-only
    da-tools migrate-to-operator --source-dir configmaps/ --json
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

# Reuse patterns from operator_generate
import re
_TENANT_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")

_RECEIVER_TEMPLATES = ("slack", "pagerduty", "email", "teams", "opsgenie", "webhook")

_HELP = {
    "zh": {
        "desc": "將 ConfigMap 式規則遷移至 Operator CRD 格式（PrometheusRule、AlertmanagerConfig）",
        "source_dir": "ConfigMap YAML 檔案來源目錄",
        "config_dir": "租户配置目錄（預設 conf.d/）",
        "output_dir": "輸出 CRD 目錄（預設 migration-output/）",
        "namespace": "目標命名空間（預設 monitoring）",
        "receiver_template": f"Receiver 模板類型（{' | '.join(_RECEIVER_TEMPLATES)}）",
        "secret_name": "K8s Secret 名稱（receiver 機密引用）",
        "secret_key": "K8s Secret 中的 key 名稱（預設依 receiver 類型自動推斷）",
        "dry_run": "列印輸出而不寫入檔案",
        "checklist_only": "僅生成遷移檢核清單（不生成 CRD）",
        "json": "以 JSON 格式輸出結果報告",
    },
    "en": {
        "desc": "Migrate ConfigMap-based Prometheus rules to Operator CRD format (PrometheusRule, AlertmanagerConfig)",
        "source_dir": "Source directory with ConfigMap YAML files",
        "config_dir": "Tenant config directory (default conf.d/)",
        "output_dir": "Output CRD directory (default migration-output/)",
        "namespace": "Target namespace (default monitoring)",
        "receiver_template": f"Receiver template type ({' | '.join(_RECEIVER_TEMPLATES)})",
        "secret_name": "K8s Secret name (for receiver credential reference)",
        "secret_key": "Key within the K8s Secret (auto-inferred from receiver type if omitted)",
        "dry_run": "Print output instead of writing files",
        "checklist_only": "Only generate migration checklist (skip CRD generation)",
        "json": "Output results as JSON report",
    },
}

_LANG = detect_cli_lang()

# ─────────────────────────────────────────────────────────────────────────────
# Utility functions (reused from operator_generate)
# ─────────────────────────────────────────────────────────────────────────────


def validate_tenant_name(name: str) -> bool:
    """Validate tenant name against K8s label value rules (RFC 1123)."""
    return bool(_TENANT_NAME_RE.match(name))


def discover_tenant_configs(config_dir: Path) -> List[str]:
    """Discover tenant names from conf.d/*.yaml files."""
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


def write_yaml_crd(output_path: Path, crd: dict, gitops: bool = False) -> None:
    """Write CRD to YAML file."""
    if yaml:
        yaml_str = yaml.dump(
            crd,
            default_flow_style=False,
            sort_keys=gitops,
            allow_unicode=True,
        )
    else:
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
        if any(c in obj for c in ":[]{},'\""):
            return f'"{obj}"'
        return obj
    else:
        return str(obj)


# ─────────────────────────────────────────────────────────────────────────────
# Migration-specific functions
# ─────────────────────────────────────────────────────────────────────────────


def parse_configmap_rules(source_dir: Path) -> List[dict]:
    """Parse ConfigMap YAML files and extract rule groups.

    Scans source_dir for YAML files containing Prometheus rules in ConfigMap format
    (i.e., with a 'data:' key containing nested YAML with 'groups:' and 'rules:').

    Args:
        source_dir: Directory containing ConfigMap YAML files

    Returns:
        List of dicts: {name, file, rule_groups}

    Raises:
        FileNotFoundError: If directory does not exist
    """
    if not source_dir.is_dir():
        raise FileNotFoundError(
            i18n_text(
                f"來源目錄不存在: {source_dir}",
                f"source directory not found: {source_dir}",
            )
        )

    results = []
    for yaml_file in sorted(source_dir.glob("*.yaml")):
        try:
            data = load_yaml_file(str(yaml_file))
            if not data:
                continue

            # Check if this is a ConfigMap with rule data
            if data.get("kind") == "ConfigMap":
                data_section = data.get("data", {})
                if isinstance(data_section, dict):
                    for key, value in data_section.items():
                        if isinstance(value, str):
                            try:
                                rule_data = yaml.safe_load(value)
                                if rule_data and isinstance(rule_data, dict):
                                    groups = rule_data.get("groups", [])
                                    if groups:
                                        results.append({
                                            "name": data.get("metadata", {}).get("name", yaml_file.stem),
                                            "file": yaml_file.name,
                                            "rule_groups": groups,
                                            "cm_data_key": key,
                                        })
                            except yaml.YAMLError:
                                pass
            # Also handle direct rule YAML files (not wrapped in ConfigMap)
            elif isinstance(data, dict) and "groups" in data:
                results.append({
                    "name": yaml_file.stem,
                    "file": yaml_file.name,
                    "rule_groups": data.get("groups", []),
                    "cm_data_key": None,
                })
        except Exception as exc:
            print(
                i18n_text(
                    f"WARNING: 解析 {yaml_file.name} 失敗: {exc}",
                    f"WARNING: Failed to parse {yaml_file.name}: {exc}",
                ),
                file=sys.stderr,
            )

    return results


def convert_rules_to_crd(
    rule_groups: list,
    pack_name: str,
    namespace: str,
) -> dict:
    """Convert ConfigMap-format rule groups into PrometheusRule CRD.

    Args:
        rule_groups: List of rule group dicts
        pack_name: Name for the rule pack
        namespace: Target Kubernetes namespace

    Returns:
        PrometheusRule CRD dict
    """
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": f"da-rule-pack-{pack_name}",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/part-of": "dynamic-alerting",
                "prometheus": "kube-prometheus",
                "migrated-from": "configmap",
            },
        },
        "spec": {
            "groups": rule_groups,
        },
    }


def _build_inhibit_rules_migration(tenant_name: str) -> list:
    """Build inhibit rules for severity dedup + silent/maintenance modes."""
    return [
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


def build_alertmanager_config_for_migration(
    tenant_name: str,
    namespace: str,
    receiver_template: Optional[str] = None,
    secret_name: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> dict:
    """Build AlertmanagerConfig CRD for tenant migration.

    Args:
        tenant_name: Tenant identifier
        namespace: Target Kubernetes namespace
        receiver_template: Receiver type (if any)
        secret_name: K8s Secret name
        secret_key: Key within the K8s Secret

    Returns:
        AlertmanagerConfig CRD dict
    """
    receiver_name = f"{tenant_name}-receiver"
    receiver = {
        "name": receiver_name,
        "webhookConfigs": [
            {"url": "http://localhost:5001/webhook"},
        ],
    }

    inhibit_rules = _build_inhibit_rules_migration(tenant_name)

    return {
        "apiVersion": "monitoring.coreos.com/v1beta1",
        "kind": "AlertmanagerConfig",
        "metadata": {
            "name": f"da-tenant-{tenant_name}",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/part-of": "dynamic-alerting",
                "tenant": tenant_name,
                "migrated-from": "configmap",
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


def analyze_migration(source_dir: Path, config_dir: Path) -> dict:
    """Analyze migration scope and identify potential issues.

    Args:
        source_dir: Directory with ConfigMap YAML files
        config_dir: Directory with tenant configs

    Returns:
        Analysis dict with counts and issues
    """
    analysis = {
        "configmap_files": 0,
        "rule_groups": 0,
        "tenants": 0,
        "estimated_crds": 0,
        "issues": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        cm_rules = parse_configmap_rules(source_dir)
        analysis["configmap_files"] = len(cm_rules)
        for item in cm_rules:
            analysis["rule_groups"] += len(item.get("rule_groups", []))
    except FileNotFoundError as exc:
        analysis["issues"].append(i18n_text(
            f"來源目錄錯誤: {exc}",
            f"Source directory error: {exc}",
        ))

    try:
        # Scan raw tenant names to detect invalid ones before filtering
        if config_dir.is_dir():
            for yaml_file in sorted(config_dir.glob("*.yaml")):
                if not yaml_file.name.startswith("_"):
                    raw_name = yaml_file.stem
                    if not validate_tenant_name(raw_name):
                        analysis["issues"].append(
                            i18n_text(
                                f"無效租戶名稱: {raw_name}（不符合 RFC 1123）",
                                f"Invalid tenant name: {raw_name} (not RFC 1123 compliant)",
                            )
                        )
        tenants = discover_tenant_configs(config_dir)
        analysis["tenants"] = len(tenants)
    except FileNotFoundError as exc:
        analysis["issues"].append(i18n_text(
            f"配置目錄錯誤: {exc}",
            f"Config directory error: {exc}",
        ))

    analysis["estimated_crds"] = analysis["configmap_files"] + analysis["tenants"]

    return analysis


def build_migration_checklist(
    source_dir: Path,
    config_dir: Path,
    output_dir: Path,
    result: dict,
) -> str:
    """Generate a markdown-formatted migration checklist.

    Args:
        source_dir: Source ConfigMap directory
        config_dir: Tenant config directory
        output_dir: Output CRD directory
        result: Migration result dict

    Returns:
        Markdown-formatted checklist string
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    pr_count = len(result.get("prometheus_rules", []))
    ac_count = len(result.get("alertmanager_configs", []))
    total = pr_count + ac_count

    zh_checklist = f"""# 遷移檢核清單

生成時間: {timestamp}

## 概覽

- **ConfigMap 檔案數**: {result.get('configmap_files', 0)}
- **Rule 群組數**: {result.get('rule_group_count', 0)}
- **租戶數**: {result.get('tenants', 0)}
- **輸出 CRD 數**: {total} ({pr_count} PrometheusRules + {ac_count} AlertmanagerConfigs)
- **來源目錄**: {source_dir}
- **輸出目錄**: {output_dir}

## 遷移步驟

### Phase 1: 前置驗證

- [ ] 備份現有 ConfigMap（`kubectl get configmap -n monitoring -o yaml > configmap-backup.yaml`）
- [ ] 備份現有 Prometheus 配置（`kubectl get prometheus -n monitoring -o yaml > prometheus-backup.yaml`）
- [ ] 驗證新 CRD 檔案數正確（應為 {total} 個）
- [ ] 檢查 CRD 檔案中是否有語法錯誤（`kubectl apply -f {output_dir}/ --dry-run=client`）

### Phase 2: 套用新 CRD

- [ ] 套用 PrometheusRule CRD (`kubectl apply -f {output_dir}/*-rule-pack-*.yaml`)
- [ ] 套用 AlertmanagerConfig CRD (`kubectl apply -f {output_dir}/*-tenant-*.yaml`)
- [ ] 等待 Prometheus reconcile（約 30-60 秒）
- [ ] 驗證 Prometheus 載入新 Rule（`kubectl logs -n monitoring -l app=prometheus -c prometheus | grep "loaded groups"`)

### Phase 3: 驗證規則

- [ ] 確認 Prometheus 成功載入所有 Rule Pack
  ```
  kubectl port-forward -n monitoring svc/prometheus 9090:9090
  # 訪問 http://localhost:9090/rules
  ```
- [ ] 檢查是否有失敗的 Rule（Rules 頁面應該沒有紅色警告）
- [ ] 驗證告警正常觸發（檢查 Active 或 Pending 的 alert）

### Phase 4: 切換 Helm 配置

- [ ] 更新 Helm values: `rules.mode: operator`（需要修改 values.yaml）
- [ ] 執行 Helm upgrade
- [ ] 驗證 Prometheus Pod 未發生 crash（`kubectl get pods -n monitoring`）

### Phase 5: 清理舊資源

- [ ] 確認所有規則都已成功遷移
- [ ] 刪除舊 ConfigMap: `kubectl delete configmap -n monitoring <configmap-names>`
- [ ] 刪除舊 Projected Volume 設定（如適用）
- [ ] 驗證 Prometheus 仍正常運作

### Phase 6: 後置驗證

- [ ] 檢查告警是否繼續正常運作
- [ ] 檢查 Alertmanager route 配置是否正確
- [ ] 運行告警品質檢查（`da-tools alert-quality-check --config-dir conf.d/`）
- [ ] 記錄遷移完成時間與結果

## 回滾計畫

如需回滾：

1. 刪除新 CRD:
   ```
   kubectl delete -f {output_dir}/
   ```

2. 恢復舊配置:
   ```
   kubectl apply -f configmap-backup.yaml
   kubectl apply -f prometheus-backup.yaml
   ```

3. 重啟 Prometheus Pod（強制 ConfigMap reload）:
   ```
   kubectl rollout restart deployment -n monitoring prometheus-operator
   ```

## 注意事項

- 遷移過程中，Prometheus 不會中斷（配置滾動更新）
- 新 CRD 被標記為 `migrated-from: configmap`，便於追蹤
- 所有 AlertmanagerConfig 預設使用 webhook receiver，需要根據實際情況調整
- 租戶 RBAC 和 routing 規則需在遷移後手動驗證

"""

    en_checklist = f"""# Migration Checklist

Generated: {timestamp}

## Overview

- **ConfigMap Files**: {result.get('configmap_files', 0)}
- **Rule Groups**: {result.get('rule_group_count', 0)}
- **Tenants**: {result.get('tenants', 0)}
- **Output CRDs**: {total} ({pr_count} PrometheusRules + {ac_count} AlertmanagerConfigs)
- **Source Directory**: {source_dir}
- **Output Directory**: {output_dir}

## Migration Steps

### Phase 1: Pre-Migration Validation

- [ ] Back up existing ConfigMaps (`kubectl get configmap -n monitoring -o yaml > configmap-backup.yaml`)
- [ ] Back up existing Prometheus config (`kubectl get prometheus -n monitoring -o yaml > prometheus-backup.yaml`)
- [ ] Verify CRD file count is correct (should be {total} files)
- [ ] Check for syntax errors in CRD files (`kubectl apply -f {output_dir}/ --dry-run=client`)

### Phase 2: Apply New CRDs

- [ ] Apply PrometheusRule CRDs (`kubectl apply -f {output_dir}/*-rule-pack-*.yaml`)
- [ ] Apply AlertmanagerConfig CRDs (`kubectl apply -f {output_dir}/*-tenant-*.yaml`)
- [ ] Wait for Prometheus reconciliation (approximately 30-60 seconds)
- [ ] Verify Prometheus loaded new Rules (`kubectl logs -n monitoring -l app=prometheus -c prometheus | grep "loaded groups"`)

### Phase 3: Verify Rules

- [ ] Confirm Prometheus successfully loaded all Rule Packs
  ```
  kubectl port-forward -n monitoring svc/prometheus 9090:9090
  # Visit http://localhost:9090/rules
  ```
- [ ] Check for failed Rules (Rules page should have no red warnings)
- [ ] Verify alerts trigger normally (check Active or Pending alerts)

### Phase 4: Switch Helm Configuration

- [ ] Update Helm values: `rules.mode: operator` (requires modifying values.yaml)
- [ ] Execute Helm upgrade
- [ ] Verify Prometheus Pod did not crash (`kubectl get pods -n monitoring`)

### Phase 5: Clean Up Old Resources

- [ ] Confirm all rules successfully migrated
- [ ] Delete old ConfigMaps: `kubectl delete configmap -n monitoring <configmap-names>`
- [ ] Delete old Projected Volume settings (if applicable)
- [ ] Verify Prometheus still works normally

### Phase 6: Post-Migration Verification

- [ ] Check if alerts continue to work normally
- [ ] Verify Alertmanager route configuration is correct
- [ ] Run alert quality checks (`da-tools alert-quality-check --config-dir conf.d/`)
- [ ] Record migration completion time and results

## Rollback Plan

If rollback is needed:

1. Delete new CRDs:
   ```
   kubectl delete -f {output_dir}/
   ```

2. Restore old configuration:
   ```
   kubectl apply -f configmap-backup.yaml
   kubectl apply -f prometheus-backup.yaml
   ```

3. Restart Prometheus Pod (force ConfigMap reload):
   ```
   kubectl rollout restart deployment -n monitoring prometheus-operator
   ```

## Important Notes

- Prometheus will not be interrupted during migration (rolling configuration update)
- New CRDs are marked with `migrated-from: configmap` for tracking
- All AlertmanagerConfigs default to webhook receiver; adjust according to actual needs
- Tenant RBAC and routing rules must be manually verified post-migration

"""

    return zh_checklist if _LANG == "zh" else en_checklist


def generate_migration(
    source_dir: Path,
    config_dir: Path,
    output_dir: Path,
    namespace: str,
    receiver_template: Optional[str] = None,
    secret_name: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> dict:
    """Main orchestration: parse → convert → write CRDs.

    Args:
        source_dir: Source ConfigMap directory
        config_dir: Tenant config directory
        output_dir: Output CRD directory
        namespace: Target Kubernetes namespace
        receiver_template: Receiver type for AlertmanagerConfig
        secret_name: K8s Secret name
        secret_key: Key within the K8s Secret

    Returns:
        Result dict with generated CRDs and metadata
    """
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "prometheus_rules": [],
        "alertmanager_configs": [],
        "configmap_files": 0,
        "rule_group_count": 0,
        "tenants": 0,
        "errors": [],
    }

    # Parse ConfigMap rules
    try:
        cm_rules = parse_configmap_rules(source_dir)
        result["configmap_files"] = len(cm_rules)

        for cm_item in cm_rules:
            try:
                rule_groups = cm_item.get("rule_groups", [])
                result["rule_group_count"] += len(rule_groups)
                rule_pack_name = cm_item["name"].replace("rule-pack-", "").lower()

                crd = convert_rules_to_crd(rule_groups, rule_pack_name, namespace)
                result["prometheus_rules"].append({
                    "name": crd["metadata"]["name"],
                    "file": cm_item["file"],
                    "crd": crd,
                })
            except Exception as exc:
                result["errors"].append(
                    i18n_text(
                        f"轉換 {cm_item['file']} 失敗: {exc}",
                        f"Failed to convert {cm_item['file']}: {exc}",
                    )
                )
    except FileNotFoundError as exc:
        result["errors"].append(str(exc))

    # Generate AlertmanagerConfig CRDs for tenants
    try:
        tenants = discover_tenant_configs(config_dir)
        result["tenants"] = len(tenants)

        for tenant in tenants:
            try:
                crd = build_alertmanager_config_for_migration(
                    tenant, namespace, receiver_template, secret_name, secret_key,
                )
                result["alertmanager_configs"].append({
                    "name": crd["metadata"]["name"],
                    "tenant": tenant,
                    "crd": crd,
                })
            except Exception as exc:
                result["errors"].append(
                    i18n_text(
                        f"為租戶 {tenant} 生成 AlertmanagerConfig 失敗: {exc}",
                        f"Failed to generate AlertmanagerConfig for tenant {tenant}: {exc}",
                    )
                )
    except FileNotFoundError as exc:
        result["errors"].append(str(exc))

    return result


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=i18n_text(_HELP["zh"]["desc"], _HELP["en"]["desc"]),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help=i18n_text(_HELP["zh"]["source_dir"], _HELP["en"]["source_dir"]),
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
        default=Path("migration-output"),
        help=i18n_text(_HELP["zh"]["output_dir"], _HELP["en"]["output_dir"]),
    )
    parser.add_argument(
        "--namespace",
        default="monitoring",
        help=i18n_text(_HELP["zh"]["namespace"], _HELP["en"]["namespace"]),
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
        "--dry-run",
        action="store_true",
        help=i18n_text(_HELP["zh"]["dry_run"], _HELP["en"]["dry_run"]),
    )
    parser.add_argument(
        "--checklist-only",
        action="store_true",
        help=i18n_text(
            _HELP["zh"]["checklist_only"],
            _HELP["en"]["checklist_only"],
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=i18n_text(_HELP["zh"]["json"], _HELP["en"]["json"]),
    )

    args = parser.parse_args()

    # Resolve paths
    source_dir = args.source_dir.resolve()
    config_dir = args.config_dir.resolve()
    output_dir = args.output_dir.resolve()

    # Analyze migration
    print(
        i18n_text(
            "正在分析遷移範圍...",
            "Analyzing migration scope...",
        ),
        file=sys.stderr,
    )
    analysis = analyze_migration(source_dir, config_dir)

    if analysis["issues"]:
        for issue in analysis["issues"]:
            print(f"WARNING: {issue}", file=sys.stderr)

    # Generate migration if not checklist-only
    if args.checklist_only:
        result = analysis
        result["prometheus_rules"] = []
        result["alertmanager_configs"] = []
    else:
        print(
            i18n_text(
                "正在生成 CRD...",
                "Generating CRDs...",
            ),
            file=sys.stderr,
        )
        result = generate_migration(
            source_dir,
            config_dir,
            output_dir,
            args.namespace,
            receiver_template=args.receiver_template,
            secret_name=args.secret_name,
            secret_key=args.secret_key,
        )

    # Write or print output
    if args.checklist_only and not args.dry_run:
        # For checklist-only mode (not dry-run), print checklist to stdout
        checklist = build_migration_checklist(source_dir, config_dir, output_dir, result)
        print(checklist)
    elif args.dry_run:
        # Print to stdout
        if args.json:
            output_obj = {
                "metadata": {
                    "timestamp": result.get("timestamp"),
                    "namespace": args.namespace,
                    "source_dir": str(source_dir),
                    "config_dir": str(config_dir),
                    "configmap_files": result.get("configmap_files", 0),
                    "rule_groups": result.get("rule_group_count", 0),
                    "tenants": result.get("tenants", 0),
                },
                "prometheus_rules": [item["crd"] for item in result.get("prometheus_rules", [])],
                "alertmanager_configs": [item["crd"] for item in result.get("alertmanager_configs", [])],
                "errors": result.get("errors", []),
            }
            print(json.dumps(output_obj, indent=2, ensure_ascii=False))
        else:
            checklist = build_migration_checklist(source_dir, config_dir, output_dir, result)
            print("# MIGRATION CHECKLIST", file=sys.stdout)
            print(checklist, file=sys.stdout)
            print("\n# CRD PREVIEW\n", file=sys.stdout)
            all_crds = []
            for item in result["prometheus_rules"]:
                all_crds.append(item["crd"])
            for item in result["alertmanager_configs"]:
                all_crds.append(item["crd"])

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
        # Write files
        output_dir.mkdir(parents=True, exist_ok=True)

        # Write CRD files
        for item in result["prometheus_rules"]:
            crd = item["crd"]
            name = crd["metadata"]["name"]
            output_path = output_dir / f"{name}.yaml"
            write_yaml_crd(output_path, crd, gitops=False)
            print(f"Generated: {output_path}", file=sys.stderr)

        for item in result["alertmanager_configs"]:
            crd = item["crd"]
            name = crd["metadata"]["name"]
            output_path = output_dir / f"{name}.yaml"
            write_yaml_crd(output_path, crd, gitops=False)
            print(f"Generated: {output_path}", file=sys.stderr)

        # Write checklist
        checklist = build_migration_checklist(source_dir, config_dir, output_dir, result)
        checklist_path = output_dir / "MIGRATION-CHECKLIST.md"
        write_text_secure(str(checklist_path), checklist)
        print(f"Generated: {checklist_path}", file=sys.stderr)

    # Summary (only for non-dry-run or non-JSON output)
    total_crds = len(result["prometheus_rules"]) + len(result["alertmanager_configs"])
    summary = {
        "configmap_files": result.get("configmap_files", 0),
        "rule_groups": result.get("rule_group_count", 0),
        "tenants": result.get("tenants", 0),
        "prometheus_rules": len(result["prometheus_rules"]),
        "alertmanager_configs": len(result["alertmanager_configs"]),
        "total_crds": total_crds,
    }

    # Only print summary if not JSON + dry-run (in that case, JSON is the only output)
    if not (args.dry_run and args.json):
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            print(
                i18n_text(
                    f"\n✓ 遷移分析完成: {result.get('configmap_files', 0)} 個 ConfigMap → "
                    f"{total_crds} 個 CRD ({summary['prometheus_rules']} PrometheusRules + "
                    f"{summary['alertmanager_configs']} AlertmanagerConfigs，"
                    f"{summary['tenants']} 個租戶)",
                    f"\n✓ Migration analysis complete: {result.get('configmap_files', 0)} ConfigMaps → "
                    f"{total_crds} CRDs ({summary['prometheus_rules']} PrometheusRules + "
                    f"{summary['alertmanager_configs']} AlertmanagerConfigs, "
                    f"{summary['tenants']} tenants)",
                ),
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
