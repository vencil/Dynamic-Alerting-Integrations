#!/usr/bin/env python3
"""patch_config.py — Patch threshold-config ConfigMap for a specific tenant.

Supports both legacy (single config.yaml key) and multi-file (per-tenant YAML keys)
ConfigMap structures. Auto-detects format by checking for '_defaults.yaml' key.

Usage: patch_config.py <tenant> <metric_key> <value>
       patch_config.py --diff <tenant> <metric_key> <value>

Three-state logic:
  - Custom value:  patch_config.py db-a mysql_connections 50
  - Default (delete key): patch_config.py db-a mysql_connections default
  - Disable:       patch_config.py db-a mysql_connections disable

Diff preview (terraform plan analogy):
  - patch_config.py --diff db-a mysql_connections 50
  Shows before/after comparison without applying any changes.

Dimensional metrics (Phase 2B):
  - patch_config.py db-a 'redis_queue_length{queue="tasks"}' 500
  - patch_config.py db-a 'redis_db_keys{db="db0"}' disable
  Note: Shell quoting is important — wrap the metric key in single quotes.
"""
import argparse
import subprocess
import yaml
import sys
import json
import tempfile
import os


def run_cmd(cmd):
    """Execute a command safely using list arguments (no shell=True)."""
    if isinstance(cmd, str):
        import shlex
        cmd = shlex.split(cmd)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error executing: {' '.join(cmd)}\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def detect_mode(cm_data):
    """Detect ConfigMap format: 'multi-file' if _defaults.yaml exists, else 'legacy'."""
    data = cm_data.get("data", {})
    if "_defaults.yaml" in data:
        return "multi-file"
    return "legacy"


def patch_legacy(cm_data, tenant, metric_key, value):
    """Legacy mode: patch the single 'config.yaml' key."""
    config_yaml_str = cm_data.get("data", {}).get("config.yaml", "")
    if not config_yaml_str:
        print("Error: config.yaml not found in ConfigMap.", file=sys.stderr)
        sys.exit(1)

    config = yaml.safe_load(config_yaml_str)

    if "tenants" not in config:
        config["tenants"] = {}
    if tenant not in config["tenants"]:
        config["tenants"][tenant] = {}

    if str(value).lower() == "default":
        config["tenants"][tenant].pop(metric_key, None)
        if not config["tenants"][tenant]:
            del config["tenants"][tenant]
    else:
        config["tenants"][tenant][metric_key] = str(value)

    updated_yaml_str = yaml.dump(config, sort_keys=False)
    return {"data": {"config.yaml": updated_yaml_str}}


def patch_multifile(cm_data, tenant, metric_key, value):
    """Multi-file mode: patch the '<tenant>.yaml' key in ConfigMap."""
    data = cm_data.get("data", {})
    tenant_key = f"{tenant}.yaml"

    # Parse existing tenant YAML, or create new
    if tenant_key in data and data[tenant_key]:
        tenant_config = yaml.safe_load(data[tenant_key]) or {}
    else:
        tenant_config = {}

    if "tenants" not in tenant_config:
        tenant_config["tenants"] = {}
    if tenant not in tenant_config["tenants"]:
        tenant_config["tenants"][tenant] = {}

    if str(value).lower() == "default":
        # Delete key → revert to default
        tenant_config["tenants"][tenant].pop(metric_key, None)

        if not tenant_config["tenants"][tenant]:
            # Tenant has no overrides left — write empty tenants block
            # (keeps the file so the tenant is still registered)
            tenant_config["tenants"][tenant] = {}
    else:
        tenant_config["tenants"][tenant][metric_key] = str(value)

    updated_yaml_str = yaml.dump(tenant_config, sort_keys=False)
    return {"data": {tenant_key: updated_yaml_str}}


def get_current_value(cm_data, mode, tenant, metric_key):
    """Get the current value of a metric key from the ConfigMap.

    Returns (current_value, source) where source is 'tenant', 'defaults', or 'none'.
    """
    data = cm_data.get("data", {})

    if mode == "legacy":
        config_str = data.get("config.yaml", "")
        if config_str:
            config = yaml.safe_load(config_str) or {}
            tenants = config.get("tenants", {})
            if tenant in tenants and metric_key in tenants[tenant]:
                return tenants[tenant][metric_key], "tenant"
            defaults = config.get("defaults", {})
            if metric_key in defaults:
                return defaults[metric_key], "defaults"
    else:
        tenant_key = f"{tenant}.yaml"
        if tenant_key in data and data[tenant_key]:
            tenant_config = yaml.safe_load(data[tenant_key]) or {}
            tenants = tenant_config.get("tenants", {})
            if tenant in tenants and metric_key in tenants[tenant]:
                return tenants[tenant][metric_key], "tenant"
        defaults_key = "_defaults.yaml"
        if defaults_key in data and data[defaults_key]:
            defaults_config = yaml.safe_load(data[defaults_key]) or {}
            defaults_section = defaults_config.get("defaults", {})
            if metric_key in defaults_section:
                return defaults_section[metric_key], "defaults"

    return None, "none"


