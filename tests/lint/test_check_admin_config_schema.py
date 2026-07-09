"""Tests for check_admin_config_schema.py (Gemini #1056 disposition 3b —
admin meta-config ↔ JSON schema pre-merge gate).

jsonschema is REQUIRED by the tool, but the CI "Python Tests" job installs only
pyyaml/pytest/... (no jsonschema) — there the pre-commit `admin-config-schema-check`
hook (its own venv carries jsonschema via additional_dependencies) exercises the
behaviour. So skip this whole module when jsonschema is absent; it still runs
locally and in the dev container. The exit-code gate (tests/shared/test_tool_exit_codes.py)
separately covers --help / bad-args WITHOUT jsonschema thanks to the tool's lazy import.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

jsonschema = pytest.importorskip("jsonschema")

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools", "lint"))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools"))

from check_admin_config_schema import validate_file, SCHEMA_MAP  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

_SCRIPT = os.path.join(_REPO, "scripts", "tools", "lint", "check_admin_config_schema.py")
_SCHEMA_DIR = os.path.join(_REPO, "docs", "schemas")
_REAL_RBAC = os.path.join(_REPO, "try-local", "seed", "conf.d", "_rbac.yaml")
_REAL_DOMAIN_POLICY = os.path.join(
    _REPO, "components", "threshold-exporter", "config", "conf.d", "examples", "_domain_policy.yaml")


def _load(name: str) -> dict:
    with open(os.path.join(_SCHEMA_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def rbac_schema():
    return _load("rbac.schema.json")


@pytest.fixture(scope="module")
def tenant_orgs_schema():
    return _load("tenant-orgs.schema.json")


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

    def test_schema_map_covers_the_three_admin_files(self):
        assert set(SCHEMA_MAP) == {"_rbac.yaml", "_domain_policy.yaml", "_tenant_orgs.yaml"}
        # every mapped schema file must exist
        for fn in SCHEMA_MAP.values():
            assert os.path.exists(os.path.join(_SCHEMA_DIR, fn)), fn


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
