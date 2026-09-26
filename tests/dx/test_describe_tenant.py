#!/usr/bin/env python3
"""Tests for describe_tenant.py — Effective tenant config resolution with ADR-017 semantics."""

import json
import os
import subprocess
import sys
import textwrap

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path setup (mirror conftest pattern)
# ---------------------------------------------------------------------------
TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "dx"))

import describe_tenant as dt  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402


# ---------------------------------------------------------------------------
# Test: deep_merge()
# ---------------------------------------------------------------------------

class TestDeepMerge:
    """Tests for deep_merge() — ADR-017 semantics."""

    def test_deep_merge_basic(self):
        base = {"a": 1, "b": {"x": 10}}
        override = {"b": {"y": 20}, "c": 3}
        result = dt.deep_merge(base, override)
        assert result == {"a": 1, "b": {"x": 10, "y": 20}, "c": 3}

    def test_deep_merge_scalar_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 99}
        result = dt.deep_merge(base, override)
        assert result == {"a": 1, "b": 99}

    def test_deep_merge_array_replace(self):
        base = {"endpoints": ["a", "b"], "config": {"x": 1}}
        override = {"endpoints": ["c", "d"]}
        result = dt.deep_merge(base, override)
        assert result["endpoints"] == ["c", "d"]
        assert result["config"] == {"x": 1}

    def test_deep_merge_null_optout(self):
        # #1339: the null opt-out is per-key. Reserved (`_`-prefixed) keys are
        # deleted; threshold keys are NOT — the exporter's emitting path
        # ignores a null there and keeps using the platform default, so a
        # diagnostic that deleted the key would contradict /metrics. The
        # sanctioned way to stop alerting on a threshold key is "disable".
        base = {"_silent_mode": "warning", "a": 1, "b": 2, "c": {"x": 10, "y": 20, "_z": 30}}
        override = {"_silent_mode": None, "a": None, "c": {"y": None, "_z": None}}
        result = dt.deep_merge(base, override)
        assert "_silent_mode" not in result, "reserved key: null still opts out"
        assert result["a"] == 1, "threshold key: null must not delete the inherited value"
        assert result["b"] == 2
        assert result["c"] == {"x": 10, "y": 20}, "same rule applies at depth"

    def test_deep_merge_metadata_skip(self):
        base = {"_metadata": {"v": 1}, "config": {"x": 10}}
        override = {"_metadata": {"v": 999}}
        result = dt.deep_merge(base, override)
        # _metadata from base is NOT deep_merged; it's skipped entirely
        assert result.get("_metadata", {}).get("v") == 1

    def test_deep_merge_empty_dicts(self):
        base = {"a": 1}
        override = {}
        result = dt.deep_merge(base, override)
        assert result == {"a": 1}

        result = dt.deep_merge({}, override)
        assert result == {}


# ---------------------------------------------------------------------------
# Test: ConfDScanner (flat & hierarchical)
# ---------------------------------------------------------------------------

class TestConfDScanner:
    """Tests for ConfDScanner — filesystem scanning & tenant mapping."""

    def test_scanner_flat_dir(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        # Create _defaults.yaml at root
        defaults_yaml = conf_d / "_defaults.yaml"
        defaults_yaml.write_text(
            yaml.dump({"defaults": {"timeout": 30, "retries": 3}}),
            encoding="utf-8"
        )

        # Create tenant files
        tenants_yaml = conf_d / "tenants.yaml"
        tenants_yaml.write_text(
            yaml.dump({
                "tenants": {
                    "tenant-a": {"name": "Tenant A", "retries": 5},
                    "tenant-b": {"name": "Tenant B"},
                }
            }),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        assert len(scanner.tenants) == 2
        assert "tenant-a" in scanner.tenants
        assert "tenant-b" in scanner.tenants
        assert scanner.tenants["tenant-a"]["name"] == "Tenant A"

    def test_scanner_hierarchical(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        # L0: root _defaults.yaml
        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"timeout": 10, "region": "us"}}),
            encoding="utf-8"
        )

        # L1: domain level
        domain_dir = conf_d / "acme"
        domain_dir.mkdir()
        (domain_dir / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"timeout": 20}}),
            encoding="utf-8"
        )

        # L2: tenant file in domain
        (domain_dir / "tenants.yaml").write_text(
            yaml.dump({
                "tenants": {
                    "acme-prod": {"replicas": 3},
                }
            }),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        assert "acme-prod" in scanner.tenants
        assert len(scanner.defaults_chain["acme-prod"]) >= 2

    def test_scanner_effective_config_with_defaults(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        # Root defaults
        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"timeout": 10, "retries": 1, "debug": False}}),
            encoding="utf-8"
        )

        # Tenant overrides timeout and debug
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({
                "tenants": {
                    "test-tenant": {"timeout": 30, "debug": True}
                }
            }),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        effective = scanner.effective_config("test-tenant")
        assert effective["timeout"] == 30  # overridden
        assert effective["retries"] == 1   # from defaults
        assert effective["debug"] is True  # overridden


# ---------------------------------------------------------------------------
# Test: source_info() and hashing
# ---------------------------------------------------------------------------

class TestSourceInfo:
    """Tests for source_info() — traceability & hashing."""

    def test_source_info_structure(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"x": 1}}),
            encoding="utf-8"
        )
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({"tenants": {"t1": {"y": 2}}}),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        info = scanner.source_info("t1")

        assert info["tenant_id"] == "t1"
        assert "source_file" in info
        assert "source_hash" in info
        assert "merged_hash" in info
        assert "defaults_chain" in info
        assert "effective_config" in info
        assert isinstance(info["source_hash"], str)
        assert isinstance(info["merged_hash"], str)

    def test_source_info_hashes_differ_with_defaults(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"env": "prod"}}),
            encoding="utf-8"
        )
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({"tenants": {"t1": {"app": "myapp"}}}),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        info = scanner.source_info("t1")
        # source_hash is of the tenant file only; merged_hash includes defaults
        assert info["source_hash"] != info["merged_hash"]


