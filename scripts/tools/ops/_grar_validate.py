"""URL / domain / schema validation for generate_alertmanager_routes.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  _extract_host(value)          → hostname (lowercase) or None
  validate_receiver_domains(...) → SSRF-prevention domain allowlist check
  load_policy(path)             → list of allowed_domains from policy YAML
  validate_tenant_keys(...)      → schema-key typo / unknown-key warnings
  _validate_profile_refs(parsed) → ADR-007 profile-reference existence check
  check_domain_policies(...)    → ADR-007 domain-policy constraint validation
"""
from __future__ import annotations

import fnmatch
import os
import sys
from urllib.parse import urlparse

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    parse_duration_seconds,
    RECEIVER_URL_FIELDS,
    VALID_RESERVED_KEYS,
    VALID_RESERVED_PREFIXES,
)


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
