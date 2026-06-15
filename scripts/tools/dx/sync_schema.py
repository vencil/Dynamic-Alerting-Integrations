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
import os
import re
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402


def extract_go_keys(go_source_path):
    """Extract valid tenant config keys from Go source files."""
    go_dir = Path(go_source_path)

    # validReservedKeys / validReservedPrefixes live in pkg/config/types.go.
    # This previously read `config.go` (where the map does NOT live), so both
    # regexes matched nothing → extract returned empty → check_drift would then
    # flag every real schema key as drift (false-positive, exit 1) if --check
    # ever ran; but its only caller, the manual-stage `schema-check` hook, never
    # ran in CI — so the gate was dead both ways (#841 review). Read the right
    # file, with a content-based fallback so a future move can't silently
    # re-break it the same way.
    config_file = go_dir / "pkg" / "config" / "types.go"
    if not config_file.exists():
        # Production-source only: a `_test.go` or a mock/testdata/vendor copy that
        # (re)declares `var validReservedKeys` must never become the authoritative
        # source — otherwise the schema would get synced to a dummy test key.
        _excluded_dirs = {"vendor", "testdata", "mocks"}
        config_file = next(
            (p for p in sorted(go_dir.rglob("*.go"))
             if not p.name.endswith("_test.go")
             and not _excluded_dirs & set(p.parts)
             and "var validReservedKeys" in p.read_text(encoding="utf-8", errors="ignore")),
            None,
        )
        if config_file is None:
            print(f"ERROR: could not find 'var validReservedKeys' under {go_dir}",
                  file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

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
    """Check for drift between the Go reserved keys and the JSON schema.

    A schema property is legitimate if it is an explicit Go reserved key OR it
    matches a Go reserved prefix (e.g. _state_maintenance ⊂ _state_, _routing*
    ⊂ _routing). The prefix allowance is derived from the actual ``go_prefixes``
    — not a hardcoded example list — so a newly added _state_*/_routing* schema
    key can't false-positive as drift.

    Returns ``(missing_in_schema, extra_in_schema)``:
      - missing_in_schema: explicit Go keys absent from the schema.
      - extra_in_schema: schema properties that are neither an explicit Go key
        nor covered by a Go prefix (e.g. the removed schema-only ``_operator``).
    """
    missing_in_schema = go_keys - schema_keys
    extra_in_schema = {
        key for key in schema_keys
        if key not in go_keys and not any(key.startswith(p) for p in go_prefixes)
    }
    return missing_in_schema, extra_in_schema


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
        sys.exit(EXIT_CALLER_ERROR)

    if not schema_path.exists():
        print(f"ERROR: Schema file not found: {schema_path}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

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
            sys.exit(EXIT_VIOLATION)
        elif args.update:
            print("\nUPDATE MODE: Would update schema (not yet implemented)")
            sys.exit(EXIT_OK)
        else:
            print("\nRun with --check for CI mode or --update to fix")
            sys.exit(EXIT_OK)
    else:
        sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