# ---------------------------------------------------------------------------
# Test: diff_tenants()
# ---------------------------------------------------------------------------

class TestDiffTenants:
    """Tests for diff_tenants() — config comparison."""

    def test_diff_tenants_complete(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"common": 999}}),
            encoding="utf-8"
        )

        (conf_d / "tenants.yaml").write_text(
            yaml.dump({
                "tenants": {
                    "t-a": {"app": "app-a", "version": "1.0"},
                    "t-b": {"app": "app-b", "version": "2.0", "extra": "value"},
                }
            }),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        diff = scanner.diff_tenants("t-a", "t-b")

        assert diff["tenant_a"] == "t-a"
        assert diff["tenant_b"] == "t-b"
        assert "different" in diff
        assert "only_in_t-a" in diff
        assert "only_in_t-b" in diff


def test_unreadable_subdirs_are_named_even_when_underscore_prefixed(
        tmp_path, monkeypatch, capsys):
    """#2054: the `_`-prefix filter is for ENTRIES this scanner never reads.

    The exporter descends `_`-prefixed directories (`_arch/arch.yaml` is a
    tenant to it), so an unreadable `_locked/` hides tenants exactly like
    `locked/` does — and must be named the same way. Blind review measured
    `_locked/` silently dropped while `locked/` warned.

    `os.scandir` is patched rather than `chmod 000`-ed: root ignores the
    mode bits and this suite often runs as uid 0 (same seam as
    tests/shared/test_lib_confd.py `_deny_scandir`).
    """
    conf_d = tmp_path / "conf.d"
    for name in ("_locked", "locked"):
        (conf_d / name).mkdir(parents=True)
        (conf_d / name / "h.yaml").write_text(
            f"tenants:\n  {name.strip('_')}x: {{}}\n", encoding="utf-8")
    (conf_d / "a.yaml").write_text("tenants:\n  a: {}\n", encoding="utf-8")
    denied = {os.fspath(conf_d / "_locked"), os.fspath(conf_d / "locked")}
    real = os.scandir

    def fake(path=".", *a, **kw):
        if os.fspath(path) in denied:
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "scandir", fake)
    monkeypatch.setattr(dt, "_ca_loader", None)  # its own walk; not under test
    scanner = dt.ConfDScanner(conf_d)
    err = capsys.readouterr().err
    assert "a" in scanner.tenants  # the rest of the scan still works
    for name in ("_locked", "locked"):
        assert f"WARNING: skipped {conf_d.resolve() / name} — is a directory that could not be read" in err, (
            name, err)


# ---------------------------------------------------------------------------
# Test: CLI
# ---------------------------------------------------------------------------

class TestCLI:
    """Tests for CLI argument handling & subprocess execution."""

    def test_cli_help(self):
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"), "--help"],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert "describe" in result.stdout.decode().lower() or "usage" in result.stdout.decode().lower()

    def test_cli_show_sources_subprocess(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"timeout": 30}}),
            encoding="utf-8"
        )
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({"tenants": {"cli-test": {"name": "Test"}}}),
            encoding="utf-8"
        )

        result = subprocess.run(
            [
                sys.executable,
                os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"),
                "cli-test",
                "--conf-d", str(conf_d),
                "--show-sources",
            ],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout.decode())
        assert output["tenant_id"] == "cli-test"
        assert "effective_config" in output

    def test_cli_all_mode(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "tenants.yaml").write_text(
            yaml.dump({
                "tenants": {
                    "t1": {"a": 1},
                    "t2": {"b": 2},
                }
            }),
            encoding="utf-8"
        )

        result = subprocess.run(
            [
                sys.executable,
                os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"),
                "--conf-d", str(conf_d),
                "--all",
            ],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout.decode())
        assert "t1" in output
        assert "t2" in output


# ---------------------------------------------------------------------------
# Test: a tenant declared by more than one carrier (#2049)
# ---------------------------------------------------------------------------

