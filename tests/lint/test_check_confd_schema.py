"""Tests for check_confd_schema.py (#880 conf.d ↔ tenant-config schema gate).

jsonschema is REQUIRED by the tool, but the CI "Python Tests" job installs only
pyyaml/pytest/... (no jsonschema) — there the pre-commit `confd-schema-check` hook
(its own venv carries jsonschema via additional_dependencies) is what exercises the
behaviour. So skip this whole module when jsonschema is absent; it still runs locally
and in the dev container. The exit-code gate (tests/shared/test_tool_exit_codes.py)
separately covers --help / bad-args WITHOUT jsonschema thanks to the tool's lazy import.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema")

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools", "lint"))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools"))

from check_confd_schema import validate_dir  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

_SCRIPT = os.path.join(_REPO, "scripts", "tools", "lint", "check_confd_schema.py")
_SCHEMA = os.path.join(_REPO, "docs", "schemas", "tenant-config.schema.json")
_PLATFORM_SCHEMA = os.path.join(_REPO, "docs", "schemas", "platform-defaults.schema.json")
_REAL_CONFD = os.path.join(_REPO, "components", "threshold-exporter", "config", "conf.d")


@pytest.fixture(scope="module")
def schema():
    with open(_SCHEMA, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def platform_schema():
    with open(_PLATFORM_SCHEMA, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def confd():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _write(d: str, name: str, text: str) -> None:
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        fh.write(text)


def _run(config_dir: str):
    return subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, _SCRIPT, "--config-dir", config_dir],
        capture_output=True, text=True, encoding="utf-8",
    )


# --- validate_dir (direct) -------------------------------------------------

class TestValidateDir:
    def test_clean_tenant_file(self, confd, schema):
        _write(confd, "db-a.yaml",
               'tenants:\n  db-a:\n    mysql_connections: "70"\n    _metadata:\n      db_type: mariadb\n')
        checked, viol, skipped = validate_dir(confd, schema, jsonschema)
        assert (checked, viol, skipped) == (1, [], [])

    def test_key_typo_rejected(self, confd, schema):
        _write(confd, "db-a.yaml", 'tenants:\n  db-a:\n    _metadata:\n      dbType: mariadb\n')
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema)
        assert any("dbType" in v for v in viol)

    def test_value_typo_rejected(self, confd, schema):
        _write(confd, "db-a.yaml", 'tenants:\n  db-a:\n    _metadata:\n      db_type: maraidb\n')
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema)
        assert any("maraidb" in v for v in viol)

    def test_no_db_type_is_opt_out_not_error(self, confd, schema):
        # Opt-in design: a tenant with _metadata but no db_type is NOT monitored
        # for liveness and that must stay valid (not forced-required).
        _write(confd, "svc.yaml", 'tenants:\n  svc:\n    _metadata:\n      owner: dba-team\n')
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema)
        assert viol == []

    def test_scalar_state_maintenance_valid(self, confd, schema):
        # #880 widened maintenanceMode to oneOf[scalar, object]; scalar must pass.
        _write(confd, "r.yaml", 'tenants:\n  r:\n    _state_maintenance: "enable"\n')
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema)
        assert viol == []

    def test_meta_file_skipped(self, confd, schema):
        _write(confd, "_defaults.yaml", "defaults:\n  mysql_cpu: 80\n")
        _write(confd, "db-a.yaml", 'tenants:\n  db-a:\n    _metadata:\n      db_type: redis\n')
        checked, viol, skipped = validate_dir(confd, schema, jsonschema)
        assert checked == 1 and viol == [] and skipped == ["_defaults.yaml"]

    def test_tenant_file_missing_wrapper_flagged(self, confd, schema):
        # A non-underscore file that forgot its `tenants:` wrapper is a tenant file
        # gone wrong → schema 'required: [tenants]' must catch it.
        _write(confd, "oops.yaml", "db-a:\n  mysql_connections: '70'\n")
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema)
        assert any("tenants" in v for v in viol)

    def test_non_dict_tenant_file_flagged(self, confd, schema):
        # A tenant-shaped (non-`_`) file whose top doc is a list/scalar must be
        # FLAGGED, not silently skipped (#880 CodeRabbit hardening gap).
        _write(confd, "oops-list.yaml", "- a\n- b\n")
        _write(confd, "oops-scalar.yaml", "just-a-string\n")
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema)
        assert sum("must be a mapping" in v for v in viol) == 2

    def test_real_confd_is_clean(self, schema):
        # The shipped conf.d must stay schema-valid (regression guard).
        checked, viol, _skipped = validate_dir(_REAL_CONFD, schema, jsonschema)
        assert viol == [], f"shipped conf.d violates the schema: {viol}"
        assert checked >= 2


# --- _defaults.yaml platform-schema guard (#658 fast-follow / Gemini 對抗3) ---

class TestDefaultsValidation:
    """_defaults*.yaml validate against platform-defaults.schema.json (top-level
    key guard); other `_*` meta-files stay skipped. Routed only when a
    platform_schema is passed (the CLI passes it by default)."""

    def test_clean_defaults_pass(self, confd, schema, platform_schema):
        _write(confd, "_defaults.yaml",
               "defaults:\n  mysql_connections: 80\n"
               "state_filters:\n  maintenance:\n    severity: info\n"
               "_routing_defaults:\n  receiver:\n    type: webhook\n")
        checked, viol, skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert (checked, viol, skipped) == (1, [], [])

    def test_toplevel_typo_rejected(self, confd, schema, platform_schema):
        # `state_flters` (typo of state_filters) would otherwise SILENTLY drop the
        # whole platform-default block — the exact silent-failure this guard kills.
        _write(confd, "_defaults.yaml",
               "defaults:\n  mysql_cpu: 80\nstate_flters:\n  x:\n    severity: warning\n")
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert any("state_flters" in v for v in viol)

    def test_defaults_multidb_basename_also_guarded(self, confd, schema, platform_schema):
        _write(confd, "_defaults-multidb.yaml", "defalts:\n  mysql_cpu: 80\n")  # 'defalts' typo
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert any("defalts" in v for v in viol)

    def test_inherited_override_keys_pass(self, confd, schema, platform_schema):
        # A _defaults.yaml carrying inherited tenant-override keys (reserved keys +
        # _state_*/_routing prefixes) must NOT false-red.
        _write(confd, "_defaults.yaml",
               "_metadata:\n  owner: dba\n_severity_dedup: auto\n"
               "_state_maintenance: disable\n_routing_enforced: true\n")
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert viol == []

    def test_loader_legit_toplevel_keys_pass(self, confd, schema, platform_schema):
        # ThresholdConfig (types.go) reads `tenants`/`profiles`/`max_metrics_per_tenant`
        # from ANY conf.d file (Go has no KnownFields) → they are loader-legitimate in a
        # _defaults.yaml and must NOT false-red (esp. a platform-wide cardinality cap with
        # no other `_*` home). Adversarial review S1.
        _write(confd, "_defaults.yaml",
               "defaults:\n  mysql_cpu: 80\nmax_metrics_per_tenant: 500\n"
               "profiles:\n  std: {}\n")
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert viol == [], f"loader-legit top-level keys false-rejected: {viol}"

    def test_empty_or_null_defaults_tolerated(self, confd, schema, platform_schema):
        # An empty / comment-only / explicit-`null` _defaults.yaml is loader-legal (a
        # placeholder) → must NOT be flagged "must be a mapping". Adversarial review N1.
        _write(confd, "_defaults.yaml", "# placeholder, no defaults yet\nnull\n")
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert viol == [], f"null/empty _defaults.yaml false-rejected: {viol}"

    def test_list_defaults_still_flagged(self, confd, schema, platform_schema):
        # A _defaults.yaml whose top doc is a LIST/scalar (not None) is still malformed.
        _write(confd, "_defaults.yaml", "- a\n- b\n")
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert any("must be a mapping" in v for v in viol)

    def test_other_meta_still_skipped(self, confd, schema, platform_schema):
        # Only _defaults* route to the platform schema; other _* stay skipped even
        # when a platform_schema is supplied.
        _write(confd, "_routing_profiles.yaml", "profiles:\n  p1:\n    receiver: x\n")
        _checked, _viol, skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert skipped == ["_routing_profiles.yaml"]

    def test_real_defaults_files_pass(self, schema, platform_schema):
        # The shipped _defaults.yaml / _defaults-multidb.yaml must stay valid.
        _checked, viol, _skipped = validate_dir(_REAL_CONFD, schema, jsonschema, platform_schema)
        assert viol == [], f"shipped _defaults.yaml violates platform schema: {viol}"


class TestPlatformSchemaDriftGuard:
    """platform-defaults.schema.json's enumerated _* keys must stay a superset of
    the reserved-key SSOT (_lib_constants.py) — else a newly added reserved key
    would make this guard FALSE-REJECT a legitimate inherited override in
    _defaults.yaml. This is the cross-surface drift guard for the new schema."""

    def test_accepts_every_reserved_key(self, platform_schema):
        from _lib_constants import VALID_RESERVED_KEYS, VALID_RESERVED_PREFIXES
        props = set(platform_schema.get("properties", {}))
        patterns = [p.lstrip("^") for p in platform_schema.get("patternProperties", {})]
        missing = {
            k for k in VALID_RESERVED_KEYS
            if k not in props and not any(k.startswith(p) for p in patterns)
        }
        assert not missing, (
            f"platform-defaults.schema.json rejects reserved key(s) {sorted(missing)} — add "
            f"to properties (or rely on a ^prefix) so a legit inherited override isn't false-red.")

    def test_reserved_prefixes_covered(self, platform_schema):
        from _lib_constants import VALID_RESERVED_PREFIXES
        patterns = [p.lstrip("^") for p in platform_schema.get("patternProperties", {})]
        for pfx in VALID_RESERVED_PREFIXES:
            assert pfx in patterns, (
                f"reserved prefix {pfx!r} missing from platform schema patternProperties "
                f"{patterns} → a legit inherited {pfx}* override would be false-rejected.")


# --- CLI exit codes --------------------------------------------------------

class TestCLI:
    def test_real_confd_exit_zero(self):
        result = _run(_REAL_CONFD)
        assert result.returncode == EXIT_OK, result.stderr
        assert "OK:" in result.stdout

    def test_violation_exit_one(self, confd):
        _write(confd, "db-a.yaml", 'tenants:\n  db-a:\n    _metadata:\n      dbType: mariadb\n')
        result = _run(confd)
        assert result.returncode == EXIT_VIOLATION
        assert "dbType" in result.stderr

    def test_missing_dir_exit_two(self):
        result = _run(os.path.join(_REPO, "no", "such", "dir"))
        assert result.returncode == EXIT_CALLER_ERROR
