"""Route generation: tenant routes, override expansion, enforced routes, inhibit rules.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  Override processing (v1.8.0):
    _validate_override_matcher / _build_override_matchers
    _process_override_receiver / _build_override_route
    expand_routing_overrides

  Platform enforced routing (v1.7.0 / v1.10.0 per-tenant {{tenant}} expansion):
    _build_per_tenant_enforced_route / _build_single_enforced_route
    _build_enforced_routes

  Main route generation:
    _build_tenant_routes / generate_routes

  Severity-dedup inhibit rules:
    _build_inhibit_rules / generate_inhibit_rules
"""
from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout

from _grar_merge import (  # noqa: E402
    _apply_timing_params,
    _contains_tenant_placeholder,
    _substitute_tenant,
    build_receiver_config,
)
from _grar_validate import validate_receiver_domains  # noqa: E402


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
