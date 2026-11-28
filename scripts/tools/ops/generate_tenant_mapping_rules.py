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
import json
import os
import re
import sys
from pathlib import Path

import yaml  # output only (yaml.dump); conf.d is read through parse-tree

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    detect_cli_lang,
    exit_on_yaml_file_error,
    load_yaml_file,
)
from _lib_io import write_operator_output_or_die  # noqa: E402  (#3107 ③b)
from _lib_validation import operator_file_type  # noqa: E402  (#3107 ③b)
from _lib_io import YamlFileError  # noqa: E402  (#3276)
from _lib_paths import require_dir  # noqa: E402  (#3276)
import _lib_input  # noqa: E402  (#3445: --metric-dictionary is read by the lib)
# ADR-036 step 4 (#2906): conf.d is read through `da-guard parse-tree`.
import _lib_parse_cache  # noqa: E402
import _lib_parse_tree  # noqa: E402
import _lib_consumer_read  # noqa: E402  (#4264: a filter is read by Prometheus' parser)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

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

    __slots__ = ('tenant', 'filter_expr', 'matchers')

    def __init__(self, tenant: str, filter_expr: str) -> None:
        self.tenant = tenant
        self.filter_expr = filter_expr
        #: parse_filter_to_matchers' answer, once main has read every filter
        #: in one batch (#4264); None until then.
        self.matchers: list[str] | None = None

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
    """`_instance_mapping.yaml` (else `.yml`) at the root of config-dir, among
    the files `da-guard parse-tree` selects (ADR-036 step 4, #2906). Raises
    ParseTreeError when parse-tree cannot run."""
    tree = _lib_parse_cache.reader_for(config_dir).tree()
    for name in _MAPPING_FILE_NAMES:
        if name in tree.selected:
            return str(Path(config_dir) / name)
    return None


def _text_of(node):
    """A scalar node's source text, else its value (`None` for no node)."""
    if node is None:
        return None
    if _lib_parse_tree.is_scalar(node):
        return _lib_parse_tree.scalar_text(node) if node["tag"] != "!!null" else None
    return _lib_parse_tree.to_python(node)


def parse_mapping_file(path: str) -> list[InstanceMapping]:
    """Parse _instance_mapping.yaml into structured mappings.

    Read through `da-guard parse-tree` (ADR-036 step 4, #2906): the file at
    the root of the conf.d it sits in, as yaml.v3 reads it. A file parse-tree
    cannot read is a path da-guard validation names: the read is the shared
    conf.d refusal (#3206, rc 2), naming it.

    #2216: an entry's ``tenant:`` value is a tenant id, read as its source
    text — ``tenant: 010`` is tenant "010", not the int 8 ``.strip()``
    raised on. ``filter:`` is its source text as well (a PromQL matcher).
    """
    p = Path(path)
    root = _lib_parse_cache.reader_for(p.parent).node(p.name)
    raw = root.get('instance_tenant_mapping')
    if not isinstance(raw, dict) or _lib_parse_tree.is_scalar(raw):
        return []

    result: list[InstanceMapping] = []
    for instance, entries_raw in sorted(raw.items()):
        if not isinstance(entries_raw, list):
            kind = ('mapping' if isinstance(entries_raw, dict)
                    and not _lib_parse_tree.is_scalar(entries_raw) else 'scalar')
            print(f"WARN: instance={instance}: expected list of mappings, got {kind}",
                  file=sys.stderr)
            continue
        entries: list[MappingEntry] = []
        for item in entries_raw:
            if not isinstance(item, dict) or _lib_parse_tree.is_scalar(item):
                kind = 'list' if isinstance(item, list) else 'scalar'
                print(f"WARN: instance={instance}: expected mapping dict, got {kind}",
                      file=sys.stderr)
                continue
            tenant = _text_of(item.get('tenant'))
            filter_expr = _text_of(item.get('filter'))
            tenant = tenant.strip() if isinstance(tenant, str) else ''
            filter_expr = filter_expr.strip() if isinstance(filter_expr, str) else ''
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
    """Every tenant id a tenant file of config-dir declares, at any depth.

    ADR-036 step 4 (#2906): the files are the ones `da-guard parse-tree`
    selects, and the ids the keys of each `tenants:` mapping as yaml.v3 keys
    the exporter's map — source text (#2216: `010:` is tenant "010", not 8).
    A file parse-tree cannot read, or a path that is or may hide a tenant
    file (a link to a directory, a directory Go could not list: both halves
    of `_lib_parse_cache.declared_tenants`' `unknown`, #3206), raises
    YamlFileError (rc 2): what the tree declares is unknown, so the
    validation could not be answered. A file
    with no `tenants:` mapping declares no tenant — the exporter serves none
    from it (the flat format is not read).
    """
    declared, unknown = _lib_parse_cache.declared_tenants(
        _lib_parse_cache.reader_for(config_dir))
    if unknown:
        first = unknown[0]
        raise YamlFileError(str(Path(config_dir) / first.file), first)
    return set(declared)


