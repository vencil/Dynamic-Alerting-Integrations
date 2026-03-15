#!/usr/bin/env python3
"""
generate_alertmanager_routes.py — Generate Alertmanager route + receiver + inhibit config from tenant YAML.

Reads all tenant YAML files from conf.d/, extracts _routing and _severity_dedup sections,
and produces an Alertmanager route tree + receivers + inhibit_rules YAML fragment.

Severity Dedup (per-tenant):
  Default (absent or "enable"): generate inhibit_rule that suppresses warning when critical fires
  "disable": skip inhibit_rule — both warning and critical notifications are sent
  Mechanism: per-tenant inhibit_rules with tenant="<name>" + metric_group matchers

v2.0.0 Bilingual Templates (i18n):
  Rule Packs can include Chinese annotations: summary_zh, description_zh, platform_summary_zh
  Alertmanager templates use fallback logic to prefer Chinese if available:
    Example: {{ or .CommonAnnotations.summary_zh .CommonAnnotations.summary }}
  Receiver templates (email, webhook, slack, teams, pagerduty) use this pattern automatically.
  No changes to route generator needed — the fallback pattern is in Alertmanager's global templates.

Usage:
  python3 scripts/tools/generate_alertmanager_routes.py --config-dir conf.d/
  python3 scripts/tools/generate_alertmanager_routes.py --config-dir conf.d/ -o alertmanager-routes.yaml
  python3 scripts/tools/generate_alertmanager_routes.py --config-dir conf.d/ --dry-run
  python3 scripts/tools/generate_alertmanager_routes.py --config-dir conf.d/ --output-configmap -o am-configmap.yaml
"""
import argparse
import fnmatch
import json
import os
import subprocess
import sys
import textwrap
from urllib.parse import urlparse

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    is_disabled as _is_disabled,
    parse_duration_seconds,
    format_duration,
    validate_and_clamp,
    GUARDRAILS,
    PLATFORM_DEFAULTS,
    RECEIVER_TYPES,
    RECEIVER_URL_FIELDS,
    VALID_RESERVED_KEYS,
    VALID_RESERVED_PREFIXES,
)


