#!/usr/bin/env python3
"""
onboard_platform.py — Reverse-analyze existing configs for Dynamic Alerting onboarding.

Three-phase analysis for enterprises migrating to the Dynamic Alerting platform:

  Phase 1: Alertmanager config → per-tenant _routing YAML
  Phase 2: Prometheus rule files → migration plan CSV + _defaults.yaml + tenant overlays
  Phase 3: Prometheus scrape config → relabel_configs suggestions

Usage:
  # Phase 1 only — Alertmanager reverse analysis
  python3 onboard_platform.py --alertmanager-config alertmanager.yml -o output/

  # Phase 2 only — Rule file analysis
  python3 onboard_platform.py --rule-files 'rules/*.yml' -o output/

  # Phase 3 only — Scrape config analysis
  python3 onboard_platform.py --scrape-config prometheus.yml -o output/

  # All three phases
  python3 onboard_platform.py --alertmanager-config am.yml --rule-files 'rules/*.yml' \\
      --scrape-config prom.yml -o output/

  # Custom tenant label (for enterprises using non-standard label names)
  python3 onboard_platform.py --alertmanager-config am.yml --tenant-label instance -o output/
"""
import argparse
import csv
import io
from collections import Counter
import glob
import json
import os
import re
import sys
import textwrap

import yaml

# ---------------------------------------------------------------------------
# Import shared utilities
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout

from _lib_python import (  # noqa: E402
    load_yaml_file,
    validate_and_clamp,
    write_onboard_hints,
    write_text_secure,
    write_json_secure,
    RECEIVER_TYPES,
    METRIC_PREFIX_DB_MAP,
)

# Conditionally import migrate_rule AST functions
try:
    from migrate_rule import (  # noqa: E402
        parse_expr,
        guess_aggregation,
        load_metric_dictionary,
    )
    HAS_MIGRATE = True
except ImportError:
    HAS_MIGRATE = False


# ============================================================
# Constants
# ============================================================
DEFAULT_TENANT_LABEL = "tenant"

# Build reverse map: am_key → (receiver_type, spec)
_AM_KEY_TO_TYPE = {}
for _rtype, _spec in RECEIVER_TYPES.items():
    ak = _spec["am_key"]
    if ak not in _AM_KEY_TO_TYPE:
        _AM_KEY_TO_TYPE[ak] = []
    _AM_KEY_TO_TYPE[ak].append((_rtype, _spec))


# ============================================================
# Phase 1: Alertmanager Config Reverse Analysis
# ============================================================

def parse_alertmanager_config(path):
    """Load and parse an Alertmanager configuration file.

    Handles both raw alertmanager.yml and ConfigMap-wrapped YAML.
    Returns the parsed Alertmanager config dict, or None on error.
    """
    data = load_yaml_file(path)
    if data is None:
        return None

    # ConfigMap wrapper: extract data.alertmanager.yml
    if "data" in data and isinstance(data["data"], dict):
        am_yml = data["data"].get("alertmanager.yml")
        if am_yml and isinstance(am_yml, str):
            data = yaml.safe_load(am_yml)
        elif am_yml and isinstance(am_yml, dict):
            data = am_yml

    # Validate basic structure
    if not isinstance(data, dict):
        return None
    if "route" not in data and "receivers" not in data:
        return None

    return data


def _extract_tenant_from_matchers(matchers, tenant_label):
    """Extract tenant name from a list of matcher strings.

    Supports both forms:
      - tenant="value"
      - tenant=~"value"  (treated as exact if no regex chars)

    Returns tenant name string or None.
    """
    if not matchers or not isinstance(matchers, list):
        return None

    for m in matchers:
        if not isinstance(m, str):
            continue
        # Pattern: label="value" or label=~"value" or label!="value"
        match = re.match(
            r'^(\w+)\s*(!?=~?)\s*["\']([^"\']*)["\']$', m.strip())
        if not match:
            continue
        label, op, value = match.groups()
        if label == tenant_label and op in ("=", "=~"):
            # For regex matcher, only accept if it looks like exact match
            if op == "=~" and re.search(r'[.*+?|\\()\[\]{}^$]', value):
                continue  # Skip true regex patterns
            return value
    return None


