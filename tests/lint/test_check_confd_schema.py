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
# The shipped conf.d trees that went unvalidated until `confd-schema-check-shipped`
# was added — demo stack + the example configs users copy from. Kept in sync with
# that hook's --config-dir list by test_shipped_hook_covers_every_shipped_tree.
_REAL_TRYLOCAL_CONFD = os.path.join(_REPO, "try-local", "seed", "conf.d")
_REAL_SHIPPED_CONFD = [
    _REAL_TRYLOCAL_CONFD,
    os.path.join(_REPO, "rule-packs", "recipes", "examples", "conf.d"),
    os.path.join(_REPO, "components", "da-tools", "app", "examples",
                 "cardinality-demo", "conf.d"),
]
_PRECOMMIT_CFG = os.path.join(_REPO, ".pre-commit-config.yaml")
_CI_WORKFLOW = os.path.join(_REPO, ".github", "workflows", "ci.yml")
_SHIPPED_HOOK_ID = "confd-schema-check-shipped"


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


def _run(*config_dirs: str):
    argv = [sys.executable, _SCRIPT]
    for d in config_dirs:
        argv += ["--config-dir", d]
    return subprocess.run(  # subprocess-timeout: ignore
        argv, capture_output=True, text=True, encoding="utf-8",
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
        _write(confd, "_defaults.yaml", "defaults:\n  mysql_threads_running: 80\n")
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

    def test_bare_number_quantile_rejected(self, confd, schema):
        # #1017: quantile is string-only. A bare YAML number is dialect-ambiguous
        # (PyYAML 1.1 reads a dotless exponent like 95e-2 as a string, yaml.v3 as
        # a float) and silently splits the Go/Python recipe_id join → the alert
        # never fires. The schema gate must force the quote at author/CI time.
        _write(confd, "db-a.yaml",
               'tenants:\n  db-a:\n    _custom_alerts:\n'
               '      - recipe: p99_latency\n'
               '        name: p99_slow\n'
               '        metric: http_request_duration_seconds\n'
               '        quantile: 0.99\n'
               '        op: ">"\n'
               '        window: 5m\n'
               '        threshold: "2:warning"\n')
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema)
        assert any("quantile" in v and "string" in v for v in viol), viol

    def test_quoted_quantile_valid(self, confd, schema):
        # #1017 companion: the quoted form (the parity contract) stays valid.
        _write(confd, "db-a.yaml",
               'tenants:\n  db-a:\n    _custom_alerts:\n'
               '      - recipe: p99_latency\n'
               '        name: p99_slow\n'
               '        metric: http_request_duration_seconds\n'
               '        quantile: "0.99"\n'
               '        op: ">"\n'
               '        window: 5m\n'
               '        threshold: "2:warning"\n')
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema)
        assert viol == [], viol

    def test_real_confd_is_clean(self, schema):
        # The shipped conf.d must stay schema-valid (regression guard).
        checked, viol, _skipped = validate_dir(_REAL_CONFD, schema, jsonschema)
        assert viol == [], f"shipped conf.d violates the schema: {viol}"
        assert checked >= 2

    @pytest.mark.parametrize("tree", _REAL_SHIPPED_CONFD)
    def test_real_shipped_confd_is_clean(self, tree, schema):
        """Every SHIPPED conf.d tree must stay schema-valid.

        Demo stack + the examples users copy from. try-local was red with no gate
        to notice; the other two had the same zero coverage and just happened to
        be clean. Twin of test_real_confd_is_clean for the non-exporter trees.
        """
        checked, viol, _skipped = validate_dir(tree, schema, jsonschema)
        assert viol == [], f"{tree} violates the schema: {viol}"
        assert checked >= 1

    def test_shipped_hook_covers_every_shipped_tree(self):
        """The hook's --config-dir list must match the trees this file asserts on.

        Drift guard: without it, adding a tree to one side silently leaves the
        other blind — the exact failure mode that let try-local rot unnoticed.
        """
        with open(_PRECOMMIT_CFG, encoding="utf-8") as fh:
            cfg = fh.read()
        entry = cfg.split(f"id: {_SHIPPED_HOOK_ID}", 1)[1].split("language:", 1)[0]
        hooked = {ln.split("--config-dir", 1)[1].strip()
                  for ln in entry.splitlines() if "--config-dir" in ln}
        expected = {os.path.relpath(p, _REPO).replace(os.sep, "/")
                    for p in _REAL_SHIPPED_CONFD}
        assert hooked == expected, f"hook covers {hooked}, tests cover {expected}"

    def test_shipped_hook_is_named_by_the_ci_workflow(self):
        """.github/workflows/ci.yml must invoke the hook by name.

        Defining a hook is not the same as CI running it: Vibe has no blanket
        `pre-commit run --all-files`, so a hook no workflow names by hand has ZERO
        CI enforcement (#1223). Deleting the invocation line would silently retire
        this gate — nothing else would go red — so the link is asserted here rather
        than left to review. A typo'd id fails loudly (pre-commit rejects unknown
        ids); a DELETED line is the silent case this covers.
        """
        with open(_CI_WORKFLOW, encoding="utf-8") as fh:
            ci = fh.read()
        assert f"pre-commit run {_SHIPPED_HOOK_ID}" in ci, (
            f"{_SHIPPED_HOOK_ID} is defined but .github/workflows/ci.yml never runs "
            f"it — the gate would exist without ever executing in CI")

    def test_severity_dedup_accepts_runtime_vocabulary(self, confd, schema):
        """The enum must not drift away from what the resolver honours.

        It once read ['auto','manual','disable'] — a vocabulary NOTHING in the
        repo speaks: ResolveSeverityDedup (resolve.go) accepts enable/disable,
        scaffold_tenant.py's --severity-dedup offers exactly those two, and every
        doc says the same. That drift made a valid config fail the lint.
        """
        for value in ("enable", "disable"):
            _write(confd, "db-a.yaml",
                   f'tenants:\n  db-a:\n    _severity_dedup: "{value}"\n')
            _checked, viol, _skipped = validate_dir(confd, schema, jsonschema)
            assert viol == [], f"_severity_dedup: {value!r} must validate: {viol}"

    def test_severity_dedup_rejects_unknown_value(self, confd, schema):
        """A typo'd value must be caught here — the schema is the only catcher.

        Negative twin: the resolver only WARNs and falls back to enable on an
        unknown value, so nothing downstream reddens. Contract pin rather than
        new detection power: the pre-fix enum rejected 'enabel' too.
        """
        _write(confd, "db-a.yaml", 'tenants:\n  db-a:\n    _severity_dedup: "enabel"\n')
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema)
        assert len(viol) == 1, viol
        assert "_severity_dedup" in viol[0]


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
               "defaults:\n  mysql_threads_running: 80\nstate_flters:\n  x:\n    severity: warning\n")
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert any("state_flters" in v for v in viol)

    def test_defaults_multidb_basename_also_guarded(self, confd, schema, platform_schema):
        _write(confd, "_defaults-multidb.yaml", "defalts:\n  mysql_threads_running: 80\n")  # 'defalts' typo
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert any("defalts" in v for v in viol)

    def test_inherited_override_keys_pass(self, confd, schema, platform_schema):
        # A _defaults.yaml carrying inherited tenant-override keys (reserved keys +
        # _state_*/_routing prefixes) must NOT false-red.
        _write(confd, "_defaults.yaml",
               "_metadata:\n  owner: dba\n_severity_dedup: enable\n"
               "_state_maintenance: disable\n_routing_enforced: true\n")
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert viol == []

    def test_loader_legit_toplevel_keys_pass(self, confd, schema, platform_schema):
        # ThresholdConfig (types.go) reads `tenants`/`profiles`/`max_metrics_per_tenant`
        # from ANY conf.d file (Go has no KnownFields) → they are loader-legitimate in a
        # _defaults.yaml and must NOT false-red (esp. a platform-wide cardinality cap with
        # no other `_*` home). Adversarial review S1.
        _write(confd, "_defaults.yaml",
               "defaults:\n  mysql_threads_running: 80\nmax_metrics_per_tenant: 500\n"
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

    def test_null_nested_blocks_tolerated(self, confd, schema, platform_schema):
        # `defaults: null` / empty `state_filters:` → Go unmarshals into a nil map
        # (valid empty-block placeholder) → schema must tolerate `null`, else
        # Schema-red-but-system-runs friction. Gemini #913 對抗1.
        _write(confd, "_defaults.yaml", "defaults: null\nstate_filters:\n")
        _checked, viol, _skipped = validate_dir(confd, schema, jsonschema, platform_schema)
        assert viol == [], f"null nested defaults/state_filters false-rejected: {viol}"

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

    def test_shipped_trees_exit_zero_in_one_invocation(self):
        """The real hook invocation: several --config-dir in one run."""
        result = _run(*_REAL_SHIPPED_CONFD)
        assert result.returncode == EXIT_OK, result.stderr
        assert "OK:" in result.stdout

    def test_multi_dir_violation_names_the_offending_tree(self, confd, tmp_path):
        """A violation must name its tree once several are scanned.

        With >1 tree in play a bare relpath does not say WHERE to go fix.
        """
        clean = tmp_path / "clean"
        clean.mkdir()
        _write(str(clean), "ok.yaml", 'tenants:\n  db-a:\n    _severity_dedup: "enable"\n')
        _write(confd, "db-a.yaml", 'tenants:\n  db-a:\n    _metadata:\n      dbType: mariadb\n')
        result = _run(str(clean), confd)
        assert result.returncode == EXIT_VIOLATION
        assert confd.replace(os.sep, "/") in result.stderr, result.stderr

    def test_one_missing_dir_among_several_exits_two(self):
        """Fail-closed: a typo'd path must not silently shrink coverage.

        Otherwise the gate quietly validates only the dirs that happen to exist.
        """
        result = _run(_REAL_TRYLOCAL_CONFD, os.path.join(_REPO, "no", "such", "dir"))
        assert result.returncode == EXIT_CALLER_ERROR
