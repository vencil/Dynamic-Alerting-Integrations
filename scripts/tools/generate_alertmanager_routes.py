#!/usr/bin/env python3
"""
generate_alertmanager_routes.py — Generate Alertmanager route + receiver + inhibit config from tenant YAML.

Reads all tenant YAML files from conf.d/, extracts _routing and _severity_dedup sections,
and produces an Alertmanager route tree + receivers + inhibit_rules YAML fragment.

Severity Dedup (per-tenant):
  Default (absent or "enable"): generate inhibit_rule that suppresses warning when critical fires
  "disable": skip inhibit_rule — both warning and critical notifications are sent
  Mechanism: per-tenant inhibit_rules with tenant="<name>" + metric_group matchers

Usage:
  python3 scripts/tools/generate_alertmanager_routes.py --config-dir components/threshold-exporter/config/conf.d/
  python3 scripts/tools/generate_alertmanager_routes.py --config-dir conf.d/ -o alertmanager-routes.yaml
  python3 scripts/tools/generate_alertmanager_routes.py --config-dir conf.d/ --dry-run
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

from _lib_python import (  # noqa: E402
    is_disabled as _is_disabled,
    parse_duration_seconds,
)

# ============================================================
# Timing Guardrails
# ============================================================
# Format: (min_seconds, max_seconds, description)
GUARDRAILS = {
    "group_wait": (5, 300, "5s–5m"),
    "group_interval": (5, 300, "5s–5m"),
    "repeat_interval": (60, 259200, "1m–72h"),
}

# Platform defaults (used when tenant doesn't specify)
PLATFORM_DEFAULTS = {
    "group_by": ["alertname", "tenant"],
    "group_wait": "30s",
    "group_interval": "5m",
    "repeat_interval": "4h",
}

# ============================================================
# Receiver Types
# ============================================================
# Each type maps to: (alertmanager_config_key, required_fields, optional_fields)
RECEIVER_TYPES = {
    "webhook": {
        "am_key": "webhook_configs",
        "required": ["url"],
        "optional": ["send_resolved", "http_config"],
    },
    "email": {
        "am_key": "email_configs",
        "required": ["to", "smarthost"],
        "optional": ["from", "auth_username", "auth_password", "require_tls",
                      "html", "text", "headers", "send_resolved"],
    },
    "slack": {
        "am_key": "slack_configs",
        "required": ["api_url"],
        "optional": ["channel", "title", "text", "title_link", "icon_emoji",
                      "send_resolved"],
    },
    "teams": {
        "am_key": "msteams_configs",
        "required": ["webhook_url"],
        "optional": ["title", "text", "send_resolved"],
    },
    "rocketchat": {
        "am_key": "webhook_configs",
        "required": ["url"],
        "optional": ["send_resolved"],
        "metadata": ["channel", "username", "icon_url"],  # documented but not passed to AM
    },
    "pagerduty": {
        "am_key": "pagerduty_configs",
        "required": ["service_key"],
        "optional": ["routing_key", "severity", "description", "client",
                      "client_url", "send_resolved"],
    },
}


# ============================================================
# Webhook Domain Allowlist (v1.5.0 — SSRF prevention)
# ============================================================
# Maps receiver type → list of fields that contain URLs to validate
RECEIVER_URL_FIELDS = {
    "webhook":    ["url"],
    "email":      ["smarthost"],      # host:port format
    "slack":      ["api_url"],
    "teams":      ["webhook_url"],
    "rocketchat": ["url"],
    "pagerduty":  [],                 # service_key only, no URL
}


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


def format_duration(seconds):
    """Format seconds back to a Prometheus-compatible duration string.

    NOTE: Prometheus/Alertmanager only supports s/m/h (not d/w/y).
    Do NOT convert to days even if evenly divisible.
    """
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def validate_and_clamp(param, value, tenant):
    """Validate a timing parameter against guardrails. Returns clamped value + warnings."""
    warnings = []

    if param not in GUARDRAILS:
        return value, warnings

    min_sec, max_sec, desc = GUARDRAILS[param]
    seconds = parse_duration_seconds(value)

    if seconds is None:
        warnings.append(f"  WARN: {tenant}: invalid {param} '{value}', using platform default")
        return PLATFORM_DEFAULTS.get(param, value), warnings

    if seconds < min_sec:
        clamped = format_duration(min_sec)
        warnings.append(f"  WARN: {tenant}: {param} '{value}' below minimum ({desc}), clamped to {clamped}")
        return clamped, warnings

    if seconds > max_sec:
        clamped = format_duration(max_sec)
        warnings.append(f"  WARN: {tenant}: {param} '{value}' above maximum ({desc}), clamped to {clamped}")
        return clamped, warnings

    return value, warnings


def _substitute_tenant(obj, tenant_name):
    """Replace {{tenant}} placeholders in all string values recursively."""
    if isinstance(obj, str):
        return obj.replace("{{tenant}}", tenant_name)
    if isinstance(obj, dict):
        return {k: _substitute_tenant(v, tenant_name) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_tenant(item, tenant_name) for item in obj]
    return obj


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


VALID_RESERVED_KEYS = {"_silent_mode", "_severity_dedup"}
VALID_RESERVED_PREFIXES = ("_state_", "_routing")


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


def load_tenant_configs(config_dir):
    """Load all tenant YAML files from a config directory.

    Returns tuple of:
      - routing_configs: {tenant_name: routing_config} for tenants that have _routing
      - dedup_configs: {tenant_name: "enable"|"disable"} for ALL tenants (default: "enable")

    Supports _routing_defaults in _defaults.yaml (v1.4.0):
      - Tenants without _routing inherit from _routing_defaults
      - Tenants with _routing get defaults merged (tenant wins)
      - _routing: "disable" → skip tenant
      - {{tenant}} placeholder substituted in all string values
    """
    routing_configs = {}
    dedup_configs = {}
    routing_defaults = {}
    explicit_routing = {}  # track which tenants have explicit _routing
    disabled_tenants = set()
    all_tenants = []
    defaults_keys = set()    # keys from defaults section (for schema validation)
    tenant_keys = {}         # {tenant: set of keys} for schema validation

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
            defaults_keys.update(data["defaults"].keys())

        # Extract _routing_defaults (only from _ prefixed files)
        is_defaults_file = os.path.basename(fname).startswith("_")
        if "_routing_defaults" in data:
            if is_defaults_file:
                routing_defaults = data["_routing_defaults"]
            else:
                print(f"  WARN: _routing_defaults in {fname} ignored "
                      "(only allowed in _ prefixed files)", file=sys.stderr)

        if "tenants" not in data:
            continue

        for tenant, overrides in data.get("tenants", {}).items():
            if not isinstance(overrides, dict):
                continue

            all_tenants.append(tenant)

            # Collect tenant keys for schema validation
            if tenant not in tenant_keys:
                tenant_keys[tenant] = set()
            tenant_keys[tenant].update(overrides.keys())

            # Severity dedup: default "enable", explicit "disable" to opt out
            # (tracked before routing disable check — dedup is independent of routing)
            raw_dedup = overrides.get("_severity_dedup", "enable")
            dedup_val = str(raw_dedup).strip().lower()
            if _is_disabled(dedup_val):
                dedup_configs[tenant] = "disable"
            else:
                dedup_configs[tenant] = "enable"

            # Routing: "disable" string → skip routing (dedup still tracked above)
            routing = overrides.get("_routing")
            if isinstance(routing, str) and _is_disabled(routing):
                disabled_tenants.add(tenant)
                continue

            if routing and isinstance(routing, dict):
                explicit_routing[tenant] = routing

    # Merge routing defaults with tenant configs
    seen_tenants = set()
    for tenant in sorted(set(all_tenants)):
        if tenant in disabled_tenants or tenant in seen_tenants:
            continue
        seen_tenants.add(tenant)

        if tenant in explicit_routing:
            # Tenant has explicit _routing → merge with defaults
            routing_configs[tenant] = merge_routing_with_defaults(
                routing_defaults, explicit_routing[tenant], tenant)
        elif routing_defaults:
            # No explicit _routing but defaults exist → inherit defaults
            routing_configs[tenant] = merge_routing_with_defaults(
                routing_defaults, {}, tenant)

    # Schema validation: check tenant keys against defaults
    schema_warnings = []
    for tenant, keys in sorted(tenant_keys.items()):
        schema_warnings.extend(validate_tenant_keys(tenant, keys, defaults_keys))

    return routing_configs, dedup_configs, schema_warnings


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


def generate_routes(routing_configs, allowed_domains=None):
    """Generate Alertmanager route tree + receivers from routing configs.

    Returns (routes_yaml_dict, receivers_list, all_warnings).
    """
    routes = []
    receivers = []
    all_warnings = []

    for tenant in sorted(routing_configs.keys()):
        cfg = routing_configs[tenant]

        # Validate receiver (required, must be dict with type)
        receiver_obj = cfg.get("receiver")
        if not receiver_obj:
            all_warnings.append(f"  WARN: {tenant}: missing required 'receiver', skipping")
            continue

        # Build receiver config from structured object
        am_config, recv_warnings = build_receiver_config(receiver_obj, tenant)
        all_warnings.extend(recv_warnings)
        if am_config is None:
            continue

        # Domain allowlist check (SSRF prevention)
        if allowed_domains:
            domain_warnings = validate_receiver_domains(
                receiver_obj, tenant, allowed_domains)
            all_warnings.extend(domain_warnings)
            if any("not in allowed_domains" in w for w in domain_warnings):
                continue

        # Receiver name derived from tenant
        receiver_name = f"tenant-{tenant}"

        # Build route entry
        route = {
            "matchers": [f'tenant="{tenant}"'],
            "receiver": receiver_name,
        }

        # group_by (optional)
        group_by = cfg.get("group_by")
        if group_by and isinstance(group_by, list):
            route["group_by"] = group_by

        # Timing parameters with guardrails
        for param in ("group_wait", "group_interval", "repeat_interval"):
            val = cfg.get(param)
            if val:
                clamped, warnings = validate_and_clamp(param, str(val), tenant)
                all_warnings.extend(warnings)
                if clamped:
                    route[param] = clamped

        routes.append(route)

        # Build receiver entry
        receiver = {"name": receiver_name}
        receiver.update(am_config)
        receivers.append(receiver)

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
    parser.add_argument("--apply", action="store_true",
                        help="Apply: merge into Alertmanager ConfigMap + reload")
    parser.add_argument("--namespace", default="monitoring",
                        help="K8s namespace for --apply (default: monitoring)")
    parser.add_argument("--configmap", default="alertmanager-config",
                        help="ConfigMap name for --apply (default: alertmanager-config)")
    parser.add_argument("--policy", default=None,
                        help="Policy YAML with allowed_domains for webhook URL validation")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt for --apply")

    args = parser.parse_args()

    # Load policy (webhook domain allowlist)
    allowed_domains = load_policy(args.policy)
    if allowed_domains:
        print(f"Policy: {len(allowed_domains)} allowed domain pattern(s) loaded")

    # Load tenant configs (routing + dedup + schema warnings)
    routing_configs, dedup_configs, schema_warnings = load_tenant_configs(args.config_dir)

    has_routing = bool(routing_configs)
    has_dedup = bool(dedup_configs)

    if not has_routing and not has_dedup:
        print("No tenants found in config directory.")
        sys.exit(0)

    if has_routing:
        print(f"Found {len(routing_configs)} tenant(s) with routing config: "
              f"{', '.join(sorted(routing_configs.keys()))}")
    print(f"Found {len(dedup_configs)} tenant(s) for severity dedup: "
          f"{', '.join(sorted(dedup_configs.keys()))}")

    # Generate routes + receivers
    routes, receivers, route_warnings = generate_routes(
        routing_configs, allowed_domains=allowed_domains)

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
