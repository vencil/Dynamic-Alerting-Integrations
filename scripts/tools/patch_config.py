#!/usr/bin/env python3
"""patch_config.py — Patch threshold-config ConfigMap for a specific tenant.

Supports both legacy (single config.yaml key) and multi-file (per-tenant YAML keys)
ConfigMap structures. Auto-detects format by checking for '_defaults.yaml' key.

Usage: patch_config.py <tenant> <metric_key> <value>

Three-state logic:
  - Custom value:  patch_config.py db-a mysql_connections 50
  - Default (delete key): patch_config.py db-a mysql_connections default
  - Disable:       patch_config.py db-a mysql_connections disable

Dimensional metrics (Phase 2B):
  - patch_config.py db-a 'redis_queue_length{queue="tasks"}' 500
  - patch_config.py db-a 'redis_db_keys{db="db0"}' disable
  Note: Shell quoting is important — wrap the metric key in single quotes.
"""
import subprocess
import yaml
import sys
import json
import tempfile
import os


def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error executing: {cmd}\n{result.stderr}", file=sys.stderr)
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


def main(tenant, metric_key, value):
    # 1. Get existing ConfigMap
    cm_json = run_cmd("kubectl get configmap threshold-config -n monitoring -o json")
    cm_data = json.loads(cm_json)

    # 2. Detect mode and build patch
    mode = detect_mode(cm_data)
    print(f"Detected ConfigMap mode: {mode}")

    if mode == "legacy":
        patch_data = patch_legacy(cm_data, tenant, metric_key, value)
    else:
        patch_data = patch_multifile(cm_data, tenant, metric_key, value)

    # 3. Execute patch
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as temp:
        json.dump(patch_data, temp)
        temp_path = temp.name

    print(f"Patching ConfigMap ({mode}) for {tenant}: {metric_key} = {value}...")
    run_cmd(f"kubectl patch configmap threshold-config -n monitoring --type merge --patch-file {temp_path}")
    os.remove(temp_path)
    print("Success! Exporter will reload within its interval.")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: patch_config.py <tenant> <metric_key> <value>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