def flatten_route_tree(route, parent_matchers=None, tenant_label=DEFAULT_TENANT_LABEL):
    """Recursively flatten a nested Alertmanager route tree.

    Returns list of dicts:
      [{tenant, receiver, group_by, group_wait, group_interval,
        repeat_interval, matchers, continue_flag}]
    """
    if parent_matchers is None:
        parent_matchers = []

    results = []

    if not isinstance(route, dict):
        return results

    # Current route matchers (merge with parent)
    matchers = list(parent_matchers)
    if "matchers" in route and isinstance(route["matchers"], list):
        matchers.extend(route["matchers"])
    # Legacy match/match_re format
    if "match" in route and isinstance(route["match"], dict):
        for k, v in route["match"].items():
            matchers.append(f'{k}="{v}"')
    if "match_re" in route and isinstance(route["match_re"], dict):
        for k, v in route["match_re"].items():
            matchers.append(f'{k}=~"{v}"')

    tenant = _extract_tenant_from_matchers(matchers, tenant_label)
    continue_flag = route.get("continue", False)

    entry = {
        "tenant": tenant,
        "receiver": route.get("receiver"),
        "group_by": route.get("group_by"),
        "group_wait": route.get("group_wait"),
        "group_interval": route.get("group_interval"),
        "repeat_interval": route.get("repeat_interval"),
        "matchers": matchers,
        "continue_flag": continue_flag,
    }

    # Only add if this route has a receiver (leaf or meaningful node)
    if entry["receiver"]:
        results.append(entry)

    # Recurse into child routes
    for child in route.get("routes", []):
        results.extend(flatten_route_tree(child, matchers, tenant_label))

    return results


def reverse_map_receiver(am_receivers, receiver_name):
    """Map an Alertmanager receiver back to a structured receiver object.

    Args:
        am_receivers: list of receiver dicts from Alertmanager config.
        receiver_name: name to look up.

    Returns:
        dict with {type, ...fields} or None if not found.
    """
    if not am_receivers or not receiver_name:
        return None

    receiver = None
    for r in am_receivers:
        if r.get("name") == receiver_name:
            receiver = r
            break

    if not receiver:
        return None

    # Try each AM config key to identify receiver type
    for am_key, type_list in _AM_KEY_TO_TYPE.items():
        configs = receiver.get(am_key)
        if not configs or not isinstance(configs, list) or len(configs) == 0:
            continue

        cfg = configs[0]  # Take first config entry
        rtype, spec = type_list[0]

        # Disambiguate: rocketchat vs webhook (both use webhook_configs)
        # Heuristic: if receiver name contains "rocket" or has channel metadata
        if am_key == "webhook_configs" and len(type_list) > 1:
            for t, s in type_list:
                if t == "rocketchat" and ("rocket" in receiver_name.lower()
                                          or "channel" in cfg):
                    rtype, spec = t, s
                    break

        result = {"type": rtype}
        # Copy all fields from config
        for field in spec["required"] + spec.get("optional", []):
            if field in cfg:
                result[field] = cfg[field]
        # Also copy metadata fields if present
        for field in spec.get("metadata", []):
            if field in cfg:
                result[field] = cfg[field]

        return result

    return None


def _check_timing_guardrails(timing_value, param_name):
    """Check if a timing value is within platform guardrails.

    Thin wrapper around validate_and_clamp() for onboard analysis context.
    Returns (value_str, warning_or_None).
    """
    if not timing_value:
        return None, None

    val_str = str(timing_value)
    _clamped, warnings = validate_and_clamp(param_name, val_str, "onboard-analysis")
    if warnings:
        # Extract the core message (strip "  WARN: onboard-analysis: " prefix)
        msg = warnings[0]
        prefix = "  WARN: onboard-analysis: "
        if msg.startswith(prefix):
            msg = msg[len(prefix):]
        return val_str, msg
    return val_str, None


