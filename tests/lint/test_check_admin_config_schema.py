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
    SCHEMA_MAP,
    schema_for,
    validate_file,
)
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

    def test_every_file_the_hook_selects_is_validated(self):
        pattern = self._hook_files_regex()
        for stem in SCHEMA_MAP:
            for ext in ADMIN_EXTENSIONS:
                rel = f"conf.d/{stem}{ext}"
                assert pattern.search(rel), f"hook regex does not select {rel}"
                assert schema_for(rel) is not None, (
                    f"FAIL-OPEN: hook selects {rel} but the script skips it (exit 0)")

    def test_hook_does_not_select_files_we_cannot_validate(self):
        pattern = self._hook_files_regex()
        for rel in ("conf.d/_rbac.txt", "conf.d/notes.yaml",
                    "conf.d/rbac.yaml", "conf.d/_rbac.yaml.bak"):
            assert not pattern.search(rel), f"hook regex unexpectedly selects {rel}"

    def test_yml_variant_is_validated_not_skipped(self, tmp):
        p = _write(tmp, "_rbac.yml", "groups:\n  - name: x\n    permissons: [read]\n")
        result = _run(p)
        assert result.returncode == EXIT_VIOLATION, (
            f"a broken _rbac.yml must FAIL the gate, got exit {result.returncode}")


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
