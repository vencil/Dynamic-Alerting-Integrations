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
  Additionally, a DUPLICATE key (writing `permissions:` twice) is silently
  last-wins in PyYAML but a load error in the strict Go parser — so this lint uses
  a duplicate-key-rejecting loader (below) to catch it, or it would be the reverse
  of the intended gap: lint green, prod crash (Gemini #1061 review).
  Note a bad permission VALUE (`permissions: [readonly]`) is likewise NOT a Go load
  error — `Permission` is an unconstrained string and validateConfig has no enum
  check; it just silently grants nothing. The schema's enum is a deliberately
  stricter authoring guard: it can only reject typos, never a real permission.
  (Gemini #1056 disposition 3b; the runtime observability half is the
  tenant_api_config_reload_failures_total counter.)

  SoT for each schema is the consumer it mirrors: the Go struct for rbac /
  tenant-orgs, and for domain-policy the union of policy.go, ADR-007 and
  check_routing_profiles.py. Keep docs/schemas/*.schema.json in sync, and keep
  each schema no LOOSER than its parser (never accept a config the parser
  load-rejects) and no stricter than the parser EXCEPT as an authoring hygiene
  guard that can only reject a typo (enum values, non-blank list items) — else a
  legitimate config cannot land.

SCOPE:
  Validates each argument file whose basename stem is a known admin meta-config
  (SCHEMA_MAP) with a `.yaml`/`.yml` extension, against its schema. Other files are
  SKIPPED (listed, never silently capped). An empty / comment-only file is
  loader-LEGAL (rbac / tenantorg decode io.EOF to the empty config) and is NOT
  flagged. A list / scalar top document is malformed and IS flagged. Cross-field
  fail-closed rules (empty `match: {}` = error, org-scope key must be declared) are
  NOT expressible in JSON Schema and remain parser-enforced — this lint guards
  structure only.

  EMBEDDED SOURCES (production RBAC): the tenant-api RBAC config also ships EMBEDDED
  as a YAML block-scalar string inside a larger document — `data._rbac.yaml` in the
  raw k8s ConfigMap (k8s/04-tenant-api/configmap-rbac.yaml) and `rbac._rbacYaml` in
  the Helm chart values (helm/tenant-api/values.yaml). Those basenames are NOT
  `_rbac.yaml`, so the whole-file path above never reaches the production authz
  config. EMBEDDED_RBAC_SOURCES maps each (by repo-relative path, NOT basename —
  there are 11 other values.yaml) to the key to EXTRACT → parse → validate against
  the SAME rbac schema. The Helm TEMPLATE
  (helm/tenant-api/templates/configmap-rbac.yaml) carries Go `{{ }}` markers and
  cannot be parsed as plain YAML, so it is DEFERRED (skipped) — its rendered RBAC is
  the values.yaml `_rbacYaml`, which IS validated. A registered embedded source
  MISSING its expected key is a VIOLATION (fail-loud), not a silent skip, so a
  renamed embedding key cannot silently re-open the fail-open hole this closes.

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


# Production RBAC embedded as a block-scalar string inside a larger document.
# Keyed by repo-relative path SUFFIX (never basename — there are 11 other
# values.yaml and a second configmap-rbac.yaml, the Helm template, which must
# NOT match). Each value = (dotted key-path to extract, schema filename). This
# MUST stay in step with the pre-commit hook's `files:` regex, which pins these
# exact paths; a path the hook selects but this map does not know would be
# silently skipped (fail-open). Pinned by TestGateIntegrity.
EMBEDDED_RBAC_SOURCES = {
    "k8s/04-tenant-api/configmap-rbac.yaml": (("data", "_rbac.yaml"), "rbac.schema.json"),
    "helm/tenant-api/values.yaml": (("rbac", "_rbacYaml"), "rbac.schema.json"),
}


def embedded_source_for(path: str):
    """Return (key_path_tuple, schema_filename) if `path` is a registered
    embedded-RBAC source (matched by repo-relative path suffix), else None."""
    norm = path.replace("\\", "/")
    for suffix, spec in EMBEDDED_RBAC_SOURCES.items():
        if norm == suffix or norm.endswith("/" + suffix):
            return spec
    return None


def _navigate(doc: dict, key_path: tuple):
    """Walk `key_path` into a nested mapping. Returns (value, None) if present,
    or (None, dotted_prefix_where_it_stopped) if any level is missing / not a
    mapping."""
    cur = doc
    for i, key in enumerate(key_path):
        if not isinstance(cur, dict) or key not in cur:
            return None, ".".join(key_path[:i + 1])
        cur = cur[key]
    return cur, None


class _DuplicateKeyError(yaml.constructor.ConstructorError):
    """A YAML mapping has a duplicate key. PyYAML's default loader silently keeps
    the LAST value (`permissions: [read]` then `permissions: [admin]` → just
    `[admin]`, no error), but the tenant-api Go parser (yaml.v3, strict) REJECTS
    a duplicate key at load. So a duplicate key this lint accepted would pass CI
    and then crash the manager at runtime (rbac is startup-fatal). We reject it
    here to stay aligned with the strict parser (Gemini #1061 review)."""


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (fail-loud), matching Go
    yaml.v3 instead of PyYAML's silent last-wins."""


def _construct_mapping_reject_dups(loader, node, deep=False):
    loader.flatten_mapping(node)  # keep default merge-key (<<) handling
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKeyError(
                "while constructing a mapping", node.start_mark,
                f"found duplicate key {key!r} — PyYAML silently keeps the last "
                f"value, but the strict tenant-api parser (yaml.v3) rejects it at "
                f"load", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_reject_dups)


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
            docs = list(yaml.load_all(fh, Loader=_StrictSafeLoader))
    except _DuplicateKeyError as exc:
        # A duplicate key is a config VIOLATION the author fixes (exit 1), not an
        # environment error — and it is invisible without this loader (PyYAML
        # would silently keep the last value while the Go parser crashes on it).
        loc = exc.problem_mark
        where = f" (line {loc.line + 1})" if loc is not None else ""
        return [f"ERROR: {path}: {exc.problem}{where}"]
    except (OSError, yaml.YAMLError) as exc:
        raise _CallerError(f"{path}: cannot read/parse YAML: {exc}")

    return _validate_docs(docs, path, schema, validator)


def _validate_docs(docs, path: str, schema: dict, validator, where: str = "") -> list[str]:
    """Validate each parsed YAML document (a mapping) against `schema`. `where`
    is an optional message prefix used to point at an EMBEDDED block-scalar
    source (e.g. "embedded 'data._rbac.yaml': ") vs. the whole file."""
    violations: list[str] = []
    for doc in docs:
        if doc is None:
            # Empty / comment-only / explicit-`null` document: loader-LEGAL for
            # the admin managers (rbac / tenantorg decode io.EOF to the empty
            # config; an empty _domain_policy is likewise inert). Not a violation.
            continue
        if not isinstance(doc, dict):
            violations.append(
                f"ERROR: {path}: {where}top-level YAML document must be a mapping "
                f"(got {type(doc).__name__})")
            continue
        try:
            validator.validate(doc, schema)
        except validator.ValidationError as exc:
            loc = "/".join(str(p) for p in exc.absolute_path)
            violations.append(f"ERROR: {path}: {where}{exc.message} @ /{loc}")
    return violations


def validate_embedded_file(path: str, key_path: tuple, schema: dict, validator):
    """Validate an RBAC config EMBEDDED as a YAML block-scalar string inside a
    larger document (k8s ConfigMap `data._rbac.yaml` / Helm `rbac._rbacYaml`).

    Returns a list of violation messages (empty if the embedded RBAC is valid).
    The Helm TEMPLATE configmap-rbac.yaml (Go `{{ }}`) is NOT handled here — it is
    excluded by the hook's path-anchored files regex; its rendered RBAC is the
    values.yaml `_rbacYaml`, validated directly as a registered embedded source.
    An unparseable outer document raises _CallerError (exit 2) rather than being
    silently skipped — a REGISTERED source is never deferred (fail-closed).

    Uses the same duplicate-key-rejecting strict loader as validate_file, applied
    to BOTH the outer document and the extracted block scalar, so a dup-key that
    the Go parser would reject at load is caught in the embedded RBAC too.
    """
    dotted = ".".join(key_path)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise _CallerError(f"{path}: cannot read: {exc}")

    # ⛔ NO whole-file `{{` defer. A REGISTERED embedded source is plain YAML by
    # contract — the Helm TEMPLATE (which does carry Go `{{ }}`) is excluded by
    # the hook's path-anchored files regex, NOT handled here. Scanning the whole
    # raw file for `{{` was a FAIL-OPEN: an unrelated tpl-passthrough value
    # (e.g. podAnnotations: "{{ .Release.Name }}") would silently defer — and so
    # SKIP — validation of the embedded production RBAC, reopening the very hole
    # this check closes. A legitimately quoted `{{ }}` parses fine as a YAML
    # string below; an unparseable one raises a CallerError (exit 2, blocks)
    # rather than passing.
    try:
        outer_docs = list(yaml.load_all(raw, Loader=_StrictSafeLoader))
    except _DuplicateKeyError as exc:
        loc = exc.problem_mark
        where = f" (line {loc.line + 1})" if loc is not None else ""
        return [f"ERROR: {path}: {exc.problem}{where}"]
    except (yaml.YAMLError, TypeError) as exc:
        # TypeError: an unquoted document-level `{{ }}` parses as a flow mapping
        # with an unhashable dict key. Either way the OUTER document is not plain
        # YAML — a registered source must be (the Helm template is excluded by the
        # files regex). Block (exit 2) rather than silently pass.
        raise _CallerError(f"{path}: cannot parse YAML "
                           f"(document-level Go template markers unsupported): {exc}")

    violations: list[str] = []
    found = False
    for doc in outer_docs:
        if not isinstance(doc, dict):
            continue
        embedded, missing_at = _navigate(doc, key_path)
        if missing_at is not None:
            continue  # this document does not carry the key; another might
        found = True
        if embedded is None:
            # present-but-null block scalar → empty RBAC (rbac decodes io.EOF to
            # the empty config); loader-legal, not a violation.
            continue
        if not isinstance(embedded, str):
            violations.append(
                f"ERROR: {path}: embedded RBAC at '{dotted}' must be a YAML "
                f"block-scalar string (got {type(embedded).__name__})")
            continue
        if "{{" in embedded:
            # The embedded RBAC block ITSELF is a Go-template indirection (its real
            # RBAC is rendered elsewhere, e.g. `_rbacYaml: |{{ .Values.rbacBody }}`)
            # — defer THIS block, it has no concrete RBAC to validate here. ⛔ This
            # check is SCOPED to the extracted block, NOT the whole file: an
            # unrelated `{{ }}` value elsewhere in the document must never suppress
            # validation of a plain-RBAC block (that was the fail-open this closes).
            continue
        try:
            inner_docs = list(yaml.load_all(embedded, Loader=_StrictSafeLoader))
        except _DuplicateKeyError as exc:
            loc = exc.problem_mark
            at = f" line {loc.line + 1}" if loc is not None else ""
            violations.append(
                f"ERROR: {path}: {exc.problem} (embedded '{dotted}'{at})")
            continue
        except yaml.YAMLError as exc:
            # A malformed embedded RBAC string is an author-fixable content bug
            # (exit 1), not an environment error.
            violations.append(
                f"ERROR: {path}: embedded '{dotted}' is not valid YAML: {exc}")
            continue
        violations.extend(
            _validate_docs(inner_docs, path, schema, validator,
                           where=f"embedded '{dotted}': "))

    if not found:
        violations.append(
            f"ERROR: {path}: expected embedded RBAC key '{dotted}' not found — "
            f"this file is a registered embedded-RBAC source, so a missing or "
            f"renamed key silently skips authz schema validation (fail-open). "
            f"Fix the key, or update EMBEDDED_RBAC_SOURCES if the embedding moved.")
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

    # Partition arguments into: whole-file admin configs (SCHEMA_MAP by stem),
    # embedded-RBAC sources (EMBEDDED_RBAC_SOURCES by path), everything else.
    direct_to_check: list[tuple[str, str]] = []          # (path, schema_filename)
    embedded_to_check: list[tuple[str, tuple, str]] = []  # (path, key_path, schema)
    skipped: list[str] = []
    for path in args.files:
        schema_file = schema_for(path)
        if schema_file is not None:
            direct_to_check.append((path, schema_file))
            continue
        emb = embedded_source_for(path)
        if emb is not None:
            key_path, emb_schema = emb
            embedded_to_check.append((path, key_path, emb_schema))
            continue
        skipped.append(path)

    if not direct_to_check and not embedded_to_check:
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
        for path, schema_file in sorted(direct_to_check):
            schema = _load_schema(args.schema_dir, schema_file, schema_cache)
            checked += 1
            violations.extend(validate_file(path, schema, jsonschema))
        for path, key_path, schema_file in sorted(embedded_to_check):
            schema = _load_schema(args.schema_dir, schema_file, schema_cache)
            # A REGISTERED embedded source is ALWAYS validated, never deferred —
            # the Helm TEMPLATE (Go `{{ }}`) is excluded by the files regex, not
            # skipped here. An unparseable outer YAML raises _CallerError (exit 2,
            # blocks) rather than silently passing.
            checked += 1
            violations.extend(validate_embedded_file(path, key_path, schema, jsonschema))
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
