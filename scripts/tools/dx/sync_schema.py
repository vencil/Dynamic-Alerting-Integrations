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

The schema also CLOSES the `_` namespace (#3016 R3): `tenantConfig.propertyNames`
rejects any `_` key that is not a reserved key, a tenant routing key
(`_grar_validate.TENANT_ROUTING_KEYS`) or under the open `_state_` prefix, so an
editor flags `_routng` as you type. That subschema is GENERATED from the Python
SSOT by `--update`, and `--check` reports it stale like any other drift.

Usage:
  sync_schema.py [--go-source PATH] [--schema PATH] [--py-source PATH]
                 [--routing-source PATH] [--check | --update]

Options:
  --go-source PATH       Go source directory (default: components/threshold-exporter/app/)
  --schema PATH          Schema file path (default: docs/schemas/tenant-config.schema.json)
  --py-source PATH       Python reserved-key SSOT (default: scripts/tools/_lib_constants.py)
  --routing-source PATH  TENANT_ROUTING_KEYS (default: scripts/tools/ops/_grar_validate.py)
  --check                Exit 1 if drift detected (for CI)
  --update               Rewrite tenantConfig.propertyNames from the SSOT
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
        # Segments are matched relative to go_dir: a Go tree checked out
        # under e.g. .../vendor/ must not have every file excluded (#1810).
        _excluded_dirs = {"vendor", "testdata", "mocks"}
        config_file = next(
            (p for p in sorted(go_dir.rglob("*.go"))
             if not p.name.endswith("_test.go")
             and not _excluded_dirs & set(p.relative_to(go_dir).parts)
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


def _frozenset_literal(node):
    """`frozenset({...})` / `frozenset([...])` → its str constants, else []."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "frozenset" and len(node.args) == 1):
        return _string_literals(node.args[0])
    return _string_literals(node)


def extract_tenant_routing_keys(path):
    """`TENANT_ROUTING_KEYS` from `scripts/tools/ops/_grar_validate.py`.

    The `_routing`-prefixed names the route generator reads in a TENANT's
    config (#2983). Read with `ast` like `extract_python_keys`, so this tool
    does not import the route generator. Missing or non-literal → empty set,
    which the propertyNames check then reports as drift.
    """
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(path))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "TENANT_ROUTING_KEYS"
                        for t in node.targets)):
            return set(_frozenset_literal(node.value))
    return set()


PROPERTY_NAMES_COMMENT = (
    "GENERATED by scripts/tools/dx/sync_schema.py --update from "
    "_lib_constants.VALID_RESERVED_KEYS / VALID_RESERVED_PREFIXES and "
    "_grar_validate.TENANT_ROUTING_KEYS — DO NOT EDIT. Closes the `_` "
    "namespace (#3016 R3): a `_`-prefixed key is either one the platform "
    "reads, or a typo nothing reads.")


def expected_property_names(reserved_keys, reserved_prefixes, tenant_routing_keys):
    """The `tenantConfig.propertyNames` subschema the SSOT implies.

    A `_`-prefixed tenant key must be a reserved key, a tenant routing key, or
    start with an OPEN reserved prefix. A prefix is CLOSED when the generator
    names exactly which keys under it it reads — `_routing` is (#2983), so
    `_routing_profle` and the platform-only `_routing_defaults` /
    `_routing_enforced` are rejected. `_state_` stays open: its suffix is a
    platform `state_filters` name, which this file cannot know (and a
    misspelled one is reported as unserved by `da-guard served-values`).
    """
    closed = {p for p in reserved_prefixes
              if any(k.startswith(p) for k in tenant_routing_keys)}
    keys = sorted(set(reserved_keys) | set(tenant_routing_keys))
    open_prefixes = sorted(p for p in reserved_prefixes if p not in closed)
    alts = ["^(?!_)", "^(?:" + "|".join(re.escape(k) for k in keys) + ")$"]
    if open_prefixes:
        alts.append("^(?:" + "|".join(re.escape(p) for p in open_prefixes) + ")")
    allowed = ", ".join(keys + [f"{p}<name>" for p in open_prefixes])
    return {
        "$comment": PROPERTY_NAMES_COMMENT,
        "pattern": "|".join(alts),
        "patternErrorMessage": (
            "A key starting with '_' must be one the platform reads: "
            f"{allowed}. Any other '_' key is read by nothing."),
    }


def splice_property_names(text, expected):
    """`text` (the schema file) with `tenantConfig.propertyNames` set to
    `expected`, every other byte unchanged.

    ⛔ Not `json.dump` of the whole document: the file is hand-formatted
    (inline arrays, literal `—`), and a re-serialisation rewrites hundreds of
    unrelated lines. Raises ValueError when `tenantConfig` is not found
    exactly once.
    """
    anchor = '"tenantConfig": {'
    if text.count(anchor) != 1:
        raise ValueError(f"expected exactly one {anchor!r} in the schema")
    start = text.index(anchor) + len(anchor) - 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    line_start = text.rindex("\n", 0, start) + 1
    indent = " " * (len(text[line_start:start]) - len(text[line_start:start].lstrip()) + 2)
    body = json.dumps(expected, indent=2, ensure_ascii=False).replace("\n", "\n" + indent)
    key = '"propertyNames": '
    # A DIRECT child only (its own indent): a nested object's propertyNames
    # inside tenantConfig must never be the one replaced.
    at = text.find("\n" + indent + key, start, end)
    if at != -1:
        at += 1 + len(indent)
        _, vend = json.JSONDecoder().raw_decode(text, at + len(key))
        return text[:at + len(key)] + body + text[vend:]
    return text[:start + 1] + "\n" + indent + key + body + "," + text[start + 1:]


def check_property_names(schema, expected):
    """Drift lines for `tenantConfig.propertyNames` (empty = in sync).

    Two ways to drift: the committed subschema is not the one the SSOT
    implies, or the schema still DEFINES a `_` property that the pattern
    rejects (a property no tenant file could ever carry).
    """
    tc = schema.get("definitions", {}).get("tenantConfig", {})
    out = []
    if tc.get("propertyNames") != expected:
        out.append("tenantConfig.propertyNames is stale vs the SSOT "
                   "(run sync_schema.py --update)")
    pat = re.compile(expected["pattern"])
    for key in sorted(tc.get("properties", {})):
        if not pat.search(key):
            out.append(f"tenantConfig.properties.{key} is a key propertyNames rejects")
    return out


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


def print_drift_report(missing_in_schema, extra_in_schema, py_go_key_drift, py_go_prefix_drift,
                       property_names_drift=()):
    """Print the 3-way drift report. Returns True if ANY drift was found."""
    has_drift = False

    if property_names_drift:
        print("DRIFT: the closed `_` namespace (tenantConfig.propertyNames):")
        for line in property_names_drift:
            print(f"  - {line}")
        has_drift = True

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
        "--routing-source",
        default="scripts/tools/ops/_grar_validate.py",
        help="TENANT_ROUTING_KEYS source (default: scripts/tools/ops/_grar_validate.py)"
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
    routing_source = Path(args.routing_source).resolve()

    if not go_source.exists():
        print(f"ERROR: Go source directory not found: {go_source}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if not schema_path.exists():
        print(f"ERROR: Schema file not found: {schema_path}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if not py_source.exists():
        print(f"ERROR: Python reserved-key source not found: {py_source}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if not routing_source.exists():
        print(f"ERROR: tenant routing-key source not found: {routing_source}", file=sys.stderr)
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

    expected = expected_property_names(
        py_keys, py_prefixes, extract_tenant_routing_keys(routing_source))
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    if args.update and schema.get("definitions", {}).get(
            "tenantConfig", {}).get("propertyNames") != expected:
        # The one surface this tool writes; the diff is exactly the subschema.
        with open(schema_path, "r", encoding="utf-8") as f:
            text = f.read()
        with open(schema_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(splice_property_names(text, expected))
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        print(f"\nUPDATED: tenantConfig.propertyNames in {schema_path}")
    property_names_drift = check_property_names(schema, expected)

    print(f"\nDrift analysis:")
    has_drift = print_drift_report(missing, extra, py_go_key_drift, py_go_prefix_drift,
                                   property_names_drift)

    if has_drift:
        if args.check:
            print("\nCI MODE: Exiting with code 1 due to schema drift")
            sys.exit(EXIT_VIOLATION)
        elif args.update:
            print("\nUPDATE MODE: only tenantConfig.propertyNames is written; "
                  "the drift above needs a hand edit")
            sys.exit(EXIT_OK)
        else:
            print("\nRun with --check for CI mode or --update to fix")
            sys.exit(EXIT_OK)
    else:
        sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