def analyze_alertmanager(am_config, tenant_label=DEFAULT_TENANT_LABEL):
    """Analyze Alertmanager config and extract per-tenant routing.

    Returns:
        (tenant_routings, summary)
        tenant_routings: {tenant_name: {_routing dict}}
        summary: dict with stats and warnings
    """
    tenant_routings = {}
    warnings = []
    skipped_routes = []

    receivers = am_config.get("receivers", [])
    root_route = am_config.get("route", {})

    # Flatten route tree
    flat_routes = flatten_route_tree(root_route, tenant_label=tenant_label)

    for entry in flat_routes:
        tenant = entry["tenant"]

        if not tenant:
            # Non-tenant route (e.g., default, platform enforced)
            if entry.get("continue_flag"):
                skipped_routes.append({
                    "receiver": entry["receiver"],
                    "reason": "platform/continue route (likely enforced routing)",
                })
            else:
                skipped_routes.append({
                    "receiver": entry["receiver"],
                    "reason": f"no {tenant_label} matcher found",
                })
            continue

        # Reverse-map receiver
        receiver_obj = reverse_map_receiver(receivers, entry["receiver"])
        if not receiver_obj:
            warnings.append(
                f"{tenant}: receiver '{entry['receiver']}' not found in receivers list")
            continue

        # Build _routing section
        routing = {"receiver": receiver_obj}

        # Group by
        if entry["group_by"]:
            routing["group_by"] = entry["group_by"]

        # Timing parameters with guardrail checks
        for param in ("group_wait", "group_interval", "repeat_interval"):
            val = entry.get(param)
            if val:
                val_str, warning = _check_timing_guardrails(val, param)
                if val_str:
                    routing[param] = val_str
                if warning:
                    warnings.append(f"{tenant}: {warning}")

        tenant_routings[tenant] = routing

    # Analyze inhibit_rules for severity dedup
    dedup_info = {}
    for rule in am_config.get("inhibit_rules", []):
        source = rule.get("source_matchers", [])
        target = rule.get("target_matchers", [])
        equal = rule.get("equal", [])

        # Check if this is a severity dedup rule
        has_severity_source = any("severity" in m and "critical" in m for m in source)
        has_severity_target = any("severity" in m and "warning" in m for m in target)
        has_metric_group = "metric_group" in equal

        if has_severity_source and has_severity_target and has_metric_group:
            tenant = _extract_tenant_from_matchers(source, tenant_label)
            if tenant:
                dedup_info[tenant] = "enable"

    summary = {
        "total_routes": len(flat_routes),
        "tenant_routes": len(tenant_routings),
        "skipped_routes": skipped_routes,
        "dedup_tenants": dedup_info,
        "warnings": warnings,
    }

    return tenant_routings, summary