def find_affected_alerts(metric_key):
    """Identify alert rules that reference this metric.

    Returns list of alert rule names (best-effort, based on naming convention).
    """
    # Strip dimensional suffix for matching
    base_metric = metric_key.split("{")[0] if "{" in metric_key else metric_key

    # Common alert naming patterns based on metric names
    alerts = []
    parts = base_metric.split("_")

    # Build likely alert name patterns
    # e.g., mysql_connections → MariaDBHighConnections
    # e.g., container_cpu → PodContainerHighCPU
    if len(parts) >= 2:
        # CamelCase conversion
        camel = "".join(p.capitalize() for p in parts)
        alerts.append(f"*{camel}*")

    return alerts


def diff_preview(cm_data, mode, tenant, metric_key, value):
    """Show before/after preview of a config change without applying it.

    Returns dict with diff details.
    """
    current_value, source = get_current_value(cm_data, mode, tenant, metric_key)
    affected_alerts = find_affected_alerts(metric_key)

    # Determine new state description
    new_value = value
    if str(value).lower() == "default":
        new_state = "default (key removed)"
        new_value = "(platform default)"
    elif str(value).lower() in ("disable", "disabled", "off", "false"):
        new_state = "disabled"
    else:
        new_state = f"custom: {value}"

    # Determine old state description
    if current_value is None:
        old_state = "default (not set)"
        old_display = "(platform default)"
    elif str(current_value).lower() in ("disable", "disabled", "off", "false"):
        old_state = "disabled"
        old_display = str(current_value)
    else:
        old_state = f"custom: {current_value}"
        old_display = str(current_value)

    # Build diff result
    diff = {
        "tenant": tenant,
        "metric_key": metric_key,
        "configmap_mode": mode,
        "before": {"value": current_value, "source": source, "state": old_state},
        "after": {"value": new_value if str(value).lower() != "default" else None,
                  "state": new_state},
        "changed": str(current_value) != str(value),
        "affected_alerts": affected_alerts,
    }

    return diff


def print_diff(diff):
    """Print human-readable diff preview."""
    print()
    print("=" * 55)
    print("  Config Change Preview (--diff)")
    print("=" * 55)
    print()
    print(f"  Tenant:   {diff['tenant']}")
    print(f"  Metric:   {diff['metric_key']}")
    print(f"  Mode:     {diff['configmap_mode']}")
    print()

    before = diff["before"]
    after = diff["after"]

    if diff["changed"]:
        print(f"  - Before: {before['state']}  (source: {before['source']})")
        print(f"  + After:  {after['state']}")
    else:
        print(f"    No change (already: {before['state']})")

    if diff["affected_alerts"]:
        print()
        print(f"  Affected alerts (pattern): {', '.join(diff['affected_alerts'])}")

    print()
    if diff["changed"]:
        print("  To apply: remove --diff flag and re-run.")
    print()


def apply_patch(cm_data, mode, tenant, metric_key, value):
    """Build and apply the ConfigMap patch."""
    if mode == "legacy":
        patch_data = patch_legacy(cm_data, tenant, metric_key, value)
    else:
        patch_data = patch_multifile(cm_data, tenant, metric_key, value)

    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json',
                                     encoding="utf-8") as temp:
        json.dump(patch_data, temp)
        temp_path = temp.name

    print(f"Patching ConfigMap ({mode}) for {tenant}: {metric_key} = {value}...")
    run_cmd(["kubectl", "patch", "configmap", "threshold-config",
             "-n", "monitoring", "--type", "merge", "--patch-file", temp_path])
    os.remove(temp_path)
    print("Success! Exporter will reload within its interval.")


def main():
    """CLI entry point: Patch threshold-config ConfigMap for a specific tenant."""
    parser = argparse.ArgumentParser(
        description="Patch threshold-config ConfigMap for a specific tenant",
    )
    parser.add_argument("tenant", help="Tenant name (e.g., db-a)")
    parser.add_argument("metric_key", help="Metric key to patch")
    parser.add_argument("value", help="New value, 'default', or 'disable'")
    parser.add_argument(
        "--diff", action="store_true",
        help="Preview change without applying (like terraform plan)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output diff as JSON (requires --diff)",
    )
    args = parser.parse_args()

    # 1. Get existing ConfigMap
    cm_json = run_cmd(["kubectl", "get", "configmap", "threshold-config",
                       "-n", "monitoring", "-o", "json"])
    cm_data = json.loads(cm_json)

    # 2. Detect mode
    mode = detect_mode(cm_data)

    if args.diff:
        # Preview mode: show diff without applying
        diff = diff_preview(cm_data, mode, args.tenant, args.metric_key, args.value)
        if args.json:
            print(json.dumps(diff, indent=2, ensure_ascii=False))
        else:
            print_diff(diff)
    else:
        # Apply mode (original behavior)
        print(f"Detected ConfigMap mode: {mode}")
        apply_patch(cm_data, mode, args.tenant, args.metric_key, args.value)


if __name__ == "__main__":
    main()
