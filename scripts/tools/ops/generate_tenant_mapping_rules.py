#!/usr/bin/env python3
"""
generate_tenant_mapping_rules.py — Generate Prometheus Recording Rules for 1:N tenant mapping.

Reads _instance_mapping.yaml from config-dir, cross-references metric-dictionary.yaml,
and produces Recording Rules that normalize instance-level metrics into tenant-level metrics
with a `tenant` label.

Topology support (ADR-006):
  1:1 (default): namespace label = tenant — no mapping needed
  N:1 (existing): relabel_configs regex — handled by scaffold_tenant.py --namespaces
  1:N (this tool): single instance → multiple tenants via schema/tablespace filter

Output formats:
  --format=yaml     Raw Recording Rules YAML (default)
  --format=configmap  Kubernetes ConfigMap wrapping the rules (Rule Pack Part 1)

Usage:
  python3 generate_tenant_mapping_rules.py --config-dir conf.d/
  python3 generate_tenant_mapping_rules.py --config-dir conf.d/ --format configmap -o rules.yaml
  python3 generate_tenant_mapping_rules.py --config-dir conf.d/ --dry-run --validate
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    detect_cli_lang,
    load_yaml_file,
    write_text_secure,
    iter_yaml_files,
)

_LANG = detect_cli_lang()

_HELP: dict[str, dict[str, str]] = {
    'description': {
        'zh': '從 _instance_mapping.yaml 產生 1:N 租戶映射的 Prometheus Recording Rules',
        'en': 'Generate Prometheus Recording Rules for 1:N tenant mapping from _instance_mapping.yaml',
    },
    'config_dir': {
        'zh': '配置目錄路徑（含 _instance_mapping.yaml）',
        'en': 'Config directory path (containing _instance_mapping.yaml)',
    },
    'metric_dict': {
        'zh': 'metric-dictionary.yaml 路徑（自動偵測）',
        'en': 'Path to metric-dictionary.yaml (auto-detected)',
    },
    'metrics': {
        'zh': '要映射的指標清單（逗號分隔），省略則使用 metric-dictionary 全部指標',
        'en': 'Comma-separated list of metrics to map; omit to use all from metric-dictionary',
    },
    'format': {
        'zh': '輸出格式：yaml（原始規則）或 configmap（K8s ConfigMap）',
        'en': 'Output format: yaml (raw rules) or configmap (K8s ConfigMap)',
    },
    'output': {
        'zh': '輸出檔案路徑（省略則輸出到 stdout）',
        'en': 'Output file path (omit for stdout)',
    },
    'namespace': {
        'zh': 'ConfigMap 的 Kubernetes namespace',
        'en': 'Kubernetes namespace for ConfigMap',
    },
    'dry_run': {
        'zh': '乾跑模式：只顯示摘要，不寫入檔案',
        'en': 'Dry-run mode: show summary only, do not write',
    },
    'validate': {
        'zh': '驗證映射中的 tenant ID 是否存在於 config-dir',
        'en': 'Validate that mapped tenant IDs exist in config-dir',
    },
}


def _h(key: str) -> str:
    return _HELP[key].get(_LANG, _HELP[key]['en'])


# ---------------------------------------------------------------------------
# Schema types
# ---------------------------------------------------------------------------

class MappingEntry:
    """One instance → tenant mapping entry."""

    __slots__ = ('tenant', 'filter_expr')

    def __init__(self, tenant: str, filter_expr: str) -> None:
        self.tenant = tenant
        self.filter_expr = filter_expr

    def __repr__(self) -> str:
        return f"MappingEntry(tenant={self.tenant!r}, filter={self.filter_expr!r})"


class InstanceMapping:
    """All mappings for one instance."""

    __slots__ = ('instance', 'entries')

    def __init__(self, instance: str, entries: list[MappingEntry]) -> None:
        self.instance = instance
        self.entries = entries


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_MAPPING_FILE_NAMES = ('_instance_mapping.yaml', '_instance_mapping.yml')


def find_mapping_file(config_dir: str) -> str | None:
    """Find _instance_mapping.yaml in config-dir."""
    for name in _MAPPING_FILE_NAMES:
        path = os.path.join(config_dir, name)
        if os.path.isfile(path):
            return path
    return None


def parse_mapping_file(path: str) -> list[InstanceMapping]:
    """Parse _instance_mapping.yaml into structured mappings."""
    data = load_yaml_file(path)
    if not data or not isinstance(data, dict):
        return []

    raw = data.get('instance_tenant_mapping', {})
    if not isinstance(raw, dict):
        return []

    result: list[InstanceMapping] = []
    for instance, entries_raw in sorted(raw.items()):
        if not isinstance(entries_raw, list):
            print(f"WARN: instance={instance}: expected list of mappings, got {type(entries_raw).__name__}",
                  file=sys.stderr)
            continue
        entries: list[MappingEntry] = []
        for item in entries_raw:
            if not isinstance(item, dict):
                print(f"WARN: instance={instance}: expected mapping dict, got {type(item).__name__}",
                      file=sys.stderr)
                continue
            tenant = item.get('tenant', '').strip()
            filter_expr = item.get('filter', '').strip()
            if not tenant:
                print(f"WARN: instance={instance}: entry missing 'tenant' field", file=sys.stderr)
                continue
            if not filter_expr:
                print(f"WARN: instance={instance}, tenant={tenant}: empty 'filter' — skipping",
                      file=sys.stderr)
                continue
            entries.append(MappingEntry(tenant=tenant, filter_expr=filter_expr))
        if entries:
            result.append(InstanceMapping(instance=instance, entries=entries))
    return result


def collect_tenant_ids_from_config_dir(config_dir: str) -> set[str]:
    """Collect all tenant IDs from tenant YAML files in config-dir."""
    tenant_ids: set[str] = set()
    for filename, filepath in iter_yaml_files(config_dir, skip_reserved=True):
        data = load_yaml_file(filepath)
        if not data or not isinstance(data, dict):
            continue
        # Wrapper format: {tenants: {name: ...}}
        tenants_block = data.get('tenants', {})
        if isinstance(tenants_block, dict):
            tenant_ids.update(tenants_block.keys())
        else:
            # Flat format: filename = tenant name
            base = os.path.splitext(filename)[0]
            tenant_ids.add(base)
    return tenant_ids


def load_metrics_from_dictionary(dict_path: str) -> list[str]:
    """Load metric names from metric-dictionary.yaml (maps_to values = golden metric names)."""
    data = load_yaml_file(dict_path)
    if not data or not isinstance(data, dict):
        return []
    # Collect unique golden metric names (maps_to values)
    metrics: set[str] = set()
    for _raw_metric, info in data.items():
        if isinstance(info, dict) and 'maps_to' in info:
            metrics.add(info['maps_to'])
    return sorted(metrics)


# ---------------------------------------------------------------------------
# Rule generation
# ---------------------------------------------------------------------------

_LABEL_MATCHER_RE = re.compile(r'^(\w+)\s*(=~|!=|=)\s*"(.*)"$')


def parse_filter_to_matchers(filter_expr: str) -> list[str]:
    """Parse filter string into PromQL label matchers.

    Supports comma-separated matchers:
      'schema=~"app_a_.*"'
      'schema=~"app_a_.*", tablespace="ts_a"'
    """
    matchers: list[str] = []
    for part in filter_expr.split(','):
        part = part.strip()
        if not part:
            continue
        m = _LABEL_MATCHER_RE.match(part)
        if m:
            label, op, value = m.group(1), m.group(2), m.group(3)
            matchers.append(f'{label}{op}"{value}"')
        else:
            # Pass through as-is (user knows PromQL)
            matchers.append(part)
    return matchers


def generate_recording_rules(
    mappings: list[InstanceMapping],
    metrics: list[str],
) -> list[dict]:
    """Generate Prometheus recording rule groups from mappings × metrics.

    Returns a list of rule group dicts ready for YAML serialization.
    """
    groups: list[dict] = []

    for mapping in mappings:
        rules: list[dict] = []
        for entry in mapping.entries:
            filter_matchers = parse_filter_to_matchers(entry.filter_expr)
            # Build label selector: instance + filter matchers
            all_matchers = [f'instance="{mapping.instance}"'] + filter_matchers
            selector = ', '.join(all_matchers)

            for metric in metrics:
                rules.append({
                    'record': f'tenant_mapped:{metric}:current',
                    'expr': f'{metric}{{{selector}}}',
                    'labels': {
                        'tenant': entry.tenant,
                    },
                })

        if rules:
            groups.append({
                'name': f'tenant_mapping_{mapping.instance}',
                'interval': '30s',
                'rules': rules,
            })

    return groups


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_as_yaml(groups: list[dict]) -> str:
    """Format recording rules as raw Prometheus rules YAML."""
    doc = {'groups': groups}
    return yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False)


def format_as_configmap(
    groups: list[dict],
    namespace: str = 'monitoring',
    name: str = 'rules-tenant-mapping',
) -> str:
    """Format as Kubernetes ConfigMap wrapping the rules."""
    rules_yaml = yaml.dump({'groups': groups}, default_flow_style=False,
                           allow_unicode=True, sort_keys=False)
    cm = {
        'apiVersion': 'v1',
        'kind': 'ConfigMap',
        'metadata': {
            'name': name,
            'namespace': namespace,
            'labels': {
                'app': 'dynamic-alerting',
                'component': 'rule-pack-part1-tenant-mapping',
            },
        },
        'data': {
            'tenant-mapping-rules.yaml': rules_yaml,
        },
    }
    return yaml.dump(cm, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_mappings(
    mappings: list[InstanceMapping],
    known_tenants: set[str],
) -> list[str]:
    """Validate mapping entries. Returns list of error/warning messages."""
    messages: list[str] = []
    seen_tenants: dict[str, list[str]] = {}  # tenant → [instances]

    for mapping in mappings:
        for entry in mapping.entries:
            # Check tenant exists in config-dir
            if entry.tenant not in known_tenants:
                messages.append(
                    f"ERROR: instance={mapping.instance}: tenant '{entry.tenant}' "
                    f"not found in config-dir")

            # Track duplicate tenant mappings (same tenant from multiple instances)
            seen_tenants.setdefault(entry.tenant, []).append(mapping.instance)

            # Validate filter syntax
            matchers = parse_filter_to_matchers(entry.filter_expr)
            if not matchers:
                messages.append(
                    f"WARN: instance={mapping.instance}, tenant={entry.tenant}: "
                    f"empty filter expression")

    # Check for tenants mapped from multiple instances (info, not error)
    for tenant, instances in seen_tenants.items():
        if len(instances) > 1:
            messages.append(
                f"INFO: tenant '{tenant}' mapped from multiple instances: "
                f"{', '.join(instances)}")

    return messages


# ---------------------------------------------------------------------------
# Cardinality estimation
# ---------------------------------------------------------------------------

def estimate_cardinality(mappings: list[InstanceMapping], metric_count: int) -> dict:
    """Estimate cardinality impact of the mappings."""
    total_entries = sum(len(m.entries) for m in mappings)
    new_series = total_entries * metric_count
    return {
        'instances': len(mappings),
        'mapping_entries': total_entries,
        'metrics_per_entry': metric_count,
        'new_series_estimate': new_series,
        'storage_note': f'~{new_series * 2}x bytes (original + mapped)',
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=_h('description'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--config-dir', required=True, help=_h('config_dir'))
    parser.add_argument('--metric-dictionary', default=None, help=_h('metric_dict'))
    parser.add_argument('--metrics', default=None, help=_h('metrics'))
    parser.add_argument('--format', choices=['yaml', 'configmap'], default='yaml',
                        help=_h('format'))
    parser.add_argument('-o', '--output', default=None, help=_h('output'))
    parser.add_argument('--namespace', default='monitoring', help=_h('namespace'))
    parser.add_argument('--dry-run', action='store_true', help=_h('dry_run'))
    parser.add_argument('--validate', action='store_true', help=_h('validate'))
    args = parser.parse_args()

    config_dir = args.config_dir
    if not os.path.isdir(config_dir):
        print(f"ERROR: config-dir not found: {config_dir}", file=sys.stderr)
        sys.exit(1)

    # Find mapping file
    mapping_path = find_mapping_file(config_dir)
    if not mapping_path:
        print(f"INFO: no _instance_mapping.yaml found in {config_dir} — "
              "no 1:N mappings to generate", file=sys.stderr)
        sys.exit(0)

    # Parse mappings
    mappings = parse_mapping_file(mapping_path)
    if not mappings:
        print("INFO: no valid mappings found in _instance_mapping.yaml", file=sys.stderr)
        sys.exit(0)

    # Resolve metrics list
    if args.metrics:
        metrics = [m.strip() for m in args.metrics.split(',') if m.strip()]
    else:
        # Auto-detect from metric-dictionary.yaml
        dict_path = args.metric_dictionary
        if not dict_path:
            dict_path = os.path.join(_THIS_DIR, '..', 'metric-dictionary.yaml')
            if not os.path.isfile(dict_path):
                dict_path = os.path.join(_THIS_DIR, 'metric-dictionary.yaml')
        if os.path.isfile(dict_path):
            metrics = load_metrics_from_dictionary(dict_path)
        else:
            print("ERROR: no --metrics specified and metric-dictionary.yaml not found",
                  file=sys.stderr)
            sys.exit(1)

    if not metrics:
        print("ERROR: no metrics to map", file=sys.stderr)
        sys.exit(1)

    # Validation
    errors_found = False
    if args.validate:
        known_tenants = collect_tenant_ids_from_config_dir(config_dir)
        messages = validate_mappings(mappings, known_tenants)
        for msg in messages:
            print(msg, file=sys.stderr)
        if any(msg.startswith('ERROR') for msg in messages):
            errors_found = True

    # Cardinality estimation
    card = estimate_cardinality(mappings, len(metrics))

    # Dry-run summary
    if args.dry_run:
        print(f"=== Tenant Mapping Rules Summary ===")
        print(f"Instances:        {card['instances']}")
        print(f"Mapping entries:  {card['mapping_entries']}")
        print(f"Metrics/entry:    {card['metrics_per_entry']}")
        print(f"New series:       ~{card['new_series_estimate']}")
        print(f"Storage impact:   {card['storage_note']}")
        print()
        for mapping in mappings:
            print(f"  {mapping.instance}:")
            for entry in mapping.entries:
                print(f"    → {entry.tenant} ({entry.filter_expr})")
        if errors_found:
            sys.exit(1)
        sys.exit(0)

    if errors_found:
        print("ERROR: validation failed — fix errors above before generating rules",
              file=sys.stderr)
        sys.exit(1)

    # Generate rules
    groups = generate_recording_rules(mappings, metrics)

    # Format output
    if args.format == 'configmap':
        output = format_as_configmap(groups, namespace=args.namespace)
    else:
        output = format_as_yaml(groups)

    # Write or print
    if args.output:
        write_text_secure(args.output, output)
        print(f"Generated {len(groups)} rule group(s), "
              f"~{card['new_series_estimate']} new series → {args.output}",
              file=sys.stderr)
    else:
        sys.stdout.write(output)


if __name__ == '__main__':
    main()