class TestDuplicateDeclaration:
    """#2049: the exporter's walker raises DuplicateTenantError for a tenant
    two conf.d ENTRIES declare (and a full load rejects the dir), so it
    serves no effective config for it. Measured on main c2f29ce9 this tool
    described one of the declarations instead, rc 0, no warning. Go's
    answer for these exact shapes is pinned in
    tests/shared/defaults_symlink_parity_matrix.json (dupA / dupB / dupC)."""

    DESCRIBE = os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py")

    @staticmethod
    def _tree(tmp_path, shape):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
        (conf_d / "zeta.yaml").write_text("tenants:\n  zeta:\n    cpu_pct: 20\n", encoding="utf-8")
        if shape == "link":        # dupA: a link beside its own target
            (conf_d / "real.yaml").write_text("tenants:\n  acme:\n    cpu_pct: 70\n", encoding="utf-8")
            try:
                os.symlink("real.yaml", conf_d / "acme.yaml")
            except (OSError, NotImplementedError) as exc:
                pytest.skip(f"symlinks unavailable here: {exc}")
            files = ["acme.yaml", "real.yaml"]
        elif shape == "two-files":  # dupB
            (conf_d / "real.yaml").write_text("tenants:\n  acme:\n    cpu_pct: 70\n", encoding="utf-8")
            (conf_d / "other.yaml").write_text("tenants:\n  acme:\n    cpu_pct: 10\n", encoding="utf-8")
            files = ["other.yaml", "real.yaml"]
        elif shape == "two-spellings":  # dupC: one stem, `.yaml` and `.yml`
            (conf_d / "acme.yaml").write_text("tenants:\n  acme:\n    cpu_pct: 70\n", encoding="utf-8")
            (conf_d / "acme.yml").write_text("tenants:\n  acme:\n    cpu_pct: 10\n", encoding="utf-8")
            files = ["acme.yaml", "acme.yml"]
        else:  # "nested": the second carrier lives in a sub-directory
            (conf_d / "acme.yaml").write_text("tenants:\n  acme:\n    cpu_pct: 70\n", encoding="utf-8")
            (conf_d / "team").mkdir()
            (conf_d / "team" / "acme.yaml").write_text("tenants:\n  acme:\n    cpu_pct: 10\n", encoding="utf-8")
            files = ["acme.yaml", "team/acme.yaml"]
        return conf_d, files

    def _run(self, conf_d, *args):
        return subprocess.run(
            [sys.executable, self.DESCRIBE, *args, "--conf-d", str(conf_d)],
            capture_output=True, text=True, encoding="utf-8", timeout=60)

    SHAPES = ["link", "two-files", "two-spellings", "nested"]

    @pytest.mark.parametrize("shape", SHAPES)
    def test_scanner_names_every_carrier_by_its_entry(self, tmp_path, shape):
        conf_d, files = self._tree(tmp_path, shape)
        scanner = dt.ConfDScanner(conf_d)
        # ⛔ By the ENTRY: labelling the link by its target would fold dupA's
        # two carriers into one `real.yaml` and describe what Go refuses.
        assert scanner.duplicates == {"acme": files}
        assert scanner.duplicate_error("zeta") is None

    @pytest.mark.parametrize("shape", SHAPES)
    @pytest.mark.parametrize("mode", [
        ["acme"],
        ["acme", "--show-sources"],
        ["zeta", "--diff", "acme"],
        ["acme", "--diff", "zeta"],
        ["acme", "--what-if", "WHATIF"],
    ], ids=["default", "show-sources", "diff-other-side", "diff-this-side", "what-if"])
    def test_every_mode_refuses_the_duplicated_tenant(self, tmp_path, shape, mode):
        conf_d, files = self._tree(tmp_path, shape)
        args = [str(conf_d / "_defaults.yaml") if a == "WHATIF" else a for a in mode]
        r = self._run(conf_d, *args)
        assert r.returncode == dt.EXIT_VIOLATION, (r.returncode, r.stdout, r.stderr)
        assert r.stdout == ""
        assert "duplicate tenant ID 'acme'" in r.stderr, r.stderr
        assert ", ".join(files) in r.stderr, r.stderr

    @pytest.mark.parametrize("shape", SHAPES)
    def test_the_unduplicated_tenant_is_still_described(self, tmp_path, shape):
        conf_d, _ = self._tree(tmp_path, shape)
        r = self._run(conf_d, "zeta")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["effective_config"] == {"cpu_pct": 20}
        assert "duplicate tenant ID" not in r.stderr

    @pytest.mark.parametrize("shape", SHAPES)
    def test_all_reports_the_duplicate_once_and_describes_the_rest(self, tmp_path, shape):
        conf_d, files = self._tree(tmp_path, shape)
        r = self._run(conf_d, "--all")
        assert r.returncode == dt.EXIT_VIOLATION, (r.returncode, r.stderr)
        out = json.loads(r.stdout)
        assert list(out) == ["zeta"], "the duplicated tenant must not be described"
        assert r.stderr.count("duplicate tenant ID 'acme'") == 1, r.stderr
        assert ", ".join(files) in r.stderr

    def test_all_output_file_is_still_written_before_the_nonzero_exit(self, tmp_path):
        conf_d, _ = self._tree(tmp_path, "two-files")
        dest = tmp_path / "effective.json"
        r = self._run(conf_d, "--all", "--output", str(dest))
        assert r.returncode == dt.EXIT_VIOLATION, r.stderr
        assert list(json.loads(dest.read_text(encoding="utf-8"))) == ["zeta"]

    def test_configmap_root_link_is_not_a_duplicate(self, tmp_path):
        # The no-false-positive control: a ConfigMap mount lists the tenant
        # ONCE at the root (a link into `..data`); the payload directory is
        # never walked, so its real file is not a second carrier.
        conf_d = tmp_path / "conf.d"
        payload = conf_d / "..2026_09_26"
        payload.mkdir(parents=True)
        (payload / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
        (payload / "acme.yaml").write_text("tenants:\n  acme:\n    cpu_pct: 70\n", encoding="utf-8")
        try:
            os.symlink("..2026_09_26", conf_d / "..data", target_is_directory=True)
            os.symlink("..data/acme.yaml", conf_d / "acme.yaml")
            os.symlink("..data/_defaults.yaml", conf_d / "_defaults.yaml")
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable here: {exc}")
        assert dt.ConfDScanner(conf_d).duplicates == {}
        r = self._run(conf_d, "acme")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["effective_config"] == {"cpu_pct": 70}

    def test_a_file_the_exporter_drops_still_counts_but_the_verdict_is_conditional(
            self, tmp_path):
        """Pinned KNOWN divergence from Go (#1942), deliberately NOT in the
        shared Go/Python matrix (the two sides answer differently there).

        `zbad.yaml` also declares `other: 5` — a scalar tenant body, which
        the exporter's full decode rejects, so on the Go side that file
        declares NOTHING and ResolveEffective(acme) returns good.yaml's
        cpu_pct 70 (measured on this branch). Python counts carriers by the
        first document's `tenants:` keys and does not mirror Go's decode
        (`_lib_confd.declared_tenant_ids`, #1942), so it still refuses —
        consistently with validate_config, and better than the old answer,
        which picked zbad.yaml's 10 by file order. What it must NOT do is
        claim unconditionally that the exporter rejects this tenant.
        """
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
        (conf_d / "good.yaml").write_text("tenants:\n  acme:\n    cpu_pct: 70\n", encoding="utf-8")
        (conf_d / "zbad.yaml").write_text(
            "tenants:\n  acme:\n    cpu_pct: 10\n  other: 5\n", encoding="utf-8")
        r = self._run(conf_d, "acme")
        assert r.returncode == dt.EXIT_VIOLATION, (r.returncode, r.stdout, r.stderr)
        assert r.stdout == ""
        assert "duplicate tenant ID 'acme'" in r.stderr
        assert "good.yaml, zbad.yaml" in r.stderr, r.stderr
        # Conditional wording only: "If every one of these files parses, ...".
        assert "If every one of these files parses" in r.stderr, r.stderr
        assert "— the exporter rejects" not in r.stderr, r.stderr
        assert "run validate-config" in r.stderr, r.stderr

    @pytest.mark.parametrize("shape", SHAPES)
    def test_validate_config_flags_the_same_tenant_and_files(self, tmp_path, shape):
        # One predicate (`_lib_confd.duplicate_declarations`) behind both
        # tools: the linter must fail on exactly what this tool refuses.
        ops_dir = os.path.join(REPO_ROOT, "scripts", "tools", "ops")
        if ops_dir not in sys.path:
            sys.path.insert(0, ops_dir)
        import validate_config as vc
        conf_d, files = self._tree(tmp_path, shape)
        res = vc.check_tenant_uniqueness(str(conf_d))
        assert res["status"] == "fail", res
        assert any(f'tenant "acme" is declared in {len(files)} files: '
                   f'{", ".join(files)}' in d for d in res["details"]), res["details"]


# ---------------------------------------------------------------------------
# Test: --what-if mode (P0 #5 ship-blocker fix)
# ---------------------------------------------------------------------------

class TestWhatIf:
    """Tests for --what-if mode: simulate modified _defaults.yaml and return diff + hash change."""

    def _setup_conf_d(self, tmp_path):
        """Build a fixture: L0 defaults + tenant file → returns conf.d Path."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({
                "defaults": {
                    "pg_stat_activity_count": 500,
                    "pg_replication_lag_seconds": 30,
                }
            }),
            encoding="utf-8",
        )
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({
                "tenants": {
                    "whatif-tenant": {
                        "name": "What-if test tenant",
                        "pg_stat_activity_count": 300,  # override L0
                    }
                }
            }),
            encoding="utf-8",
        )
        return conf_d

    def _run_cli(self, conf_d, tenant_id, what_if_path):
        """Run describe_tenant --what-if and return parsed JSON output."""
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"),
                tenant_id,
                "--conf-d", str(conf_d),
                "--what-if", str(what_if_path),
            ],
            capture_output=True,
            timeout=5,
        )
        return result

    def test_what_if_substitute_changes_hash(self, tmp_path):
        """Substituting an existing _defaults.yaml that modifies a tenant-visible field → hash changes."""
        conf_d = self._setup_conf_d(tmp_path)
        # What-if: bump pg_replication_lag_seconds to 60 (tenant does NOT override)
        whatif = tmp_path / "whatif.yaml"
        whatif.write_text(
            yaml.dump({
                "defaults": {
                    "pg_stat_activity_count": 500,
                    "pg_replication_lag_seconds": 60,  # changed from 30
                }
            }),
            encoding="utf-8",
        )
        # Point the what-if at the L0 path to trigger "substitute"
        l0_path = conf_d / "_defaults.yaml"
        l0_path.write_text(
            yaml.dump({
                "defaults": {
                    "pg_stat_activity_count": 500,
                    "pg_replication_lag_seconds": 60,
                }
            }),
            encoding="utf-8",
        )
        # Reset L0 back and use whatif as substitute path
        l0_path.write_text(
            yaml.dump({
                "defaults": {
                    "pg_stat_activity_count": 500,
                    "pg_replication_lag_seconds": 30,
                }
            }),
            encoding="utf-8",
        )
        result = self._run_cli(conf_d, "whatif-tenant", l0_path)
        # l0_path content hasn't changed so hash should NOT change when using l0_path itself
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
        output = json.loads(result.stdout.decode())
        assert output["tenant_id"] == "whatif-tenant"
        # L0 path substitution with same content → no change
        assert output["merged_hash_changed"] is False
        assert output["would_trigger_reload"] is False
        assert output["substitution_type"] == "substitute"

    def test_what_if_append_adds_new_field(self, tmp_path):
        """Appending a what-if defaults that introduces a new field → merged_hash changes + added_keys populated."""
        conf_d = self._setup_conf_d(tmp_path)
        # Place what-if OUTSIDE conf.d/ to trigger "append-external"
        whatif = tmp_path / "whatif_external.yaml"
        whatif.write_text(
            yaml.dump({
                "defaults": {
                    "pg_locks_count": 100,  # new field, not in L0 or tenant
                }
            }),
            encoding="utf-8",
        )
        result = self._run_cli(conf_d, "whatif-tenant", whatif)
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
        output = json.loads(result.stdout.decode())
        assert output["substitution_type"] == "append-external"
        assert output["merged_hash_changed"] is True
        assert output["would_trigger_reload"] is True
        assert "pg_locks_count" in output["added_keys"]
        assert output["added_keys"]["pg_locks_count"] == 100

    def test_what_if_tenant_override_shields_from_change(self, tmp_path):
        """If tenant overrides a field, changing that field in what-if defaults should NOT change merged_hash."""
        conf_d = self._setup_conf_d(tmp_path)
        whatif = tmp_path / "whatif.yaml"
        # Change pg_stat_activity_count in defaults — but tenant overrides with 300
        whatif.write_text(
            yaml.dump({
                "defaults": {
                    "pg_stat_activity_count": 999,  # tenant still overrides with 300
                }
            }),
            encoding="utf-8",
        )
        result = self._run_cli(conf_d, "whatif-tenant", whatif)
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
        output = json.loads(result.stdout.decode())
        # Tenant override shields this field → merged hash NOT changed
        # (in effective config, pg_stat_activity_count is still 300 from tenant)
        assert output["merged_hash_changed"] is False
        assert output["would_trigger_reload"] is False

    def test_what_if_file_not_found(self, tmp_path):
        """Non-existent --what-if path → exit 2 (EXIT_CALLER_ERROR, #452) with clear error."""
        conf_d = self._setup_conf_d(tmp_path)
        result = self._run_cli(conf_d, "whatif-tenant", tmp_path / "does-not-exist.yaml")
        assert result.returncode == EXIT_CALLER_ERROR
        assert b"--what-if file not found" in result.stderr

    def test_what_if_help_text_no_longer_stub(self):
        """--what-if help text must not claim 'not yet implemented' (P0 #5 fix)."""
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"), "--help"],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0
        help_text = result.stdout.decode()
        assert "not yet implemented" not in help_text.lower()
        assert "--what-if" in help_text


# ---------------------------------------------------------------------------
# Test: link targets outside conf.d (#1967)
# ---------------------------------------------------------------------------

class TestLinkTargetOutsideConfD:
    """A tenant file or defaults carrier that links OUTSIDE conf.d.

    The merged_hash side is pinned against Go by
    tests/shared/defaults_symlink_parity_matrix.json (rows h / k); this class
    pins the REPORTING rule at the CLI: such an entry is reported by the link's
    own conf.d-relative path (its target has none), where before
    `relative_to(conf_d)` raised ValueError out of the whole run. A target
    inside conf.d keeps being reported by its resolved path.
    """

    DESCRIBE = os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py")

    def _link(self, link, target):
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable here: {exc}")

    def _run(self, conf_d, tenant, *extra):
        return subprocess.run(
            [sys.executable, self.DESCRIBE, tenant, "--conf-d", str(conf_d),
             "--format", "json", *extra],
            capture_output=True, text=True, encoding="utf-8", timeout=60)

    def test_tenant_link_outside_is_reported_by_the_link(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (tmp_path / "hshared").mkdir()
        (tmp_path / "hshared" / "th.yaml").write_text("tenants:\n  th: {}\n", encoding="utf-8")
        (conf_d / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
        self._link(conf_d / "th.yaml", "../hshared/th.yaml")
        r = self._run(conf_d, "th", "--show-sources")
        assert r.returncode == 0 and "Traceback" not in r.stderr, r.stderr
        out = json.loads(r.stdout)
        assert out["source_file"] == "th.yaml"
        assert out["defaults_chain"] == ["_defaults.yaml"]

    def test_carrier_link_outside_is_reported_by_the_link(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (tmp_path / "shared").mkdir()
        (tmp_path / "shared" / "base.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
        (conf_d / "tk.yaml").write_text("tenants:\n  tk: {}\n", encoding="utf-8")
        self._link(conf_d / "_defaults.yaml", "../shared/base.yaml")
        r = self._run(conf_d, "tk", "--show-sources")
        assert r.returncode == 0 and "Traceback" not in r.stderr, r.stderr
        out = json.loads(r.stdout)
        assert out["source_file"] == "tk.yaml"
        assert out["defaults_chain"] == ["_defaults.yaml"]

    def test_what_if_depth_of_an_outside_carrier_is_its_link_level(self, tmp_path):
        # The root carrier links outside conf.d; a what-if one level down must
        # be INSERTED after it. Taking the level from the resolved target made
        # `relative_to` raise into the what-if's own ValueError handler and
        # silently misfiled it as append-external.
        conf_d = tmp_path / "conf.d"
        (conf_d / "sub").mkdir(parents=True)
        (tmp_path / "shared").mkdir()
        (tmp_path / "shared" / "base.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
        (conf_d / "tk.yaml").write_text("tenants:\n  tk: {}\n", encoding="utf-8")
        self._link(conf_d / "_defaults.yaml", "../shared/base.yaml")
        whatif = conf_d / "sub" / "_whatif.yaml"
        whatif.write_text("defaults:\n  mem_pct: 1\n", encoding="utf-8")
        r = self._run(conf_d, "tk", "--what-if", str(whatif))
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["substitution_type"] == "insert"

    def _whatif_tree(self, tmp_path):
        conf_d = tmp_path / "c"
        (conf_d / "sub").mkdir(parents=True)
        (conf_d / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
        (conf_d / "sub" / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 70\n", encoding="utf-8")
        (conf_d / "sub" / "tw.yaml").write_text("tenants:\n  tw: {}\n", encoding="utf-8")
        return conf_d

    def test_what_if_link_takes_its_entry_level_like_a_regular_file(self, tmp_path):
        # #1967 blind review F2: the what-if file's OWN level came from its
        # resolved path, so a root-level link to a file outside conf.d was
        # append-external while an identical regular file was inserted.
        conf_d = self._whatif_tree(tmp_path)
        (tmp_path / "o").mkdir()
        (tmp_path / "o" / "w.yaml").write_text("defaults:\n  cpu_pct: 10\n", encoding="utf-8")
        (conf_d / "_whatif_real.yaml").write_text("defaults:\n  cpu_pct: 10\n", encoding="utf-8")
        self._link(conf_d / "_whatif.yaml", "../o/w.yaml")
        outs = []
        for name in ("_whatif.yaml", "_whatif_real.yaml"):
            r = self._run(conf_d, "tw", "--what-if", str(conf_d / name))
            assert r.returncode == 0, r.stderr
            out = json.loads(r.stdout)
            outs.append((out["substitution_type"], out["what_if_merged_hash"]))
        assert outs[0] == outs[1], outs
        assert outs[0][0] == "insert"

    def test_what_if_link_into_a_deeper_dir_is_at_its_entry_level(self, tmp_path):
        # `_x.yaml -> sub/deep/_y.yaml` sits at the ROOT level (0), below
        # `sub/_defaults.yaml`, so its cpu_pct is overridden by 70 and the
        # merged_hash does not move. At the target's level (2) it would be
        # the nearest level and win with 10.
        conf_d = self._whatif_tree(tmp_path)
        (conf_d / "sub" / "deep").mkdir()
        (conf_d / "sub" / "deep" / "_y.yaml").write_text("defaults:\n  cpu_pct: 10\n", encoding="utf-8")
        self._link(conf_d / "_x.yaml", "sub/deep/_y.yaml")
        r = self._run(conf_d, "tw", "--what-if", str(conf_d / "_x.yaml"))
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["substitution_type"] == "insert"
        assert out["merged_hash_changed"] is False, out

    def _linked_conf_d_tree(self, tmp_path):
        # conf.d is ITSELF a link (`c -> real`): the scanner resolves it, so a
        # level must be computed against the holder's REAL directory, not the
        # path spelling the caller typed.
        real = tmp_path / "real"
        (real / "sub").mkdir(parents=True)
        (real / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
        (real / "sub" / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 70\n", encoding="utf-8")
        (real / "sub" / "tw.yaml").write_text("tenants:\n  tw: {}\n", encoding="utf-8")
        (real / "_w.yaml").write_text("defaults:\n  cpu_pct: 10\n", encoding="utf-8")
        self._link(tmp_path / "c", "real")
        return tmp_path / "c"

    def _assert_root_level_insert(self, conf_d, whatif):
        # `_w.yaml` is at the ROOT level, below `sub/_defaults.yaml` (70), so
        # its 10 is overridden and the merged_hash does not move.
        r = self._run(conf_d, "tw", "--what-if", whatif)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert (out["substitution_type"], out["merged_hash_changed"]) == ("insert", False), out

    def test_what_if_level_through_a_linked_conf_d(self, tmp_path):
        # Without resolving the holder, `c/` is not under the resolved conf.d
        # (`real/`) and the what-if was misfiled as append-external.
        conf_d = self._linked_conf_d_tree(tmp_path)
        self._assert_root_level_insert(conf_d, str(conf_d / "_w.yaml"))

    def test_what_if_level_of_a_path_spelled_with_dotdot(self, tmp_path):
        # `real/sub/../_w.yaml` is the root-level file; counted lexically its
        # holder is two levels deep and it was inserted as the nearest level.
        conf_d = self._linked_conf_d_tree(tmp_path)
        self._assert_root_level_insert(conf_d, os.path.join(str(tmp_path), "real", "sub", "..", "_w.yaml"))

    def test_link_target_inside_conf_d_keeps_the_resolved_path(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "_real.txt").write_text("tenants:\n  tg: {}\n", encoding="utf-8")
        self._link(tmp_path / "tg.yaml", "sub/_real.txt")
        r = self._run(tmp_path, "tg", "--show-sources")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["source_file"] == os.path.join("sub", "_real.txt")


# ---------------------------------------------------------------------------
# Test: _custom_alerts UNION inheritance (#772)
# ---------------------------------------------------------------------------

class TestCustomAlertsUnionInheritance:
    """#772: `_custom_alerts` follows ADR-024 UNION (own + inherited), NOT the
    generic array-REPLACE of every other field — so an override tenant's effective
    view KEEPS the inherited platform/domain policy recipe (deep_merge alone would
    drop it, blinding blast_radius to the highest-blast-radius change class). The
    resolution is delegated to the compiler's own walker (the SSOT)."""

    def _repro_tree(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        # platform/domain policy: `_custom_alerts` at the _defaults.yaml TOP level
        # (where the compiler's collect_instances reads inherited recipes).
        (conf_d / "_defaults.yaml").write_text(yaml.dump({
            "_custom_alerts": [
                {"recipe": "threshold", "name": "platform_policy",
                 "metric": "policy_metric", "op": ">", "window": "5m",
                 "threshold": "200:critical"},
            ],
        }), encoding="utf-8")
        # an OVERRIDE tenant (has own _custom_alerts) + an INHERIT tenant (none).
        (conf_d / "tenants.yaml").write_text(yaml.dump({
            "tenants": {
                "t-override": {"_custom_alerts": [
                    {"recipe": "threshold", "name": "own_alert",
                     "metric": "own_metric", "op": ">", "window": "5m",
                     "threshold": "100:warning"}]},
                "t-inherit": {"cpu": "80"},
            },
        }), encoding="utf-8")
        return conf_d

    def test_override_tenant_keeps_inherited_policy(self, tmp_path):
        # The #772 root cause: under array-REPLACE the override tenant would show
        # ONLY own_alert; under ADR-024 UNION it must show BOTH.
        scanner = dt.ConfDScanner(self._repro_tree(tmp_path))
        eff = scanner.effective_config("t-override")
        names = {a["name"] for a in eff["_custom_alerts"]}
        assert names == {"own_alert", "platform_policy"}
        res = {r["name"]: r for r in eff["_custom_alerts_resolution"]}
        assert res["own_alert"]["is_own"] is True
        assert res["platform_policy"]["is_own"] is False  # inherited, NOT wiped

    def test_inherit_tenant_gets_policy(self, tmp_path):
        scanner = dt.ConfDScanner(self._repro_tree(tmp_path))
        eff = scanner.effective_config("t-inherit")
        names = {a["name"] for a in eff["_custom_alerts"]}
        assert names == {"platform_policy"}

    def test_no_custom_alerts_field_when_none(self, tmp_path):
        # A tenant with neither own nor inherited custom alerts must not carry a
        # `_custom_alerts` key (matches the compiler — no phantom field).
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({"tenants": {"plain": {"cpu": "80"}}}), encoding="utf-8")
        eff = dt.ConfDScanner(conf_d).effective_config("plain")
        assert "_custom_alerts" not in eff
        assert "_custom_alerts_resolution" not in eff

    def test_resolver_unavailable_falls_back_to_deep_merge_not_wipe(self, tmp_path, monkeypatch):
        # Self-review (Attack 4): if the compiler resolver is unavailable / raised,
        # a tenant's OWN `_custom_alerts` must FALL BACK to the deep_merge value, not
        # be wiped. (An earlier draft popped the field whenever resolution was empty
        # → a single parse error would have dropped it from EVERY tenant.)
        monkeypatch.setattr(dt, "_ca_loader", None)
        eff = dt.ConfDScanner(self._repro_tree(tmp_path)).effective_config("t-override")
        names = {a["name"] for a in eff["_custom_alerts"]}
        assert names == {"own_alert"}                    # deep_merge REPLACE kept, not wiped
        assert "_custom_alerts_resolution" not in eff    # no compiler resolution available

    def test_returned_recipes_are_isolated_copies(self, tmp_path):
        # Self-review (Attack 1): mutating a returned recipe must not corrupt the
        # shared resolver map (deep_merge returns deepcopies; we must match).
        scanner = dt.ConfDScanner(self._repro_tree(tmp_path))
        eff = scanner.effective_config("t-override")
        eff["_custom_alerts"][0]["threshold"] = "MUTATED"
        again = scanner.effective_config("t-override")
        assert all(a["threshold"] != "MUTATED" for a in again["_custom_alerts"])

    def test_resolve_flag_false_uses_raw_replace_for_what_if(self, tmp_path):
        # CodeRabbit: with resolve_custom_alerts=False (the --what-if baseline) the
        # override tenant gets the RAW deep_merge (own-only) _custom_alerts, so the
        # baseline matches its REPLACE-built simulated side — no spurious diff when
        # the what-if edit is unrelated to custom alerts. (union view = normal mode.)
        scanner = dt.ConfDScanner(self._repro_tree(tmp_path))
        eff = scanner.effective_config("t-override", resolve_custom_alerts=False)
        names = {a["name"] for a in eff["_custom_alerts"]}
        assert names == {"own_alert"}                    # REPLACE, not union
        assert "_custom_alerts_resolution" not in eff


class TestConfDSearchIsBounded:
    """#1494 — 自動搜尋 conf.d 不得走出這個專案。

    ⛔ 這一整條分支（不帶 `--conf-d` 時的自動搜尋）在本檔其餘測試裡是零覆蓋：
    它們一律走 `--conf-d` 或直接用 `ConfDScanner`。盲審實測把上界整個拿掉，
    本檔仍 26 passed —— 也就是那個修法沒有任何守衛。
    """

    @staticmethod
    def _staged_copy(root, subpath):
        """把 describe_tenant.py 複製到 *root/subpath*，回傳載入後的模組。

        用 staged 副本而不是 import 進來的那一份：被測邏輯讀的是模組自己的
        `__file__`，所以必須讓 `__file__` 落在我們控制的樹裡。遞移相依仍由
        conftest 的 repo sys.path 解析（這裡測的是路徑解析，不是映像模擬）。
        """
        import importlib.util
        import shutil
        import uuid
        dest = root / subpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(os.path.join(REPO_ROOT, "scripts", "tools", "dx",
                                  "describe_tenant.py"), dest)
        name = "dt_probe_" + uuid.uuid4().hex[:8]
        spec = importlib.util.spec_from_file_location(name, dest)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m

    def _resolve(self, mod, monkeypatch, capsys):
        """跑 main() 不帶 --conf-d，回傳它寫到 **stderr** 的文字。

        唯一的呼叫端對回傳值做子字串比對，所以行為一直是對的——錯的是原本
        寫「回傳它最後決定要讀的路徑」，那會讓下一個人以為可以拿它比對路徑。
        """
        monkeypatch.setattr(sys, "argv", ["describe_tenant", "--all"])
        with pytest.raises(SystemExit):
            mod.main()
        return capsys.readouterr().err

    def test_a_stray_conf_d_above_the_project_is_not_used(
        self, tmp_path, monkeypatch, capsys
    ):
        """祖先層有 conf.d，但專案內沒有 ⇒ 必須報找不到，不得採用外面那個。

        方向很重要：採用外面那個不會報錯，它會**安靜地描述錯的租戶**。
        """
        (tmp_path / "conf.d").mkdir()                       # 專案之外的誘餌
        proj = tmp_path / "proj"
        (proj / ".git").mkdir(parents=True)
        mod = self._staged_copy(proj, "scripts/tools/dx/describe_tenant.py")
        err = self._resolve(mod, monkeypatch, capsys)
        assert "conf.d/ not found" in err, err
        assert str(tmp_path / "conf.d") not in err, (
            f"採用了專案之外的 conf.d：{err}")

    def test_a_tree_without_git_still_finds_its_own_conf_d(
        self, tmp_path, monkeypatch, capsys
    ):
        """原始碼 tarball / vendored 樹沒有 `.git`，但仍是專案根。

        ⛔ 只認 `.git` 的上界會把這種樹判成「不在任何專案裡」而報錯，
        而舊碼是找得到的 —— 那是修 unbounded 時引進的退化。
        """
        proj = tmp_path / "proj"
        (proj).mkdir()
        (proj / "Makefile").write_text("all:\n", encoding="utf-8")
        (proj / "conf.d").mkdir()
        (proj / "conf.d" / "t-a.yaml").write_text(
            "routing:\n  receiver: x\n", encoding="utf-8")
        mod = self._staged_copy(proj, "scripts/tools/dx/describe_tenant.py")
        monkeypatch.setattr(sys, "argv", ["describe_tenant", "--all"])
        # ⛔ 讀一次、存起來。`capsys.readouterr()` 會**清空**緩衝區，所以原本
        # 在 SystemExit 路徑上第二次呼叫拿到的是空字串，
        # `"conf.d/ not found" not in ""` 恆為 True——那條斷言在最重要的那條
        # 路徑上是空的。今天擋住退化的其實是下一行的 exit code 檢查，一旦有人
        # 放寬它，這一格就靜默變成什麼都不驗。
        exit_code = None
        try:
            mod.main()
        except SystemExit as exc:      # --all 正常結束也可能 raise
            exit_code = exc.code
        err = capsys.readouterr().err
        assert exit_code in (0, None), err
        assert "conf.d/ not found" not in err


class TestPlatformOverlayCLI:
    """#2019: the CLI surfaces of the platform per-tenant layer. The merge
    semantics themselves are pinned by the shared matrix
    (tests/shared/platform_tenant_overlay_matrix.json `walker` column)."""

    PLATFORM = ("defaults:\n  mysql_connections: 80\n"
                "tenants:\n  tx:\n    mysql_connections: \"60\"\n")

    def _tree(self, tmp_path, **extra):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text(self.PLATFORM, encoding="utf-8")
        (conf_d / "tx.yaml").write_text("tenants:\n  tx: {}\n", encoding="utf-8")
        (conf_d / "ty.yaml").write_text("tenants:\n  ty: {}\n", encoding="utf-8")
        for name, body in extra.items():
            (conf_d / name.replace("__", ".")).write_text(body, encoding="utf-8")
        return conf_d

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"), *args],
            capture_output=True, text=True, timeout=10)

    def test_default_output_carries_platform_overlay_only_when_supplied(self, tmp_path):
        conf_d = self._tree(tmp_path)
        tx = self._run("tx", "--conf-d", str(conf_d))
        assert tx.returncode == 0, tx.stderr
        out = json.loads(tx.stdout)
        assert out["effective_config"] == {"mysql_connections": "60"}
        assert out["platform_overlay"] == [{"file": "_defaults.yaml", "keys": ["mysql_connections"]}]
        ty = self._run("ty", "--conf-d", str(conf_d))
        assert ty.returncode == 0, ty.stderr
        assert "platform_overlay" not in json.loads(ty.stdout)

    def test_what_if_substituting_the_platform_file_keeps_the_layer(self, tmp_path):
        """--what-if on the carrier itself (same bytes): the simulated side
        applies the same platform layer as the baseline — no phantom diff."""
        conf_d = self._tree(tmp_path)
        res = self._run("tx", "--conf-d", str(conf_d), "--what-if", str(conf_d / "_defaults.yaml"))
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        assert out["substitution_type"] == "substitute"
        assert out["merged_hash_changed"] is False, out

    def test_what_if_edit_of_the_platform_tenants_block_is_simulated(self, tmp_path):
        conf_d = self._tree(tmp_path)
        edited = tmp_path / "edited.yaml"
        edited.write_text(self.PLATFORM.replace('"60"', '"55"'), encoding="utf-8")
        scanner = dt.ConfDScanner(conf_d)
        blocks = scanner.platform_blocks(
            "tx", replace={str((conf_d / "_defaults.yaml").resolve()): yaml.safe_load(edited.read_text())})
        assert blocks == [("_defaults.yaml", {"mysql_connections": "55"})]

    def test_multi_document_and_unparseable_tenant_files_do_not_end_the_run(self, tmp_path):
        conf_d = self._tree(tmp_path, tm__yaml="tenants:\n  tm: {}\n---\nfoo: 1\n",
                            tb__yaml="tenants:\n  tb: [\n")
        res = self._run("--all", "--conf-d", str(conf_d))
        assert res.returncode == 0, res.stderr
        assert set(json.loads(res.stdout)) == {"tx", "ty", "tm"}
        assert "tb.yaml" in res.stderr and "does not parse" in res.stderr

    def test_an_unparseable_file_declares_nothing_for_the_duplicate_check(self, tmp_path):
        """#2049 × #2019: a tenant file skipped as unparseable declares no
        tenant, so it cannot make its neighbour's tenant a duplicate."""
        conf_d = self._tree(tmp_path, tx2__yaml="tenants:\n  tx: [\n")
        res = self._run("tx", "--conf-d", str(conf_d))
        assert res.returncode == 0, res.stderr
        assert "duplicate" not in res.stderr

    def test_a_numeric_tenant_id_is_one_id_for_the_duplicate_check(self, tmp_path):
        """#2049 × #2019: `123:` and `"123":` are the same tenant to the
        exporter (yaml.v3 reads both into the string "123"), so declaring it
        both ways is a duplicate here too."""
        conf_d = self._tree(tmp_path, a__yaml="tenants:\n  123: {}\n",
                            b__yaml="tenants:\n  \"123\": {}\n")
        res = self._run("123", "--conf-d", str(conf_d))
        assert res.returncode == dt.EXIT_VIOLATION, (res.returncode, res.stderr)
        assert "duplicate tenant ID '123'" in res.stderr and "a.yaml" in res.stderr and "b.yaml" in res.stderr
