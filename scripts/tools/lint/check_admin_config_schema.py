#!/usr/bin/env python3
"""check_admin_config_schema.py — validate tenant-api admin meta-config YAML against JSON Schemas.

Lint class & scope (lint-policy.md §3): hybrid schema lint (jsonschema engine +
Vibe wrapper). Pre-merge validation for the `_`-prefixed ADMIN meta-config files
that define tenant-api authorization / policy boundaries — `_rbac.yaml`,
`_domain_policy.yaml`, `_tenant_orgs.yaml` — and the production RBAC embedded in
the Helm values.

WHO READS EACH FILE DECIDES WHAT IT MEANS (ADR-036 §12). This lint does not read
any of them with PyYAML:
  * `_rbac.yaml` / `_tenant_orgs.yaml` go to `tenant-api config check`, which
    judges them with the parse function the server reads them with (parse-tree's
    verdict, then a strict KnownFields decode, then the config's own checks; RBAC
    against the declared identity claims). A file it refuses is a VIOLATION, its
    `problem` in the message: a typo'd key, a duplicate key, a second document,
    an undeclared claim — everything the server would refuse at startup (fatal)
    or on hot-reload (last-good masking, the edit silently never takes effect).
  * The JSON Schemas stay as the AUTHORING checks stricter than the parser (a
    permission value must be one of the known ones; list items non-blank), run on
    the `config` that `config check` decoded — never on a second reading of the
    file. A bad permission VALUE (`permissions: [readonly]`) is not a load error
    (Permission is an unconstrained string) and silently grants nothing; the
    schema's enum can only reject a typo, never a real permission.
  * `_domain_policy.yaml` is a conf.d file: read, like every other conf.d reader,
    through `da-guard parse-tree` (a file it refuses is a violation). Its runtime
    reader is LENIENT (an unknown constraint key is silently ignored), so the
    schema's additionalProperties:false is the only guard there.
  * The Helm values (`rbac._rbacYaml` in helm/tenant-api/values.yaml, tenant-api's
    only deployment source since #1594) hold the RBAC as a string inside a YAML
    document Helm reads with sigs.k8s.io/yaml: `da-crdecode` takes the outer layer
    and the string goes to `config check --kind rbac -` on standard input. A
    registered embedded source MISSING its key is a VIOLATION (fail-loud), not a
    skip; an embedded block that is itself a Go-template indirection (`{{`) has
    no concrete RBAC here and is deferred. The Helm TEMPLATE configmap-rbac.yaml
    is not selected by the hook (its rendered RBAC is the values `_rbacYaml`).
  * The declared identity claims an RBAC config is checked against come from the
    Helm values' `identity.claimHeaders` (read with `da-crdecode`, joined as the
    deployment template renders them: `key=Header` pairs, keys sorted, `,`) and
    are passed as `--identity-claim-headers` — for the embedded RBAC and for a
    standalone `_rbac.yaml` alike: a check inside the repo has no running server.

  Known loss against the PyYAML schema check this replaced: a value the decode
  folds away is no longer seen by the schema — `org-scope: ""` decodes to the
  empty string, which `config` omits like an absent key, so the schema's
  minLength no longer rejects it (the parser treats both as "no org scope").

BINARIES (all fail-closed: one that cannot be found or run is exit 2, never a skip):
  tenant-api   --tenant-api-binary, else $TENANT_API_BINARY, else `tenant-api` on $PATH
  da-crdecode  $DA_CRDECODE_BINARY, else `da-crdecode` on $PATH
  da-guard     $DA_GUARD_BINARY, else `da-guard` on $PATH
  In a checkout, scripts/ops/with_admin_config_tools.sh builds the unset ones
  (`make tenant-api-build`, `da-crdecode-build`, `da-guard-build`). A tenant-api
  that does not print a `tenant-api.config-check/v1` document (one older than
  `config check`) is named as such, exit 2.

SCOPE:
  Validates each argument file whose basename stem is a known admin meta-config
  (SCHEMA_MAP) with a `.yaml`/`.yml` extension, and each registered embedded
  source (EMBEDDED_RBAC_SOURCES, by repo-relative path). Other files are SKIPPED
  (listed, never silently capped). An empty / comment-only file is the empty
  config to the parser and is not flagged.

Exit codes (scripts/tools/_lib_exitcodes.py):
  0  all checked files valid (or no admin files among the arguments)
  1  >=1 violation (the parser refuses it, or a stricter authoring check does)
  2  bad invocation / unreadable schema / jsonschema missing / a binary missing,
     failing or older than this tool / an outer document da-crdecode refuses

Usage:
  python3 check_admin_config_schema.py path/to/_rbac.yaml path/to/_tenant_orgs.yaml
  # pre-commit passes the matching staged files; CI runs it over --all-files.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
import _lib_parse_tree  # noqa: E402  (ADR-036 §12: _domain_policy.yaml is a conf.d file)

_REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, "..", "..", ".."))
# Repo-root-relative default: lint -> tools -> scripts -> <root>/docs/schemas/...
_DEFAULT_SCHEMA_DIR = os.path.join(_REPO_ROOT, "docs", "schemas")
#: The Helm values the declared identity claims are read from (ADR-036 §12).
_DEFAULT_HELM_VALUES = os.path.join(_REPO_ROOT, "helm", "tenant-api", "values.yaml")
#: Where in those values the claims are declared (deployment.yaml renders it).
CLAIMS_KEY_PATH = ("identity", "claimHeaders")

# Admin meta-config basename STEM -> schema filename (under the schema dir).
# Each schema mirrors the Go struct named in its `description`. Extend both this
# map and docs/schemas/ together when a new admin meta-config manager is added.
SCHEMA_MAP = {
    "_rbac": "rbac.schema.json",
    "_domain_policy": "domain-policy.schema.json",
    "_tenant_orgs": "tenant-orgs.schema.json",
}

#: The stems tenant-api reads, and the `config check --kind` that judges each.
#: `_domain_policy` is not here: it is a conf.d file (parse-tree).
CONFIG_CHECK_KIND = {
    "_rbac": "rbac",
    "_tenant_orgs": "tenant-orgs",
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


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


# Production RBAC embedded as a block-scalar string inside a larger document.
# Keyed by repo-relative path SUFFIX (never basename — there are other
# values.yaml and a configmap-rbac.yaml Helm template, which must NOT match).
# Each value = (dotted key-path to extract, schema filename). This MUST stay in
# step with the pre-commit hook's `files:` regex, which pins these exact paths;
# a path the hook selects but this map does not know would be silently skipped
# (fail-open). Pinned by TestGateIntegrity.
EMBEDDED_RBAC_SOURCES = {
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


class _CallerError(Exception):
    """Environment/invocation failure -> EXIT_CALLER_ERROR (2)."""


# ── the Go readers ──────────────────────────────────────────────────────

#: The document `tenant-api config check` prints.
CONFIG_CHECK_SCHEMA = "tenant-api.config-check/v1"
TENANT_API_ENV = "TENANT_API_BINARY"
CRDECODE_ENV = "DA_CRDECODE_BINARY"
#: A tenant-api older than `config check` reads `config` as the server's
#: first positional argument and may start serving: bounded, then named.
_TIMEOUT_SECONDS = 120


def _resolve_tenant_api(explicit: str | None) -> str:
    binary = explicit or os.environ.get(TENANT_API_ENV) or shutil.which("tenant-api")
    if not binary:
        raise _CallerError(
            "tenant-api was not found: _rbac / _tenant_orgs are judged by "
            "`tenant-api config check` (ADR-036 §12). Pass --tenant-api-binary, set "
            f"${TENANT_API_ENV}, put tenant-api on $PATH, or in a checkout run this "
            "through scripts/ops/with_admin_config_tools.sh (make tenant-api-build)")
    return binary


def _resolve_crdecode() -> str:
    binary = os.environ.get(CRDECODE_ENV) or shutil.which("da-crdecode")
    if not binary:
        raise _CallerError(
            "da-crdecode was not found: the Helm values are read with it, as Helm "
            f"reads them (ADR-036 §12). Set ${CRDECODE_ENV}, put da-crdecode on $PATH, "
            "or in a checkout run this through scripts/ops/with_admin_config_tools.sh "
            "(make da-crdecode-build)")
    return binary


def _stderr_text(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="backslashreplace").strip()


def crdecode_documents(binary: str, path: str) -> list:
    """The documents of *path* as Kubernetes / Helm read them (sigs.k8s.io/yaml),
    via `da-crdecode`. A file it refuses, or output that is not its JSON, is a
    caller error: a registered source must be readable the way Helm reads it."""
    try:
        proc = subprocess.run([binary, os.path.abspath(path)], capture_output=True,
                              check=False, timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise _CallerError(f"{path}: da-crdecode did not finish within {_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise _CallerError(f"cannot run {binary}: {exc}") from exc
    if proc.returncode != 0:
        raise _CallerError(f"{path}: da-crdecode exited {proc.returncode} (Helm cannot read "
                           f"this file as plain YAML): {_stderr_text(proc.stderr)}")
    try:
        out = json.loads(proc.stdout)
    except ValueError as exc:
        raise _CallerError(f"{path}: da-crdecode printed output that is not JSON: {exc}") from exc
    docs = out.get("documents") if isinstance(out, dict) else None
    if not isinstance(docs, list):
        raise _CallerError(f"{path}: da-crdecode printed JSON without a \"documents\" list")
    return docs


def claim_declaration(crdecode: str, values_path: str) -> str:
    """`--identity-claim-headers` as the chart renders it from *values_path*'s
    `identity.claimHeaders`: `key=Header` pairs, keys sorted, joined by `,`
    (helm/tenant-api/templates/deployment.yaml). Empty when none is declared."""
    docs = crdecode_documents(crdecode, values_path)
    claims = None
    for doc in docs:
        if isinstance(doc, dict):
            value, missing = _navigate(doc, CLAIMS_KEY_PATH)
            if missing is None:
                claims = value
    if claims is None:
        return ""
    if not isinstance(claims, dict) or not all(isinstance(v, str) for v in claims.values()):
        raise _CallerError(f"{values_path}: {'.'.join(CLAIMS_KEY_PATH)} is not a map of "
                           "claim key to header name")
    return ",".join(f"{k}={claims[k]}" for k in sorted(claims))


def config_check(binary: str, kind: str, where: str, *, path: str | None = None,
                 data: bytes | None = None, claims: str = "") -> tuple[object, dict | None]:
    """`tenant-api config check --kind <kind>` of *path* (or *data* on stdin):
    (config, None) when tenant-api reads it, (None, problem) when it refuses it.
    Anything else — a failing or missing binary, a document that is not
    `tenant-api.config-check/v1` (a tenant-api older than `config check`) — is
    a caller error."""
    cmd = [binary, "config", "check", "--kind", kind]
    if kind == "rbac":
        cmd += ["--identity-claim-headers", claims]
    cmd.append(os.path.abspath(path) if path is not None else "-")
    older = ("(this needs a tenant-api that has `config check`; build one from this "
             "checkout: make tenant-api-build)")
    try:
        proc = subprocess.run(cmd, input=data if data is not None else b"",
                              capture_output=True, check=False, timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise _CallerError(f"{where}: `{binary} config check` did not finish within "
                           f"{_TIMEOUT_SECONDS}s {older}") from exc
    except OSError as exc:
        raise _CallerError(f"cannot run {binary}: {exc}") from exc
    try:
        doc = json.loads(proc.stdout) if proc.returncode in (0, 1) else None
    except ValueError:
        doc = None
    if (not isinstance(doc, dict) or doc.get("schema") != CONFIG_CHECK_SCHEMA
            or doc.get("kind") != kind):
        raise _CallerError(f"{where}: `{binary} config check` exited {proc.returncode} without a "
                           f"{CONFIG_CHECK_SCHEMA} document {older}: {_stderr_text(proc.stderr)}")
    config, problem = doc.get("config"), doc.get("problem")
    if proc.returncode == 0 and problem is None and isinstance(config, dict):
        return config, None
    if (proc.returncode == 1 and config is None and isinstance(problem, dict)
            and isinstance(problem.get("kind"), str) and isinstance(problem.get("message"), str)):
        return None, problem
    raise _CallerError(f"{where}: `{binary} config check` exited {proc.returncode} with a "
                       "document that breaks its contract (exactly one of config and problem)")


class _Readers:
    """The run's Go readers, each resolved on first use: a run that checks no
    RBAC needs neither tenant-api nor da-crdecode."""

    def __init__(self, tenant_api: str | None, helm_values: str) -> None:
        self._tenant_api_arg = tenant_api
        self._helm_values = helm_values
        self._tenant_api: str | None = None
        self._crdecode: str | None = None
        self._claims: str | None = None

    def tenant_api(self) -> str:
        if self._tenant_api is None:
            self._tenant_api = _resolve_tenant_api(self._tenant_api_arg)
        return self._tenant_api

    def crdecode(self) -> str:
        if self._crdecode is None:
            self._crdecode = _resolve_crdecode()
        return self._crdecode

    def claims(self) -> str:
        if self._claims is None:
            self._claims = claim_declaration(self.crdecode(), self._helm_values)
        return self._claims


# ── validation ──────────────────────────────────────────────────────────

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


def _validate_doc(doc, path: str, schema: dict, validator, where: str = "") -> list[str]:
    """The authoring schema on one decoded document (a mapping). None is the
    empty config, not a violation."""
    if doc is None:
        return []
    if not isinstance(doc, dict):
        return [f"ERROR: {path}: {where}top-level YAML document must be a mapping "
                f"(got {type(doc).__name__})"]
    violations = []
    for exc in sorted(validator.Draft7Validator(schema).iter_errors(doc),
                      key=lambda e: list(map(str, e.absolute_path))):
        loc = "/".join(str(p) for p in exc.absolute_path)
        violations.append(f"ERROR: {path}: {where}{exc.message} @ /{loc}")
    return violations


def _problem_line(path: str, problem: dict, where: str = "") -> str:
    return (f"ERROR: {path}: {where}tenant-api refuses it ({problem['kind']}): "
            f"{problem['message']}")


def validate_file(path: str, schema: dict, validator, readers: _Readers) -> list[str]:
    """Validate one whole-file admin config. Returns violation messages."""
    stem = _stem(path)
    kind = CONFIG_CHECK_KIND.get(stem)
    if kind is not None:
        claims = readers.claims() if kind == "rbac" else ""
        if not os.path.isfile(path):
            raise _CallerError(f"{path}: not a readable file")
        config, problem = config_check(readers.tenant_api(), kind, path, path=path, claims=claims)
        if problem is not None:
            return [_problem_line(path, problem)]
        return _validate_doc(config, path, schema, validator)
    return _validate_domain_policy(path, schema, validator)


def _validate_domain_policy(path: str, schema: dict, validator) -> list[str]:
    """`_domain_policy.yaml` read through `da-guard parse-tree` (file mode, as
    the file at its own name in its own directory)."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise _CallerError(f"{path}: cannot read: {exc}")
    try:
        tree = _lib_parse_tree.load_file(p.parent, p.name, raw)
        node = tree.node(p.name)
    except _lib_parse_tree.FileUnreadable as exc:
        return [f"ERROR: {path}: parse-tree refuses it ({exc.kind}): {exc.message}"]
    except KeyError as exc:
        raise _CallerError(f"{path}: {exc}") from exc
    except _lib_parse_tree.ParseTreeError as exc:
        raise _CallerError(f"{path}: {exc}") from exc
    doc = _lib_parse_tree.to_python_text_sequences(node, ("tenants",))
    return _validate_doc(doc, path, schema, validator)


