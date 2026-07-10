#!/usr/bin/env python3
"""check_admin_config_schema.py — validate tenant-api admin meta-config YAML against JSON Schemas.

Lint class & scope (lint-policy.md §3): hybrid schema lint (jsonschema engine +
Vibe wrapper). Pre-merge structural validation for the `_`-prefixed ADMIN
meta-config files that define tenant-api authorization / policy boundaries —
`_rbac.yaml`, `_domain_policy.yaml`, `_tenant_orgs.yaml`.

WHY (the silent failures this shifts left) — the three files differ, deliberately:
  * `_rbac.yaml` / `_tenant_orgs.yaml` are parsed STRICTLY (yaml KnownFields), so a
    typo'd key (`permissons:` / `tenant_org:`) IS a runtime load error — but only
    DISCOVERED in production: startup-fatal, or (on hot-reload) configwatcher keeps
    the LAST-GOOD snapshot so the edited file silently never takes effect.
  * `_domain_policy.yaml` is parsed LENIENTLY (policy.go uses plain yaml.Unmarshal,
    NOT KnownFields), so a typo'd constraint key is SILENTLY IGNORED — the
    constraint simply never applies and the runtime never complains at all. Here
    this lint is the ONLY guard, which makes it more valuable, not less.
  Note a bad permission VALUE (`permissions: [readonly]`) is likewise NOT a Go load
  error — `Permission` is an unconstrained string and validateConfig has no enum
  check; it just silently grants nothing. The schema's enum is a deliberately
  stricter authoring guard: it can only reject typos, never a real permission.
  (Gemini #1056 disposition 3b; the runtime observability half is the
  tenant_api_config_reload_failures_total counter.)

  SoT for each schema is the consumer it mirrors: the Go struct for rbac /
  tenant-orgs, and for domain-policy the union of policy.go, ADR-007 and
  check_routing_profiles.py. Keep docs/schemas/*.schema.json in sync — and keep
  the schemas no STRICTER than the parser, or a legitimate config cannot land.

SCOPE:
  Validates each argument file whose basename stem is a known admin meta-config
  (SCHEMA_MAP) with a `.yaml`/`.yml` extension, against its schema. Other files are
  SKIPPED (listed, never silently capped). An empty / comment-only file is
  loader-LEGAL (rbac / tenantorg decode io.EOF to the empty config) and is NOT
  flagged. A list / scalar top document is malformed and IS flagged. Cross-field
  fail-closed rules (empty `match: {}` = error, org-scope key must be declared) are
  NOT expressible in JSON Schema and remain parser-enforced — this lint guards
  structure only.

Exit codes (scripts/tools/_lib_exitcodes.py):
  0  all checked files valid (or no admin files among the arguments)
  1  >=1 schema violation (user fixes the YAML or the schema)
  2  bad invocation / unreadable schema / jsonschema missing / malformed YAML

Usage:
  python3 check_admin_config_schema.py path/to/_rbac.yaml path/to/_tenant_orgs.yaml
  # pre-commit passes the matching staged files; CI runs it over --all-files.
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
_DEFAULT_SCHEMA_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "docs", "schemas"))

# Admin meta-config basename STEM -> schema filename (under the schema dir).
# Each schema mirrors the Go struct named in its `description`. Extend both this
# map and docs/schemas/ together when a new admin meta-config manager is added.
SCHEMA_MAP = {
    "_rbac": "rbac.schema.json",
    "_domain_policy": "domain-policy.schema.json",
    "_tenant_orgs": "tenant-orgs.schema.json",
}

# Both spellings are recognized. This MUST stay in step with the pre-commit
# hook's `files:` regex (`…\.ya?ml$`), or a file the hook SELECTS but this
# script does not recognize would be silently skipped with exit 0 — a fail-open
# gate that reports OK while validating nothing. `_rbac.yml` is not academic:
# the rbac path is operator-chosen via `--rbac` (cmd/server/main.go), so a
# `.yml` spelling really loads at runtime and rbac fails CLOSED on a bad parse.
# tests/lint/test_check_admin_config_schema.py pins the regex <-> map agreement.
ADMIN_EXTENSIONS = (".yaml", ".yml")


def schema_for(path: str) -> str | None:
    """Return the schema filename for an admin meta-config path, else None."""
    stem, ext = os.path.splitext(os.path.basename(path))
    if ext.lower() not in ADMIN_EXTENSIONS:
        return None
    return SCHEMA_MAP.get(stem)


class _CallerError(Exception):
    """Environment/invocation failure -> EXIT_CALLER_ERROR (2)."""


def _load_schema(schema_dir: str, filename: str, cache: dict) -> dict:
    if filename in cache:
        return cache[filename]
    path = os.path.join(schema_dir, filename)
    try:
        with open(path, encoding="utf-8") as fh:
            schema = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise _CallerError(f"cannot load schema {path}: {exc}")
    cache[filename] = schema
    return schema


def validate_file(path: str, schema: dict, validator) -> list[str]:
    """Validate one admin-config file against `schema`. Returns violation messages.

    `validator` is the jsonschema module (injected so the import stays lazy — the
    CI exit-code gate runs `--help` in an env that may not have jsonschema).
    """
    try:
        with open(path, encoding="utf-8") as fh:
            docs = list(yaml.safe_load_all(fh))
    except (OSError, yaml.YAMLError) as exc:
        raise _CallerError(f"{path}: cannot read/parse YAML: {exc}")

    violations: list[str] = []
    for doc in docs:
        if doc is None:
            # Empty / comment-only / explicit-`null` document: loader-LEGAL for
            # the admin managers (rbac / tenantorg decode io.EOF to the empty
            # config; an empty _domain_policy is likewise inert). Not a violation.
            continue
        if not isinstance(doc, dict):
            violations.append(
                f"ERROR: {path}: top-level YAML document must be a mapping "
                f"(got {type(doc).__name__})")
            continue
        try:
            validator.validate(doc, schema)
        except validator.ValidationError as exc:
            loc = "/".join(str(p) for p in exc.absolute_path)
            violations.append(f"ERROR: {path}: {exc.message} @ /{loc}")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate tenant-api admin meta-config YAML "
                    "(_rbac / _domain_policy / _tenant_orgs) against JSON Schemas")
    parser.add_argument("files", nargs="*",
                        help="Files to check (pre-commit passes matching staged files)")
    parser.add_argument("--schema-dir", default=_DEFAULT_SCHEMA_DIR,
                        help="Directory holding the *.schema.json files "
                             "(default: docs/schemas/)")
    parser.add_argument("--ci", action="store_true",
                        help="CI mode (accepted for symmetry with sibling lints; no behaviour change)")
    args = parser.parse_args()

    if not os.path.isdir(args.schema_dir):
        print(f"ERROR: schema-dir not found: {args.schema_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    # Partition arguments into admin files we own vs. everything else (skipped).
    to_check: list[tuple[str, str]] = []  # (path, schema_filename)
    skipped: list[str] = []
    for path in args.files:
        schema_file = schema_for(path)
        if schema_file is None:
            skipped.append(path)
            continue
        to_check.append((path, schema_file))

    if not to_check:
        if skipped:
            print(f"OK: no admin meta-config among {len(skipped)} file(s) "
                  f"(skipped: {', '.join(sorted(skipped))})")
        else:
            print("OK: no admin meta-config files to check")
        return EXIT_OK

    # Lazy import: keep --help / invalid-args (the exit-code gate) working in a
    # jsonschema-less env.
    try:
        import jsonschema
    except ImportError:
        print("ERROR: jsonschema not installed — `pip install jsonschema` "
              "(pre-commit injects it via additional_dependencies).", file=sys.stderr)
        return EXIT_CALLER_ERROR

    schema_cache: dict = {}
    violations: list[str] = []
    checked = 0
    try:
        for path, schema_file in sorted(to_check):
            schema = _load_schema(args.schema_dir, schema_file, schema_cache)
            checked += 1
            violations.extend(validate_file(path, schema, jsonschema))
    except _CallerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if skipped:
        print(f"skipped {len(skipped)} non-admin file(s): {', '.join(sorted(skipped))}")
    if violations:
        for msg in violations:
            print(msg, file=sys.stderr)
        print(f"\n{len(violations)} schema violation(s) across {checked} admin "
              f"config file(s). Fix the YAML, or the schema in docs/schemas/ if the "
              f"schema is wrong (SoT = the Go struct it mirrors).", file=sys.stderr)
        return EXIT_VIOLATION

    print(f"OK: {checked} admin meta-config file(s) valid against docs/schemas/")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
