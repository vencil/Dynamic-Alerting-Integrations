#!/usr/bin/env python3
"""check_confd_schema.py — validate conf.d tenant YAML + _defaults.yaml against their JSON Schemas (#880).

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
  tenant-config.schema.json. The schema's top-level `required: [tenants]` also flags a
  tenant file that forgot its `tenants:` wrapper. _defaults*.yaml validate against
  platform-defaults.schema.json (top-level-key guard — a typo like `state_flters`
  silently drops the whole platform-default block, the highest-blast-radius config;
  #658 fast-follow / Gemini #911 對抗3). All OTHER meta-files (_routing_profiles /
  _domain_policy / _instance_mapping / _rbac ... — basename starts with "_") have their
  own shapes and validators (check_routing_profiles.py); they are SKIPPED and listed
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
# #658 fast-follow / Gemini #911 對抗3: _defaults.yaml has its own (top-level-key
# strict, nested-loose) schema. Every OTHER `_`-prefixed meta-file keeps its own
# validator and is still skipped here.
_DEFAULT_PLATFORM_SCHEMA = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "docs", "schemas", "platform-defaults.schema.json")
)


def _is_defaults_file(basename: str) -> bool:
    """`_defaults.yaml` / `_defaults-multidb.yaml` … — the platform default files
    (top-level keys guarded by platform-defaults.schema.json). Other `_*` files
    (`_routing_profiles`, `_domain_policy`, `_instance_mapping`, `_rbac` …) have
    their own shapes/validators and remain skipped."""
    return basename.startswith("_defaults") and basename.endswith((".yaml", ".yml"))


class _CallerError(Exception):
    """Environment/invocation failure → EXIT_CALLER_ERROR (2)."""


def _iter_yaml_files(config_dir: str) -> list[str]:
    out: list[str] = []
    for root, _dirs, files in os.walk(config_dir):
        for fn in files:
            if fn.endswith((".yaml", ".yml")) and not fn.startswith("."):
                out.append(os.path.join(root, fn))
    return sorted(out)


def validate_dir(config_dir: str, schema: dict, validator,
                 platform_schema: dict | None = None) -> tuple[int, list[str], list[str]]:
    """Return (checked_count, violation_messages, skipped_relpaths).

    `validator` is the jsonschema module (injected so the import stays lazy — the
    CI exit-code gate runs `--help` in an env that may not have jsonschema, so a
    module-level import would crash --help and fail the gate).

    Tenant files (no `_` prefix) validate against `schema`; `_defaults*.yaml`
    validate against `platform_schema` (top-level-key guard, #658 fast-follow)
    when provided; all other `_*` meta-files are skipped (own validators).
    """
    violations: list[str] = []
    skipped: list[str] = []
    checked = 0
    for path in _iter_yaml_files(config_dir):
        rel = os.path.relpath(path, config_dir).replace(os.sep, "/")
        basename = os.path.basename(path)
        is_defaults = platform_schema is not None and _is_defaults_file(basename)
        if basename.startswith("_") and not is_defaults:
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
        active_schema = platform_schema if is_defaults else schema
        for doc in docs:
            if doc is None and is_defaults:
                # An empty / comment-only / explicit-`null` _defaults.yaml is
                # loader-LEGAL: hierarchy.go's extractDefaultsBlock returns nil for
                # a non-map defaults doc → no-op (a placeholder file is valid). Do
                # NOT flag it (the old skip-all-`_*` behaviour never did, and a
                # tenant file's `None` is still flagged below because it needs a
                # `tenants:` block). A LIST/scalar _defaults is still malformed.
                continue
            if not isinstance(doc, dict):
                # A tenant-shaped file (no `_` prefix) whose top document is a
                # list / scalar / empty (None) is malformed — flag it instead of
                # silently skipping, or it would escape this hardening gate
                # entirely (the schema below is only applied to mappings). A
                # `_defaults*.yaml` with a list/scalar top document is likewise flagged.
                kind = "`_defaults` platform file" if is_defaults else (
                    "tenant file with a `tenants:` block")
                violations.append(
                    f"ERROR: {rel}: top-level YAML document must be a mapping "
                    f"({kind}; got {type(doc).__name__})")
                continue
            checked += 1
            try:
                validator.validate(doc, active_schema)
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
                        help="Tenant JSON Schema path (default: docs/schemas/tenant-config.schema.json)")
    parser.add_argument("--platform-schema", default=_DEFAULT_PLATFORM_SCHEMA,
                        help="_defaults.yaml JSON Schema path "
                             "(default: docs/schemas/platform-defaults.schema.json)")
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

    try:
        with open(args.platform_schema, encoding="utf-8") as fh:
            platform_schema = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot load platform schema {args.platform_schema}: {exc}",
              file=sys.stderr)
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
        checked, violations, skipped = validate_dir(
            args.config_dir, schema, jsonschema, platform_schema)
    except _CallerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if skipped:
        print(f"skipped {len(skipped)} meta-file(s) not modelled by the tenant-config "
              f"or platform-defaults schema (own shape/validator): {', '.join(skipped)}")
    if violations:
        for msg in violations:
            print(msg, file=sys.stderr)
        print(f"\n{len(violations)} schema violation(s) across {checked} conf.d file(s). "
              f"Fix the conf.d YAML, or the schema (docs/schemas/tenant-config.schema.json "
              f"/ platform-defaults.schema.json) if the schema is wrong.", file=sys.stderr)
        return EXIT_VIOLATION

    print(f"OK: {checked} tenant conf.d file(s) valid against tenant-config.schema.json")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
