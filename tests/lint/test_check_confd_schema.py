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
_REAL_CONFD = os.path.join(_REPO, "components", "threshold-exporter", "config", "conf.d")


@pytest.fixture(scope="module")
def schema():
    with open(_SCHEMA, encoding="utf-8") as fh:
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
