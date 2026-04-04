#!/usr/bin/env python3
"""租戶元資料產生器 — 從 conf.d/ 解析 YAML，推斷 rule_packs、owner、tier、routing_channel。

Usage:
    python3 scripts/tools/dx/generate_tenant_metadata.py              # 產生 JSON
    python3 scripts/tools/dx/generate_tenant_metadata.py --config-dir conf.d
    python3 scripts/tools/dx/generate_tenant_metadata.py --output /tmp/out.json
    python3 scripts/tools/dx/generate_tenant_metadata.py --check      # CI drift 偵測
    python3 scripts/tools/dx/generate_tenant_metadata.py --dry-run    # 只印出不寫檔
"""
import argparse
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "components" / "threshold-exporter" / "config" / "conf.d"


# ---------------------------------------------------------------------------
# Metric prefix to rule pack mapping (from _lib_constants.py)
# ---------------------------------------------------------------------------
METRIC_PREFIX_DB_MAP = {
    "mysql_": "mariadb",
    "mariadb_": "mariadb",
    "pg_": "postgresql",
    "postgres_": "postgresql",
    "redis_": "redis",
    "mongo_": "mongodb",
    "mongodb_": "mongodb",
    "kafka_": "kafka",
    "rabbitmq_": "rabbitmq",
    "rabbit_": "rabbitmq",
    "elasticsearch_": "elasticsearch",
    "es_": "elasticsearch",
    "oracle_": "oracle",
    "clickhouse_": "clickhouse",
    "db2_": "db2",
    "container_": "kubernetes",
    "jvm_": "jvm",
    "nginx_": "nginx",
}

# Always-included packs (inferred from reserved metric keys)
RESERVED_PACKS = {"operational", "platform"}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def infer_rule_packs(tenant_config: dict) -> list[str]:
    """Infer rule packs from metric key prefixes."""
    packs = set(RESERVED_PACKS)

    for key in tenant_config.keys():
        # Skip reserved keys
        if key.startswith("_"):
            continue

        # Extract metric prefix (everything before first underscore or [)
        metric = key.split("[")[0] if "[" in key else key

        # Match metric prefix to rule pack
        for prefix, pack in METRIC_PREFIX_DB_MAP.items():
            if metric.startswith(prefix):
                packs.add(pack)
                break

    return sorted(list(packs))


def extract_owner(tenant_config: dict) -> str:
    """Extract owner from _metadata.owner if present."""
    metadata = tenant_config.get("_metadata", {})
    if isinstance(metadata, dict):
        return metadata.get("owner", "")
    return ""


def extract_tier(tenant_config: dict) -> str:
    """Extract tier from _metadata.tier if present."""
    metadata = tenant_config.get("_metadata", {})
    if isinstance(metadata, dict):
        return metadata.get("tier", "")
    return ""


def extract_environment(tenant_name: str, metadata: dict) -> str:
    """Infer environment from _metadata or tenant name pattern."""
    if isinstance(metadata, dict):
        env = metadata.get("environment", "")
        if env:
            return env

    # Infer from name pattern
    if tenant_name.startswith("prod-") or "production" in tenant_name:
        return "production"
    elif tenant_name.startswith("staging-") or "staging" in tenant_name:
        return "staging"
    elif tenant_name.startswith("dev-") or "development" in tenant_name:
        return "development"

    return ""


def extract_routing_channel(tenant_config: dict) -> str:
    """Extract routing channel from _routing.receiver (format: type:url or type:endpoint)."""
    routing = tenant_config.get("_routing", {})
    if not isinstance(routing, dict):
        return ""

    receiver = routing.get("receiver", {})
    if not isinstance(receiver, dict):
        return ""

    recv_type = receiver.get("type", "")
    if not recv_type:
        return ""

    # Format: type:url or type:endpoint
    if recv_type == "webhook":
        url = receiver.get("url", "")
        return f"{recv_type}:{url}" if url else ""
    elif recv_type == "email":
        to = receiver.get("to", "")
        return f"{recv_type}:{to}" if to else ""
    elif recv_type == "slack":
        api_url = receiver.get("api_url", "")
        return f"{recv_type}:{api_url}" if api_url else ""
    elif recv_type == "teams":
        webhook_url = receiver.get("webhook_url", "")
        return f"{recv_type}:{webhook_url}" if webhook_url else ""
    elif recv_type == "pagerduty":
        service_key = receiver.get("service_key", "")
        return f"{recv_type}:{service_key}" if service_key else ""

    return ""