def generate_tenant_routing_yamls(tenant_routings, dedup_info=None):
    """Generate per-tenant YAML content from routing analysis.

    Returns dict: {tenant_name: yaml_content_string}
    """
    results = {}
    if dedup_info is None:
        dedup_info = {}

    for tenant in sorted(tenant_routings.keys()):
        routing = tenant_routings[tenant]
        content = {"tenants": {tenant: {"_routing": routing}}}

        # Add severity dedup info if detected
        if tenant in dedup_info:
            content["tenants"][tenant]["_severity_dedup"] = dedup_info[tenant]

        results[tenant] = yaml.dump(
            content, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return results


# ============================================================
# Phase 2: Prometheus Rule File Analysis
# ============================================================

def scan_rule_files(pattern):
    """Scan for Prometheus rule files matching a glob pattern.

    Returns list of file paths.
    """
    files = glob.glob(pattern, recursive=True)
    return sorted(f for f in files if os.path.isfile(f))


def classify_rule(rule):
    """Classify a Prometheus rule as recording/alert/unknown.

    Returns: "recording" | "alert" | "unknown"
    """
    if "record" in rule:
        return "recording"
    if "alert" in rule:
        return "alert"
    return "unknown"


def _clean_alert_expr(expr):
    """清理 PromQL 告警運算式以利解析。

    處理步驟：
    1. 合併多行為單行
    2. 移除 ``unless on(...) (user_state_filter{...} == N)`` 維護模式子句
    3. 移除最外層平衡括號

    Returns:
        str — 清理後的運算式。
    """
    # 合併多行
    clean = " ".join(expr.strip().split())
    # 移除維護模式 unless 子句
    clean = re.sub(
        r'\s*unless\s+on\s*\([^)]*\)\s*\(user_state_filter\{[^}]*\}\s*==\s*\d+\)',
        '', clean)
    clean = clean.strip()
    # 移除最外層平衡括號
    if clean.startswith("(") and clean.endswith(")"):
        inner = clean[1:-1].strip()
        depth = 0
        balanced = True
        for c in inner:
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth < 0:
                    balanced = False
                    break
        if balanced and depth == 0:
            clean = inner
    return clean


def _enrich_parsed_result(result, parsed, metric_dict):
    """從 parse_expr 結果填充閾值候選資訊（aggregation、dict_match）。

    就地修改 *result* dict。
    """
    result["metric_key"] = parsed["base_key"]
    result["threshold_value"] = parsed["val"]
    result["operator"] = parsed["op"]
    result["status"] = "complex" if parsed["is_complex"] else "perfect"

    # 推測 aggregation 方式
    agg, reason = guess_aggregation(parsed["base_key"], parsed["lhs"])
    result["aggregation"] = agg
    result["agg_reason"] = reason

    # Metric dictionary 比對
    if parsed["base_key"] in metric_dict:
        result["dict_match"] = metric_dict[parsed["base_key"]]


def extract_threshold_candidates(alert_rule, metric_dict=None):
    """Extract threshold candidates from an alert rule expression.

    委派至 :func:`_clean_alert_expr` 清理運算式，再由
    :func:`_enrich_parsed_result` 填充解析結果。

    Returns dict:
      {metric_key, threshold_value, operator, severity, aggregation,
       agg_reason, dict_match, status}
    """
    if metric_dict is None:
        metric_dict = {}

    alert_name = alert_rule.get("alert", "unknown")
    labels = alert_rule.get("labels", {})
    severity = labels.get("severity", "warning")

    result = {
        "alert_name": alert_name,
        "metric_key": None,
        "threshold_value": None,
        "operator": None,
        "severity": severity,
        "aggregation": None,
        "agg_reason": None,
        "dict_match": None,
        "status": "unparseable",
        "metric_group": labels.get("metric_group"),
    }

    if not HAS_MIGRATE:
        return result

    clean_expr = _clean_alert_expr(alert_rule.get("expr", ""))
    parsed = parse_expr(clean_expr)
    if parsed is None:
        return result

    _enrich_parsed_result(result, parsed, metric_dict)
    return result


def analyze_rule_files(file_paths, tenant_label=DEFAULT_TENANT_LABEL, metric_dict=None):
    """Analyze Prometheus rule files and generate migration plan.

    Returns:
        (candidates, recording_rules, summary)
        candidates: list of threshold candidate dicts
        recording_rules: list of recording rule info dicts
        summary: dict with stats
    """
    if metric_dict is None:
        metric_dict = {}

    candidates = []
    recording_rules = []
    errors = []
    total_groups = 0
    total_rules = 0

    for fpath in file_paths:
        data = load_yaml_file(fpath)
        if data is None:
            errors.append(f"Failed to load: {fpath}")
            continue

        groups = data.get("groups")
        if not groups or not isinstance(groups, list):
            # Try ConfigMap wrapper
            if "data" in data and isinstance(data["data"], dict):
                for key, val in data["data"].items():
                    if isinstance(val, str):
                        try:
                            inner = yaml.safe_load(val)
                            if isinstance(inner, dict) and "groups" in inner:
                                groups = inner["groups"]
                        except yaml.YAMLError:
                            pass
            if not isinstance(groups, list):
                errors.append(f"No 'groups' found in: {fpath}")
                continue

        for group in groups:
            if not isinstance(group, dict):
                continue
            total_groups += 1
            group_name = group.get("name", "unnamed")

            for rule in group.get("rules", []):
                total_rules += 1
                rtype = classify_rule(rule)

                if rtype == "recording":
                    recording_rules.append({
                        "name": rule.get("record", ""),
                        "expr": rule.get("expr", ""),
                        "group": group_name,
                        "file": os.path.basename(fpath),
                    })
                elif rtype == "alert":
                    candidate = extract_threshold_candidates(rule, metric_dict)
                    candidate["group"] = group_name
                    candidate["file"] = os.path.basename(fpath)
                    candidates.append(candidate)

    summary = {
        "files_scanned": len(file_paths),
        "total_groups": total_groups,
        "total_rules": total_rules,
        "alert_rules": len(candidates),
        "recording_rules": len(recording_rules),
        "parseable": sum(1 for c in candidates if c["status"] != "unparseable"),
        "unparseable": sum(1 for c in candidates if c["status"] == "unparseable"),
        "errors": errors,
    }

    return candidates, recording_rules, summary


def generate_defaults_from_candidates(candidates):
    """Generate _defaults.yaml content from threshold candidates.

    Groups candidates by metric_key, uses most common threshold as default.
    Returns dict suitable for YAML output.
    """
    metric_values = {}  # metric_key → [threshold_values]

    for c in candidates:
        if c["status"] == "unparseable" or not c["metric_key"]:
            continue
        key = c["metric_key"]
        if c["severity"] == "critical":
            key = f"{key}_critical"
        if key not in metric_values:
            metric_values[key] = []
        metric_values[key].append(c["threshold_value"])

    defaults = {}
    for key in sorted(metric_values.keys()):
        values = metric_values[key]
        # Use most common value as default
        counts = Counter(values)
        default_val = counts.most_common(1)[0][0]
        defaults[key] = default_val

    return {"defaults": defaults} if defaults else {}


def write_migration_csv(candidates, output_path):
    """Write migration plan CSV from candidates."""
    fieldnames = [
        "alert_name", "file", "group", "status", "severity",
        "metric_key", "threshold_value", "operator",
        "aggregation", "agg_reason", "metric_group", "dict_match",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for c in candidates:
        row = dict(c)
        if row.get("dict_match"):
            row["dict_match"] = row["dict_match"].get("maps_to", "")
        writer.writerow(row)
    write_text_secure(output_path, buf.getvalue())


# ============================================================
# Phase 3: Prometheus Scrape Config Analysis
# ============================================================

def parse_scrape_configs(path):
    """Parse Prometheus config and extract scrape_configs.

    Handles both raw prometheus.yml and ConfigMap-wrapped YAML.
    Returns list of scrape_config dicts.
    """
    data = load_yaml_file(path)
    if data is None:
        return []

    # ConfigMap wrapper
    if "data" in data and isinstance(data["data"], dict):
        prom_yml = data["data"].get("prometheus.yml")
        if prom_yml and isinstance(prom_yml, str):
            data = yaml.safe_load(prom_yml)
        elif prom_yml and isinstance(prom_yml, dict):
            data = prom_yml

    if not isinstance(data, dict):
        return []

    return data.get("scrape_configs", [])


def analyze_relabel_configs(scrape_config, tenant_label=DEFAULT_TENANT_LABEL):
    """Analyze a single scrape_config's relabel_configs for tenant mapping patterns.

    Returns dict:
      {job_name, has_tenant_mapping, mapping_type, source_labels,
       regex, replacement, suggestions}
    """
    job_name = scrape_config.get("job_name", "unknown")
    relabels = scrape_config.get("relabel_configs", [])
    metric_relabels = scrape_config.get("metric_relabel_configs", [])

    result = {
        "job_name": job_name,
        "has_tenant_mapping": False,
        "mapping_type": None,
        "source_labels": None,
        "regex": None,
        "replacement": None,
        "suggestions": [],
    }

    all_relabels = relabels + metric_relabels

    for relabel in all_relabels:
        if not isinstance(relabel, dict):
            continue
        target = relabel.get("target_label", "")
        if target == tenant_label:
            result["has_tenant_mapping"] = True
            source = relabel.get("source_labels", [])
            result["source_labels"] = source

            if "__meta_kubernetes_namespace" in source:
                result["mapping_type"] = "namespace"
            elif any("__meta_kubernetes_service_label" in s for s in source):
                result["mapping_type"] = "service_label"
            elif any("__meta_kubernetes_pod_label" in s for s in source):
                result["mapping_type"] = "pod_label"
            else:
                result["mapping_type"] = "custom"

            result["regex"] = relabel.get("regex")
            result["replacement"] = relabel.get("replacement")
            break

    # Suggestions for jobs without tenant mapping
    if not result["has_tenant_mapping"]:
        result["suggestions"].append({
            "type": "namespace_mapping",
            "description": f"Add relabel_configs to map namespace → {tenant_label}",
            "snippet": {
                "relabel_configs": [
                    {
                        "source_labels": ["__meta_kubernetes_namespace"],
                        "target_label": tenant_label,
                    }
                ]
            },
        })

    return result


def analyze_scrape_configs(scrape_configs, tenant_label=DEFAULT_TENANT_LABEL):
    """Analyze all scrape configs for tenant mapping patterns.

    Returns (job_analyses, summary).
    """
    job_analyses = []
    for sc in scrape_configs:
        if not isinstance(sc, dict):
            continue
        analysis = analyze_relabel_configs(sc, tenant_label)
        job_analyses.append(analysis)

    summary = {
        "total_jobs": len(job_analyses),
        "with_tenant_mapping": sum(1 for j in job_analyses if j["has_tenant_mapping"]),
        "without_tenant_mapping": sum(1 for j in job_analyses if not j["has_tenant_mapping"]),
        "mapping_types": {},
    }

    for j in job_analyses:
        if j["mapping_type"]:
            mt = j["mapping_type"]
            summary["mapping_types"][mt] = summary["mapping_types"].get(mt, 0) + 1

    return job_analyses, summary


# ============================================================
# Output Generation
# ============================================================

def _write_phase1_outputs(output_dir, phase1_results, report):
    """寫入 Phase 1（Alertmanager 逆向分析）輸出檔案。

    Args:
        output_dir: 輸出目錄路徑。
        phase1_results: ``(tenant_routings, summary)`` 二元組。
        report: 報告 dict，會被就地更新。
    """
    tenant_routings, summary = phase1_results
    dedup_info = summary.get("dedup_tenants", {})
    yamls = generate_tenant_routing_yamls(tenant_routings, dedup_info)

    phase1_dir = os.path.join(output_dir, "phase1-routing")
    os.makedirs(phase1_dir, exist_ok=True)

    for tenant, content in yamls.items():
        fpath = os.path.join(phase1_dir, f"{tenant}.yaml")
        yaml_content = (
            "# Generated by onboard_platform.py — Phase 1\n"
            "# Source: Alertmanager config reverse analysis\n"
            f"# Review and merge into conf.d/{tenant}.yaml\n\n"
            + content
        )
        write_text_secure(fpath, yaml_content)
        report["files_written"].append(fpath)

    # Summary CSV
    csv_path = os.path.join(phase1_dir, "routing-summary.csv")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["tenant", "receiver_type", "group_wait",
                     "group_interval", "repeat_interval", "severity_dedup"])
    for tenant in sorted(tenant_routings.keys()):
        r = tenant_routings[tenant]
        recv = r.get("receiver", {})
        writer.writerow([
            tenant,
            recv.get("type", "unknown"),
            r.get("group_wait", ""),
            r.get("group_interval", ""),
            r.get("repeat_interval", ""),
            dedup_info.get(tenant, "unknown"),
        ])
    write_text_secure(csv_path, buf.getvalue())
    report["files_written"].append(csv_path)

    report["phases"]["phase1"] = {
        "tenant_count": len(tenant_routings),
        "tenants": sorted(tenant_routings.keys()),
        "summary": summary,
    }


def _write_phase2_outputs(output_dir, phase2_results, report):
    """寫入 Phase 2（Prometheus rule 分析）輸出檔案。

    Args:
        output_dir: 輸出目錄路徑。
        phase2_results: ``(candidates, recording_rules, summary)`` 三元組。
        report: 報告 dict，會被就地更新。
    """
    candidates, recording_rules, summary = phase2_results

    phase2_dir = os.path.join(output_dir, "phase2-rules")
    os.makedirs(phase2_dir, exist_ok=True)

    # Migration plan CSV
    csv_path = os.path.join(phase2_dir, "migration-plan.csv")
    write_migration_csv(candidates, csv_path)
    report["files_written"].append(csv_path)

    # Suggested _defaults.yaml
    defaults = generate_defaults_from_candidates(candidates)
    if defaults:
        defaults_path = os.path.join(phase2_dir, "_defaults-suggestion.yaml")
        defaults_content = (
            "# Generated by onboard_platform.py — Phase 2\n"
            "# Suggested platform defaults from rule analysis\n"
            "# Review and merge into conf.d/_defaults.yaml\n\n"
            + yaml.dump(defaults, default_flow_style=False,
                        allow_unicode=True, sort_keys=False)
        )
        write_text_secure(defaults_path, defaults_content)
        report["files_written"].append(defaults_path)

    report["phases"]["phase2"] = {
        "candidates": len(candidates),
        "recording_rules": len(recording_rules),
        "summary": summary,
    }


def _write_phase3_outputs(output_dir, phase3_results, report):
    """寫入 Phase 3（Scrape config 分析）輸出檔案。

    Args:
        output_dir: 輸出目錄路徑。
        phase3_results: ``(job_analyses, summary)`` 二元組。
        report: 報告 dict，會被就地更新。
    """
    job_analyses, summary = phase3_results

    phase3_dir = os.path.join(output_dir, "phase3-scrape")
    os.makedirs(phase3_dir, exist_ok=True)

    # Per-job analysis + suggestions
    for analysis in job_analyses:
        if not analysis["suggestions"]:
            continue
        job_name = analysis["job_name"].replace("/", "_")
        fpath = os.path.join(phase3_dir, f"{job_name}-relabel-suggestion.yaml")
        buf = io.StringIO()
        buf.write("# Generated by onboard_platform.py — Phase 3\n")
        buf.write(f"# Suggested relabel_configs for job: {analysis['job_name']}\n")
        buf.write("# Add to scrape_configs[].relabel_configs\n\n")
        for suggestion in analysis["suggestions"]:
            buf.write(f"# {suggestion['description']}\n")
            buf.write(yaml.dump(suggestion["snippet"], default_flow_style=False,
                                allow_unicode=True, sort_keys=False))
            buf.write("\n")
        write_text_secure(fpath, buf.getvalue())
        report["files_written"].append(fpath)

    # Summary
    summary_path = os.path.join(phase3_dir, "scrape-analysis.yaml")
    summary_content = (
        "# Generated by onboard_platform.py — Phase 3\n"
        "# Scrape config analysis summary\n\n"
        + yaml.dump({
            "summary": summary,
            "jobs": [
                {
                    "job_name": j["job_name"],
                    "has_tenant_mapping": j["has_tenant_mapping"],
                    "mapping_type": j["mapping_type"],
                }
                for j in job_analyses
            ],
        }, default_flow_style=False, allow_unicode=True, sort_keys=False)
    )
    write_text_secure(summary_path, summary_content)
    report["files_written"].append(summary_path)

    report["phases"]["phase3"] = {
        "jobs_analyzed": len(job_analyses),
        "summary": summary,
    }


def write_outputs(output_dir, phase1_results=None, phase2_results=None,
                  phase3_results=None, dry_run=False, json_output=False):
    """Write all analysis outputs to the output directory.

    委派至 :func:`_write_phase1_outputs`、:func:`_write_phase2_outputs`、
    :func:`_write_phase3_outputs` 分別處理各階段輸出。

    Returns report dict summarizing what was generated.
    """
    report = {"phases": {}, "files_written": [], "warnings": []}

    if dry_run:
        # Collect all output for display
        if phase1_results:
            tenant_routings, summary = phase1_results
            report["phases"]["phase1"] = {
                "tenant_count": len(tenant_routings),
                "tenants": sorted(tenant_routings.keys()),
                "summary": summary,
            }
        if phase2_results:
            candidates, recording_rules, summary = phase2_results
            report["phases"]["phase2"] = {
                "candidates": len(candidates),
                "recording_rules": len(recording_rules),
                "summary": summary,
            }
        if phase3_results:
            job_analyses, summary = phase3_results
            report["phases"]["phase3"] = {
                "jobs_analyzed": len(job_analyses),
                "summary": summary,
            }
        return report

    os.makedirs(output_dir, exist_ok=True)

    if phase1_results:
        _write_phase1_outputs(output_dir, phase1_results, report)

    if phase2_results:
        _write_phase2_outputs(output_dir, phase2_results, report)

    if phase3_results:
        _write_phase3_outputs(output_dir, phase3_results, report)

    # Write onboard hints for scaffold pipeline (--auto-scaffold)
    if not dry_run and not json_output:
        hints = _build_onboard_hints(phase1_results, phase2_results, phase3_results)
        if hints.get("tenants"):
            hints_path = write_onboard_hints(output_dir, hints)
            report["files_written"].append(hints_path)
            report["onboard_hints"] = hints

    return report


def _build_onboard_hints(phase1_results, phase2_results, phase3_results):
    """Build onboard hints dict from analysis results for scaffold consumption."""
    hints = {"tenants": [], "db_types": {}, "routing_hints": {}}

    # From Phase 1: tenant names + routing info
    if phase1_results:
        tenant_routings, _ = phase1_results
        for tenant, routing in tenant_routings.items():
            if tenant not in hints["tenants"]:
                hints["tenants"].append(tenant)
            recv = routing.get("receiver", {})
            hints["routing_hints"][tenant] = {
                "receiver_type": recv.get("type", "webhook"),
                "group_wait": routing.get("group_wait"),
                "group_interval": routing.get("group_interval"),
                "repeat_interval": routing.get("repeat_interval"),
            }

    # From Phase 2: DB types inferred from rule metric prefixes
    if phase2_results:
        candidates, _, summary = phase2_results
        for c in candidates:
            metric = c.get("alert", c.get("record", "")).lower()
            for prefix, db_type in METRIC_PREFIX_DB_MAP.items():
                if prefix in metric:
                    # Associate with all known tenants
                    for t in hints["tenants"]:
                        hints["db_types"].setdefault(t, set()).add(db_type)
                    break

        # Convert sets to lists for JSON serialization
        for t in hints["db_types"]:
            hints["db_types"][t] = sorted(hints["db_types"][t])

    hints["tenants"] = sorted(hints["tenants"])
    return hints


# ============================================================
# CLI
# ============================================================

def main():
    """CLI entry point: Reverse-analyze existing configs for Dynamic Alerting onboarding."""
    parser = argparse.ArgumentParser(
        description="Reverse-analyze existing configs for Dynamic Alerting onboarding",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s --alertmanager-config alertmanager.yml -o output/
              %(prog)s --rule-files 'rules/*.yml' -o output/
              %(prog)s --scrape-config prometheus.yml -o output/
              %(prog)s --alertmanager-config am.yml --rule-files 'rules/*.yml' -o output/
              %(prog)s --alertmanager-config am.yml --tenant-label instance -o output/
        """),
    )
    parser.add_argument("--alertmanager-config",
                        help="Phase 1: Alertmanager config file (YAML or ConfigMap)")
    parser.add_argument("--rule-files",
                        help="Phase 2: Prometheus rule files glob pattern")
    parser.add_argument("--scrape-config",
                        help="Phase 3: Prometheus scrape config file")
    parser.add_argument("--tenant-label", default=DEFAULT_TENANT_LABEL,
                        help=f"Custom tenant label name (default: {DEFAULT_TENANT_LABEL})")
    parser.add_argument("-o", "--output-dir", default="onboard_output",
                        help="Output directory (default: onboard_output)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview analysis without writing files")
    parser.add_argument("--json", action="store_true",
                        help="JSON output for CI pipeline integration")

    args = parser.parse_args()

    if not any([args.alertmanager_config, args.rule_files, args.scrape_config]):
        parser.print_help()
        print("\nERROR: At least one phase input is required.", file=sys.stderr)
        sys.exit(1)

    phase1_results = None
    phase2_results = None
    phase3_results = None

    # Phase 1: Alertmanager
    if args.alertmanager_config:
        print(f"Phase 1: Analyzing Alertmanager config: {args.alertmanager_config}")
        am_config = parse_alertmanager_config(args.alertmanager_config)
        if am_config is None:
            print(f"ERROR: Failed to parse Alertmanager config: {args.alertmanager_config}",
                  file=sys.stderr)
            sys.exit(1)

        tenant_routings, summary = analyze_alertmanager(am_config, args.tenant_label)
        phase1_results = (tenant_routings, summary)

        print(f"  Found {summary['tenant_routes']} tenant route(s) "
              f"(of {summary['total_routes']} total)")
        for w in summary["warnings"]:
            print(f"  WARN: {w}", file=sys.stderr)
        for s in summary["skipped_routes"]:
            print(f"  SKIP: receiver='{s['receiver']}' — {s['reason']}")

    # Phase 2: Rule files
    if args.rule_files:
        print(f"Phase 2: Analyzing rule files: {args.rule_files}")
        if not HAS_MIGRATE:
            print("  WARN: migrate_rule.py not available, Phase 2 will have limited analysis",
                  file=sys.stderr)

        rule_files = scan_rule_files(args.rule_files)
        if not rule_files:
            print(f"  WARN: No rule files found matching: {args.rule_files}",
                  file=sys.stderr)
        else:
            metric_dict = load_metric_dictionary() if HAS_MIGRATE else {}
            candidates, recording_rules, summary = analyze_rule_files(
                rule_files, args.tenant_label, metric_dict)
            phase2_results = (candidates, recording_rules, summary)

            print(f"  Scanned {summary['files_scanned']} file(s), "
                  f"{summary['total_rules']} rule(s) in {summary['total_groups']} group(s)")
            print(f"  Alert rules: {summary['alert_rules']} "
                  f"(parseable: {summary['parseable']}, unparseable: {summary['unparseable']})")
            print(f"  Recording rules: {summary['recording_rules']}")
            for e in summary["errors"]:
                print(f"  ERROR: {e}", file=sys.stderr)

    # Phase 3: Scrape config
    if args.scrape_config:
        print(f"Phase 3: Analyzing scrape config: {args.scrape_config}")
        scrape_configs = parse_scrape_configs(args.scrape_config)
        if not scrape_configs:
            print(f"  WARN: No scrape_configs found in: {args.scrape_config}",
                  file=sys.stderr)
        else:
            job_analyses, summary = analyze_scrape_configs(
                scrape_configs, args.tenant_label)
            phase3_results = (job_analyses, summary)

            print(f"  Found {summary['total_jobs']} job(s): "
                  f"{summary['with_tenant_mapping']} with tenant mapping, "
                  f"{summary['without_tenant_mapping']} without")

    # Generate outputs
    report = write_outputs(
        args.output_dir,
        phase1_results=phase1_results,
        phase2_results=phase2_results,
        phase3_results=phase3_results,
        dry_run=args.dry_run,
        json_output=args.json,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    elif args.dry_run:
        print("\n--- DRY RUN SUMMARY ---")
        for phase, info in report["phases"].items():
            print(f"\n{phase}:")
            for k, v in info.items():
                if k != "summary":
                    print(f"  {k}: {v}")
    else:
        print(f"\nOutputs written to: {args.output_dir}")
        for f in report["files_written"]:
            print(f"  {f}")

    # Exit code: 0 if at least one phase produced results
    has_results = any(report["phases"].values())
    sys.exit(0 if has_results else 1)


if __name__ == "__main__":
    main()