def _extract_host(value):
    """Extract hostname from a URL or host:port string.

    Returns hostname (lowercase) or None if unparseable.
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    # host:port format (e.g., smtp.example.com:587)
    if "://" not in value:
        return value.split(":")[0].lower() or None
    parsed = urlparse(value)
    return parsed.hostname


def validate_receiver_domains(receiver_obj, tenant, allowed_domains):
    """Validate receiver URL fields against a domain allowlist.

    Args:
        receiver_obj: dict with 'type' and type-specific fields.
        tenant: tenant name for messages.
        allowed_domains: list of allowed domain patterns (fnmatch).

    Returns:
        list of warning strings (empty if all valid).
    """
    warnings = []
    if not allowed_domains or not isinstance(receiver_obj, dict):
        return warnings

    rtype = receiver_obj.get("type", "")
    if isinstance(rtype, str):
        rtype = rtype.strip().lower()

    url_fields = RECEIVER_URL_FIELDS.get(rtype, [])
    for field in url_fields:
        raw = receiver_obj.get(field)
        if not raw:
            continue
        host = _extract_host(raw)
        if not host:
            warnings.append(
                f"  WARN: {tenant}: cannot parse host from receiver "
                f"{field}='{raw}', skipping domain check")
            continue
        if not any(fnmatch.fnmatch(host, pat) for pat in allowed_domains):
            warnings.append(
                f"  WARN: {tenant}: receiver {field} host '{host}' "
                f"not in allowed_domains, skipping")
    return warnings


def load_policy(policy_path):
    """Load policy YAML and return allowed_domains list (may be empty)."""
    if not policy_path or not os.path.isfile(policy_path):
        return []
    with open(policy_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    domains = data.get("allowed_domains", [])
    if not isinstance(domains, list):
        return []
    return [d for d in domains if isinstance(d, str)]


def _apply_timing_params(source_dict, context_name):
    """Apply timing parameters with guardrails to a route dict.

    Reads group_wait, group_interval, repeat_interval from source_dict,
    validates each against GUARDRAILS, and returns applied values + warnings.

    Returns:
        (timing_dict, warnings_list) — timing_dict has clamped param values.
    """
    timing = {}
    warnings = []
    for param in ("group_wait", "group_interval", "repeat_interval"):
        val = source_dict.get(param)
        if val:
            clamped, param_warnings = validate_and_clamp(param, str(val), context_name)
            warnings.extend(param_warnings)
            if clamped:
                timing[param] = clamped
    return timing, warnings


def _substitute_tenant(obj, tenant_name):
    """Replace {{tenant}} placeholders in all string values recursively."""
    if isinstance(obj, str):
        return obj.replace("{{tenant}}", tenant_name)
    if isinstance(obj, dict):
        return {k: _substitute_tenant(v, tenant_name) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_tenant(item, tenant_name) for item in obj]
    return obj


def _contains_tenant_placeholder(obj):
    """Check if any string value contains {{tenant}} placeholder."""
    if isinstance(obj, str):
        return "{{tenant}}" in obj
    if isinstance(obj, dict):
        return any(_contains_tenant_placeholder(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_tenant_placeholder(item) for item in obj)
    return False


def merge_routing_with_defaults(defaults, tenant_routing, tenant_name):
    """Merge _routing_defaults with tenant _routing.

    Rules:
    - Tenant values override defaults (shallow merge)
    - {{tenant}} in string values is replaced with tenant_name
    - Lists (e.g., group_by) are replaced, not concatenated
    """
    merged = dict(defaults)
    if isinstance(tenant_routing, dict):
        for key, value in tenant_routing.items():
            merged[key] = value
    return _substitute_tenant(merged, tenant_name)


def validate_tenant_keys(tenant, keys, defaults_keys):
    """Check tenant config keys for typos / unknown reserved keys.

    Returns list of warning strings.
    """
    warnings = []
    for key in keys:
        if key in VALID_RESERVED_KEYS:
            continue
        if any(key.startswith(p) for p in VALID_RESERVED_PREFIXES):
            continue
        if key in defaults_keys:
            continue
        # _critical suffix → check base
        if key.endswith("_critical"):
            base = key.removesuffix("_critical")
            if base in defaults_keys:
                continue
        # Dimensional key with {labels}
        if "{" in key:
            base = key.split("{")[0]
            if base in defaults_keys:
                continue
        # Unknown key
        if key.startswith("_"):
            warnings.append(f"  WARN: {tenant}: unknown reserved key '{key}' (typo?)")
        else:
            warnings.append(f"  WARN: {tenant}: unknown key '{key}' not in defaults")
    return warnings


def _parse_config_files(config_dir):
    """Parse all YAML files in config_dir and extract raw data.

    Returns a dict with keys:
        all_tenants, defaults_keys, routing_defaults, enforced_routing,
        explicit_routing, disabled_tenants, dedup_configs, metadata_configs,
        tenant_keys.
    """
    result = {
        "all_tenants": [],
        "defaults_keys": set(),
        "routing_defaults": {},
        "enforced_routing": None,
        "explicit_routing": {},
        "disabled_tenants": set(),
        "dedup_configs": {},
        "metadata_configs": {},
        "tenant_keys": {},
    }

    if not os.path.isdir(config_dir):
        print(f"ERROR: config directory not found: {config_dir}", file=sys.stderr)
        sys.exit(1)

    files = sorted(f for f in os.listdir(config_dir)
                   if (f.endswith(".yaml") or f.endswith(".yml"))
                   and not f.startswith("."))

    for fname in files:
        path = os.path.join(config_dir, fname)
        with open(path, encoding="utf-8") as f:
            try:
                data = yaml.safe_load(f)
            except yaml.YAMLError as e:
                print(f"  WARN: skip unparseable {fname}: {e}", file=sys.stderr)
                continue

        if not data:
            continue

        # Collect defaults keys for schema validation
        if isinstance(data.get("defaults"), dict):
            result["defaults_keys"].update(data["defaults"].keys())

        # Extract _routing_defaults (only from _ prefixed files)
        is_defaults_file = os.path.basename(fname).startswith("_")
        if "_routing_defaults" in data:
            if is_defaults_file:
                result["routing_defaults"] = data["_routing_defaults"]
            else:
                print(f"  WARN: _routing_defaults in {fname} ignored "
                      "(only allowed in _ prefixed files)", file=sys.stderr)

        # v1.7.0: Extract _routing_enforced (only from _ prefixed files)
        if "_routing_enforced" in data:
            if is_defaults_file:
                raw = data["_routing_enforced"]
                if isinstance(raw, dict) and raw.get("enabled", False):
                    result["enforced_routing"] = raw
                elif isinstance(raw, dict) and not raw.get("enabled", False):
                    pass  # explicitly disabled → None
                else:
                    print(f"  WARN: _routing_enforced in {fname} must be a dict "
                          "with 'enabled: true', ignoring", file=sys.stderr)
            else:
                print(f"  WARN: _routing_enforced in {fname} ignored "
                      "(only allowed in _ prefixed files)", file=sys.stderr)

        if "tenants" not in data:
            continue

        for tenant, overrides in data.get("tenants", {}).items():
            if not isinstance(overrides, dict):
                continue

            result["all_tenants"].append(tenant)

            # Collect tenant keys for schema validation
            if tenant not in result["tenant_keys"]:
                result["tenant_keys"][tenant] = set()
            result["tenant_keys"][tenant].update(overrides.keys())

            # Severity dedup: default "enable", explicit "disable" to opt out
            raw_dedup = overrides.get("_severity_dedup", "enable")
            dedup_val = str(raw_dedup).strip().lower()
            if _is_disabled(dedup_val):
                result["dedup_configs"][tenant] = "disable"
            else:
                result["dedup_configs"][tenant] = "enable"

            # v1.11.0: Metadata extraction
            metadata = overrides.get("_metadata")
            if metadata and isinstance(metadata, dict):
                result["metadata_configs"][tenant] = _substitute_tenant(metadata, tenant)

            # Routing: "disable" string → skip routing
            routing = overrides.get("_routing")
            if isinstance(routing, str) and _is_disabled(routing):
                result["disabled_tenants"].add(tenant)
                continue

            if routing and isinstance(routing, dict):
                result["explicit_routing"][tenant] = routing

    return result


def _merge_tenant_routing(parsed, routing_defaults):
    """Merge routing defaults with explicit tenant routing configs.

    Returns routing_configs dict {tenant: merged_routing}.
    """
    routing_configs = {}
    seen_tenants = set()
    for tenant in sorted(set(parsed["all_tenants"])):
        if tenant in parsed["disabled_tenants"] or tenant in seen_tenants:
            continue
        seen_tenants.add(tenant)

        if tenant in parsed["explicit_routing"]:
            routing_configs[tenant] = merge_routing_with_defaults(
                routing_defaults, parsed["explicit_routing"][tenant], tenant)
        elif routing_defaults:
            routing_configs[tenant] = merge_routing_with_defaults(
                routing_defaults, {}, tenant)

    return routing_configs


def load_tenant_configs(config_dir):
    """Load all tenant YAML files from a config directory.

    Returns tuple of:
      - routing_configs: {tenant_name: routing_config} for tenants that have _routing
      - dedup_configs: {tenant_name: "enable"|"disable"} for ALL tenants (default: "enable")
      - schema_warnings: list of validation warning strings
      - enforced_routing: dict or None — platform enforced routing config (v1.7.0)
      - metadata_configs: {tenant_name: {runbook_url, owner, tier, ...}} (v1.11.0)

    Delegates to _parse_config_files() for YAML parsing and
    _merge_tenant_routing() for defaults merging.
    """
    parsed = _parse_config_files(config_dir)

    routing_configs = _merge_tenant_routing(
        parsed, parsed["routing_defaults"])

    # Schema validation: check tenant keys against defaults
    schema_warnings = []
    for tenant, keys in sorted(parsed["tenant_keys"].items()):
        schema_warnings.extend(
            validate_tenant_keys(tenant, keys, parsed["defaults_keys"]))

    return (routing_configs, parsed["dedup_configs"], schema_warnings,
            parsed["enforced_routing"], parsed["metadata_configs"])


def build_receiver_config(receiver_obj, tenant):
    """Build Alertmanager receiver config from structured receiver object.

    Args:
        receiver_obj: dict with 'type' and type-specific fields.
        tenant: tenant name for error messages.

    Returns:
        (am_config_dict, warnings) where am_config_dict is e.g.
        {"webhook_configs": [{"url": "..."}]} or None on error.
    """
    warnings = []

    if not isinstance(receiver_obj, dict):
        warnings.append(f"  WARN: {tenant}: 'receiver' must be an object with 'type', skipping")
        return None, warnings

    rtype = receiver_obj.get("type")
    if not rtype or not isinstance(rtype, str):
        warnings.append(f"  WARN: {tenant}: missing required 'receiver.type', skipping")
        return None, warnings

    rtype = rtype.strip().lower()
    if rtype not in RECEIVER_TYPES:
        supported = ", ".join(sorted(RECEIVER_TYPES.keys()))
        warnings.append(f"  WARN: {tenant}: unknown receiver type '{rtype}' "
                        f"(supported: {supported}), skipping")
        return None, warnings

    spec = RECEIVER_TYPES[rtype]

    # Validate required fields
    for field in spec["required"]:
        if field not in receiver_obj or not receiver_obj[field]:
            warnings.append(f"  WARN: {tenant}: receiver type '{rtype}' requires "
                            f"'{field}', skipping")
            return None, warnings

    # Build AM config — include required + present optional fields
    am_entry = {}
    for field in spec["required"] + spec["optional"]:
        if field in receiver_obj:
            am_entry[field] = receiver_obj[field]

    return {spec["am_key"]: [am_entry]}, warnings


def expand_routing_overrides(tenant, routing_config, allowed_domains=None):
    """Expand per-rule routing overrides into sub-routes.

    v1.8.0: Supports per-alertname or per-metric_group receiver overrides.
    Each override generates a sub-route that matches before the main tenant route.

    Args:
        tenant: tenant name for error messages and matchers.
        routing_config: dict containing optional 'overrides' list.
        allowed_domains: domain allowlist for webhook validation (may be None).

    Returns:
        (sub_routes, override_receivers, warnings) where:
        - sub_routes: list of Alertmanager route dicts (prepended before main tenant route)
        - override_receivers: list of receiver dicts to append to receivers list
        - warnings: list of warning/error strings
    """
    sub_routes = []
    override_receivers = []
    warnings = []

    overrides = routing_config.get("overrides", [])
    if not overrides:
        return sub_routes, override_receivers, warnings

    if not isinstance(overrides, list):
        warnings.append(f"  WARN: {tenant}: 'overrides' must be a list, skipping")
        return sub_routes, override_receivers, warnings

    for idx, override in enumerate(overrides):
        if not isinstance(override, dict):
            warnings.append(f"  WARN: {tenant}: override[{idx}] must be a dict, skipping")
            continue

        # Validate exactly one of alertname or metric_group is set
        has_alertname = "alertname" in override and override["alertname"]
        has_metric_group = "metric_group" in override and override["metric_group"]

        if not has_alertname and not has_metric_group:
            warnings.append(
                f"  WARN: {tenant}: override[{idx}] must have either "
                "'alertname' or 'metric_group', skipping")
            continue

        if has_alertname and has_metric_group:
            warnings.append(
                f"  WARN: {tenant}: override[{idx}] has both 'alertname' and "
                "'metric_group' (exactly one required), skipping")
            continue

        # Build matcher based on override type
        if has_alertname:
            alertname = override["alertname"]
            matchers = [
                f'tenant="{tenant}"',
                f'alertname="{alertname}"',
            ]
        else:  # has_metric_group
            metric_group = override["metric_group"]
            matchers = [
                f'tenant="{tenant}"',
                f'metric_group="{metric_group}"',
            ]

        # Validate and build receiver config
        receiver_obj = override.get("receiver")
        if not receiver_obj:
            warnings.append(
                f"  WARN: {tenant}: override[{idx}] missing 'receiver', skipping")
            continue

        am_config, recv_warnings = build_receiver_config(receiver_obj, f"{tenant}-override-{idx}")
        warnings.extend(recv_warnings)
        if am_config is None:
            continue

        # Domain allowlist check (SSRF prevention)
        if allowed_domains:
            domain_warnings = validate_receiver_domains(
                receiver_obj, f"{tenant}-override-{idx}", allowed_domains)
            warnings.extend(domain_warnings)
            if any("not in allowed_domains" in w for w in domain_warnings):
                continue

        # Receiver name for this override
        receiver_name = f"tenant-{tenant}-override-{idx}"

        # Build sub-route with specific matchers
        sub_route = {
            "matchers": matchers,
            "receiver": receiver_name,
        }

        # Optional: group_by from override
        group_by = override.get("group_by")
        if group_by and isinstance(group_by, list):
            sub_route["group_by"] = group_by

        # Optional: timing parameters with guardrails
        timing, timing_warnings = _apply_timing_params(override, f"{tenant}-override-{idx}")
        warnings.extend(timing_warnings)
        sub_route.update(timing)

        sub_routes.append(sub_route)

        # Build receiver entry
        receiver = {"name": receiver_name}
        receiver.update(am_config)
        override_receivers.append(receiver)

    return sub_routes, override_receivers, warnings


def _build_enforced_routes(enforced_routing, routing_configs, allowed_domains=None):
    """產生 Platform Enforced Routing 的 routes 和 receivers。

    v1.7.0: 單一平台 enforced route（``continue: true``）確保 NOC 永遠收到通知。
    v1.10.0: 當 enforced_routing 含 ``{{tenant}}`` 佔位符時，為每個 tenant
    展開獨立的 enforced route。

    Args:
        enforced_routing: ``_routing_enforced`` 設定 dict。
        routing_configs: tenant routing configs（用於 ``{{tenant}}`` 展開）。
        allowed_domains: Webhook domain allowlist（SSRF 防護）。

    Returns:
        ``(routes, receivers, warnings)`` 三元組。
    """
    routes = []
    receivers = []
    warnings = []

    if not enforced_routing or not isinstance(enforced_routing, dict):
        return routes, receivers, warnings

    enforced_receiver = enforced_routing.get("receiver")
    if not enforced_receiver:
        warnings.append("  WARN: _routing_enforced: missing 'receiver', skipping enforced route")
        return routes, receivers, warnings

    if _contains_tenant_placeholder(enforced_routing):
        # Per-tenant enforced routes: 為每個 tenant 展開 {{tenant}} 佔位符
        for tenant in sorted(routing_configs.keys()):
            substituted = _substitute_tenant(enforced_routing, tenant)
            sub_receiver = substituted.get("receiver")
            am_config, recv_warnings = build_receiver_config(
                sub_receiver, f"platform-enforced-{tenant}")
            warnings.extend(recv_warnings)
            if am_config is None:
                continue

            if allowed_domains:
                domain_warnings = validate_receiver_domains(
                    sub_receiver, f"platform-enforced-{tenant}", allowed_domains)
                warnings.extend(domain_warnings)
                if any("not in allowed_domains" in w for w in domain_warnings):
                    continue

            receiver_name = f"platform-enforced-{tenant}"
            route = {
                "matchers": [f'tenant="{tenant}"'],
                "receiver": receiver_name,
                "continue": True,
            }

            # Optional extra matchers (also substituted)
            match = substituted.get("match")
            if match and isinstance(match, list):
                route["matchers"].extend(match)

            group_by = substituted.get("group_by")
            if group_by and isinstance(group_by, list):
                route["group_by"] = group_by

            timing, timing_warnings = _apply_timing_params(
                substituted, f"platform-enforced-{tenant}")
            warnings.extend(timing_warnings)
            route.update(timing)

            routes.append(route)
            receiver = {"name": receiver_name}
            receiver.update(am_config)
            receivers.append(receiver)
    else:
        # 單一平台 enforced route（無 {{tenant}} 佔位符）
        am_config, recv_warnings = build_receiver_config(enforced_receiver, "platform-enforced")
        warnings.extend(recv_warnings)
        if am_config is not None:
            # Domain allowlist check for platform receiver too
            if allowed_domains:
                domain_warnings = validate_receiver_domains(
                    enforced_receiver, "platform-enforced", allowed_domains)
                warnings.extend(domain_warnings)
                if any("not in allowed_domains" in w for w in domain_warnings):
                    warnings.append("  WARN: _routing_enforced: receiver blocked by domain policy")
                    am_config = None

        if am_config is not None:
            receiver_name = "platform-enforced"
            route = {
                "receiver": receiver_name,
                "continue": True,  # critical: alert 繼續比對 tenant routes
            }

            # Optional matchers — 若指定，僅符合的 alert 傳送至 NOC
            match = enforced_routing.get("match")
            if match and isinstance(match, list):
                route["matchers"] = match

            # Optional group_by
            group_by = enforced_routing.get("group_by")
            if group_by and isinstance(group_by, list):
                route["group_by"] = group_by

            # Timing parameters with guardrails
            timing, timing_warnings = _apply_timing_params(
                enforced_routing, "platform-enforced")
            warnings.extend(timing_warnings)
            route.update(timing)

            routes.append(route)

            receiver = {"name": receiver_name}
            receiver.update(am_config)
            receivers.append(receiver)

    return routes, receivers, warnings


def _build_tenant_routes(routing_configs, allowed_domains=None):
    """產生各 tenant 的 routes 和 receivers（含 per-rule overrides）。

    Args:
        routing_configs: tenant routing configs dict。
        allowed_domains: Webhook domain allowlist（SSRF 防護）。

    Returns:
        ``(routes, receivers, warnings)`` 三元組。
    """
    routes = []
    receivers = []
    warnings = []

    for tenant in sorted(routing_configs.keys()):
        cfg = routing_configs[tenant]

        # 驗證 receiver（必要欄位，須為含 type 的 dict）
        receiver_obj = cfg.get("receiver")
        if not receiver_obj:
            warnings.append(f"  WARN: {tenant}: missing required 'receiver', skipping")
            continue

        # 從結構化物件建立 receiver config
        am_config, recv_warnings = build_receiver_config(receiver_obj, tenant)
        warnings.extend(recv_warnings)
        if am_config is None:
            continue

        # Domain allowlist 檢查（SSRF 防護）
        if allowed_domains:
            domain_warnings = validate_receiver_domains(
                receiver_obj, tenant, allowed_domains)
            warnings.extend(domain_warnings)
            if any("not in allowed_domains" in w for w in domain_warnings):
                continue

        # v1.8.0: 展開 per-rule routing overrides（插入在 tenant 主 route 之前）
        override_sub_routes, override_receivers, override_warnings = \
            expand_routing_overrides(tenant, cfg, allowed_domains=allowed_domains)
        warnings.extend(override_warnings)
        routes.extend(override_sub_routes)
        receivers.extend(override_receivers)

        # Receiver name 由 tenant 推導
        receiver_name = f"tenant-{tenant}"

        # 建立 route 項目
        route = {
            "matchers": [f'tenant="{tenant}"'],
            "receiver": receiver_name,
        }

        # group_by（可選）
        group_by = cfg.get("group_by")
        if group_by and isinstance(group_by, list):
            route["group_by"] = group_by

        # Timing parameters with guardrails
        timing, timing_warnings = _apply_timing_params(cfg, tenant)
        warnings.extend(timing_warnings)
        route.update(timing)

        routes.append(route)

        # 建立 receiver 項目
        receiver = {"name": receiver_name}
        receiver.update(am_config)
        receivers.append(receiver)

    return routes, receivers, warnings


def generate_routes(routing_configs, allowed_domains=None, enforced_routing=None):
    """Generate Alertmanager route tree + receivers from routing configs.

    委派至 :func:`_build_enforced_routes` 和 :func:`_build_tenant_routes`，
    依序產生 enforced routes（NOC 通知）和 tenant routes。

    v1.7.0: Platform Enforced Routing（``continue: true``）。
    v1.8.0: Per-rule routing overrides。
    v1.10.0: ``{{tenant}}`` 佔位符展開。

    Returns:
        ``(routes_yaml_dict, receivers_list, all_warnings)`` 三元組。
    """
    routes = []
    receivers = []
    all_warnings = []

    # Platform Enforced Routing — NOC 永遠收到通知
    enf_routes, enf_receivers, enf_warnings = _build_enforced_routes(
        enforced_routing, routing_configs, allowed_domains)
    routes.extend(enf_routes)
    receivers.extend(enf_receivers)
    all_warnings.extend(enf_warnings)

    # Tenant routes（在 enforced route 之後）
    t_routes, t_receivers, t_warnings = _build_tenant_routes(
        routing_configs, allowed_domains)
    routes.extend(t_routes)
    receivers.extend(t_receivers)
    all_warnings.extend(t_warnings)

    return routes, receivers, all_warnings


def generate_inhibit_rules(dedup_configs):
    """Generate per-tenant severity dedup inhibit rules.

    For each tenant with dedup enabled (default), generates an inhibit_rule:
      - source: critical + metric_group present + tenant="<name>"
      - target: warning + metric_group present + tenant="<name>"
      - equal: metric_group

    Tenants with _severity_dedup: "disable" are skipped — both warning
    and critical notifications are sent.

    Returns (inhibit_rules_list, all_warnings).
    """
    rules = []
    all_warnings = []

    for tenant in sorted(dedup_configs.keys()):
        mode = dedup_configs[tenant]
        if mode == "disable":
            all_warnings.append(f"  INFO: {tenant}: severity_dedup disabled, skipping inhibit rule")
            continue

        rule = {
            "source_matchers": [
                'severity="critical"',
                'metric_group=~".+"',
                f'tenant="{tenant}"',
            ],
            "target_matchers": [
                'severity="warning"',
                'metric_group=~".+"',
                f'tenant="{tenant}"',
            ],
            "equal": ["metric_group"],
        }
        rules.append(rule)

    return rules, all_warnings


def render_output(routes, receivers, inhibit_rules=None):
    """Render the final YAML fragment."""
    # Build the fragment as a clean dict
    fragment = {}

    if routes:
        fragment["route"] = {
            "routes": routes,
        }

    if receivers:
        fragment["receivers"] = receivers

    if inhibit_rules:
        fragment["inhibit_rules"] = inhibit_rules

    return yaml.dump(fragment, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ── §11.3 AM GitOps: --output-configmap ────────────────────────────

# Minimal inline defaults when --base-config is not provided
_DEFAULT_BASE_CONFIG = {
    "global": {"resolve_timeout": "5m"},
    "route": {
        "group_by": ["alertname", "tenant"],
        "group_wait": "10s",
        "group_interval": "10s",
        "repeat_interval": "12h",
        "receiver": "default",
    },
    "receivers": [{"name": "default"}],
    "inhibit_rules": [],
}


def load_base_config(path):
    """Load base Alertmanager config from YAML file.

    Returns dict with global, route, receivers, inhibit_rules.
    Falls back to _DEFAULT_BASE_CONFIG on any error.
    """
    if not path or not os.path.isfile(path):
        return dict(_DEFAULT_BASE_CONFIG)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    # Ensure required keys exist
    for key in ("global", "route", "receivers", "inhibit_rules"):
        if key not in data:
            data[key] = _DEFAULT_BASE_CONFIG[key]
    return data


def assemble_configmap(base, routes, receivers, inhibit_rules,
                       namespace="monitoring", configmap_name="alertmanager-config"):
    """Merge tenant fragments into base config and wrap as K8s ConfigMap YAML.

    Returns the complete ConfigMap YAML string.
    """
    merged = dict(base)

    # Merge routes into base route
    merged_route = dict(merged.get("route", {}))
    merged_route["routes"] = routes
    merged["route"] = merged_route

    # Merge receivers: keep base receivers, append tenant receivers
    base_names = {r["name"] for r in merged.get("receivers", [])}
    tenant_receivers = [r for r in receivers if r["name"] not in base_names]
    merged["receivers"] = list(merged.get("receivers", [])) + tenant_receivers

    # Merge inhibit_rules: keep base rules, append tenant rules
    merged["inhibit_rules"] = list(merged.get("inhibit_rules", [])) + list(inhibit_rules or [])

    # Render alertmanager.yml content
    am_yml = yaml.dump(merged, default_flow_style=False,
                       allow_unicode=True, sort_keys=False)

    # Wrap in ConfigMap structure
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": configmap_name,
            "namespace": namespace,
        },
        "data": {
            "alertmanager.yml": am_yml,
        },
    }
    return yaml.dump(configmap, default_flow_style=False,
                     allow_unicode=True, sort_keys=False)


def apply_to_configmap(routes, receivers, inhibit_rules, namespace, configmap_name):
    """Merge generated fragment into existing Alertmanager ConfigMap and reload.

    Steps:
    1. kubectl get cm → extract alertmanager.yml
    2. Merge routes, receivers, inhibit_rules into existing config
    3. kubectl apply updated ConfigMap
    4. curl POST /-/reload
    """
    # 1. Read existing ConfigMap
    result = subprocess.run(
        ["kubectl", "get", "configmap", configmap_name, "-n", namespace,
         "-o", "json"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"ERROR: Failed to read ConfigMap {configmap_name}: {result.stderr}",
              file=sys.stderr)
        return False

    cm = json.loads(result.stdout)
    existing_yml = cm.get("data", {}).get("alertmanager.yml", "")
    if not existing_yml:
        print("ERROR: ConfigMap has no 'alertmanager.yml' key", file=sys.stderr)
        return False

    existing = yaml.safe_load(existing_yml)

    # 2. Merge fragment into existing config
    if routes:
        if "route" not in existing:
            existing["route"] = {}
        existing["route"]["routes"] = routes

    if receivers:
        # Keep default receiver, replace tenant receivers
        existing_names = {r["name"] for r in receivers}
        kept = [r for r in existing.get("receivers", [])
                if r["name"] not in existing_names]
        existing["receivers"] = kept + receivers

    if inhibit_rules:
        # Keep non-generated inhibit rules (e.g., Silent Mode sentinel rules)
        # Generated rules have metric_group matcher; silent mode rules don't
        kept_rules = [r for r in existing.get("inhibit_rules", [])
                      if not any('metric_group' in m for m in r.get("source_matchers", []))]
        existing["inhibit_rules"] = kept_rules + inhibit_rules

    merged_yml = yaml.dump(existing, default_flow_style=False,
                           allow_unicode=True, sort_keys=False)

    # 3. Apply updated ConfigMap
    apply_result = subprocess.run(
        ["kubectl", "create", "configmap", configmap_name,
         f"--from-literal=alertmanager.yml={merged_yml}",
         "-n", namespace, "--dry-run=client", "-o", "yaml"],
        capture_output=True, text=True
    )
    if apply_result.returncode != 0:
        print(f"ERROR: Failed to generate ConfigMap: {apply_result.stderr}",
              file=sys.stderr)
        return False

    pipe_result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=apply_result.stdout, capture_output=True, text=True
    )
    if pipe_result.returncode != 0:
        print(f"ERROR: kubectl apply failed: {pipe_result.stderr}", file=sys.stderr)
        return False

    print(f"ConfigMap {namespace}/{configmap_name} updated")

    # 4. Reload Alertmanager
    svc_url = f"http://alertmanager.{namespace}.svc.cluster.local:9093"
    reload_result = subprocess.run(
        ["curl", "-sf", "-X", "POST", f"{svc_url}/-/reload"],
        capture_output=True, text=True
    )
    if reload_result.returncode != 0:
        print(f"WARN: Alertmanager reload failed (is --web.enable-lifecycle enabled?)",
              file=sys.stderr)
        print("ConfigMap was updated — Alertmanager will pick up changes on next restart")
        return True

    print("Alertmanager reloaded")
    return True


def main():
    """CLI entry point: Generate Alertmanager route + receiver + inhibit config from tenant YAML."""
    parser = argparse.ArgumentParser(
        description="Generate Alertmanager route + receiver config from tenant YAML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              %(prog)s --config-dir components/threshold-exporter/config/conf.d/
              %(prog)s --config-dir conf.d/ -o alertmanager-routes.yaml
              %(prog)s --config-dir conf.d/ --dry-run
        """),
    )
    parser.add_argument("--config-dir", required=True,
                        help="Directory containing tenant YAML configs (conf.d/)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output file path (default: stdout)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview output without writing file")
    parser.add_argument("--validate", action="store_true",
                        help="Validate generated config (exit 0 if valid, 1 if errors)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--apply", action="store_true",
                            help="Apply: merge into Alertmanager ConfigMap + reload")
    mode_group.add_argument("--output-configmap", action="store_true",
                            help="Output complete Alertmanager ConfigMap YAML (for GitOps PR flow)")
    parser.add_argument("--base-config", default=None,
                        help="Base Alertmanager YAML for --output-configmap (global + defaults)")
    parser.add_argument("--namespace", default="monitoring",
                        help="K8s namespace for --apply/--output-configmap (default: monitoring)")
    parser.add_argument("--configmap", default="alertmanager-config",
                        help="ConfigMap name for --apply/--output-configmap (default: alertmanager-config)")
    parser.add_argument("--policy", default=None,
                        help="Policy YAML with allowed_domains for webhook URL validation")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt for --apply")

    args = parser.parse_args()

    # Load policy (webhook domain allowlist)
    allowed_domains = load_policy(args.policy)
    if allowed_domains:
        print(f"Policy: {len(allowed_domains)} allowed domain pattern(s) loaded")

    # Load tenant configs (routing + dedup + schema warnings + enforced routing + metadata)
    routing_configs, dedup_configs, schema_warnings, enforced_routing, metadata_configs = \
        load_tenant_configs(args.config_dir)

    has_routing = bool(routing_configs)
    has_dedup = bool(dedup_configs)

    if not has_routing and not has_dedup and not enforced_routing:
        print("No tenants found in config directory.")
        sys.exit(0)

    if enforced_routing:
        print("Platform enforced routing: ENABLED")
    if has_routing:
        print(f"Found {len(routing_configs)} tenant(s) with routing config: "
              f"{', '.join(sorted(routing_configs.keys()))}")
    print(f"Found {len(dedup_configs)} tenant(s) for severity dedup: "
          f"{', '.join(sorted(dedup_configs.keys()))}")

    # Generate routes + receivers (enforced route inserted first)
    routes, receivers, route_warnings = generate_routes(
        routing_configs, allowed_domains=allowed_domains,
        enforced_routing=enforced_routing)

    # Generate per-tenant severity dedup inhibit rules
    inhibit_rules, dedup_warnings = generate_inhibit_rules(dedup_configs)

    # Collect all warnings
    all_warnings = schema_warnings + route_warnings + dedup_warnings
    for w in all_warnings:
        print(w, file=sys.stderr)

    if not routes and not inhibit_rules:
        print("No valid routes or inhibit rules generated.")
        sys.exit(1)

    # Validate mode: check for errors and exit
    if args.validate:
        errors = [w for w in all_warnings if "WARN" in w and "skipping" in w]
        route_count = len(routes)
        inhibit_count = len(inhibit_rules)
        print(f"Validation: {route_count} route(s), {len(receivers)} receiver(s), "
              f"{inhibit_count} inhibit rule(s)")
        if errors:
            print(f"FAIL: {len(errors)} error(s) found:", file=sys.stderr)
            for e in errors:
                print(e, file=sys.stderr)
            sys.exit(1)
        print("OK: all configs valid")
        sys.exit(0)

    # Apply mode: merge into ConfigMap + reload
    if args.apply:
        route_count = len(routes)
        inhibit_count = len(inhibit_rules)
        print(f"\nApply: {route_count} route(s), {len(receivers)} receiver(s), "
              f"{inhibit_count} inhibit rule(s)")
        print(f"Target: {args.namespace}/{args.configmap}")
        if not args.yes:
            confirm = input("Proceed? [y/N] ").strip().lower()
            if confirm not in ("y", "yes"):
                print("Aborted.")
                sys.exit(0)
        success = apply_to_configmap(routes, receivers, inhibit_rules,
                                     args.namespace, args.configmap)
        sys.exit(0 if success else 1)

    # Output-configmap mode: produce complete ConfigMap YAML for GitOps
    if args.output_configmap:
        base = load_base_config(args.base_config)
        cm_yaml = assemble_configmap(
            base, routes, receivers, inhibit_rules,
            namespace=args.namespace, configmap_name=args.configmap)

        route_count = len(routes)
        inhibit_count = len(inhibit_rules)

        if args.dry_run:
            print("\n--- DRY RUN: ConfigMap YAML ---")
            print(cm_yaml)
            print(f"\n--- {route_count} route(s), {len(receivers)} receiver(s), "
                  f"{inhibit_count} inhibit rule(s) ---")
            return

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(cm_yaml)
            os.chmod(args.output, 0o600)
            print(f"Written to {args.output} ({route_count} routes, "
                  f"{len(receivers)} receivers, {inhibit_count} inhibit rules)")
        else:
            print(cm_yaml)
        return

    # Render output
    header = (
        "# ============================================================\n"
        "# Alertmanager Route + Receiver + Inhibit Rules Fragment\n"
        "# Generated by: generate_alertmanager_routes.py\n"
        "# Merge into your Alertmanager config:\n"
        "#   - route.routes: append the routes below\n"
        "#   - receivers: append the receivers below\n"
        "#   - inhibit_rules: append the severity dedup inhibit rules below\n"
        "# ============================================================\n"
    )
    body = render_output(routes, receivers, inhibit_rules)
    content = header + body

    route_count = len(routes)
    inhibit_count = len(inhibit_rules)

    if args.dry_run:
        print("\n--- DRY RUN OUTPUT ---")
        print(content)
        print(f"\n--- {route_count} route(s), {len(receivers)} receiver(s), "
              f"{inhibit_count} inhibit rule(s) ---")
        return

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(args.output, 0o600)
        print(f"Written to {args.output} ({route_count} routes, {len(receivers)} receivers, "
              f"{inhibit_count} inhibit rules)")
    else:
        print(content)


if __name__ == "__main__":
    main()
