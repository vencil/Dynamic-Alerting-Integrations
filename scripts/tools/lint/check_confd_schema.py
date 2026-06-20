#!/usr/bin/env python3
"""check_confd_schema.py — validate conf.d tenant YAML against the tenant-config JSON Schema (#880).

WHY (the silent-failure this shifts left):
  The threshold-exporter opts a tenant INTO per-tenant exporter liveness only when
  its _metadata declares a db_type (collector.go collectTenantExpectedExporter,
  #869). A mistyped KEY (dbType / db_typ) or VALUE (maraidb) makes the resolver see
  DBType=="" and silently drop the tenant from liveness — the exact silent-removal
  class #869 set out to kill. docs/schemas/tenant-config.schema.json now models the
  _metadata block as additionalProperties:false + a db_type enum, so this lint catches
  both typo shapes at author/CI time instead of in production (#880 Day-3 hardening).

SCOPE:
  Validates every TENANT-shaped conf.d file (filename NOT starting with "_") against
  the schema. The schema's top-level `required: [tenants]` also flags a tenant file
  that forgot its `tenants:` wrapper. Meta-files (_defaults* / _routing_profiles /
  _domain_policy / _instance_mapping ... — basename starts with "_") have their own
  shapes and validators (check_routing_profiles.py); they are SKIPPED and listed
  explicitly so coverage is never silently capped.

Exit codes (scripts/tools/_lib_exitcodes.py):
  0  all tenant files valid
  1  >=1 schema violation (user fixes the YAML or the schema)
  2  bad invocation / unreadable schema / jsonschema missing / malformed YAML

Usage:
  python3 check_confd_schema.py --config-dir components/threshold-exporter/config/conf.d
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

# Repo-root-relative default: lint -> tools -> scripts -> <root>/docs/schemas/...
_DEFAULT_SCHEMA = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "docs", "schemas", "tenant-config.schema.json")
)


class _CallerError(Exception):
    """Environment/invocation failure → EXIT_CALLER_ERROR (2)."""


def _iter_yaml_files(config_dir: str) -> list[str]:
    out: list[str] = []
    for root, _dirs, files in os.walk(config_dir):
        for fn in files:
            if fn.endswith((".yaml", ".yml")) and not fn.startswith("."):
                out.append(os.path.join(root, fn))
    return sorted(out)


def validate_dir(config_dir: str, schema: dict, validator) -> tuple[int, list[str], list[str]]:
    """Return (checked_count, violation_messages, skipped_relpaths).

    `validator` is the jsonschema module (injected so the import stays lazy — the
    CI exit-code gate runs `--help` in an env that may not have jsonschema, so a
    module-level import would crash --help and fail the gate).
    """
    violations: list[str] = []
    skipped: list[str] = []
    checked = 0
    for path in _iter_yaml_files(config_dir):
        rel = os.path.relpath(path, config_dir).replace(os.sep, "/")
        if os.path.basename(path).startswith("_"):
            skipped.append(rel)
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                docs = list(yaml.safe_load_all(fh))
        except (OSError, yaml.YAMLError) as exc:
            # Unreadable file or malformed YAML is an environment/caller error, not
            # a schema violation — surface it as exit 2 (open() can raise OSError
            # too, not only yaml.YAMLError).
            raise _CallerError(f"{rel}: cannot read/parse YAML: {exc}")
        for doc in docs:
            if not isinstance(doc, dict):
                # A tenant-shaped file (no `_` prefix) whose top document is a
                # list / scalar / empty (None) is malformed — flag it instead of
                # silently skipping, or it would escape this hardening gate
                # entirely (the schema below is only applied to mappings).
                violations.append(
                    f"ERROR: {rel}: top-level YAML document must be a mapping with a "
                    f"`tenants:` block (got {type(doc).__name__})")
                continue
            checked += 1
            try:
                validator.validate(doc, schema)
            except validator.ValidationError as exc:
                loc = "/".join(str(p) for p in exc.absolute_path)
                violations.append(f"ERROR: {rel}: {exc.message} @ /{loc}")
    return checked, violations, sorted(set(skipped))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate conf.d tenant YAML against tenant-config.schema.json (#880)")
    parser.add_argument("--config-dir", required=True,
                        help="conf.d directory to scan (recurses into examples/)")
    parser.add_argument("--schema", default=_DEFAULT_SCHEMA,
                        help="JSON Schema path (default: docs/schemas/tenant-config.schema.json)")
    parser.add_argument("--ci", action="store_true",
                        help="CI mode (accepted for symmetry with sibling lints; no behaviour change)")
    args = parser.parse_args()

    if not os.path.isdir(args.config_dir):
        print(f"ERROR: config-dir not found: {args.config_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        with open(args.schema, encoding="utf-8") as fh:
            schema = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot load schema {args.schema}: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    # Lazy import: keep --help / invalid-args (the exit-code gate) working in a
    # jsonschema-less env.
    try:
        import jsonschema
    except ImportError:
        print("ERROR: jsonschema not installed — `pip install jsonschema` "
              "(pre-commit injects it via additional_dependencies).", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        checked, violations, skipped = validate_dir(args.config_dir, schema, jsonschema)
    except _CallerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if skipped:
        print(f"skipped {len(skipped)} meta-file(s) not modelled by the tenant-config "
              f"schema (own shape/validator): {', '.join(skipped)}")
    if violations:
        for msg in violations:
            print(msg, file=sys.stderr)
        print(f"\n{len(violations)} schema violation(s) across {checked} tenant file(s). "
              f"Fix the conf.d YAML, or docs/schemas/tenant-config.schema.json if the "
              f"schema is wrong.", file=sys.stderr)
        return EXIT_VIOLATION

    print(f"OK: {checked} tenant conf.d file(s) valid against tenant-config.schema.json")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