def load_metrics_from_dictionary(dict_path: str) -> list[str]:
    """Load raw exporter metric names from metric-dictionary.yaml (its keys).

    The generated rule selects the raw series by instance
    (``oracle_sessions{instance=...}``, ADR-006), so the names must be ones an
    exporter emits: the dictionary's keys. This used to collect the
    ``maps_to`` values, which are tenant threshold keys (``mysql_connection_usage_percent``)
    — no series carries that name, so every generated rule was empty — and a
    ``maps_to: null`` entry crashed ``sorted()`` (issue 1196).
    """
    data = load_yaml_file(dict_path)
    if not data or not isinstance(data, dict):
        return []
    return metrics_in_dictionary(data)


#: --metric-dictionary: a YAML mapping (#3445: a list or a scalar there was
#: "no metrics to map"; it is the named wrong-type refusal now).
DICTIONARY_DOCUMENT = _lib_input.Document("yaml", dict)


def metrics_in_dictionary(data: dict) -> list[str]:
    """The raw exporter metric names of a parsed metric dictionary: its keys
    whose entry is a mapping (see `load_metrics_from_dictionary`)."""
    return sorted(str(raw) for raw, info in data.items() if isinstance(info, dict))


# ---------------------------------------------------------------------------
# Rule generation
# ---------------------------------------------------------------------------

class FilterRefused(ValueError):
    """A mapping entry's `filter` is not a list of PromQL label matchers."""


def _promql_string(value: str) -> str:
    """*value* as a PromQL double-quoted string: JSON's escapes are a subset
    of the Go string syntax PromQL reads."""
    return json.dumps(value, ensure_ascii=False)


_LEGACY_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _promql_label_name(name: str) -> str:
    """*name* as a matcher writes it: bare when it is a legacy label name,
    quoted otherwise (a UTF-8 name such as `my.label`)."""
    return name if _LEGACY_LABEL_NAME.fullmatch(name) else _promql_string(name)


_SENTINEL = "__mapping_filter__"


def _matchers_of(filter_expr: str, tree) -> list[str]:
    if isinstance(tree, _lib_consumer_read.InputRefused):
        raise FilterRefused(f"filter {filter_expr!r} is not a list of PromQL label matchers: "
                            f"{_lib_consumer_read.error_text(tree)}")
    if tree.get("kind") != "selector" or tree.get("range"):
        raise FilterRefused(f"filter {filter_expr!r} is not a list of PromQL label matchers")
    out: list[str] = []
    for m in tree["matchers"]:
        if m["name"] == "__name__":
            if m["type"] == "=" and m["value"] == _SENTINEL:
                continue
            raise FilterRefused(f"filter {filter_expr!r} names the metric (__name__); "
                                "the metric comes from the dictionary")
        out.append(f'{_promql_label_name(m["name"])}{m["type"]}{_promql_string(m["value"])}')
    return out


def parse_filters(filters: list[str]) -> "list[list[str] | FilterRefused]":
    """parse_filter_to_matchers of every filter, read in one da-read run:
    per filter its matchers, or the FilterRefused that says why not."""
    out: "list[list[str] | FilterRefused | None]" = [[] if not f.strip() else None for f in filters]
    todo = [i for i, v in enumerate(out) if v is None]
    trees = _lib_consumer_read.promql_shape_each(
        [_SENTINEL + "{" + filters[i] + "}" for i in todo])
    for i, tree in zip(todo, trees):
        try:
            out[i] = _matchers_of(filters[i], tree)
        except FilterRefused as exc:
            out[i] = exc
    return out  # type: ignore[return-value]


def parse_filter_to_matchers(filter_expr: str) -> list[str]:
    """The PromQL label matchers of a mapping entry's `filter`, read by
    Prometheus' parser as the label block of a selector and written back in
    one spelling, e.g.

      'schema=~"app_a_.*"'
      'schema=~"app_a_.*", tablespace="ts_a"'

    #4264: read on the tree, so a comma or a quote inside a value is part of
    the value (the text split cut `schema=~"a,b"` in two). A filter the parser
    refuses, or one that names the metric (`__name__`), raises FilterRefused.
    """
    [got] = parse_filters([filter_expr])
    if isinstance(got, FilterRefused):
        raise got
    return got


