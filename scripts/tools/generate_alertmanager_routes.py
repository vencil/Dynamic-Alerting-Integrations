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
import os
import sys
import textwrap

import yaml

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


def parse_duration_seconds(value):
    """Parse a Prometheus-style duration string to seconds.

    Supports: 5s, 30s, 1m, 5m, 1h, 4h, 72h, 1d, etc.
    Returns seconds as int, or None if invalid.
    """
    if not value or not isinstance(value, str):
        return None

    value = value.strip()
    if len(value) < 2:
        return None

    unit = value[-1]
    try:
        num = float(value[:-1])
    except ValueError:
        return None

    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if unit not in multipliers:
        return None

    return int(num * multipliers[unit])


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


def load_tenant_configs(config_dir):
    """Load all tenant YAML files from a config directory.

    Returns tuple of:
      - routing_configs: {tenant_name: routing_config} for tenants that have _routing
      - dedup_configs: {tenant_name: "enable"|"disable"} for ALL tenants (default: "enable")
    """
    routing_configs = {}
    dedup_configs = {}

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

        if not data or "tenants" not in data:
            continue

        for tenant, overrides in data.get("tenants", {}).items():
            if not isinstance(overrides, dict):
                continue

            # Routing (optional)
            routing = overrides.get("_routing")
            if routing and isinstance(routing, dict):
                routing_configs[tenant] = routing

            # Severity dedup: default "enable", explicit "disable" to opt out
            raw_dedup = overrides.get("_severity_dedup", "enable")
            dedup_val = str(raw_dedup).strip().lower()
            if dedup_val in ("disable", "disabled", "off", "false"):
                dedup_configs[tenant] = "disable"
            else:
                dedup_configs[tenant] = "enable"

    return routing_configs, dedup_configs


def generate_routes(routing_configs):
    """Generate Alertmanager route tree + receivers from routing configs.

    Returns (routes_yaml_dict, receivers_list, all_warnings).
    """
    routes = []
    receivers = []
    all_warnings = []

    for tenant in sorted(routing_configs.keys()):
        cfg = routing_configs[tenant]

        # Validate receiver (required)
        receiver_url = cfg.get("receiver")
        if not receiver_url:
            all_warnings.append(f"  WARN: {tenant}: missing required 'receiver', skipping")
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

        # Build receiver entry (webhook_configs for now, extensible)
        receiver = {
            "name": receiver_name,
            "webhook_configs": [
                {"url": receiver_url},
            ],
        }
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

    args = parser.parse_args()

    # Load tenant configs (routing + dedup)
    routing_configs, dedup_configs = load_tenant_configs(args.config_dir)

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
    routes, receivers, route_warnings = generate_routes(routing_configs)

    # Generate per-tenant severity dedup inhibit rules
    inhibit_rules, dedup_warnings = generate_inhibit_rules(dedup_configs)

    # Collect all warnings
    all_warnings = route_warnings + dedup_warnings
    for w in all_warnings:
        print(w, file=sys.stderr)

    if not routes and not inhibit_rules:
        print("No valid routes or inhibit rules generated.")
        sys.exit(1)

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
