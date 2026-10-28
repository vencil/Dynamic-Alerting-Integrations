"""Tests for check_admin_config_schema.py (Gemini #1056 disposition 3b —
admin meta-config pre-merge gate; ADR-036 §12: each file read by its consumer).

`_rbac.yaml` / `_tenant_orgs.yaml` and the Helm-embedded RBAC are judged by
`tenant-api config check` (the server's parse functions), `_domain_policy.yaml`
by `da-guard parse-tree`, the Helm values by `da-crdecode`; the JSON Schemas
are the stricter authoring checks on what those decoded. The binaries are
built from this checkout (tests/conftest.py: a missing go FAILS, never skips —
an unmeasured Go reader is not a green).

jsonschema is REQUIRED by the tool. The CI "Python Tests" job DOES install it
(.github/workflows/ci.yml `pip install … jsonschema`), so this module runs there
as well as locally / in the dev container; the importorskip is a guard for a bare
env, not an expectation of being skipped in CI. The exit-code gate
(tests/shared/test_tool_exit_codes.py) separately covers --help / bad-args WITHOUT
jsonschema thanks to the tool's lazy import.

Two invariants these tests exist to protect:
  1. The gate must never be FAIL-OPEN: any file the pre-commit hook selects must
     be recognized and validated by the script (see TestGateIntegrity), and a
     reader that cannot run is exit 2, never a pass (TestFailClosed).
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
    CONFIG_CHECK_KIND,
    EMBEDDED_RBAC_SOURCES,
    SCHEMA_MAP,
    _CallerError,
    _Readers,
    claim_declaration,
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
_REAL_HELM_VALUES = os.path.join(_REPO, "helm", "tenant-api", "values.yaml")
_REAL_HELM_TEMPLATE = os.path.join(_REPO, "helm", "tenant-api", "templates", "configmap-rbac.yaml")


@pytest.fixture(autouse=True)
def _go_readers(tenant_api_env, da_crdecode_env, da_guard_env):
    """Every test runs with the three Go readers built from this checkout."""


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


def _readers(values: str = _REAL_HELM_VALUES) -> _Readers:
    return _Readers(None, values)


def _values_declaring(d: str, claims: dict[str, str]) -> str:
    """A Helm values file declaring *claims* in identity.claimHeaders."""
    lines = ["identity:", "  claimHeaders:" + ("" if claims else " {}")]
    lines += [f"    {k}: {v}" for k, v in claims.items()]
    return _write(d, "claims-values.yaml", "\n".join(lines) + "\n")


def _run(*args: str, env: dict | None = None):
    return subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, _SCRIPT, *args],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )


# --- validate_file (direct) ------------------------------------------------

class TestValidateFile:
    def test_clean_rbac_with_match_and_orgscope(self, tmp, rbac_schema):
        # The full P3 (match.groups + match.claims) + P4 (org-scope) + all
        # GroupRule fields shape must NOT false-reject — with the claim declared.
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
        readers = _readers(_values_declaring(tmp, {"org-code": "X-Org-Code"}))
        assert validate_file(p, rbac_schema, jsonschema, readers) == []

    def test_undeclared_claim_is_refused_by_tenant_api(self, tmp, rbac_schema):
        # The same file against the repo's values (no claim declared): the server
        # would refuse it at load, so the gate does.
        p = _write(tmp, "_rbac.yaml",
                   "groups:\n  - name: x\n    match:\n      claims:\n        org-code: [ORG-1]\n"
                   "    tenants: [\"*\"]\n    permissions: [read]\n")
        viol = validate_file(p, rbac_schema, jsonschema, _readers())
        assert len(viol) == 1 and "tenant-api refuses it (rejected)" in viol[0], viol

    def test_rbac_key_typo_rejected(self, tmp, rbac_schema):
        # `permissons` (typo of permissions) — the strict KnownFields parser
        # rejects this at load; the gate reports tenant-api's refusal.
        p = _write(tmp, "_rbac.yaml",
                   "groups:\n  - name: x\n    permissons: [read]\n    tenants: [\"*\"]\n")
        viol = validate_file(p, rbac_schema, jsonschema, _readers())
        assert any("permissons" in v and "tenant-api refuses it" in v for v in viol), viol

    def test_rbac_bad_permission_enum_rejected(self, tmp, rbac_schema):
        # Parser-legal (Permission is an unconstrained string): the schema's
        # authoring enum on the decoded config catches it.
        p = _write(tmp, "_rbac.yaml",
                   "groups:\n  - name: x\n    permissions: [readonly]\n    tenants: [\"*\"]\n")
        viol = validate_file(p, rbac_schema, jsonschema, _readers())
        assert any("readonly" in v and "refuses" not in v for v in viol), viol

    def test_rbac_match_claims_value_non_empty_non_blank(self, tmp, rbac_schema):
        readers = _readers(_values_declaring(tmp, {"org-code": "X-Org-Code"}))
        for bad in ("      org-code: []\n", "      org-code: ['']\n"):
            p = _write(tmp, "_rbac.yaml",
                       "groups:\n  - name: x\n    match:\n      claims:\n" + bad +
                       "    tenants: [\"*\"]\n    permissions: [read]\n")
            assert validate_file(p, rbac_schema, jsonschema, readers) != [], f"should reject claims {bad!r}"
        ok = _write(tmp, "_rbac.yaml",
                    "groups:\n  - name: x\n    match:\n      claims:\n        org-code: [ORG-1]\n"
                    "    tenants: [\"*\"]\n    permissions: [read]\n")
        assert validate_file(ok, rbac_schema, jsonschema, readers) == []

    def test_rbac_empty_match_is_refused_by_the_parser(self, tmp, rbac_schema):
        # An empty `match: {}` is a LOAD ERROR at runtime — a cross-field rule the
        # schema cannot express, now reported because tenant-api judges the file.
        p = _write(tmp, "_rbac.yaml",
                   "groups:\n  - name: x\n    match: {}\n    tenants: [\"*\"]\n    permissions: [read]\n")
        viol = validate_file(p, rbac_schema, jsonschema, _readers())
        assert len(viol) == 1 and "tenant-api refuses it" in viol[0], viol

    def test_clean_tenant_orgs(self, tmp, tenant_orgs_schema):
        p = _write(tmp, "_tenant_orgs.yaml",
                   "tenant_orgs:\n  db-a: [ORG-4821]\n  db-b: [ORG-4821, ORG-1900]\n  db-c: []\n")
        assert validate_file(p, tenant_orgs_schema, jsonschema, _readers()) == []

    def test_tenant_orgs_toplevel_typo_rejected(self, tmp, tenant_orgs_schema):
        # `tenant_org` (singular typo) — KnownFields refuses it at load.
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_org:\n  db-a: [ORG-1]\n")
        viol = validate_file(p, tenant_orgs_schema, jsonschema, _readers())
        assert any("tenant_org" in v and "tenant-api refuses it" in v for v in viol), viol

    def test_tenant_orgs_non_list_rejected(self, tmp, tenant_orgs_schema):
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_orgs:\n  db-a: ORG-1\n")
        assert validate_file(p, tenant_orgs_schema, jsonschema, _readers()) != []

    def test_tenant_orgs_blank_org_rejected(self, tmp, tenant_orgs_schema):
        # Parser-legal, refused by the schema's authoring minLength.
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_orgs:\n  db-a: ['']\n")
        assert validate_file(p, tenant_orgs_schema, jsonschema, _readers()) != []

    def test_tenant_orgs_numeric_org_is_its_text(self, tmp, tenant_orgs_schema):
        # yaml.v3 decodes the scalar 123 into the []string as its text "123":
        # what the server reads, so no violation.
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_orgs:\n  db-a: [123]\n")
        assert validate_file(p, tenant_orgs_schema, jsonschema, _readers()) == []

    @pytest.mark.parametrize("text", ["", "# no rules yet\n", "groups:\n", "groups: []\n"])
    def test_zero_group_rbac_is_a_violation(self, tmp, rbac_schema, text):
        # tenant-api reads it (config check exits 0), but a server started with
        # --rbac fails closed on zero groups and denies every request.
        p = _write(tmp, "_rbac.yaml", text)
        viol = validate_file(p, rbac_schema, jsonschema, _readers())
        assert len(viol) == 1 and "zero groups" in viol[0] and "denies every request" in viol[0], viol

    def test_empty_tenant_orgs_is_tolerated(self, tmp, tenant_orgs_schema):
        p = _write(tmp, "_tenant_orgs.yaml", "# none yet\n")
        assert validate_file(p, tenant_orgs_schema, jsonschema, _readers()) == []

    def test_list_top_doc_flagged(self, tmp, tenant_orgs_schema):
        p = _write(tmp, "_tenant_orgs.yaml", "- db-a\n- db-b\n")
        viol = validate_file(p, tenant_orgs_schema, jsonschema, _readers())
        assert len(viol) == 1 and "tenant-api refuses it" in viol[0], viol


# --- real committed files (regression guard) -------------------------------

class TestRealFiles:
    def test_real_rbac_seed_clean(self, rbac_schema):
        assert os.path.exists(_REAL_RBAC), _REAL_RBAC
        assert validate_file(_REAL_RBAC, rbac_schema, jsonschema, _readers()) == []

    def test_real_domain_policy_example_clean(self, domain_policy_schema):
        assert os.path.exists(_REAL_DOMAIN_POLICY), _REAL_DOMAIN_POLICY
        assert validate_file(_REAL_DOMAIN_POLICY, domain_policy_schema, jsonschema, _readers()) == []

    def test_schema_map_covers_the_three_admin_stems(self):
        assert set(SCHEMA_MAP) == {"_rbac", "_domain_policy", "_tenant_orgs"}
        assert set(CONFIG_CHECK_KIND) == {"_rbac", "_tenant_orgs"}
        for fn in SCHEMA_MAP.values():
            assert os.path.exists(os.path.join(_SCHEMA_DIR, fn)), fn


# --- identity claims come from the Helm values -----------------------------

class TestClaimDeclaration:
    def test_rendered_like_the_deployment_template(self, tmp):
        values = _values_declaring(tmp, {"region": "X-R", "org": "X-Auth-Request-Org"})
        assert claim_declaration(os.environ["DA_CRDECODE_BINARY"], values) == \
            "org=X-Auth-Request-Org,region=X-R"

    def test_repo_values_declare_none(self):
        assert claim_declaration(os.environ["DA_CRDECODE_BINARY"], _REAL_HELM_VALUES) == ""

    def test_not_a_map_is_a_caller_error(self, tmp):
        values = _write(tmp, "v.yaml", "identity:\n  claimHeaders: [org]\n")
        with pytest.raises(_CallerError):
            claim_declaration(os.environ["DA_CRDECODE_BINARY"], values)


# --- embedded RBAC (production authz shipped as a block-scalar string) -------

_CONFIGMAP_TMPL = (
    "apiVersion: v1\n"
    "kind: ConfigMap\n"
    "metadata:\n"
    "  name: rbac-config\n"
    "data:\n"
    "  _rbac.yaml: |\n"
    "{body}"
)
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
    """Production RBAC ships EMBEDDED as a block-scalar string (Helm
    rbac._rbacYaml), whose file basename is NOT _rbac.yaml. The outer layer is
    read with da-crdecode (as Helm reads it), the string judged by tenant-api."""

    def test_real_helm_values_clean(self, rbac_schema):
        assert validate_embedded_file(
            _REAL_HELM_VALUES, ("rbac", "_rbacYaml"), rbac_schema, jsonschema, _readers()) == []

    def test_bad_permission_enum_in_embedded_rbac_rejected(self, tmp, rbac_schema):
        body = _indent("groups:\n  - name: x\n    tenants: [\"*\"]\n"
                       "    permissions: [readonly]\n", 4)
        p = _write(tmp, "configmap-rbac.yaml", _CONFIGMAP_TMPL.format(body=body))
        viol = validate_embedded_file(p, ("data", "_rbac.yaml"), rbac_schema, jsonschema, _readers())
        assert any("readonly" in v for v in viol), viol

    def test_key_typo_in_embedded_rbac_rejected(self, tmp, rbac_schema):
        body = _indent("groups:\n  - name: x\n    permissons: [read]\n"
                       "    tenants: [\"*\"]\n", 4)
        p = _write(tmp, "values.yaml", _VALUES_TMPL.format(body=body))
        viol = validate_embedded_file(p, ("rbac", "_rbacYaml"), rbac_schema, jsonschema, _readers())
        assert any("permissons" in v and "tenant-api refuses it" in v for v in viol), viol

    def test_missing_embedded_key_is_fail_loud(self, tmp, rbac_schema):
        p = _write(tmp, "values.yaml", "replicaCount: 1\nrbac:\n  _rbacYamlTYPO: |\n    groups: []\n")
        viol = validate_embedded_file(p, ("rbac", "_rbacYaml"), rbac_schema, jsonschema, _readers())
        assert any("not found" in v for v in viol), viol

    def test_embedded_non_string_rejected(self, tmp, rbac_schema):
        p = _write(tmp, "configmap-rbac.yaml",
                   "data:\n  _rbac.yaml:\n    groups: []\n")
        viol = validate_embedded_file(p, ("data", "_rbac.yaml"), rbac_schema, jsonschema, _readers())
        assert any("block-scalar" in v for v in viol), viol

    @pytest.mark.parametrize("value", ["", " |\n    groups: []\n", " |\n    # nothing\n"])
    def test_embedded_zero_group_rbac_is_a_violation(self, tmp, rbac_schema, value):
        # A null or group-less block: the chart mounts it as the --rbac file,
        # and zero groups deny every request.
        p = _write(tmp, "values.yaml", "rbac:\n  _rbacYaml:" + value + "\n")
        viol = validate_embedded_file(p, ("rbac", "_rbacYaml"), rbac_schema, jsonschema, _readers())
        assert len(viol) == 1 and "zero groups" in viol[0], viol

    def test_embedded_duplicate_key_rejected(self, tmp, rbac_schema):
        # Duplicate key INSIDE the block scalar: parse-tree's verdict in tenant-api.
        body = _indent("groups:\n  - name: ops\n    tenants: [\"*\"]\n"
                       "    permissions: [read]\n    permissions: [admin]\n", 4)
        p = _write(tmp, "configmap-rbac.yaml", _CONFIGMAP_TMPL.format(body=body))
        viol = validate_embedded_file(p, ("data", "_rbac.yaml"), rbac_schema, jsonschema, _readers())
        assert any("(unreadable)" in v and "permissions" in v for v in viol), viol

    def test_embedded_rbac_is_checked_against_the_declared_claims(self, tmp, rbac_schema):
        body = _indent("groups:\n  - name: x\n    match:\n      claims:\n        org: [a]\n"
                       "    tenants: [\"*\"]\n    permissions: [read]\n", 4)
        p = _write(tmp, "values.yaml", _VALUES_TMPL.format(body=body))
        assert validate_embedded_file(p, ("rbac", "_rbacYaml"), rbac_schema, jsonschema,
                                      _readers()) != []
        assert validate_embedded_file(p, ("rbac", "_rbacYaml"), rbac_schema, jsonschema,
                                      _readers(_values_declaring(tmp, {"org": "X-Org"}))) == []

    def test_helm_template_not_plain_yaml_raises(self, rbac_schema):
        # The Helm TEMPLATE (document-level Go `{{ }}`) is EXCLUDED by the hook's
        # files regex. If forced through, da-crdecode refuses it → _CallerError
        # (exit 2), never silently deferred.
        assert os.path.exists(_REAL_HELM_TEMPLATE), _REAL_HELM_TEMPLATE
        with pytest.raises(_CallerError):
            validate_embedded_file(
                _REAL_HELM_TEMPLATE, ("data", "_rbac.yaml"), rbac_schema, jsonschema, _readers())

    def test_embedded_source_classifier_is_path_anchored(self):
        assert embedded_source_for("repo/helm/tenant-api/values.yaml") is not None
        assert embedded_source_for("helm/da-portal/values.yaml") is None
        assert embedded_source_for("values.yaml") is None
        assert embedded_source_for("helm/tenant-api/templates/configmap-rbac.yaml") is None


class TestEmbeddedCLI:
    """End-to-end exit codes for the embedded-RBAC path (path-anchored matching,
    so fixtures live under the real repo-relative subpaths)."""

    def test_real_embedded_files_exit_zero(self):
        result = _run(_REAL_HELM_VALUES)
        assert result.returncode == EXIT_OK, result.stderr
        assert "OK:" in result.stdout

    def test_bad_embedded_rbac_exit_one(self, tmp):
        body = _indent("groups:\n  - name: x\n    tenants: [\"*\"]\n"
                       "    permissions: [readonly]\n", 4)
        p = _write_at(tmp, "helm/tenant-api/values.yaml", _VALUES_TMPL.format(body=body))
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION, result.stdout + result.stderr
        assert "readonly" in result.stderr, result.stderr

    def test_missing_key_exit_one(self, tmp):
        p = _write_at(tmp, "helm/tenant-api/values.yaml",
                      "rbac:\n  _rbacYaml_TYPO: |\n    groups: []\n")
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION, result.stdout + result.stderr
        assert "not found" in result.stderr, result.stderr

    def test_templated_rbac_block_is_deferred_not_errored(self, tmp):
        p = _write_at(tmp, "helm/tenant-api/values.yaml",
                      "rbac:\n  _rbacYaml: |\n    {{ .Values.rbacBody | nindent 4 }}\n")
        result = _run(p)
        assert result.returncode == EXIT_OK, result.stdout + result.stderr

    def test_unrelated_template_marker_does_not_defer_bad_rbac(self, tmp):
        # FAIL-OPEN regression: an UNRELATED quoted Go-template value elsewhere in
        # a registered values.yaml must NOT suppress validation of the plain RBAC.
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
        assert result.returncode == EXIT_VIOLATION, result.stdout + result.stderr
        assert "readonly" in result.stderr, result.stderr

    def test_real_embedded_files_are_validated_not_deferred(self):
        result = _run(_REAL_HELM_VALUES)
        assert result.returncode == EXIT_OK, result.stderr
        assert "1 admin meta-config file(s) valid" in result.stdout, result.stdout
        assert "deferred" not in result.stdout.lower(), result.stdout

    def test_helm_template_path_skipped_not_error(self):
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

    def _hook(self) -> dict:
        with open(os.path.join(_REPO, ".pre-commit-config.yaml"), encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        hooks = [h for repo in cfg["repos"] for h in repo.get("hooks", [])]
        return next(h for h in hooks if h.get("id") == "admin-config-schema-check")

    def _hook_files_regex(self) -> re.Pattern:
        return re.compile(self._hook()["files"])

    @staticmethod
    def _handled(rel: str) -> bool:
        return schema_for(rel) is not None or embedded_source_for(rel) is not None

    def test_no_fail_open__every_file_the_hook_selects_is_validated(self):
        pattern = self._hook_files_regex()
        stems = ["_rbac", "_domain_policy", "_tenant_orgs",
                 "_groups", "_federation_policy", "notes", "rbac"]
        exts = [".yaml", ".yml", ".YAML", ".YML", ".Yaml", ".json", ".txt", ".yaml.bak", ""]
        candidates = []
        for prefix in ("", "conf.d/", "deploy/overlays/prod/"):
            for stem in stems:
                for ext in exts:
                    candidates.append(f"{prefix}{stem}{ext}")
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
        assert selected >= len(SCHEMA_MAP) + len(EMBEDDED_RBAC_SOURCES), (
            f"regex selected only {selected} candidate(s) — guard likely vacuous")

    def test_embedded_sources_are_selected_by_the_hook(self):
        pattern = self._hook_files_regex()
        for suffix in EMBEDDED_RBAC_SOURCES:
            assert pattern.search(suffix), f"hook regex must select {suffix}"
            assert pattern.search(f"some/prefix/{suffix}"), suffix

    def test_hook_does_not_select_files_we_cannot_validate(self):
        pattern = self._hook_files_regex()
        for rel in ("conf.d/_rbac.txt", "conf.d/notes.yaml",
                    "conf.d/rbac.yaml", "conf.d/_rbac.yaml.bak",
                    "helm/da-portal/values.yaml", "helm/vector/values.yaml",
                    "values.yaml",
                    "helm/tenant-api/templates/configmap-rbac.yaml"):
            assert not pattern.search(rel), f"hook regex unexpectedly selects {rel}"

    def test_hook_runs_with_the_go_readers(self):
        # ADR-036 §12: the hook's entry provides tenant-api / da-crdecode /
        # da-guard (built when unset), so it never runs without them.
        assert self._hook()["entry"].startswith("bash scripts/ops/with_admin_config_tools.sh "), \
            self._hook()["entry"]

    def test_yml_variant_is_validated_not_skipped(self, tmp):
        p = _write(tmp, "_rbac.yml", "groups:\n  - name: x\n    permissons: [read]\n")
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION, (
            f"a broken _rbac.yml must FAIL the gate, got exit {result.returncode}")

    def test_duplicate_key_rejected(self, tmp):
        # yaml.v3 (parse-tree's verdict in tenant-api) refuses a duplicate key; the
        # gate reports it as a violation (exit 1), never a silent last-wins pass.
        p = _write(tmp, "_rbac.yaml",
                   "groups:\n  - name: ops\n    tenants: [\"*\"]\n"
                   "    permissions: [read]\n    permissions: [admin]\n")
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION, result.stdout + result.stderr
        assert "duplicate_key" in result.stderr, result.stderr

    def test_duplicate_toplevel_key_rejected(self, tmp):
        p = _write(tmp, "_tenant_orgs.yaml",
                   "tenant_orgs:\n  db-a: [ORG-1]\ntenant_orgs:\n  db-b: [ORG-2]\n")
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION


# --- fail-closed: a reader that cannot run is exit 2, never a pass ---------

class TestFailClosed:
    def _env_without(self, *names: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in names}
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if not any(os.path.exists(os.path.join(p, b)) for b in
                                                 ("tenant-api", "da-crdecode", "da-guard")))
        return env

    def test_no_tenant_api_is_exit_two(self, tmp):
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_orgs: {}\n")
        result = _run(p, env=self._env_without("TENANT_API_BINARY"))
        assert result.returncode == EXIT_CALLER_ERROR, result.stdout + result.stderr
        assert "tenant-api was not found" in result.stderr

    def test_no_crdecode_is_exit_two(self):
        result = _run(_REAL_RBAC, env=self._env_without("DA_CRDECODE_BINARY"))
        assert result.returncode == EXIT_CALLER_ERROR, result.stdout + result.stderr
        assert "da-crdecode was not found" in result.stderr

    def test_no_da_guard_is_exit_two(self):
        result = _run(_REAL_DOMAIN_POLICY, env=self._env_without("DA_GUARD_BINARY"))
        assert result.returncode == EXIT_CALLER_ERROR, result.stdout + result.stderr

    def test_binary_flag_wins(self, tmp):
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_orgs: {}\n")
        result = _run("--tenant-api-binary", os.path.join(tmp, "nope"), p)
        assert result.returncode == EXIT_CALLER_ERROR, result.stdout + result.stderr

    def test_a_binary_without_config_check_is_exit_two(self, tmp):
        # A tenant-api older than `config check` prints no config-check document.
        fake = _write(tmp, "old-tenant-api", "#!/bin/sh\necho 'usage: tenant-api state restore' >&2\nexit 2\n")
        os.chmod(fake, 0o755)
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_orgs: {}\n")
        result = _run("--tenant-api-binary", fake, p)
        assert result.returncode == EXIT_CALLER_ERROR, result.stdout + result.stderr
        assert "a tenant-api that has `config check`" in result.stderr, result.stderr


# --- parser parity: the schema must never be STRICTER than the parser ------

class TestParserParity:
    """Every case here is a config the Go parser ACCEPTS. yaml.v3 KnownFields
    decodes a present-but-null value to the zero value WITHOUT a load error, and
    the domain-policy reader is LENIENT. A schema that rejected any of these
    (on what the parser decoded) would block a legitimate commit."""

    def test_rbac_rule_null_lists(self, tmp, rbac_schema):
        # (A config with zero groups is refused on purpose: TestValidateFile.)
        p = _write(tmp, "_rbac.yaml", "groups:\n  - name: x\n    tenants:\n    permissions:\n")
        assert validate_file(p, rbac_schema, jsonschema, _readers()) == []

    def test_tenant_orgs_null_org_list(self, tmp, tenant_orgs_schema):
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_orgs:\n  db-a:\n")
        assert validate_file(p, tenant_orgs_schema, jsonschema, _readers()) == []

    def test_tenant_orgs_null_map(self, tmp, tenant_orgs_schema):
        p = _write(tmp, "_tenant_orgs.yaml", "tenant_orgs:\n")
        assert validate_file(p, tenant_orgs_schema, jsonschema, _readers()) == []

    def test_domain_policy_require_critical_escalation(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    tenants: [db-a]\n"
                   "    constraints:\n      require_critical_escalation: true\n")
        assert validate_file(p, domain_policy_schema, jsonschema, _readers()) == []

    def test_domain_policy_tenant_id_is_its_text(self, tmp, domain_policy_schema):
        # `tenants: [010]` names tenant "010" (the text the exporter keys it by).
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    tenants: [010]\n")
        assert validate_file(p, domain_policy_schema, jsonschema, _readers()) == []

    def test_domain_policy_description_only(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    description: placeholder\n")
        assert validate_file(p, domain_policy_schema, jsonschema, _readers()) == []

    def test_domain_policy_empty_map(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml", "domain_policies: {}\n")
        assert validate_file(p, domain_policy_schema, jsonschema, _readers()) == []

    def test_domain_policy_fractional_duration(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    constraints:\n"
                   "      max_repeat_interval: 1.5h\n")
        assert validate_file(p, domain_policy_schema, jsonschema, _readers()) == []


# --- domain-policy negatives (this schema is the SOLE guard there) ---------

class TestDomainPolicyNegatives:
    def test_typo_constraint_key_rejected(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    constraints:\n"
                   "      reqire_critical_escalation: true\n")
        viol = validate_file(p, domain_policy_schema, jsonschema, _readers())
        assert any("reqire_critical_escalation" in v for v in viol), viol

    def test_bad_receiver_type_rejected(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    constraints:\n"
                   "      allowed_receiver_types: [carrier-pigeon]\n")
        assert validate_file(p, domain_policy_schema, jsonschema, _readers()) != []

    def test_bad_duration_rejected(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml",
                   "domain_policies:\n  finance:\n    constraints:\n"
                   "      max_repeat_interval: 1hour\n")
        assert validate_file(p, domain_policy_schema, jsonschema, _readers()) != []

    def test_unknown_toplevel_key_rejected(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml", "domain_polices: {}\n")
        viol = validate_file(p, domain_policy_schema, jsonschema, _readers())
        assert any("domain_polices" in v for v in viol), viol

    def test_parse_tree_refusal_is_a_violation(self, tmp, domain_policy_schema):
        p = _write(tmp, "_domain_policy.yaml", "domain_policies: {}\ndomain_policies: {}\n")
        viol = validate_file(p, domain_policy_schema, jsonschema, _readers())
        assert len(viol) == 1 and "parse-tree refuses it (duplicate_key)" in viol[0], viol


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
        result = _run("--schema-dir", os.path.join(_REPO, "no", "such"), _REAL_RBAC)
        assert result.returncode == EXIT_CALLER_ERROR

    def test_extensions_are_both_spellings(self):
        assert ADMIN_EXTENSIONS == (".yaml", ".yml")
