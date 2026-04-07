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
from __future__ import annotations

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
    write_text_secure,
    GUARDRAILS,
    PLATFORM_DEFAULTS,
    RECEIVER_TYPES,
    RECEIVER_URL_FIELDS,
    VALID_RESERVED_KEYS,
    VALID_RESERVED_PREFIXES,
)


# ============================================================
# Helper Functions: URL/Domain Validation
# ============================================================

def _extract_host(value: str | None) -> str | None:
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


def validate_receiver_domains(receiver_obj: dict, tenant: str, allowed_domains: list[str]) -> list[str]:
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


def load_policy(policy_path: str | None) -> list[str]:
    """Load policy YAML and return allowed_domains list (may be empty)."""
    if not policy_path or not os.path.isfile(policy_path):
        return []
    with open(policy_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    domains = data.get("allowed_domains", [])
    if not isinstance(domains, list):
        return []
    return [d for d in domains if isinstance(d, str)]


# ============================================================
# Helper Functions: Routing Configuration Merging & Substitution
# ============================================================

def _apply_timing_params(source_dict: dict, context_name: str) -> tuple[dict, list[str]]:
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


# ============================================================
# Tenant Substitution & Placeholder Handling
# ============================================================

def _substitute_tenant(obj: object, tenant_name: str) -> object:
    """Replace {{tenant}} placeholders in all string values recursively."""
    if isinstance(obj, str):
        return obj.replace("{{tenant}}", tenant_name)
    if isinstance(obj, dict):
        return {k: _substitute_tenant(v, tenant_name) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_tenant(item, tenant_name) for item in obj]
    return obj


def _contains_tenant_placeholder(obj: object) -> bool:
    """Check if any string value contains {{tenant}} placeholder."""
    if isinstance(obj, str):
        return "{{tenant}}" in obj
    if isinstance(obj, dict):
        return any(_contains_tenant_placeholder(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_tenant_placeholder(item) for item in obj)
    return False


def merge_routing_with_defaults(defaults: dict, tenant_routing: dict | None, tenant_name: str) -> dict:
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


def validate_tenant_keys(tenant: str, keys: set[str], defaults_keys: set[str]) -> list[str]:
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


# ============================================================
# Configuration Loading & Parsing
# ============================================================

def _parse_platform_config(data: dict, fname: str, result: dict) -> None:
    """Extract platform-level config from a _ prefixed YAML file.

    Handles: defaults keys, _routing_defaults, _routing_enforced,
    routing_profiles (ADR-007), domain_policies (ADR-007).
    Mutates *result* in place.
    """
    is_defaults_file = os.path.basename(fname).startswith("_")

    # Collect defaults keys for schema validation
    if isinstance(data.get("defaults"), dict):
        result["defaults_keys"].update(data["defaults"].keys())

    # Extract _routing_defaults (only from _ prefixed files)
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

    # v2.1.0 ADR-007: Extract routing_profiles (only from _routing_profiles.yaml)
    if "routing_profiles" in data:
        rp_fname = os.path.basename(fname)
        if rp_fname in ("_routing_profiles.yaml", "_routing_profiles.yml"):
            profiles = data["routing_profiles"]
            if isinstance(profiles, dict):
                result["routing_profiles"].update(profiles)
            else:
                print(f"  WARN: routing_profiles in {fname} must be a dict, ignoring",
                      file=sys.stderr)
        else:
            print(f"  WARN: routing_profiles in {fname} ignored "
                  "(only allowed in _routing_profiles.yaml)", file=sys.stderr)

    # v2.1.0 ADR-007: Extract domain_policies (only from _domain_policy.yaml)
    if "domain_policies" in data:
        dp_fname = os.path.basename(fname)
        if dp_fname in ("_domain_policy.yaml", "_domain_policy.yml"):
            policies = data["domain_policies"]
            if isinstance(policies, dict):
                result["domain_policies"].update(policies)
            else:
                print(f"  WARN: domain_policies in {fname} must be a dict, ignoring",
                      file=sys.stderr)
        else:
            print(f"  WARN: domain_policies in {fname} ignored "
                  "(only allowed in _domain_policy.yaml)", file=sys.stderr)


def _parse_tenant_overrides(tenant: str, overrides: dict, result: dict) -> None:
    """Extract per-tenant config from a tenant overrides dict.

    Handles: tenant keys, _routing_profile (ADR-007), _severity_dedup,
    _metadata, _routing (explicit or disabled).
    Mutates *result* in place.
    """
    result["all_tenants"].append(tenant)

    # Collect tenant keys for schema validation
    if tenant not in result["tenant_keys"]:
        result["tenant_keys"][tenant] = set()
    result["tenant_keys"][tenant].update(overrides.keys())

    # v2.1.0 ADR-007: Extract _routing_profile reference
    rp_ref = overrides.get("_routing_profile")
    if rp_ref and isinstance(rp_ref, str):
        result["tenant_profile_refs"][tenant] = rp_ref.strip()

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
        return

    if routing and isinstance(routing, dict):
        result["explicit_routing"][tenant] = routing


def _parse_config_files(config_dir: str) -> dict:
    """Parse all YAML files in config_dir and extract raw data.

    Returns a dict with keys:
        all_tenants, defaults_keys, routing_defaults, enforced_routing,
        explicit_routing, disabled_tenants, dedup_configs, metadata_configs,
        tenant_keys, routing_profiles, domain_policies, tenant_profile_refs.

    Delegates to:
        _parse_platform_config() — platform-level keys from _ prefixed files
        _parse_tenant_overrides() — per-tenant overrides from tenant sections
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
        "routing_profiles": {},      # v2.1.0 ADR-007: named routing configs
        "domain_policies": {},       # v2.1.0 ADR-007: domain compliance constraints
        "tenant_profile_refs": {},   # v2.1.0 ADR-007: tenant → profile name
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

        _parse_platform_config(data, fname, result)

        if "tenants" not in data:
            continue

        for tenant, overrides in data.get("tenants", {}).items():
            if not isinstance(overrides, dict):
                continue
            _parse_tenant_overrides(tenant, overrides, result)

    return result


def _merge_tenant_routing(parsed: dict, routing_defaults: dict) -> dict[str, dict]:
    """Merge routing defaults with explicit tenant routing configs.

    v2.1.0 ADR-007: Four-layer merge pipeline:
      1. _routing_defaults → global defaults
      2. routing_profiles[ref] → team/domain shared config (if _routing_profile set)
      3. tenant _routing → per-tenant overrides
      4. _routing_enforced → NOC immutable override (applied later in generate_routes)

    Returns routing_configs dict {tenant: merged_routing}.
    """
    profiles = parsed.get("routing_profiles", {})
    profile_refs = parsed.get("tenant_profile_refs", {})

    routing_configs = {}
    seen_tenants = set()
    for tenant in sorted(set(parsed["all_tenants"])):
        if tenant in parsed["disabled_tenants"] or tenant in seen_tenants:
            continue
        seen_tenants.add(tenant)

        # Layer 1: Start with routing defaults
        base = dict(routing_defaults) if routing_defaults else {}

        # Layer 2: Merge routing profile (if referenced)
        if tenant in profile_refs:
            profile_name = profile_refs[tenant]
            if profile_name in profiles:
                profile_cfg = profiles[profile_name]
                if isinstance(profile_cfg, dict):
                    for k, v in profile_cfg.items():
                        base[k] = v
            # Warning for unknown profile is emitted by _validate_profile_refs()

        # Layer 3: Merge explicit tenant routing overrides
        tenant_routing = parsed["explicit_routing"].get(tenant)

        # Only produce a routing config if there's something to route
        if tenant_routing or base:
            routing_configs[tenant] = merge_routing_with_defaults(
                base, tenant_routing, tenant)

    return routing_configs


def _validate_profile_refs(parsed: dict) -> list[str]:
    """Validate that _routing_profile references point to existing profiles.

    v2.1.0 ADR-007.
    Returns list of warning messages.
    """
    warnings: list[str] = []
    profiles = parsed.get("routing_profiles", {})
    refs = parsed.get("tenant_profile_refs", {})
    for tenant, profile_name in sorted(refs.items()):
        if profile_name not in profiles:
            warnings.append(
                f"  WARN: {tenant}: _routing_profile references unknown "
                f"profile '{profile_name}'")
    return warnings


def check_domain_policies(
    routing_configs: dict[str, dict],
    domain_policies: dict[str, dict],
    *,
    strict: bool = False,
) -> list[str]:
    """Validate resolved routing configs against domain policy constraints.

    v2.1.0 ADR-007.

    Args:
        routing_configs: {tenant: resolved_routing_config}
        domain_policies: {policy_name: {tenants, constraints, ...}}
        strict: if True, return ERROR instead of WARN for violations.

    Returns list of warning/error messages.
    """
    messages: list[str] = []
    severity = "ERROR" if strict else "WARN"

    for policy_name, policy in sorted(domain_policies.items()):
        if not isinstance(policy, dict):
            continue
        tenants = policy.get("tenants", [])
        if not isinstance(tenants, list):
            messages.append(f"  WARN: domain_policy '{policy_name}': "
                            "'tenants' must be a list")
            continue
        constraints = policy.get("constraints", {})
        if not isinstance(constraints, dict):
            continue

        forbidden_types = set(constraints.get("forbidden_receiver_types", []))
        allowed_types = set(constraints.get("allowed_receiver_types", []))
        max_repeat = constraints.get("max_repeat_interval")
        min_group_wait = constraints.get("min_group_wait")
        enforce_group_by = constraints.get("enforce_group_by")

        for tenant in tenants:
            if tenant not in routing_configs:
                continue
            rc = routing_configs[tenant]

            # Check receiver type constraints
            recv = rc.get("receiver", {})
            recv_type = recv.get("type", "") if isinstance(recv, dict) else ""
            if recv_type:
                if forbidden_types and recv_type in forbidden_types:
                    messages.append(
                        f"  {severity}: domain_policy '{policy_name}', "
                        f"tenant '{tenant}': receiver type '{recv_type}' "
                        f"is forbidden")
                if allowed_types and recv_type not in allowed_types:
                    messages.append(
                        f"  {severity}: domain_policy '{policy_name}', "
                        f"tenant '{tenant}': receiver type '{recv_type}' "
                        f"not in allowed types {sorted(allowed_types)}")

            # Check max_repeat_interval
            if max_repeat:
                tenant_repeat = rc.get("repeat_interval")
                if tenant_repeat:
                    max_sec = parse_duration_seconds(max_repeat)
                    tenant_sec = parse_duration_seconds(tenant_repeat)
                    if max_sec and tenant_sec and tenant_sec > max_sec:
                        messages.append(
                            f"  {severity}: domain_policy '{policy_name}', "
                            f"tenant '{tenant}': repeat_interval "
                            f"'{tenant_repeat}' exceeds max '{max_repeat}'")

            # Check min_group_wait
            if min_group_wait:
                tenant_gw = rc.get("group_wait")
                if tenant_gw:
                    min_sec = parse_duration_seconds(min_group_wait)
                    tenant_sec = parse_duration_seconds(tenant_gw)
                    if min_sec and tenant_sec and tenant_sec < min_sec:
                        messages.append(
                            f"  {severity}: domain_policy '{policy_name}', "
                            f"tenant '{tenant}': group_wait "
                            f"'{tenant_gw}' below minimum '{min_group_wait}'")

            # Check enforce_group_by
            if enforce_group_by and isinstance(enforce_group_by, list):
                tenant_gb = rc.get("group_by", [])
                if isinstance(tenant_gb, list):
                    missing = set(enforce_group_by) - set(tenant_gb)
                    if missing:
                        messages.append(
                            f"  {severity}: domain_policy '{policy_name}', "
                            f"tenant '{tenant}': group_by missing required "
                            f"labels: {sorted(missing)}")

    return messages


def load_tenant_configs(config_dir: str) -> tuple[dict[str, dict], dict[str, str], list[str], dict | None, dict[str, dict]]:
    """Load and parse all tenant YAML files from a config directory.

    Orchestrates the full configuration pipeline:
      1. Parse all .yaml/.yml files in config_dir (delegated to _parse_config_files())
      2. Merge _routing_defaults with tenant _routing overrides (delegated to _merge_tenant_routing())
      3. Validate tenant keys against defaults (checks for typos)
      4. Resolve routing profile references (ADR-007)
      5. Validate domain policies against resolved routes (ADR-007)

    Configuration Hierarchy (4 layers):
      - Layer 0: _routing_defaults from _ prefixed files
      - Layer 1: routing_profile reference (_routing_profile key)
      - Layer 2: profile config resolved from _routing_profiles.yaml
      - Layer 3: explicit tenant _routing overrides

    Returns:
        (routing_configs, dedup_configs, schema_warnings, enforced_routing, metadata_configs):
        - routing_configs: {tenant_name: routing_config_dict} for tenants with _routing
        - dedup_configs: {tenant_name: "enable"|"disable"} for ALL tenants (default: "enable")
        - schema_warnings: list of validation warning strings
        - enforced_routing: dict or None — platform enforced routing config (v1.7.0+)
        - metadata_configs: {tenant_name: {runbook_url, owner, tier, ...}} (v1.11.0+)

    Note:
        All tenants appear in dedup_configs even if they have no _routing config.
    """
    parsed = _parse_config_files(config_dir)

    routing_configs = _merge_tenant_routing(
        parsed, parsed["routing_defaults"])

    # Schema validation: check tenant keys against defaults
    schema_warnings = []
    for tenant, keys in sorted(parsed["tenant_keys"].items()):
        schema_warnings.extend(
            validate_tenant_keys(tenant, keys, parsed["defaults_keys"]))

    # v2.1.0 ADR-007: Validate profile references
    schema_warnings.extend(_validate_profile_refs(parsed))

    # v2.1.0 ADR-007: Validate domain policies against resolved routing
    if parsed["domain_policies"]:
        schema_warnings.extend(
            check_domain_policies(routing_configs, parsed["domain_policies"]))

    return (routing_configs, parsed["dedup_configs"], schema_warnings,
            parsed["enforced_routing"], parsed["metadata_configs"])


def build_receiver_config(receiver_obj: dict, tenant: str) -> tuple[dict | None, list[str]]:
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


# ============================================================
# Routing Override Processing (Tenant-level alert routing overrides)
# ============================================================

def _validate_override_matcher(override: dict, idx: int, tenant: str) -> tuple[bool, list[str], bool, bool]:
    """Validate override has exactly one of alertname or metric_group.

    Returns (is_valid, warnings, has_alertname, has_metric_group).
    """
    warnings = []
    has_alertname = "alertname" in override and override["alertname"]
    has_metric_group = "metric_group" in override and override["metric_group"]

    if not has_alertname and not has_metric_group:
        warnings.append(
            f"  WARN: {tenant}: override[{idx}] must have either "
            "'alertname' or 'metric_group', skipping")
        return False, warnings, has_alertname, has_metric_group

    if has_alertname and has_metric_group:
        warnings.append(
            f"  WARN: {tenant}: override[{idx}] has both 'alertname' and "
            "'metric_group' (exactly one required), skipping")
        return False, warnings, has_alertname, has_metric_group

    return True, warnings, has_alertname, has_metric_group


def _build_override_matchers(override: dict, tenant: str, has_alertname: bool) -> list[str]:
    """Build matcher list based on override type (alertname or metric_group)."""
    if has_alertname:
        alertname = override["alertname"]
        return [f'tenant="{tenant}"', f'alertname="{alertname}"']
    else:
        metric_group = override["metric_group"]
        return [f'tenant="{tenant}"', f'metric_group="{metric_group}"']


def _process_override_receiver(override: dict, idx: int, tenant: str,
                               allowed_domains: list[str] | None) -> tuple[dict | None, list[str]]:
    """Process receiver config for a single override.

    Returns (am_config, warnings) — am_config is None if invalid.
    """
    warnings = []
    receiver_obj = override.get("receiver")
    if not receiver_obj:
        warnings.append(f"  WARN: {tenant}: override[{idx}] missing 'receiver', skipping")
        return None, warnings

    am_config, recv_warnings = build_receiver_config(receiver_obj, f"{tenant}-override-{idx}")
    warnings.extend(recv_warnings)
    if am_config is None:
        return None, warnings

    # Domain allowlist check (SSRF prevention)
    if allowed_domains:
        domain_warnings = validate_receiver_domains(
            receiver_obj, f"{tenant}-override-{idx}", allowed_domains)
        warnings.extend(domain_warnings)
        if any("not in allowed_domains" in w for w in domain_warnings):
            return None, warnings

    return am_config, warnings


def _build_override_route(idx: int, tenant: str, matchers: list[str],
                         override: dict) -> tuple[dict, list[str]]:
    """Build a sub-route dict for an override with timing parameters.

    Returns (route_dict, timing_warnings).
    """
    warnings = []
    sub_route = {
        "matchers": matchers,
        "receiver": f"tenant-{tenant}-override-{idx}",
    }

    # Optional: group_by from override
    group_by = override.get("group_by")
    if group_by and isinstance(group_by, list):
        sub_route["group_by"] = group_by

    # Optional: timing parameters with guardrails
    timing, timing_warnings = _apply_timing_params(override, f"{tenant}-override-{idx}")
    warnings.extend(timing_warnings)
    sub_route.update(timing)

    return sub_route, warnings


def expand_routing_overrides(tenant: str, routing_config: dict, allowed_domains: list[str] | None = None) -> tuple[list[dict], list[dict], list[str]]:
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
        is_valid, val_warnings, has_alertname, has_metric_group = _validate_override_matcher(
            override, idx, tenant)
        warnings.extend(val_warnings)
        if not is_valid:
            continue

        # Build matcher list
        matchers = _build_override_matchers(override, tenant, has_alertname)

        # Process receiver config
        am_config, recv_warnings = _process_override_receiver(override, idx, tenant, allowed_domains)
        warnings.extend(recv_warnings)
        if am_config is None:
            continue

        # Build sub-route with timing parameters
        receiver_name = f"tenant-{tenant}-override-{idx}"
        sub_route, timing_warnings = _build_override_route(idx, tenant, matchers, override)
        warnings.extend(timing_warnings)

        sub_routes.append(sub_route)

        # Build receiver entry
        receiver = {"name": receiver_name}
        receiver.update(am_config)
        override_receivers.append(receiver)

    return sub_routes, override_receivers, warnings


# ============================================================
# Platform Enforced Routing (NOC/SRE always-on notifications)
# ============================================================

def _build_per_tenant_enforced_route(tenant: str, enforced_routing: dict,
                                      allowed_domains: list[str] | None) -> tuple[dict | None, dict | None, list[str]]:
    """Build a single per-tenant enforced route with receiver.

    Returns (route_dict, receiver_dict, warnings) — both None if invalid.
    """
    warnings = []
    substituted = _substitute_tenant(enforced_routing, tenant)
    sub_receiver = substituted.get("receiver")
    am_config, recv_warnings = build_receiver_config(
        sub_receiver, f"platform-enforced-{tenant}")
    warnings.extend(recv_warnings)
    if am_config is None:
        return None, None, warnings

    if allowed_domains:
        domain_warnings = validate_receiver_domains(
            sub_receiver, f"platform-enforced-{tenant}", allowed_domains)
        warnings.extend(domain_warnings)
        if any("not in allowed_domains" in w for w in domain_warnings):
            return None, None, warnings

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

    receiver = {"name": receiver_name}
    receiver.update(am_config)
    return route, receiver, warnings


def _build_single_enforced_route(enforced_routing: dict,
                                 allowed_domains: list[str] | None) -> tuple[dict | None, dict | None, list[str]]:
    """Build a single platform-wide enforced route with receiver.

    Returns (route_dict, receiver_dict, warnings) — both None if invalid.
    """
    warnings = []
    enforced_receiver = enforced_routing.get("receiver")
    am_config, recv_warnings = build_receiver_config(enforced_receiver, "platform-enforced")
    warnings.extend(recv_warnings)
    if am_config is None:
        return None, None, warnings

    # Domain allowlist check for platform receiver too
    if allowed_domains:
        domain_warnings = validate_receiver_domains(
            enforced_receiver, "platform-enforced", allowed_domains)
        warnings.extend(domain_warnings)
        if any("not in allowed_domains" in w for w in domain_warnings):
            warnings.append("  WARN: _routing_enforced: receiver blocked by domain policy")
            return None, None, warnings

    receiver_name = "platform-enforced"
    route = {
        "receiver": receiver_name,
        "continue": True,
    }

    # Optional matchers
    match = enforced_routing.get("match")
    if match and isinstance(match, list):
        route["matchers"] = match

    # Optional group_by
    group_by = enforced_routing.get("group_by")
    if group_by and isinstance(group_by, list):
        route["group_by"] = group_by

    # Timing parameters with guardrails
    timing, timing_warnings = _apply_timing_params(enforced_routing, "platform-enforced")
    warnings.extend(timing_warnings)
    route.update(timing)

    receiver = {"name": receiver_name}
    receiver.update(am_config)
    return route, receiver, warnings


def _build_enforced_routes(enforced_routing: dict, routing_configs: dict[str, dict], allowed_domains: list[str] | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """Generate platform-enforced Alertmanager routes and receivers.

    Implements platform-wide Enforced Routing with continue: true to ensure
    NOC (Network Operations Center) always receives notifications. Supports
    both single platform-wide routes and per-tenant routes via {{tenant}} expansion.

    Routing Modes:
      - v1.7.0: Single enforced route (continue: true) applies to all alerts
      - v1.10.0: Per-tenant expansion when _routing_enforced contains {{tenant}}
        placeholder — each tenant gets a separate enforced route with tenant=<name> matcher

    Args:
        enforced_routing: _routing_enforced config dict or None
        routing_configs: {tenant_name: routing_config} for {{tenant}} expansion
        allowed_domains: optional fnmatch domain patterns for webhook URL validation (SSRF protection)

    Returns:
        (routes_list, receivers_list, warnings_list) where:
        - routes: enforced route dicts with continue: true
        - receivers: platform-enforced receiver dicts
        - warnings: domain validation, receiver config errors, etc.
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
        # Per-tenant enforced routes: expand {{tenant}} for each tenant
        for tenant in sorted(routing_configs.keys()):
            route, receiver, route_warnings = _build_per_tenant_enforced_route(
                tenant, enforced_routing, allowed_domains)
            warnings.extend(route_warnings)
            if route is not None and receiver is not None:
                routes.append(route)
                receivers.append(receiver)
    else:
        # Single platform-wide enforced route
        route, receiver, route_warnings = _build_single_enforced_route(
            enforced_routing, allowed_domains)
        warnings.extend(route_warnings)
        if route is not None and receiver is not None:
            routes.append(route)
            receivers.append(receiver)

    return routes, receivers, warnings


# ============================================================
# Main Route Generation (Tenant routing + inhibit rules)
# ============================================================

def _build_tenant_routes(routing_configs: dict[str, dict], allowed_domains: list[str] | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """Generate tenant-specific Alertmanager routes and receivers.

    Iterates over all tenants and produces their main routing configuration,
    including any per-rule routing overrides (v1.8.0). Each tenant route is
    matched by tenant=<name> label and routed to a tenant-specific receiver.

    Processing Per Tenant:
      1. Validate receiver config (required, must have type)
      2. Build Alertmanager receiver config (webhook, email, slack, etc.)
      3. Apply domain policy constraints if allowed_domains is provided
      4. Expand per-rule routing overrides and insert before main tenant route
      5. Build tenant route with matchers, receiver name, timing, and group_by

    Args:
        routing_configs: {tenant_name: routing_config_dict} resolved from defaults and overrides
        allowed_domains: optional fnmatch domain patterns for webhook URL validation (SSRF protection)

    Returns:
        (routes_list, receivers_list, warnings_list) where:
        - routes: tenant route dicts with per-rule overrides injected first
        - receivers: tenant receiver dicts built from routing_configs
        - warnings: validation warnings (domain policy, missing receiver, etc.)
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


def generate_routes(routing_configs: dict[str, dict], allowed_domains: list[str] | None = None, enforced_routing: dict | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """Generate Alertmanager route tree + receivers from routing configs.

    Delegates to _build_enforced_routes() and _build_tenant_routes() to produce
    enforced routes (NOC notifications with continue: true) and tenant-specific
    routes. Enforced routes are inserted first in the route tree to ensure
    platform-wide visibility.

    Features:
      - v1.7.0: Platform Enforced Routing with continue: true
      - v1.8.0: Per-rule routing overrides
      - v1.10.0: {{tenant}} placeholder expansion
      - Domain policy enforcement (webhook URL allowlist validation)

    Args:
        routing_configs: {tenant_name: routing_config_dict} resolved from defaults
        allowed_domains: optional list of fnmatch domain patterns for webhook URL validation
        enforced_routing: optional platform-wide routing rule (NOC fallback)

    Returns:
        (routes_list, receivers_list, warnings_list) where:
        - routes_list: Alertmanager route dicts (enforced routes first, then tenant routes)
        - receivers_list: Alertmanager receiver dicts with webhook/email/etc. configs
        - warnings_list: validation warnings (domain check, schema, etc.)
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


def _build_inhibit_rules(tenant: str) -> dict:
    """Build a single per-tenant severity dedup inhibit rule.

    Constructs an Alertmanager inhibit_rule that suppresses warning alerts
    when a corresponding critical alert exists for the same metric_group.
    This implements severity deduplication: when both critical and warning
    fire together, only the critical notification is sent.

    Args:
        tenant: tenant name (used in tenant matcher and label equal).

    Returns:
        inhibit_rule dict with source_matchers, target_matchers, and equal fields.

    Structure:
      - source: critical alert with metric_group + tenant="<name>"
      - target: warning alert with metric_group + tenant="<name>"
      - equal: ["metric_group"] — suppress target if source matches same metric_group
    """
    return {
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


def generate_inhibit_rules(dedup_configs: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Generate per-tenant severity dedup inhibit rules.

    Iterates over all tenants and builds inhibit rules for those with
    severity deduplication enabled (default). Tenants with _severity_dedup: "disable"
    are skipped — both warning and critical notifications are sent.

    Severity Dedup Mechanism:
      When enabled (default), suppresses warning alerts when a critical alert
      fires for the same metric_group. This reduces alert fatigue while
      preserving critical visibility. Implemented via Alertmanager inhibit_rules.

    Args:
        dedup_configs: {tenant_name: "enable"|"disable"} for all tenants.

    Returns:
        (inhibit_rules_list, warnings_list) where:
        - inhibit_rules_list: list of dicts ready for alertmanager.yml
        - warnings_list: INFO messages for each tenant (e.g., dedup disabled)
    """
    rules = []
    all_warnings = []

    for tenant in sorted(dedup_configs.keys()):
        mode = dedup_configs[tenant]
        if mode == "disable":
            all_warnings.append(f"  INFO: {tenant}: severity_dedup disabled, skipping inhibit rule")
            continue

        rule = _build_inhibit_rules(tenant)
        rules.append(rule)

    return rules, all_warnings


def render_output(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict] | None = None) -> str:
    """Render Alertmanager route + receiver + inhibit config as YAML fragment.

    Constructs a clean YAML dictionary containing the tenant routing config
    (route tree + receivers + inhibit rules) suitable for merging into an
    existing alertmanager.yml or for --dry-run output.

    Args:
        routes: list of Alertmanager route dicts
        receivers: list of Alertmanager receiver dicts
        inhibit_rules: optional list of inhibit_rule dicts (severity dedup)

    Returns:
        YAML string fragment with keys: route (with nested routes), receivers, inhibit_rules.
        Empty sections are omitted from the output.
    """
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


def load_base_config(path: str | None) -> dict:
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


def assemble_configmap(base: dict, routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                       namespace: str = "monitoring", configmap_name: str = "alertmanager-config") -> str:
    """Merge tenant routing fragments into base Alertmanager config and wrap as K8s ConfigMap.

    Merges generated routes, receivers, and inhibit_rules into a base Alertmanager
    configuration, then wraps the result as a Kubernetes ConfigMap YAML suitable
    for kubectl apply or GitOps workflows.

    Merge Strategy:
      - Routes: replace base route.routes with generated tenant routes
      - Receivers: append tenant receivers to base receivers (dedup by name)
      - Inhibit Rules: append tenant rules to base rules
      - Global/Other: preserve from base config

    Args:
        base: base Alertmanager config dict (from load_base_config or YAML file)
        routes: generated tenant route dicts
        receivers: generated tenant receiver dicts
        inhibit_rules: generated inhibit_rules for severity dedup
        namespace: K8s namespace for ConfigMap (default: monitoring)
        configmap_name: ConfigMap name (default: alertmanager-config)

    Returns:
        Complete Kubernetes ConfigMap YAML string (apiVersion, kind, metadata, data.alertmanager.yml).
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


# ============================================================
# ConfigMap Operations (K8s cluster deployment)
# ============================================================

def _read_existing_configmap(namespace: str, configmap_name: str) -> tuple[dict | None, list[str]]:
    """Read existing Alertmanager ConfigMap from K8s cluster.

    Returns (config_dict, warnings) — config_dict is None if read failed.
    """
    warnings = []
    result = subprocess.run(
        ["kubectl", "get", "configmap", configmap_name, "-n", namespace, "-o", "json"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        warnings.append(f"ERROR: Failed to read ConfigMap {configmap_name}: {result.stderr}")
        return None, warnings

    cm = json.loads(result.stdout)
    existing_yml = cm.get("data", {}).get("alertmanager.yml", "")
    if not existing_yml:
        warnings.append("ERROR: ConfigMap has no 'alertmanager.yml' key")
        return None, warnings

    existing = yaml.safe_load(existing_yml)
    return existing, warnings


def _merge_routes_receivers_inhibits(existing: dict, routes: list[dict],
                                     receivers: list[dict], inhibit_rules: list[dict]) -> dict:
    """Merge generated routes, receivers, and inhibit rules into existing config.

    Returns merged config dict.
    """
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
        kept_rules = [r for r in existing.get("inhibit_rules", [])
                      if not any('metric_group' in m for m in r.get("source_matchers", []))]
        existing["inhibit_rules"] = kept_rules + inhibit_rules

    return existing


def _apply_merged_configmap(merged_yml: str, namespace: str, configmap_name: str) -> bool:
    """Apply merged ConfigMap to K8s cluster.

    Returns True if successful, False otherwise.
    """
    apply_result = subprocess.run(
        ["kubectl", "create", "configmap", configmap_name,
         f"--from-literal=alertmanager.yml={merged_yml}",
         "-n", namespace, "--dry-run=client", "-o", "yaml"],
        capture_output=True, text=True
    )
    if apply_result.returncode != 0:
        print(f"ERROR: Failed to generate ConfigMap: {apply_result.stderr}", file=sys.stderr)
        return False

    pipe_result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=apply_result.stdout, capture_output=True, text=True
    )
    if pipe_result.returncode != 0:
        print(f"ERROR: kubectl apply failed: {pipe_result.stderr}", file=sys.stderr)
        return False

    print(f"ConfigMap {namespace}/{configmap_name} updated")
    return True


def _reload_alertmanager(namespace: str) -> bool:
    """Reload Alertmanager configuration via HTTP POST.

    Returns True on success (or if warning-level failure), False on critical error.
    """
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


def apply_to_configmap(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict], namespace: str, configmap_name: str) -> bool:
    """Merge generated routing config into existing Alertmanager ConfigMap and reload.

    Applies tenant routing configuration directly to a running Alertmanager cluster.
    This is the --apply mode for immediate deployment without GitOps workflow.

    Process:
      1. kubectl get configmap → extract alertmanager.yml
      2. Merge generated routes, receivers, inhibit_rules into existing config
      3. kubectl apply ConfigMap with merged config
      4. curl POST /-/reload to trigger Alertmanager configuration reload

    Notes:
      - Keeps existing base routes/receivers, appends tenant-generated ones
      - Preserves non-generated inhibit rules (e.g., Silent Mode sentinel rules)
      - Requires Alertmanager --web.enable-lifecycle flag for /-/reload to work

    Args:
        routes: generated tenant route dicts
        receivers: generated tenant receiver dicts
        inhibit_rules: generated inhibit_rules for severity dedup
        namespace: K8s namespace where ConfigMap is located
        configmap_name: ConfigMap name (typically alertmanager-config)

    Returns:
        True if merge and reload succeeded, False otherwise.
    """
    # 1. Read existing ConfigMap
    existing, read_warnings = _read_existing_configmap(namespace, configmap_name)
    if existing is None:
        for w in read_warnings:
            print(w, file=sys.stderr)
        return False

    # 2. Merge fragment into existing config
    existing = _merge_routes_receivers_inhibits(existing, routes, receivers, inhibit_rules)
    merged_yml = yaml.dump(existing, default_flow_style=False,
                           allow_unicode=True, sort_keys=False)

    # 3. Apply updated ConfigMap
    if not _apply_merged_configmap(merged_yml, namespace, configmap_name):
        return False

    # 4. Reload Alertmanager
    return _reload_alertmanager(namespace)


# ============================================================
# CLI Mode Handlers (--validate, --apply, --output-configmap, default render)
# ============================================================

def _validate_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                   all_warnings: list[str]) -> None:
    """Handle --validate mode: check for errors and exit."""
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


def _apply_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                namespace: str, configmap_name: str, yes_flag: bool) -> None:
    """Handle --apply mode: merge into ConfigMap and reload."""
    route_count = len(routes)
    inhibit_count = len(inhibit_rules)
    print(f"\nApply: {route_count} route(s), {len(receivers)} receiver(s), "
          f"{inhibit_count} inhibit rule(s)")
    print(f"Target: {namespace}/{configmap_name}")
    if not yes_flag:
        confirm = input("Proceed? [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)
    success = apply_to_configmap(routes, receivers, inhibit_rules, namespace, configmap_name)
    sys.exit(0 if success else 1)


def _output_configmap_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                           base_config: str | None, namespace: str, configmap_name: str,
                           dry_run: bool, output: str | None) -> None:
    """Handle --output-configmap mode: produce complete ConfigMap YAML."""
    base = load_base_config(base_config)
    cm_yaml = assemble_configmap(
        base, routes, receivers, inhibit_rules,
        namespace=namespace, configmap_name=configmap_name)

    route_count = len(routes)
    inhibit_count = len(inhibit_rules)

    if dry_run:
        print("\n--- DRY RUN: ConfigMap YAML ---")
        print(cm_yaml)
        print(f"\n--- {route_count} route(s), {len(receivers)} receiver(s), "
              f"{inhibit_count} inhibit rule(s) ---")
        return

    if output:
        write_text_secure(output, cm_yaml)
        print(f"Written to {output} ({route_count} routes, "
              f"{len(receivers)} receivers, {inhibit_count} inhibit rules)")
    else:
        print(cm_yaml)


def _render_output_mode(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                       dry_run: bool, output: str | None) -> None:
    """Handle default render mode: output routes/receivers fragment."""
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

    if dry_run:
        print("\n--- DRY RUN OUTPUT ---")
        print(content)
        print(f"\n--- {route_count} route(s), {len(receivers)} receiver(s), "
              f"{inhibit_count} inhibit rule(s) ---")
        return

    if output:
        write_text_secure(output, content)
        print(f"Written to {output} ({route_count} routes, {len(receivers)} receivers, "
              f"{inhibit_count} inhibit rules)")
    else:
        print(content)


def _print_config_summary(routing_configs: dict, dedup_configs: dict, enforced_routing: dict | None) -> None:
    """Print summary of loaded configs."""
    if enforced_routing:
        print("Platform enforced routing: ENABLED")
    if routing_configs:
        print(f"Found {len(routing_configs)} tenant(s) with routing config: "
              f"{', '.join(sorted(routing_configs.keys()))}")
    print(f"Found {len(dedup_configs)} tenant(s) for severity dedup: "
          f"{', '.join(sorted(dedup_configs.keys()))}")


def main() -> None:
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

    _print_config_summary(routing_configs, dedup_configs, enforced_routing)

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

    # Validate mode
    if args.validate:
        _validate_mode(routes, receivers, inhibit_rules, all_warnings)

    # Apply mode
    if args.apply:
        _apply_mode(routes, receivers, inhibit_rules, args.namespace,
                    args.configmap, args.yes)

    # Output-configmap mode
    if args.output_configmap:
        _output_configmap_mode(routes, receivers, inhibit_rules, args.base_config,
                              args.namespace, args.configmap, args.dry_run, args.output)
        return

    # Default render mode
    _render_output_mode(routes, receivers, inhibit_rules, args.dry_run, args.output)


if __name__ == "__main__":
    main()