def _entry_matchers(entry: "MappingEntry") -> list[str]:
    return entry.matchers if entry.matchers is not None else parse_filter_to_matchers(entry.filter_expr)


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
            filter_matchers = _entry_matchers(entry)
            # Build label selector: instance + filter matchers
            all_matchers = [f'instance={_promql_string(mapping.instance)}'] + filter_matchers
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
            try:
                matchers = _entry_matchers(entry)
            except FilterRefused as exc:
                messages.append(f"ERROR: instance={mapping.instance}, tenant={entry.tenant}: {exc}")
                continue
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

@exit_on_yaml_file_error  # #1654: unreadable mapping/tenant/dictionary file → rc 2, named
@_lib_parse_cache.exit_on_parse_tree_error  # #2906: no usable parse-tree → rc 2
def main() -> None:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=_h('description'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--config-dir', required=True, help=_h('config_dir'))
    _lib_input.add_input(parser, '--metric-dictionary', decode=DICTIONARY_DOCUMENT,
                         help=_h('metric_dict'))
    parser.add_argument('--metrics', default=None, help=_h('metrics'))
    parser.add_argument('--format', choices=['yaml', 'configmap'], default='yaml',
                        help=_h('format'))
    # #3107 ③b: nothing else writes stdout here, so `-` is stdout.
    parser.add_argument('-o', '--output', default=None, type=operator_file_type(),
                        help=_h('output'))
    parser.add_argument('--namespace', default='monitoring', help=_h('namespace'))
    parser.add_argument('--dry-run', action='store_true', help=_h('dry_run'))
    parser.add_argument('--validate', action='store_true', help=_h('validate'))
    args = parser.parse_args()

    config_dir = args.config_dir
    require_dir(config_dir, lang="en", flag="--config-dir")  # #3276

    # Find mapping file
    mapping_path = find_mapping_file(config_dir)
    if not mapping_path:
        print(f"INFO: no _instance_mapping.yaml found in {config_dir} — "
              "no 1:N mappings to generate", file=sys.stderr)
        sys.exit(EXIT_OK)

    # Parse mappings
    mappings = parse_mapping_file(mapping_path)
    if not mappings:
        print("INFO: no valid mappings found in _instance_mapping.yaml", file=sys.stderr)
        sys.exit(EXIT_OK)
    # #4264 / #3445: a filter Prometheus' parser cannot read is an unreadable
    # input — named, exit 2, nothing generated.
    pairs = [(m, e) for m in mappings for e in m.entries]
    try:
        parsed = parse_filters([e.filter_expr for _m, e in pairs])
    except _lib_consumer_read.ConsumerReadError as exc:
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    for (mapping, entry), got in zip(pairs, parsed):
        if isinstance(got, FilterRefused):
            print(f"ERROR: {mapping_path}: instance={mapping.instance}, "
                  f"tenant={entry.tenant}: {got}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        entry.matchers = got

    # Resolve metrics list
    if args.metrics:
        metrics = [m.strip() for m in args.metrics.split(',') if m.strip()]
    else:
        # Auto-detect from metric-dictionary.yaml
        # #3276 / #3445: one given is read by the lib — path and content,
        # named; the shipped one is the default.
        dictionary = _lib_input.document(args, "metric_dictionary", lang="en")
        this_dir = Path(_THIS_DIR)
        dict_path = this_dir.parent / "metric-dictionary.yaml"
        if not dict_path.is_file():
            dict_path = this_dir / "metric-dictionary.yaml"
        if dictionary is not None:
            metrics = metrics_in_dictionary(dictionary)
        elif dict_path.is_file():
            metrics = load_metrics_from_dictionary(str(dict_path))
        else:
            print("ERROR: no --metrics specified and metric-dictionary.yaml not found",
                  file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

    if not metrics:
        print("ERROR: no metrics to map", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

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
            sys.exit(EXIT_VIOLATION)
        sys.exit(EXIT_OK)

    if errors_found:
        print("ERROR: validation failed — fix errors above before generating rules",
              file=sys.stderr)
        sys.exit(EXIT_VIOLATION)

    # Generate rules
    groups = generate_recording_rules(mappings, metrics)

    # Format output
    if args.format == 'configmap':
        output = format_as_configmap(groups, namespace=args.namespace)
    else:
        output = format_as_yaml(groups)

    # Write or print
    if args.output:
        # #1641: an unwritable -o is rc=2 + one line, not a traceback at rc=1.
        write_operator_output_or_die(args.output, output, flag="-o/--output")
        print(f"Generated {len(groups)} rule group(s), "
              f"~{card['new_series_estimate']} new series → {args.output}",
              file=sys.stderr)
    else:
        sys.stdout.write(output)


if __name__ == '__main__':
    import _lib_exitcodes
    _lib_exitcodes.run(main)