def detect_operational_mode(tenant_config: dict) -> str:
    """Detect operational mode: normal (default), silent, or maintenance."""
    # Check for maintenance state
    if "_state_maintenance" in tenant_config:
        state_val = tenant_config["_state_maintenance"]
        if state_val != "disable":
            return "maintenance"

    # Check for silent mode
    silent_mode = tenant_config.get("_silent_mode", "")
    if silent_mode and silent_mode != "disable":
        return "silent"

    return "normal"


def count_metrics(tenant_config: dict) -> int:
    """Count non-reserved metric keys."""
    count = 0
    for key in tenant_config.keys():
        if not key.startswith("_"):
            count += 1
    return count


def get_git_head_commit() -> str:
    """Get current git HEAD commit hash, or empty if not in git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:7]  # Short hash
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


# ---------------------------------------------------------------------------
# Build tenant metadata
# ---------------------------------------------------------------------------
def build_tenant_metadata(config_dir: Path) -> dict[str, Any]:
    """Parse tenant YAML files and build metadata structure."""
    tenant_groups = {}
    tenant_metadata = {}
    tenant_configs = {}

    # Load all tenant YAML files
    for yaml_file in sorted(config_dir.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if data and "tenants" in data and isinstance(data["tenants"], dict):
                tenant_configs.update(data["tenants"])
        except Exception as e:
            print(f"WARNING: {yaml_file.name}: {e}", file=sys.stderr)

    # Process each tenant
    for tenant_name, tenant_config in sorted(tenant_configs.items()):
        if not isinstance(tenant_config, dict):
            continue

        # Extract metadata fields
        metadata = tenant_config.get("_metadata", {})
        environment = extract_environment(tenant_name, metadata)
        region = metadata.get("region", "") if isinstance(metadata, dict) else ""
        domain = metadata.get("domain", "") if isinstance(metadata, dict) else ""

        # Build tenant entry
        tenant_entry = {
            "environment": environment,
            "region": region,
            "tier": extract_tier(tenant_config),
            "domain": domain,
            "rule_packs": infer_rule_packs(tenant_config),
            "owner": extract_owner(tenant_config),
            "routing_channel": extract_routing_channel(tenant_config),
            "operational_mode": detect_operational_mode(tenant_config),
            "metric_count": count_metrics(tenant_config),
            "last_config_commit": get_git_head_commit(),
        }

        # Clean up empty fields
        for key in ["environment", "region", "domain"]:
            if not tenant_entry[key]:
                tenant_entry[key] = ""

        tenant_metadata[tenant_name] = tenant_entry

        # Track tenant groups by environment
        if environment:
            if environment not in tenant_groups:
                tenant_groups[environment] = {
                    "label": environment.capitalize(),
                    "tenants": [],
                }
            tenant_groups[environment]["tenants"].append(tenant_name)

    return {
        "_comment": "Auto-generated by generate_tenant_metadata.py — DO NOT EDIT",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "scripts/tools/dx/generate_tenant_metadata.py",
        "tenant_groups": tenant_groups,
        "tenant_metadata": tenant_metadata,
        "totals": {
            "tenants": len(tenant_metadata),
            "groups": len(tenant_groups),
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    """CLI entry point: 租戶元資料產生器."""
    parser = argparse.ArgumentParser(
        description="Generate tenant metadata from conf.d/ YAML files",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help=f"Config directory (default: {DEFAULT_CONFIG_DIR.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output file path (default: stdout)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="CI mode: exit 1 if metadata is outdated",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print JSON to stdout without writing file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Format output as pretty JSON",
    )
    args = parser.parse_args()

    # Validate config directory
    if not args.config_dir.exists():
        print(
            f"ERROR: Config directory not found: {args.config_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Build metadata
    data = build_tenant_metadata(args.config_dir)

    # Format output
    if args.check:
        data.pop("generated", None)

    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    # Handle output
    if args.dry_run or not args.output:
        print(content, end="")
        return

    if args.check:
        if not args.output.exists():
            print(
                f"ERROR: {args.output} does not exist. "
                f"Run without --check first.",
                file=sys.stderr,
            )
            sys.exit(1)

        existing = json.loads(args.output.read_text(encoding="utf-8"))
        existing.pop("generated", None)
        existing_str = json.dumps(existing, indent=2, ensure_ascii=False) + "\n"

        if existing_str != content:
            print(
                f"ERROR: {args.output} is outdated.",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"OK: {args.output} is up to date.")
        return

    # Write output file
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    os.chmod(
        args.output,
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
    )

    try:
        display_path = args.output.relative_to(REPO_ROOT)
    except ValueError:
        display_path = args.output
    print(f"✅ Generated {display_path}")
    print(f"   {data['totals']['tenants']} tenants, {data['totals']['groups']} groups")


if __name__ == "__main__":
    main()
