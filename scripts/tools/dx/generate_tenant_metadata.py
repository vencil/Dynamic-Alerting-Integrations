#!/usr/bin/env python3
"""租戶元資料產生器 — 讀 exporter 實際發出的值（da-guard served-values），推斷 rule_packs、owner、tier、routing_channel。

The tenants and every value read here come from `da-guard served-values`
(#2115 0-B/B4), so the tool needs da-guard: `$DA_GUARD_BINARY`, else
`da-guard` on `$PATH` (in a checkout, `make da-guard-build` builds it to
.build/da-guard). da-guard missing or failing, or a file the exporter's load
drops, exits 2 with da-guard's stderr below the ERROR line.

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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import (  # noqa: E402  (#2137 one presence rule)
    FIELD_NOT_STRING,
    FIELD_SET,
    receiver_field_state,
)
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_yaml_keys import load_exporter_keys  # noqa: E402  (#2216 tenant id as text)
from _lib_tenant_values import (  # noqa: E402  (#2115 0-B/B4: the exporter's answer)
    exit_on_served_values_error,
    load_served_tree,
    print_load_warnings,
)
from _lib_io import (  # noqa: E402  (#1789)
    exit_on_output_write_error,
    exit_on_yaml_file_error,
    output_write,
)
from _atomic_write import atomic_write_text  # noqa: E402  (#2082 → #2128)

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
    """Infer rule packs from metric key prefixes.

    `tenant_config` is the tenant's served values (`TenantValues.values`), so
    a key inherited from `defaults:` counts as if the tenant had written it
    (#2115 0-B/B4); a key /metrics does not serve (switched off, no default)
    is not there."""
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


def extract_db_type(tenant_config: dict, metadata: dict) -> str:
    """Extract db_type from _metadata or infer from metric prefixes.

    The inference takes the first served key, in sorted order, whose prefix
    names a database (#2115 0-B/B4: served keys include inherited ones, and
    sorting keeps the answer independent of the JSON's key order)."""
    if isinstance(metadata, dict):
        db_type = metadata.get("db_type", "")
        if db_type:
            return db_type

    # Infer from metric key prefixes
    db_prefixes = {
        "mysql_": "mariadb",
        "mariadb_": "mariadb",
        "pg_": "postgresql",
        "postgres_": "postgresql",
        "redis_": "redis",
        "mongo_": "mongodb",
        "mongodb_": "mongodb",
        "kafka_": "kafka",
        "rabbitmq_": "rabbitmq",
        "elasticsearch_": "elasticsearch",
        "oracle_": "oracle",
        "clickhouse_": "clickhouse",
    }
    for key in sorted(tenant_config.keys()):
        if key.startswith("_"):
            continue
        metric = key.split("[")[0] if "[" in key else key
        for prefix, db in db_prefixes.items():
            if metric.startswith(prefix):
                return db
    return ""


def extract_tags(metadata: dict) -> list[str]:
    """Extract tags from _metadata.tags if present."""
    if isinstance(metadata, dict):
        tags = metadata.get("tags", [])
        if isinstance(tags, list):
            return [str(t) for t in tags]
    return []


def extract_groups(metadata: dict) -> list[str]:
    """Extract group memberships from _metadata.groups if present."""
    if isinstance(metadata, dict):
        grps = metadata.get("groups", [])
        if isinstance(grps, list):
            return [str(g) for g in grps]
    return []


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
        # The keys are credentials: name the Events API version, never the
        # key. service_key wins when both are set, as it does in Alertmanager.
        # Presence is the routing pipeline's rule (receiver_field_state). A
        # non-string key is a config error the pipeline rejects, so the
        # version cannot be told: "" like every other undeterminable channel.
        sk = receiver_field_state(receiver, "service_key")
        rk = receiver_field_state(receiver, "routing_key")
        if FIELD_NOT_STRING in (sk, rk):
            return ""
        if sk == FIELD_SET:
            return f"{recv_type}:v1"
        if rk == FIELD_SET:
            return f"{recv_type}:v2"
        return ""

    return ""


def detect_operational_mode(tenant_config: dict) -> str:
    """Detect operational mode: normal (default), silent, or maintenance.

    Reads the served values (#2115 0-B/B4): `_state_maintenance` is the
    exporter's verdict, `true` while maintenance is on (an expired time-box
    already reads `false`); `_silent_mode` is the list of severities it
    silences, empty when off."""
    if tenant_config.get("_state_maintenance") is True:
        return "maintenance"
    if tenant_config.get("_silent_mode"):
        return "silent"
    return "normal"


def count_metrics(tenant_config: dict) -> int:
    """Count non-reserved metric keys: the threshold keys /metrics serves
    for the tenant, inherited ones included (#2115 0-B/B4)."""
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
            text=True, encoding="utf-8", errors="replace",
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
    """Build the metadata structure from what the exporter serves.

    #2115 0-B/B4: the tenants, their `_metadata` and every key the inference
    reads are `da-guard served-values` (/metrics), not this tool's own read
    of the YAML: tenants in sub-directories and in a root platform file's
    `tenants:`, `_metadata` inherited there, threshold keys inherited from
    `defaults:` — all as the exporter resolves them. Nothing is merged here.

    Raises what `load_served_tree` raises (da-guard missing or failing, a file
    the exporter's load drops); a caller must not turn that into an empty
    result. Files the load serves no tenant from, and every line da-guard
    wrote to stderr, are printed to stderr (`print_load_warnings`).
    """
    tenant_groups = {}
    tenant_metadata = {}

    tree = load_served_tree(config_dir)
    print_load_warnings(tree)
    tenant_configs = {tid: tv.values for tid, tv in tree.tenants.items()}

    # Process each tenant
    for tenant_name, tenant_config in sorted(tenant_configs.items()):
        if not isinstance(tenant_config, dict):
            continue

        # Extract metadata fields
        metadata = tenant_config.get("_metadata", {})
        environment = extract_environment(tenant_name, metadata)
        region = metadata.get("region", "") if isinstance(metadata, dict) else ""
        domain = metadata.get("domain", "") if isinstance(metadata, dict) else ""

        # v2.5.0: extract extended metadata fields
        db_type = extract_db_type(tenant_config, metadata)
        tags = extract_tags(metadata)
        group_memberships = extract_groups(metadata)

        # Build tenant entry
        tenant_entry = {
            "environment": environment,
            "region": region,
            "tier": extract_tier(tenant_config),
            "domain": domain,
            "db_type": db_type,
            "rule_packs": infer_rule_packs(tenant_config),
            "owner": extract_owner(tenant_config),
            "routing_channel": extract_routing_channel(tenant_config),
            "operational_mode": detect_operational_mode(tenant_config),
            "metric_count": count_metrics(tenant_config),
            "last_config_commit": get_git_head_commit(),
            "tags": tags,
            "groups": group_memberships,
        }

        # Clean up empty fields
        for key in ["environment", "region", "domain", "db_type"]:
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

    # v2.5.0: Load custom groups from _groups.yaml if it exists
    custom_groups = _load_custom_groups(config_dir)

    # v2.5.0: Build multi-dimension group views
    dimension_groups = _build_dimension_groups(tenant_metadata)

    return {
        "_comment": "Auto-generated by generate_tenant_metadata.py — DO NOT EDIT",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "scripts/tools/dx/generate_tenant_metadata.py",
        "tenant_groups": tenant_groups,
        "custom_groups": custom_groups,
        "dimension_groups": dimension_groups,
        "tenant_metadata": tenant_metadata,
        "totals": {
            "tenants": len(tenant_metadata),
            "groups": len(tenant_groups),
        },
    }


def _load_custom_groups(config_dir: Path) -> dict:
    """Load custom groups from _groups.yaml if it exists."""
    groups_file = config_dir / "_groups.yaml"
    if not groups_file.exists():
        return {}

    try:
        # #2216: `members:` lists tenant ids, read as their source text —
        # `members: [010]` names tenant "010", not 8.
        data = load_exporter_keys(groups_file.read_text(encoding="utf-8"),
                                  raw_text_sequences=("members",))
        if data and isinstance(data.get("groups"), dict):
            result = {}
            for gid, gdef in data["groups"].items():
                if isinstance(gdef, dict):
                    result[gid] = {
                        "label": gdef.get("label", gid),
                        "description": gdef.get("description", ""),
                        "members": gdef.get("members", []),
                    }
            return result
        return {}
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: failed to parse _groups.yaml: {safe_label(exc)}",
              file=sys.stderr)
        return {}


def _build_dimension_groups(tenant_metadata: dict[str, dict]) -> dict:
    """Build multi-dimension group views from tenant metadata.

    Produces three dimension views: by-domain, by-db_type, by-region.
    Each view maps dimension value → list of tenant IDs.
    """
    dimensions = {
        "by_domain": "domain",
        "by_db_type": "db_type",
        "by_region": "region",
    }
    result: dict[str, dict[str, list[str]]] = {}

    for dim_key, field in dimensions.items():
        groups: dict[str, list[str]] = {}
        for tenant_id, meta in tenant_metadata.items():
            value = meta.get(field, "")
            if value:
                groups.setdefault(value, []).append(tenant_id)
        # Sort member lists for deterministic output
        for members in groups.values():
            members.sort()
        result[dim_key] = dict(sorted(groups.items()))

    return result


# ---------------------------------------------------------------------------
# --output: atomic replace (#2082) — the shared helper since #2128
# ---------------------------------------------------------------------------
# The #2082 writer that lived here (realpath, private mkstemp tmp, owner /
# mode / xattr carry-over, in-place fallback + WARN for hard links and
# unwritable directories) moved into `_atomic_write.atomic_write_text` so
# there is one implementation; this tool passes its flag so an error still
# ends "check the value given to --output".


def _read_existing_for_check(out: Path) -> tuple[dict | None, str | None]:
    """``(metadata, None)`` or ``(None, why it cannot be compared)`` (#2082).

    Every way the ``--output`` file can be unusable — half-written JSON, a
    non-object top level, non-UTF-8 bytes, a directory, an unreadable file —
    becomes a reason string instead of a traceback, so ``--check`` can tell
    "damaged" apart from "outdated".
    """
    try:
        raw = out.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return None, f"not UTF-8 ({exc.reason} at byte {exc.start})"
    except OSError as exc:
        return None, f"cannot be read ({exc.strerror or exc})"
    try:
        existing = json.loads(raw)
    except ValueError as exc:
        return None, f"not valid JSON ({exc})"
    except RecursionError:
        return None, "not usable JSON (nested too deeply to parse)"
    if not isinstance(existing, dict):
        kind = {list: "an array", str: "a string", bool: "a boolean",
                int: "a number", float: "a number", type(None): "null",
                }.get(type(existing), type(existing).__name__)
        return None, f"top level is {kind}, expected a JSON object"
    return existing, None


@exit_on_yaml_file_error  # #1654: any other unreadable YAML → rc 2, named
@exit_on_served_values_error  # #2115 0-B/B4: da-guard missing or failing, a dropped file → rc 2, named
@exit_on_output_write_error
def main():
    """CLI entry point: 租戶元資料產生器."""
    try_utf8_stdout()
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
        help="CI mode: exit 1 if metadata is outdated, "
             "exit 2 if the --output file is damaged or unreadable",
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
        sys.exit(EXIT_CALLER_ERROR)

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
            sys.exit(EXIT_VIOLATION)

        existing, damaged = _read_existing_for_check(args.output)
        if damaged is not None:
            # #2082: rc 2, not the "outdated" rc 1 — the file is not an older
            # version of this output, it is not one at all, and a CI consumer
            # must not read a truncated file as ordinary drift.
            print(
                f"ERROR: {safe_label(str(args.output))} is damaged: {damaged}. "
                f"Regenerate it by re-running without --check.",
                file=sys.stderr,
            )
            sys.exit(EXIT_CALLER_ERROR)
        existing.pop("generated", None)
        existing_str = json.dumps(existing, indent=2, ensure_ascii=False) + "\n"

        if existing_str != content:
            print(
                f"ERROR: {args.output} is outdated.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VIOLATION)

        print(f"OK: {args.output} is up to date.")
        return

    # Write output file
    # #1789: the wrapper is given the PARENT — the directory this mkdir
    # actually creates. Handing it the output FILE makes the message a
    # sentence about a path nobody was creating (worked example in
    # `_lib_io.output_write`). The ancestor rule still converts the
    # failure, and the write below keeps naming the file.
    with output_write(args.output.parent, flag="--output",
                      action="create directory"):
        args.output.parent.mkdir(parents=True, exist_ok=True)
    # #2082: atomic replace (0644 set on the tmp before the rename), so a
    # failed run leaves the previous file whole. It converts its own
    # failures to OutputWriteError; the wrapper stays as the #1789 guard for
    # anything it does not, and is what the static pin looks for.
    with output_write(args.output, flag="--output"):
        atomic_write_text(args.output, content, flag="--output")

    try:
        display_path = args.output.relative_to(REPO_ROOT)
    except ValueError:
        display_path = args.output
    print(f"✅ Generated {display_path}")
    print(f"   {data['totals']['tenants']} tenants, {data['totals']['groups']} groups")


if __name__ == "__main__":
    main()