def validate_embedded_file(path: str, key_path: tuple, schema: dict, validator,
                           readers: _Readers) -> list[str]:
    """Validate an RBAC config EMBEDDED as a string inside a larger document
    (Helm `rbac._rbacYaml`): the outer layer read by `da-crdecode`, the string
    judged by `tenant-api config check --kind rbac -`, the authoring schema on
    its decoded config. An outer document da-crdecode refuses raises
    _CallerError (exit 2) — a REGISTERED source is never deferred."""
    dotted = ".".join(key_path)
    docs = crdecode_documents(readers.crdecode(), path)
    violations: list[str] = []
    found = False
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        embedded, missing_at = _navigate(doc, key_path)
        if missing_at is not None:
            continue  # this document does not carry the key; another might
        found = True
        if embedded is None:
            # present-but-null: the empty RBAC; loader-legal, not a violation.
            continue
        if not isinstance(embedded, str):
            violations.append(
                f"ERROR: {path}: embedded RBAC at '{dotted}' must be a YAML "
                f"block-scalar string (got {type(embedded).__name__})")
            continue
        if "{{" in embedded:
            # The block ITSELF is a Go-template indirection (its RBAC is rendered
            # elsewhere): no concrete RBAC to judge here. ⛔ Scoped to the block,
            # never the whole file — an unrelated `{{ }}` value elsewhere must not
            # suppress the check of a plain-RBAC block.
            continue
        where = f"embedded '{dotted}': "
        config, problem = config_check(readers.tenant_api(), "rbac", f"{path} [{dotted}]",
                                       data=embedded.encode("utf-8"), claims=readers.claims())
        if problem is not None:
            violations.append(_problem_line(path, problem, where))
            continue
        violations.extend(_validate_doc(config, path, schema, validator, where=where))

    if not found:
        violations.append(
            f"ERROR: {path}: expected embedded RBAC key '{dotted}' not found — "
            f"this file is a registered embedded-RBAC source, so a missing or "
            f"renamed key silently skips authz validation (fail-open). "
            f"Fix the key, or update EMBEDDED_RBAC_SOURCES if the embedding moved.")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate tenant-api admin meta-config "
                    "(_rbac / _domain_policy / _tenant_orgs + the Helm-embedded RBAC): "
                    "tenant-api's own parse (`tenant-api config check`) plus the "
                    "stricter JSON Schemas on what it decoded")
    parser.add_argument("files", nargs="*",
                        help="Files to check (pre-commit passes matching staged files)")
    parser.add_argument("--schema-dir", default=_DEFAULT_SCHEMA_DIR,
                        help="Directory holding the *.schema.json files "
                             "(default: docs/schemas/)")
    parser.add_argument("--tenant-api-binary", default=None,
                        help=f"tenant-api to run `config check` with (default: ${TENANT_API_ENV}, "
                             "else tenant-api on $PATH)")
    parser.add_argument("--helm-values", default=_DEFAULT_HELM_VALUES,
                        help="Helm values whose identity.claimHeaders declare the claims an "
                             "RBAC config is checked against (default: helm/tenant-api/values.yaml)")
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

    readers = _Readers(args.tenant_api_binary, args.helm_values)
    schema_cache: dict = {}
    violations: list[str] = []
    checked = 0
    try:
        for path, schema_file in sorted(direct_to_check):
            schema = _load_schema(args.schema_dir, schema_file, schema_cache)
            checked += 1
            violations.extend(validate_file(path, schema, jsonschema, readers))
        for path, key_path, schema_file in sorted(embedded_to_check):
            schema = _load_schema(args.schema_dir, schema_file, schema_cache)
            checked += 1
            violations.extend(validate_embedded_file(path, key_path, schema, jsonschema, readers))
    except _CallerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if skipped:
        print(f"skipped {len(skipped)} non-admin file(s): {', '.join(sorted(skipped))}")
    if violations:
        for msg in violations:
            print(msg, file=sys.stderr)
        print(f"\n{len(violations)} violation(s) across {checked} admin config file(s). "
              f"Fix the YAML, or the schema in docs/schemas/ if an authoring check is "
              f"wrong (the parse itself is tenant-api's / parse-tree's).", file=sys.stderr)
        return EXIT_VIOLATION

    print(f"OK: {checked} admin meta-config file(s) valid (tenant-api config check / "
          f"parse-tree + docs/schemas/)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
