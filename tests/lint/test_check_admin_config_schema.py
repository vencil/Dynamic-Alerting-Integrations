"""Tests for check_admin_config_schema.py (Gemini #1056 disposition 3b —
admin meta-config ↔ JSON schema pre-merge gate).

jsonschema is REQUIRED by the tool. The CI "Python Tests" job DOES install it
(.github/workflows/ci.yml `pip install … jsonschema`), so this module runs there
as well as locally / in the dev container; the importorskip is a guard for a bare
env, not an expectation of being skipped in CI. The exit-code gate
(tests/shared/test_tool_exit_codes.py) separately covers --help / bad-args WITHOUT
jsonschema thanks to the tool's lazy import.

Two invariants these tests exist to protect:
  1. The gate must never be FAIL-OPEN: any file the pre-commit hook selects must
     be recognized and validated by the script (see TestGateIntegrity).
  2. The schemas must never be STRICTER than the runtime parser, or a legitimate
     config cannot land (see TestParserParity) — every case there is a config the
     Go parser accepts.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema")

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools", "lint"))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools"))

from check_admin_config_schema import (  # noqa: E402
    ADMIN_EXTENSIONS,
    EMBEDDED_RBAC_SOURCES,
    SCHEMA_MAP,
    _CallerError,
    embedded_source_for,
    schema_for,
    validate_embedded_file,
    validate_file,
)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

_SCRIPT = os.path.join(_REPO, "scripts", "tools", "lint", "check_admin_config_schema.py")
_SCHEMA_DIR = os.path.join(_REPO, "docs", "schemas")
_REAL_RBAC = os.path.join(_REPO, "try-local", "seed", "conf.d", "_rbac.yaml")
_REAL_DOMAIN_POLICY = os.path.join(
    _REPO, "components", "threshold-exporter", "config", "conf.d", "examples", "_domain_policy.yaml")
# Production RBAC embedded as a block-scalar string (the fail-open hole this closes).
_REAL_K8S_CONFIGMAP = os.path.join(_REPO, "k8s", "04-tenant-api", "configmap-rbac.yaml")
_REAL_HELM_VALUES = os.path.join(_REPO, "helm", "tenant-api", "values.yaml")
_REAL_HELM_TEMPLATE = os.path.join(_REPO, "helm", "tenant-api", "templates", "configmap-rbac.yaml")


def _load(name: str) -> dict:
    with open(os.path.join(_SCHEMA_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def _write_at(d: str, relpath: str, text: str) -> str:
    """Write a fixture at a nested repo-relative-looking path (so path-anchored
    EMBEDDED_RBAC_SOURCES matching fires) and return the absolute path."""
    path = os.path.join(d, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


@pytest.fixture(scope="module")
def rbac_schema():
    return _load("rbac.schema.json")


@pytest.fixture(scope="module")
def tenant_orgs_schema():
    return _load("tenant-orgs.schema.json")


@pytest.fixture(scope="module")
def domain_policy_schema():
    return _load("domain-policy.schema.json")


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _write(d: str, basename: str, text: str) -> str:
    path = os.path.join(d, basename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _run(*files: str):
    return subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, _SCRIPT, *files],
        capture_output=True, text=True, encoding="utf-8",
    )


# --- validate_file (direct) ------------------------------------------------

class TestValidateFile:
    def test_clean_rbac_with_match_and_orgscope(self, tmp, rbac_schema):
        # The full P3 (match.groups + match.claims) + P4 (org-scope) + all
        # GroupRule fields shape must NOT false-reject.
        p = _write(tmp, "_rbac.yaml",
                   "groups:\n"
                   "  - name: finance-admins\n"
                   "    tenants: [\"db-a\", \"db-b-*\"]\n"
                   "    permissions: [read, write, admin]\n"
                   "    environments: [production, staging]\n"
                   "    domains: [finance]\n"
                   "  - name: org-viewers\n"
                   "    match:\n"
                   "      groups: [viewers]\n"
                   "      claims:\n"
                   "        org-code: [ORG-4821, ORG-1900]\n"
                   "    tenants: [\"*\"]\n"
                   "    permissions: [read]\n"
                   "    org-scope: org-code\n")
        assert validate_file(p, rbac_schema, jsonschema) == []

    def test_rbac_key_typo_rejected(self, tmp, rbac_schema):
        # `permissons` (typo of permissions) — the strict KnownFields parser
        # rejects this at load; the schema must catch it at author time.
        p = _write(tmp, "_rbac.yaml",
                   "groups:\n  - name: x\n    permissons: [read]\n    tenants: [\"*\"]\n")
        viol = validate_file(p, rbac_schema, jsonschema)
        assert any("permissons" in v for v in viol), viol

    def test_rbac_bad_permission_enum_rejected(self, tmp, rbac_schema):
        p = _write(tmp, "_rbac.yaml",
                   "groups:\n  - name: x\n    permissions: [readonly]\n    tenants: [\"*\"]\n")
        viol = validate_file(p, rbac_schema, jsonschema)
        assert any("readonly" in v for v in viol), viol

    def test_rbac_match_claims_value_non_empty_non_blank(self, tmp, rbac_schema):
        # validateConfig (rbac.go) load-rejects a match.claims value list that is
        # empty or contains a blank string, for a declared key — so the schema
        # must too, mirroring the match.groups minLength constraint. A prior schema
        # accepted these (schema looser than the parser in the dangerous direction).
        for bad in ("      org-code: []\n", "      org-code: ['']\n"):
            p = _write(tmp, "_rbac.yaml",
                       "groups:\n  - name: x\n    match:\n      claims:\n" + bad +
                       "    tenants: [\"*\"]\n    permissions: [read]\n")
            assert validate_file(p, rbac_schema, jsonschema) != [], f"should reject claims {bad!r}"
        # the well-formed form still passes
        ok = _write(tmp, "_rbac.yaml",
                    "groups:\n  - name: x\n    match:\n      claims:\n        org-code: [ORG-1]\n"
                    "    tenants: [\"*\"]\n    permissions: [read]\n")
        assert validate_file(ok, rbac_schema, jsonschema) == []

    def test_rbac_empty_match_shape_ok_semantics_parser_enforced(self, tmp, rbac_schema):
        # An empty `match: {}` is a LOAD ERROR at runtime, but that cross-field
        # rule is NOT expressible in JSON Schema — the schema accepts the shape
        # (empty object) and the parser rejects the semantics. Document that here
        # so a future reader does not "fix" the schema to require a match key.
        p = _write(tmp, "_rbac.yaml",
                   "groups:\n  - name: x\n    match: {}\n    tenants: [\"*\"]\n    permissions: [read]\n")
        assert validate_file(p, rbac_schema, jsonschema) == []

    def test_clean_tenant_orgs(self, tmp, tenant_orgs_schema):
        p = _write(tmp, "_tenant_orgs.yaml",
                   "tenant_orgs:\n  db-a: [ORG-4821]\n  db-b: [ORG-4821, ORG-1900]\n  db-c: []\n")
        assert validate_file(p, tenant_orgs_schema, jsonschema) == []

    def test_tenant_orgs_toplevel_typo_rejected(self, tmp, tenant_orgs_schema):
        # `tenant_org` (singular typo) would decode to an empty map at runtime,
        # silently making every org-scoped rule behave as if no tenant has an org.
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_org:\n  db-a: [ORG-1]\n")
        viol = validate_file(p, tenant_orgs_schema, jsonschema)
        assert any("tenant_org" in v for v in viol), viol

    def test_tenant_orgs_non_string_org_rejected(self, tmp, tenant_orgs_schema):
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_orgs:\n  db-a: [123]\n")
        viol = validate_file(p, tenant_orgs_schema, jsonschema)
        assert viol != []

    def test_empty_or_comment_file_tolerated(self, tmp, rbac_schema):
        # An empty / comment-only admin file decodes to the empty config
        # (rbac / tenantorg io.EOF special-case) → must NOT be flagged.
        p = _write(tmp, "_rbac.yaml", "# no rules yet\n")
        assert validate_file(p, rbac_schema, jsonschema) == []

    def test_list_top_doc_flagged(self, tmp, tenant_orgs_schema):
        p = _write(tmp, "_tenant_orgs.yaml", "- db-a\n- db-b\n")
        viol = validate_file(p, tenant_orgs_schema, jsonschema)
        assert any("must be a mapping" in v for v in viol), viol


# --- real committed files (regression guard) -------------------------------

class TestRealFiles:
    def test_real_rbac_seed_clean(self, rbac_schema):
        assert os.path.exists(_REAL_RBAC), _REAL_RBAC
        assert validate_file(_REAL_RBAC, rbac_schema, jsonschema) == []

    def test_real_domain_policy_example_clean(self, domain_policy_schema):
        assert os.path.exists(_REAL_DOMAIN_POLICY), _REAL_DOMAIN_POLICY
        assert validate_file(_REAL_DOMAIN_POLICY, domain_policy_schema, jsonschema) == []

    def test_schema_map_covers_the_three_admin_stems(self):
        assert set(SCHEMA_MAP) == {"_rbac", "_domain_policy", "_tenant_orgs"}
        # every mapped schema file must exist
        for fn in SCHEMA_MAP.values():
            assert os.path.exists(os.path.join(_SCHEMA_DIR, fn)), fn


# --- embedded RBAC (production authz shipped as a block-scalar string) -------

# A k8s ConfigMap with a GOOD RBAC embedded at data._rbac.yaml.
_CONFIGMAP_TMPL = (
    "apiVersion: v1\n"
    "kind: ConfigMap\n"
    "metadata:\n"
    "  name: rbac-config\n"
    "data:\n"
    "  _rbac.yaml: |\n"
    "{body}"
)
# A Helm values.yaml with an RBAC embedded at rbac._rbacYaml.
_VALUES_TMPL = (
    "replicaCount: 1\n"
    "rbac:\n"
    "  _rbacYaml: |\n"
    "{body}"
)


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "".join(pad + line if line.strip() else line
                   for line in text.splitlines(keepends=True))


class TestEmbeddedRbac:
    """The P7c-review blind spot: production RBAC ships EMBEDDED as a block-scalar
    string (k8s data._rbac.yaml / Helm rbac._rbacYaml), whose file basename is NOT
    _rbac.yaml — so the whole-file gate never reached it. These pin that the
    extractor validates it against the SAME rbac schema, and that a BAD embedded
    RBAC actually fails."""

    def test_real_k8s_configmap_clean(self, rbac_schema):
        assert os.path.exists(_REAL_K8S_CONFIGMAP), _REAL_K8S_CONFIGMAP
        assert validate_embedded_file(
            _REAL_K8S_CONFIGMAP, ("data", "_rbac.yaml"), rbac_schema, jsonschema) == []

    def test_real_helm_values_clean(self, rbac_schema):
        assert os.path.exists(_REAL_HELM_VALUES), _REAL_HELM_VALUES
        assert validate_embedded_file(
            _REAL_HELM_VALUES, ("rbac", "_rbacYaml"), rbac_schema, jsonschema) == []

    def test_bad_permission_enum_in_embedded_rbac_rejected(self, tmp, rbac_schema):
        # THE core proof: a bad permission value hidden inside the block scalar
        # (invisible to a whole-file _rbac.yaml gate that never sees this file).
        body = _indent("groups:\n  - name: x\n    tenants: [\"*\"]\n"
                       "    permissions: [readonly]\n", 4)
        p = _write(tmp, "configmap-rbac.yaml", _CONFIGMAP_TMPL.format(body=body))
        viol = validate_embedded_file(p, ("data", "_rbac.yaml"), rbac_schema, jsonschema)
        assert any("readonly" in v for v in viol), viol

    def test_key_typo_in_embedded_rbac_rejected(self, tmp, rbac_schema):
        body = _indent("groups:\n  - name: x\n    permissons: [read]\n"
                       "    tenants: [\"*\"]\n", 4)
        p = _write(tmp, "values.yaml", _VALUES_TMPL.format(body=body))
        viol = validate_embedded_file(p, ("rbac", "_rbacYaml"), rbac_schema, jsonschema)
        assert any("permissons" in v for v in viol), viol

    def test_missing_embedded_key_is_fail_loud(self, tmp, rbac_schema):
        # A registered embedded source whose key is renamed / absent must be a
        # VIOLATION, not a silent skip — else a typo'd key silently re-opens the
        # fail-open hole (validates nothing, exits 0).
        p = _write(tmp, "values.yaml", "replicaCount: 1\nrbac:\n  _rbacYamlTYPO: |\n    groups: []\n")
        viol = validate_embedded_file(p, ("rbac", "_rbacYaml"), rbac_schema, jsonschema)
        assert any("not found" in v for v in viol), viol

    def test_embedded_non_string_rejected(self, tmp, rbac_schema):
        # data._rbac.yaml authored as a nested mapping (forgot the `|`) is not a
        # block scalar → flagged, not silently accepted.
        p = _write(tmp, "configmap-rbac.yaml",
                   "data:\n  _rbac.yaml:\n    groups: []\n")
        viol = validate_embedded_file(p, ("data", "_rbac.yaml"), rbac_schema, jsonschema)
        assert any("block-scalar" in v for v in viol), viol

    def test_embedded_null_block_tolerated(self, tmp, rbac_schema):
        # present-but-null block scalar = empty RBAC (rbac io.EOF), loader-legal.
        p = _write(tmp, "values.yaml", "rbac:\n  _rbacYaml:\n")
        assert validate_embedded_file(p, ("rbac", "_rbacYaml"), rbac_schema, jsonschema) == []

    def test_embedded_duplicate_key_rejected(self, tmp, rbac_schema):
        # Duplicate key INSIDE the block scalar: PyYAML last-wins, Go yaml.v3
        # load-rejects. The strict loader must catch it in the embedded RBAC too.
        body = _indent("groups:\n  - name: ops\n    tenants: [\"*\"]\n"
                       "    permissions: [read]\n    permissions: [admin]\n", 4)
        p = _write(tmp, "configmap-rbac.yaml", _CONFIGMAP_TMPL.format(body=body))
        viol = validate_embedded_file(p, ("data", "_rbac.yaml"), rbac_schema, jsonschema)
        assert any("duplicate key" in v.lower() for v in viol), viol

    def test_helm_template_not_plain_yaml_raises(self, rbac_schema):
        # The Helm TEMPLATE (document-level Go `{{ }}`) is EXCLUDED by the hook's
        # files regex (see test_embedded_source_classifier_is_path_anchored), so it
        # never reaches this function via the CLI. If forced through, it is NOT plain
        # YAML → raises _CallerError (exit 2), never silently deferred — a registered
        # source is fail-CLOSED (the old None-defer was a fail-open).
        assert os.path.exists(_REAL_HELM_TEMPLATE), _REAL_HELM_TEMPLATE
        with pytest.raises(_CallerError):
            validate_embedded_file(
                _REAL_HELM_TEMPLATE, ("data", "_rbac.yaml"), rbac_schema, jsonschema)

    def test_embedded_source_classifier_is_path_anchored(self):
        # Only the two exact production files classify as embedded sources; a bare
        # basename or another chart's values.yaml must NOT (avoids false positives
        # on the 10 other values.yaml).
        assert embedded_source_for("k8s/04-tenant-api/configmap-rbac.yaml") is not None
        assert embedded_source_for("repo/helm/tenant-api/values.yaml") is not None
        assert embedded_source_for("helm/da-portal/values.yaml") is None
        assert embedded_source_for("values.yaml") is None
        assert embedded_source_for("helm/tenant-api/templates/configmap-rbac.yaml") is None


class TestEmbeddedCLI:
    """End-to-end exit codes for the embedded-RBAC path (path-anchored matching,
    so fixtures live under the real repo-relative subpaths)."""

    def test_real_embedded_files_exit_zero(self):
        result = _run(_REAL_K8S_CONFIGMAP, _REAL_HELM_VALUES)
        assert result.returncode == EXIT_OK, result.stderr
        assert "OK:" in result.stdout

    def test_bad_embedded_rbac_exit_one(self, tmp):
        # Prove the GATE (not just the function) blocks a bad embedded RBAC.
        body = _indent("groups:\n  - name: x\n    tenants: [\"*\"]\n"
                       "    permissions: [readonly]\n", 4)
        p = _write_at(tmp, "helm/tenant-api/values.yaml",
                      _VALUES_TMPL.format(body=body))
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION, (
            f"a bad embedded RBAC must FAIL the gate, got exit {result.returncode}\n"
            f"{result.stdout}\n{result.stderr}")
        assert "readonly" in result.stderr, result.stderr

    def test_missing_key_exit_one(self, tmp):
        p = _write_at(tmp, "k8s/04-tenant-api/configmap-rbac.yaml",
                      "data:\n  _rbac_TYPO.yaml: |\n    groups: []\n")
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION, result.stdout + result.stderr
        assert "not found" in result.stderr, result.stderr

    def test_templated_rbac_block_is_deferred_not_errored(self, tmp):
        # A registered source whose embedded RBAC BLOCK is itself a Go-template
        # indirection (`_rbacYaml: |{{ .Values.rbacBody }}`) has no concrete RBAC to
        # validate here — the BLOCK is deferred (skipped), never a spurious error.
        # (Defensive: the real values.yaml embeds plain RBAC.)
        p = _write_at(tmp, "helm/tenant-api/values.yaml",
                      "rbac:\n  _rbacYaml: |\n    {{ .Values.rbacBody | nindent 4 }}\n")
        result = _run(p)
        assert result.returncode == EXIT_OK, result.stdout + result.stderr

    def test_unrelated_template_marker_does_not_defer_bad_rbac(self, tmp):
        # FAIL-OPEN regression (adversarial review): an UNRELATED quoted Go-template
        # value elsewhere in a registered values.yaml must NOT suppress validation of
        # the plain embedded RBAC. The old whole-file `{{` scan deferred the ENTIRE
        # file → a bad RBAC shipped silently (exit 0). The scoped block-level check
        # must let the plain-RBAC block through and FAIL it.
        body = _indent("groups:\n  - name: x\n    tenants: [\"*\"]\n"
                       "    permissions: [readonly]\n", 4)
        content = (
            "replicaCount: 1\n"
            "podAnnotations:\n"
            "  example.com/rendered: \"{{ .Release.Name }}\"\n"
            "rbac:\n"
            "  _rbacYaml: |\n" + body
        )
        p = _write_at(tmp, "helm/tenant-api/values.yaml", content)
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION, (
            f"an unrelated {{{{ }}}} must NOT defer a bad plain-RBAC block, got exit "
            f"{result.returncode}\n{result.stdout}\n{result.stderr}")
        assert "readonly" in result.stderr, result.stderr

    def test_real_embedded_files_are_validated_not_deferred(self):
        # Tripwire (adversarial MINOR): the two real registered sources must be
        # VALIDATED (counted), not silently deferred — a regression that pushes a
        # registered source into a skipped/deferred branch would fail this.
        result = _run(_REAL_K8S_CONFIGMAP, _REAL_HELM_VALUES)
        assert result.returncode == EXIT_OK, result.stderr
        assert "2 admin meta-config file(s) valid" in result.stdout, result.stdout
        assert "deferred" not in result.stdout.lower(), result.stdout

    def test_helm_template_path_skipped_not_error(self):
        # The real Helm template is NOT a registered embedded source (deferred at
        # the regex level); passing it to the CLI must be a clean skip, never a
        # parse ERROR from the `{{ }}` markers.
        result = _run(_REAL_HELM_TEMPLATE)
        assert result.returncode == EXIT_OK, result.stdout + result.stderr
        assert "ERROR" not in result.stderr, result.stderr


# --- gate integrity: the hook must never select a file the script skips ----

class TestGateIntegrity:
    """Regression guard for a FAIL-OPEN hole: the pre-commit `files:` regex
    accepts `\\.ya?ml$` while SCHEMA_MAP was once keyed on `.yaml` basenames, so a
    `_rbac.yml` was SELECTED by the hook, passed to the script, and silently
    SKIPPED with exit 0 — the gate reported OK while validating nothing. That is
    not academic: the rbac path is operator-chosen via `--rbac`, so a `.yml`
    spelling really loads at runtime and rbac fails CLOSED on a bad parse."""

    def _hook_files_regex(self) -> re.Pattern:
        with open(os.path.join(_REPO, ".pre-commit-config.yaml"), encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        hooks = [h for repo in cfg["repos"] for h in repo.get("hooks", [])]
        hook = next(h for h in hooks if h.get("id") == "admin-config-schema-check")
        return re.compile(hook["files"])

    @staticmethod
    def _handled(rel: str) -> bool:
        # A path is handled (not fail-open) if EITHER the whole-file classifier
        # (schema_for, by stem) OR the embedded-RBAC classifier
        # (embedded_source_for, by path suffix) recognizes it.
        return schema_for(rel) is not None or embedded_source_for(rel) is not None

    def test_no_fail_open__every_file_the_hook_selects_is_validated(self):
        # THE fail-open invariant is regex ⊆ script: any path the hook SELECTS must
        # be recognized by the script (schema_for OR embedded_source_for). Drive a
        # broad candidate space through the ACTUAL hook regex and, for everything it
        # selects, require the script to handle it. This would have caught the
        # original `.yml` hole (regex selected it, script skipped it → exit 0 on a
        # broken authz config), and now also the embedded production RBAC paths.
        pattern = self._hook_files_regex()
        stems = ["_rbac", "_domain_policy", "_tenant_orgs",
                 "_groups", "_federation_policy", "notes", "rbac"]
        exts = [".yaml", ".yml", ".YAML", ".YML", ".Yaml", ".json", ".txt", ".yaml.bak", ""]
        candidates = []
        for prefix in ("", "conf.d/", "deploy/overlays/prod/"):
            for stem in stems:
                for ext in exts:
                    candidates.append(f"{prefix}{stem}{ext}")
        # The embedded production-RBAC sources must be in the candidate space too.
        candidates += list(EMBEDDED_RBAC_SOURCES) + [
            f"repo/{p}" for p in EMBEDDED_RBAC_SOURCES]
        selected = 0
        for rel in candidates:
            if pattern.search(rel):
                selected += 1
                assert self._handled(rel), (
                    f"FAIL-OPEN: hook regex selects {rel!r} but the script does not "
                    f"recognize it (schema_for + embedded_source_for both None) → "
                    f"the script skips it and exits 0.")
        # >= stems + the 2 embedded sources ensures the guard isn't vacuous.
        assert selected >= len(SCHEMA_MAP) + len(EMBEDDED_RBAC_SOURCES), (
            f"regex selected only {selected} candidate(s) — guard likely vacuous")

    def test_embedded_sources_are_selected_by_the_hook(self):
        # The regex MUST select the two production embedded-RBAC files (that is the
        # whole point of this follow-up), from repo root and nested.
        pattern = self._hook_files_regex()
        for suffix in EMBEDDED_RBAC_SOURCES:
            assert pattern.search(suffix), f"hook regex must select {suffix}"
            assert pattern.search(f"some/prefix/{suffix}"), suffix

    def test_hook_does_not_select_files_we_cannot_validate(self):
        pattern = self._hook_files_regex()
        for rel in ("conf.d/_rbac.txt", "conf.d/notes.yaml",
                    "conf.d/rbac.yaml", "conf.d/_rbac.yaml.bak",
                    # 10 OTHER chart values.yaml must NOT be selected (path-anchored):
                    "helm/da-portal/values.yaml", "helm/vector/values.yaml",
                    "values.yaml",
                    # the Helm TEMPLATE configmap-rbac.yaml (Go `{{ }}`) is DEFERRED,
                    # so it must not be selected — its RBAC is validated via values.yaml:
                    "helm/tenant-api/templates/configmap-rbac.yaml"):
            assert not pattern.search(rel), f"hook regex unexpectedly selects {rel}"

    def test_yml_variant_is_validated_not_skipped(self, tmp):
        p = _write(tmp, "_rbac.yml", "groups:\n  - name: x\n    permissons: [read]\n")
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION, (
            f"a broken _rbac.yml must FAIL the gate, got exit {result.returncode}")

    def test_duplicate_key_rejected(self, tmp):
        # PyYAML silently keeps the LAST value on a duplicate key, but the strict
        # Go parser (yaml.v3) load-REJECTS it — so a dup-key config that this lint
        # accepted would pass CI and crash the manager at runtime (rbac is
        # startup-fatal). The lint uses a duplicate-key-rejecting loader; this pins
        # that it is a VIOLATION (exit 1), not a silent pass (Gemini #1061).
        p = _write(tmp, "_rbac.yaml",
                   "groups:\n  - name: ops\n    tenants: [\"*\"]\n"
                   "    permissions: [read]\n    permissions: [admin]\n")
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION, (
            f"a duplicate-key _rbac.yaml must FAIL the gate, got exit {result.returncode}")
        assert "duplicate key" in (result.stdout + result.stderr).lower(), \
            (result.stdout + result.stderr)

    def test_duplicate_toplevel_key_rejected(self, tmp):
        p = _write(tmp, "_tenant_orgs.yaml",
                   "tenant_orgs:\n  db-a: [ORG-1]\ntenant_orgs:\n  db-b: [ORG-2]\n")
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION


# --- parser parity: the schema must never be STRICTER than the parser ------

class TestParserParity:
    """Every case here is a config the Go parser ACCEPTS. yaml.v3 KnownFields
    decodes a present-but-null value to the zero value WITHOUT a load error
    (rbac.go documents exactly this for `match:`), and policy.go parses
    _domain_policy.yaml LENIENTLY (plain yaml.Unmarshal, no KnownFields). A schema
    that rejected any of these would block a legitimate commit."""

    def test_rbac_bare_groups_null(self, tmp, rbac_schema):
        p = _write(tmp, "_rbac.yaml", "groups:\n")
        assert validate_file(p, rbac_schema, jsonschema) == []

    def test_rbac_rule_null_lists(self, tmp, rbac_schema):
        p = _write(tmp, "_rbac.yaml", "groups:\n  - name: x\n    tenants:\n    permissions:\n")
        assert validate_file(p, rbac_schema, jsonschema) == []

    def test_tenant_orgs_null_org_list(self, tmp, tenant_orgs_schema):
        # `db-a:` is the terse spelling of `db-a: []` — the documented, tested
        # "created-but-unassigned" state (tenantorg_test.go).
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_orgs:\n  db-a:\n")
        assert validate_file(p, tenant_orgs_schema, jsonschema) == []

    def test_tenant_orgs_null_map(self, tmp, tenant_orgs_schema):
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_orgs:\n")
        assert validate_file(p, tenant_orgs_schema, jsonschema) == []

    def test_domain_policy_require_critical_escalation(self, tmp, domain_policy_schema):
        # In ADR-007's canonical example and blessed by check_routing_profiles.py.
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    tenants: [db-a]\n"
                   "    constraints:\n      require_critical_escalation: true\n")
        assert validate_file(p, domain_policy_schema, jsonschema) == []

    def test_domain_policy_description_only(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    description: placeholder\n")
        assert validate_file(p, domain_policy_schema, jsonschema) == []

    def test_domain_policy_empty_map(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml", "domain_policies: {}\n")
        assert validate_file(p, domain_policy_schema, jsonschema) == []

    def test_domain_policy_fractional_duration(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    constraints:\n"
                   "      max_repeat_interval: 1.5h\n")
        assert validate_file(p, domain_policy_schema, jsonschema) == []


# --- domain-policy negatives (this schema is the SOLE guard there) ---------

class TestDomainPolicyNegatives:
    """policy.go parses _domain_policy.yaml leniently, so an unknown constraint key
    is silently ignored at runtime and never applies — this schema is the only
    guard. These pin that a loosening of domain-policy.schema.json is caught."""

    def test_typo_constraint_key_rejected(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    constraints:\n"
                   "      reqire_critical_escalation: true\n")
        viol = validate_file(p, domain_policy_schema, jsonschema)
        assert any("reqire_critical_escalation" in v for v in viol), viol

    def test_bad_receiver_type_rejected(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    constraints:\n"
                   "      allowed_receiver_types: [carrier-pigeon]\n")
        assert validate_file(p, domain_policy_schema, jsonschema) != []

    def test_bad_duration_rejected(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    constraints:\n"
                   "      max_repeat_interval: 1hour\n")
        assert validate_file(p, domain_policy_schema, jsonschema) != []

    def test_unknown_toplevel_key_rejected(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml", "domain_polices: {}\n")
        viol = validate_file(p, domain_policy_schema, jsonschema)
        assert any("domain_polices" in v for v in viol), viol


# --- CLI exit codes --------------------------------------------------------

class TestCLI:
    def test_real_files_exit_zero(self):
        result = _run(_REAL_RBAC, _REAL_DOMAIN_POLICY)
        assert result.returncode == EXIT_OK, result.stderr
        assert "OK:" in result.stdout

    def test_violation_exit_one(self, tmp):
        p = _write(tmp, "_rbac.yaml",
                   "groups:\n  - name: x\n    permissions: [readonly]\n    tenants: [\"*\"]\n")
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION
        assert "readonly" in result.stderr

    def test_non_admin_file_skipped_exit_zero(self, tmp):
        p = _write(tmp, "notes.txt", "hello\n")
        result = _run(p)
        assert result.returncode == EXIT_OK

    def test_missing_schema_dir_exit_two(self):
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, _SCRIPT, "--schema-dir", os.path.join(_REPO, "no", "such"), _REAL_RBAC],
            capture_output=True, text=True, encoding="utf-8",
        )
        assert result.returncode == EXIT_CALLER_ERROR
