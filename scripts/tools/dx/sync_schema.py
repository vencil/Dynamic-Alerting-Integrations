#!/usr/bin/env python3
"""
sync_schema.py — Check tenant reserved-key drift across Schema / Go / Python.

The tenant reserved-key allowlist is declared THREE times and the three MUST
agree, or one surface flags a shipped key as a typo (or accepts a key the others
reject):

  - JSON Schema : docs/schemas/tenant-config.schema.json (definitions.tenantConfig)
  - Go          : components/threshold-exporter/app/pkg/config/types.go
                  (validReservedKeys / validReservedPrefixes)
  - Python      : scripts/tools/_lib_constants.py
                  (VALID_RESERVED_KEYS / VALID_RESERVED_PREFIXES)

This tool reads all three and reports drift in ONE place (the explicit 3-way
gate #658 asked for). Two pairwise pytest gates already enforce the edges in CI —
tests/dx/test_sync_schema.py (Schema↔Go) and
tests/shared/test_reserved_key_py_go_parity.py (Go↔Python) — so the triangle was
already closed by transitivity; checking Schema↔Python HERE removes that
transitive reliance (a disabled edge no longer silently opens the Schema↔Python
gap) and surfaces a Python-only drift in the same report as the Schema/Go drift.

Usage:
  sync_schema.py [--go-source PATH] [--schema PATH] [--py-source PATH] [--check | --update]

Options:
  --go-source PATH     Go source directory (default: components/threshold-exporter/app/)
  --schema PATH        Schema file path (default: docs/schemas/tenant-config.schema.json)
  --py-source PATH     Python reserved-key SSOT (default: scripts/tools/_lib_constants.py)
  --check              Exit 1 if drift detected (for CI)
  --update             Auto-update schema file (if drift found)
"""

import argparse
import ast
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

    # Strip Go `//` line comments first: a key deprecated by commenting it out
    # (`// "_old": true,`) is GONE for the compiler, so the gate must agree, and
    # a stray `}` inside a comment must not truncate the brace-matched block.
    # Mirrors tests/shared/test_reserved_key_py_go_parity.py's _strip_go_line_comments.
    content = re.sub(r"//.*", "", content)

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


def _string_literals(node):
    """Return the str constants of a set/list/tuple literal AST node, in order.

    Non-literal forms (a comprehension, a name reference, a call) have no `.elts`
    of plain string constants → returns [] (caller treats as empty, the 3-way
    gate then fail-louds the resulting drift so a maintainer notices).
    """
    if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return []
    return [
        elt.value for elt in node.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    ]


def extract_python_keys(py_source_path):
    """Extract VALID_RESERVED_KEYS / VALID_RESERVED_PREFIXES from the Python SSOT.

    Uses the stdlib `ast` module, NOT regex — the source IS Python, so the
    compiler's own parser gives 100% accuracy immune to `#` comments (incl.
    quoted tokens inside them, e.g. _lib_constants.py's `component="custom"`),
    multi-line literals, and reformatting. (extract_go_keys must stay regex —
    there's no Go AST in Python; here Python parses Python.) A SyntaxError in the
    SSOT is intentionally left to propagate — that file is imported app-wide, so
    a malformed edit is already caught by every other test.

    Scans MODULE-LEVEL statements only (`tree.body`), not `ast.walk` — the SSOT
    is a module-level constant, and walking every nested node would let a
    function-local `VALID_RESERVED_KEYS = {...}` decoy shadow it (adversarial
    review NIT-1). Non-literal / augmented (`|=`) forms aren't matched → return
    empty → the 3-way gate fail-louds the drift.
    """
    path = Path(py_source_path)
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(path))

    reserved_keys = set()
    reserved_prefixes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            names = {node.target.id}  # `X: Final[...] = {...}`
            value = node.value
        else:
            continue
        if "VALID_RESERVED_KEYS" in names:
            reserved_keys = set(_string_literals(value))
        if "VALID_RESERVED_PREFIXES" in names:
            reserved_prefixes = _string_literals(value)

    return reserved_keys, reserved_prefixes


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


def check_py_go_parity(go_keys, go_prefixes, py_keys, py_prefixes):
    """Check the Go↔Python leg of the triangle.

    Unlike Schema↔Go (where the schema legitimately carries prefix-EXPANDED keys
    such as _state_maintenance), the Python and Go allowlists are the SAME shape —
    a flat key set + a prefix list — so they must be byte-for-byte equal.

    Returns ``(key_drift, prefix_drift)`` as symmetric differences (empty = OK).
    """
    key_drift = set(py_keys) ^ set(go_keys)
    prefix_drift = set(py_prefixes) ^ set(go_prefixes)
    return key_drift, prefix_drift


def print_drift_report(missing_in_schema, extra_in_schema, py_go_key_drift, py_go_prefix_drift):
    """Print the 3-way drift report. Returns True if ANY drift was found."""
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

    if py_go_key_drift:
        print("DRIFT: Reserved KEYS differ between Go and Python (_lib_constants.py):")
        for key in sorted(py_go_key_drift):
            print(f"  - {key}")
        has_drift = True

    if py_go_prefix_drift:
        print("DRIFT: Reserved PREFIXES differ between Go and Python (_lib_constants.py):")
        for key in sorted(py_go_prefix_drift):
            print(f"  - {key}")
        has_drift = True

    if not has_drift:
        print("OK: Schema, Go, and Python reserved keys are in sync (3-way)")

    return has_drift


def main():
    """CLI entry point: check Schema/Go/Python reserved-key drift (3-way)."""
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
        "--py-source",
        default="scripts/tools/_lib_constants.py",
        help="Python reserved-key SSOT (default: scripts/tools/_lib_constants.py)"
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
    py_source = Path(args.py_source).resolve()

    if not go_source.exists():
        print(f"ERROR: Go source directory not found: {go_source}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if not schema_path.exists():
        print(f"ERROR: Schema file not found: {schema_path}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if not py_source.exists():
        print(f"ERROR: Python reserved-key source not found: {py_source}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # Extract keys
    print(f"Reading Go source from {go_source}...")
    go_keys, go_prefixes = extract_go_keys(go_source)
    print(f"  Found {len(go_keys)} reserved keys: {sorted(go_keys)}")
    print(f"  Found {len(go_prefixes)} reserved prefixes: {go_prefixes}")

    print(f"\nReading schema from {schema_path}...")
    schema_keys = extract_schema_keys(schema_path)
    print(f"  Found {len(schema_keys)} schema properties: {sorted(schema_keys)}")

    print(f"\nReading Python reserved keys from {py_source}...")
    py_keys, py_prefixes = extract_python_keys(py_source)
    print(f"  Found {len(py_keys)} reserved keys: {sorted(py_keys)}")
    print(f"  Found {len(py_prefixes)} reserved prefixes: {py_prefixes}")

    # Check for drift across all three surfaces
    missing, extra = check_drift(go_keys, go_prefixes, schema_keys)
    py_go_key_drift, py_go_prefix_drift = check_py_go_parity(
        go_keys, go_prefixes, py_keys, py_prefixes)

    print(f"\nDrift analysis:")
    has_drift = print_drift_report(missing, extra, py_go_key_drift, py_go_prefix_drift)

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
