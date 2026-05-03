"""Routing-config merging + tenant substitution + receiver building.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  _apply_timing_params(...)         → group_wait/interval/repeat with guardrails
  _substitute_tenant(obj, name)     → recursive {{tenant}} → name replacement
  _contains_tenant_placeholder(obj) → True if any string contains {{tenant}}
  merge_routing_with_defaults(...)  → shallow merge of defaults + tenant routing
  build_receiver_config(...)        → structured receiver dict → AM config dict
"""
from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    validate_and_clamp,
    RECEIVER_TYPES,
)


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
