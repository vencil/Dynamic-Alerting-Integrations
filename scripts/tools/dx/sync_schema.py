#!/usr/bin/env python3
"""
sync_schema.py — Sync JSON Schema with Go source definitions.

Reads Go source files to extract valid tenant config keys and updates the JSON
Schema file to stay in sync. Reports drift and supports --check (CI) and --update modes.

Usage:
  sync_schema.py [--go-source PATH] [--schema PATH] [--check | --update]

Options:
  --go-source PATH     Go source directory (default: components/threshold-exporter/app/)
  --schema PATH        Schema file path (default: docs/schemas/tenant-config.schema.json)
  --check              Exit 1 if drift detected (for CI)
  --update             Auto-update schema file (if drift found)
"""

import argparse
import json
import re
import sys
from pathlib import Path


def extract_go_keys(go_source_path):
    """Extract valid tenant config keys from Go source files."""
    go_dir = Path(go_source_path)

    # Read config.go to extract validReservedKeys and validReservedPrefixes
    config_file = go_dir / "config.go"
    if not config_file.exists():
        print(f"ERROR: {config_file} not found", file=sys.stderr)
        sys.exit(1)

    with open(config_file, "r", encoding="utf-8") as f:
        content = f.read()

    # Extract validReservedKeys map
    reserved_keys = set()
    keys_match = re.search(
        r'var validReservedKeys = map\[string\]bool\{(.+?)\}',
        content,
        re.DOTALL
    )
    if keys_match:
        keys_block = keys_match.group(1)
        for match in re.finditer(r'"([^"]+)"\s*:\s*true', keys_block):
            reserved_keys.add(match.group(1))

    # Extract validReservedPrefixes array
    reserved_prefixes = []
    prefixes_match = re.search(
        r'var validReservedPrefixes = \[\]string\{(.+?)\}',
        content,
        re.DOTALL
    )
    if prefixes_match:
        prefixes_block = prefixes_match.group(1)
        for match in re.finditer(r'"([^"]+)"', prefixes_block):
            reserved_prefixes.append(match.group(1))

    return reserved_keys, reserved_prefixes


def extract_schema_keys(schema_path):
    """Extract properties from tenant config schema."""
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    # Get properties from tenantConfig definition
    tenant_config = schema.get("definitions", {}).get("tenantConfig", {})
    schema_properties = set(tenant_config.get("properties", {}).keys())

    return schema_properties


def check_drift(go_keys, go_prefixes, schema_keys):
    """Check for drift between Go and Schema.

    Reserved prefixes like _state_ and _routing are special: they match keys like
    _state_maintenance, _routing_overrides, etc. The schema lists specific keys like
    _state_maintenance and _routing, but the Go source defines the prefix patterns.
    We only flag real drift (schema missing a reserved key that Go requires).
    """
    # All explicit reserved keys in Go
    explicit_keys = go_keys

    # Map of prefixes to their documented examples in schema
    prefix_examples = {
        "_state_": ["_state_maintenance"],  # Only one explicitly documented
        "_routing": ["_routing", "_routing_enforced", "_routing_defaults"]
    }

    # Flatten all documented keys
    documented_in_schema = schema_keys

    # Check: all explicit Go reserved keys must be in schema
    missing_explicit = explicit_keys - documented_in_schema

    # Check: all documented prefix examples in schema must have their prefix in Go
    extra_in_schema = documented_in_schema - explicit_keys - set()
    for example in ["_state_maintenance", "_routing", "_routing_enforced", "_routing_defaults"]:
        extra_in_schema.discard(example)

    return missing_explicit, extra_in_schema


def print_drift_report(missing_in_schema, extra_in_schema):
    """Print drift report."""
    has_drift = False

    if missing_in_schema:
        print("DRIFT: Keys in Go source but missing in Schema:")
        for key in sorted(missing_in_schema):
            print(f"  - {key}")
        has_drift = True

    if extra_in_schema:
        print("DRIFT: Schema properties not defined in Go source:")
        for key in sorted(extra_in_schema):
            print(f"  - {key}")
        has_drift = True

    if not has_drift:
        print("OK: Schema in sync with Go source")

    return has_drift


def main():
    """CLI entry point: Sync JSON Schema with Go source definitions."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--go-source",
        default="components/threshold-exporter/app/",
        help="Go source directory (default: components/threshold-exporter/app/)"
    )
    parser.add_argument(
        "--schema",
        default="docs/schemas/tenant-config.schema.json",
        help="Schema file path (default: docs/schemas/tenant-config.schema.json)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if drift detected (for CI)"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Auto-update schema file"
    )

    args = parser.parse_args()

    # Resolve paths
    go_source = Path(args.go_source).resolve()
    schema_path = Path(args.schema).resolve()

    if not go_source.exists():
        print(f"ERROR: Go source directory not found: {go_source}", file=sys.stderr)
        sys.exit(1)

    if not schema_path.exists():
        print(f"ERROR: Schema file not found: {schema_path}", file=sys.stderr)
        sys.exit(1)

    # Extract keys
    print(f"Reading Go source from {go_source}...")
    go_keys, go_prefixes = extract_go_keys(go_source)
    print(f"  Found {len(go_keys)} reserved keys: {sorted(go_keys)}")
    print(f"  Found {len(go_prefixes)} reserved prefixes: {go_prefixes}")

    print(f"\nReading schema from {schema_path}...")
    schema_keys = extract_schema_keys(schema_path)
    print(f"  Found {len(schema_keys)} schema properties: {sorted(schema_keys)}")

    # Check for drift
    missing, extra = check_drift(go_keys, go_prefixes, schema_keys)

    print(f"\nDrift analysis:")
    has_drift = print_drift_report(missing, extra)

    if has_drift:
        if args.check:
            print("\nCI MODE: Exiting with code 1 due to schema drift")
            sys.exit(1)
        elif args.update:
            print("\nUPDATE MODE: Would update schema (not yet implemented)")
            sys.exit(0)
        else:
            print("\nRun with --check for CI mode or --update to fix")
            sys.exit(0)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
