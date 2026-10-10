#!/usr/bin/env python3
"""Tests for describe_tenant.py — Effective tenant config resolution with ADR-017 semantics."""

import hashlib
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
from _platform_fs import symlink_or_skip  # noqa: E402


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

    def test_source_info_structure(self, tmp_path, da_guard_env):
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
                symlink_or_skip("real.yaml", conf_d / "acme.yaml")
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
            symlink_or_skip("..2026_09_26", conf_d / "..data", target_is_directory=True)
            symlink_or_skip("..data/acme.yaml", conf_d / "acme.yaml")
            symlink_or_skip("..data/_defaults.yaml", conf_d / "_defaults.yaml")
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable here: {exc}")
        assert dt.ConfDScanner(conf_d).duplicates == {}
        r = self._run(conf_d, "acme")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["effective_config"] == {"cpu_pct": 70}

    def test_a_file_the_exporter_drops_declares_nothing(self, tmp_path):
        """`zbad.yaml` also declares `other: 5` — a scalar tenant body, which
        the exporter's full decode rejects, so on the Go side that file
        declares NOTHING and ResolveEffective(acme) returns good.yaml's
        cpu_pct 70 (`da-guard effective`: parse_failed [zbad.yaml], acme 70).

        This was a pinned KNOWN divergence (#1942): describe counted zbad.yaml
        as a second carrier and refused acme as a duplicate (rc 1). Since
        #2115 describe skips a tenant file the exporter cannot decode
        (`_undecodable_tenant_body`), so it answers as Go does: acme from
        good.yaml, rc 0, zbad.yaml named on stderr.
        ⚠️ validate_config's `tenant_uniqueness` still counts zbad.yaml
        (`_lib_confd.declared_tenant_ids`) — a divergence left to that tool.
        """
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
        (conf_d / "good.yaml").write_text("tenants:\n  acme:\n    cpu_pct: 70\n", encoding="utf-8")
        (conf_d / "zbad.yaml").write_text(
            "tenants:\n  acme:\n    cpu_pct: 10\n  other: 5\n", encoding="utf-8")
        r = self._run(conf_d, "acme")
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert json.loads(r.stdout)["effective_config"] == {"cpu_pct": 70}
        assert "duplicate" not in r.stderr, r.stderr
        assert f"skipped {conf_d / 'zbad.yaml'}" in r.stderr, r.stderr
        assert "tenant 'other' is a int, not a mapping" in r.stderr, r.stderr

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

    def _run_cli(self, conf_d, tenant_id, what_if_path, *extra):
        """Run describe_tenant --what-if and return the completed process."""
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"),
                tenant_id,
                "--conf-d", str(conf_d),
                "--what-if", str(what_if_path),
                *map(str, extra),
            ],
            capture_output=True,
            timeout=5,
        )
        return result

    def test_what_if_substitute_changes_hash(self, tmp_path):
        """--what-if <copy> --replaces <L0>: a copy that bumps a tenant-visible
        field → substitute + hash changes; an unchanged copy → no change.
        #2097 F1: the L0 file itself as --what-if compared it with itself
        (an in-place edit always read "no reload") and is now rc 2."""
        conf_d = self._setup_conf_d(tmp_path)
        l0_path = conf_d / "_defaults.yaml"
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
        result = self._run_cli(conf_d, "whatif-tenant", whatif, "--replaces", l0_path)
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
        output = json.loads(result.stdout.decode())
        assert output["tenant_id"] == "whatif-tenant"
        assert output["substitution_type"] == "substitute"
        assert output["replaces"] == str(l0_path.resolve())
        assert output["merged_hash_changed"] is True
        assert output["would_trigger_reload"] is True
        assert output["changed_keys"] == {
            "pg_replication_lag_seconds": {"baseline": 30, "what_if": 60}}

        # Unchanged copy → substitute, no change
        same = tmp_path / "same.yaml"
        same.write_bytes(l0_path.read_bytes())
        result = self._run_cli(conf_d, "whatif-tenant", same, "--replaces", l0_path)
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
        output = json.loads(result.stdout.decode())
        assert output["substitution_type"] == "substitute"
        assert output["merged_hash_changed"] is False
        assert output["would_trigger_reload"] is False

        # The L0 file itself, without --replaces → rc 2 naming the remedy
        result = self._run_cli(conf_d, "whatif-tenant", l0_path)
        assert result.returncode == 2
        assert b"--replaces" in result.stderr

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
            symlink_or_skip(target, link)
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
        # #2097: a `.`-prefixed draft — a file of the scanned tree would be
        # substituted in place, not inserted as a level.
        whatif = conf_d / "sub" / ".whatif.yaml"
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
        # #2097: `.`-prefixed drafts the walker does not list — a file of the
        # scanned tree would be substituted in place, not inserted as a level.
        (conf_d / ".whatif_real.yaml").write_text("defaults:\n  cpu_pct: 10\n", encoding="utf-8")
        self._link(conf_d / ".whatif.yaml", "../o/w.yaml")
        outs = []
        for name in (".whatif.yaml", ".whatif_real.yaml"):
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
        # #2097: link and target both outside the walker's listing (a
        # `.`-prefixed entry, a pruned `.`-prefixed directory) — a file of the
        # scanned tree would be substituted in place, not inserted as a level.
        conf_d = self._whatif_tree(tmp_path)
        (conf_d / "sub" / ".deep").mkdir()
        (conf_d / "sub" / ".deep" / "_y.yaml").write_text("defaults:\n  cpu_pct: 10\n", encoding="utf-8")
        self._link(conf_d / ".x.yaml", "sub/.deep/_y.yaml")
        r = self._run(conf_d, "tw", "--what-if", str(conf_d / ".x.yaml"))
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
        # #2097: a `.`-prefixed draft the walker does not list (a file of the
        # scanned tree would be substituted in place, not inserted).
        (real / ".w.yaml").write_text("defaults:\n  cpu_pct: 10\n", encoding="utf-8")
        self._link(tmp_path / "c", "real")
        return tmp_path / "c"

    def _assert_root_level_insert(self, conf_d, whatif):
        # `.w.yaml` is at the ROOT level, below `sub/_defaults.yaml` (70), so
        # its 10 is overridden and the merged_hash does not move.
        r = self._run(conf_d, "tw", "--what-if", whatif)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert (out["substitution_type"], out["merged_hash_changed"]) == ("insert", False), out

    def test_what_if_level_through_a_linked_conf_d(self, tmp_path):
        # Without resolving the holder, `c/` is not under the resolved conf.d
        # (`real/`) and the what-if was misfiled as append-external.
        conf_d = self._linked_conf_d_tree(tmp_path)
        self._assert_root_level_insert(conf_d, str(conf_d / ".w.yaml"))

    def test_what_if_level_of_a_path_spelled_with_dotdot(self, tmp_path):
        # `real/sub/../.w.yaml` is the root-level file; counted lexically its
        # holder is two levels deep and it was inserted as the nearest level.
        conf_d = self._linked_conf_d_tree(tmp_path)
        self._assert_root_level_insert(conf_d, os.path.join(str(tmp_path), "real", "sub", "..", ".w.yaml"))

    def test_link_target_inside_conf_d_keeps_the_resolved_path(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "_real.txt").write_text("tenants:\n  tg: {}\n", encoding="utf-8")
        self._link(tmp_path / "tg.yaml", "sub/_real.txt")
        r = self._run(tmp_path, "tg", "--show-sources")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["source_file"] == "sub/_real.txt"


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
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)

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
        """--what-if <copy of the carrier> --replaces <carrier> (same bytes): the simulated side
        applies the same platform layer as the baseline — no phantom diff."""
        conf_d = self._tree(tmp_path)
        copy = tmp_path / "copy.yaml"
        copy.write_bytes((conf_d / "_defaults.yaml").read_bytes())
        res = self._run("tx", "--conf-d", str(conf_d), "--what-if", str(copy),
                        "--replaces", str(conf_d / "_defaults.yaml"))
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


class TestProfileOverlayCLI:
    """#2117: the CLI surfaces of profile expansion. The merge semantics are
    pinned against /metrics by the shared matrix
    (tests/shared/platform_tenant_overlay_matrix.json p-rows)."""

    DEFAULTS = "defaults:\n  mysql_connections: 80\n"
    PROFILES = "profiles:\n  std:\n    mysql_connections: 60\n"

    def _tree(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text(self.DEFAULTS, encoding="utf-8")
        (conf_d / "_profiles.yaml").write_text(self.PROFILES, encoding="utf-8")
        (conf_d / "tx.yaml").write_text("tenants:\n  tx:\n    _profile: std\n", encoding="utf-8")
        (conf_d / "ty.yaml").write_text("tenants:\n  ty: {}\n", encoding="utf-8")
        return conf_d

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)

    def test_default_output_carries_profile_overlay_only_when_supplied(self, tmp_path):
        conf_d = self._tree(tmp_path)
        tx = self._run("tx", "--conf-d", str(conf_d))
        assert tx.returncode == 0, tx.stderr
        out = json.loads(tx.stdout)
        assert out["effective_config"] == {"_profile": "std", "mysql_connections": 60}
        assert out["profile_overlay"] == [
            {"profile": "std", "file": "_profiles.yaml", "keys": ["mysql_connections"]}]
        ty = self._run("ty", "--conf-d", str(conf_d))
        assert ty.returncode == 0, ty.stderr
        assert "profile_overlay" not in json.loads(ty.stdout)

    def test_what_if_on_the_carrier_keeps_the_profile(self, tmp_path):
        """Same bytes substituted (a copy, `--replaces` the carrier): the simulated side expands the profile as
        the baseline does — no phantom `mysql_connections` diff."""
        conf_d = self._tree(tmp_path)
        copy = tmp_path / "copy.yaml"
        copy.write_bytes((conf_d / "_defaults.yaml").read_bytes())
        res = self._run("tx", "--conf-d", str(conf_d), "--what-if", str(copy),
                        "--replaces", str(conf_d / "_defaults.yaml"))
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        assert out["merged_hash_changed"] is False, out

    def test_what_if_edit_of_a_profile_is_simulated(self, tmp_path):
        """A what-if carrier that adds `profiles:` (standing in for the root
        carrier) moves the simulated value; `_profiles.yaml`'s later value
        still wins per key, as the merge order says."""
        conf_d = self._tree(tmp_path)
        (conf_d / "_profiles.yaml").write_text("profiles:\n  std:\n    pg_connections: 90\n",
                                               encoding="utf-8")
        edited = tmp_path / "edited.yaml"
        edited.write_text(self.DEFAULTS + self.PROFILES, encoding="utf-8")
        scanner = dt.ConfDScanner(conf_d)
        replace = {str((conf_d / "_defaults.yaml").resolve()): yaml.safe_load(edited.read_text())}
        layer, _platform, profile = scanner._tenant_layer("tx", {}, replace=replace)
        assert layer["mysql_connections"] == 60 and layer["pg_connections"] == 90, layer
        assert profile == [
            {"profile": "std", "file": "_defaults.yaml", "keys": ["mysql_connections"]},
            {"profile": "std", "file": "_profiles.yaml", "keys": ["pg_connections"]}]

    def test_alias_spelling_in_the_tenant_file_blocks_the_fill_in(self, tmp_path):
        """#1231 × #2117: a tenant writing the deprecated spelling already
        sets the threshold, so the profile's canonical key is not filled in
        (ApplyProfiles' hasAliasEquivalent)."""
        # The deprecated spelling comes from the alias table, not a literal:
        # the #1231 pre-commit hook forbids the retired key as a live value.
        legacy = dt._legacy_spelling("mysql_threads_running")
        if legacy is None:
            pytest.skip("no deprecated spelling of mysql_threads_running in its alias window")
        conf_d = self._tree(tmp_path)
        (conf_d / "_profiles.yaml").write_text(
            "profiles:\n  std:\n    mysql_threads_running: 40\n", encoding="utf-8")
        (conf_d / "tx.yaml").write_text(
            f"tenants:\n  tx:\n    _profile: std\n    {legacy}: 45\n", encoding="utf-8")
        info = dt.ConfDScanner(conf_d).source_info("tx")
        assert "mysql_threads_running" not in info["effective_config"], info
        assert "profile_overlay" not in info

    def test_a_declared_key_is_not_filled_in(self, tmp_path):
        """#1189 × #2117: a key the root carrier declares in
        `optional_overrides` is never filled in from a profile."""
        conf_d = self._tree(tmp_path)
        (conf_d / "_defaults.yaml").write_text(
            self.DEFAULTS + "optional_overrides:\n  - redis_x\n", encoding="utf-8")
        (conf_d / "_profiles.yaml").write_text(
            self.PROFILES + "    redis_x: 5\n", encoding="utf-8")
        info = dt.ConfDScanner(conf_d).source_info("tx")
        assert "redis_x" not in info["effective_config"], info
        assert info["profile_overlay"] == [
            {"profile": "std", "file": "_profiles.yaml", "keys": ["mysql_connections"]}]


class TestYamlTypedScalarParity:
    """#2371: a value PyYAML reads as `date` / `datetime` / `bytes` (an
    unquoted `2026-12-31`, a `!!binary`) used to end every mode with a
    TypeError traceback while the exporter served the tree. The hash and the
    output now render them as the exporter's canonical JSON does; timestamps
    are decided at read time by yaml.v3's layouts, not PyYAML's.

    The Go column is the oracle, measured with pkg/config
    `CanonicalJSON(ComputeEffectiveConfig(t1.yaml, "t1", [_defaults.yaml]))`
    over the same two files. The date and `!!binary` shapes are also
    re-checked against Go on every run by the golden rows `yaml-date` /
    `yaml-binary` (tests/golden, TestGoldenParity_*); the rest are pinned
    here only."""

    DEFAULTS = "defaults:\n  mysql_connections: 50\n"
    DESCRIBE = os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py")

    def _tree(self, tmp_path, tenant_body, defaults=None):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text(defaults or self.DEFAULTS, encoding="utf-8")
        (conf_d / "t1.yaml").write_text(
            "tenants:\n  t1:\n" + textwrap.indent(tenant_body, "    "), encoding="utf-8")
        return conf_d

    # (id, tenant body, defaults or None, Go canonical JSON)
    ALIGNED = [
        ("date", "_state_maintenance:\n  target: all\n  expires: 2026-12-31\n", None,
         '{"_state_maintenance":{"expires":"2026-12-31T00:00:00Z","target":"all"},"mysql_connections":50}'),
        ("date-tagged", "_x:\n  at: !!timestamp 2026-12-31\n", None,
         '{"_x":{"at":"2026-12-31T00:00:00Z"},"mysql_connections":50}'),
        ("date-in-list", "_x:\n  - 2026-12-31\n  - a\n", None,
         '{"_x":["2026-12-31T00:00:00Z","a"],"mysql_connections":50}'),
        ("date-year-below-1000", "_x:\n  at: 0999-01-02\n", None,
         '{"_x":{"at":"0999-01-02T00:00:00Z"},"mysql_connections":50}'),
        ("date-in-defaults", "mysql_connections: 60\n",
         "defaults:\n  mysql_connections: 50\n  _state_maintenance:\n    expires: 2026-12-31\n",
         '{"_state_maintenance":{"expires":"2026-12-31T00:00:00Z"},"mysql_connections":60}'),
        ("date-in-defaults-merged", "_state_maintenance:\n  target: all\n",
         "defaults:\n  _state_maintenance:\n    expires: 2026-12-31\n",
         '{"_state_maintenance":{"expires":"2026-12-31T00:00:00Z","target":"all"}}'),
        ("datetime-z", "_x:\n  at: 2026-12-31T10:20:30Z\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30Z"},"mysql_connections":50}'),
        ("datetime-lower-t-z", "_x:\n  at: 2026-12-31t10:20:30Z\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30Z"},"mysql_connections":50}'),
        ("datetime-plus-zero", "_x:\n  at: 2026-12-31T10:20:30+00:00\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30Z"},"mysql_connections":50}'),
        ("datetime-offset", "_x:\n  at: 2026-12-31T10:20:30+08:00\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30+08:00"},"mysql_connections":50}'),
        ("datetime-negative-offset", "_x:\n  at: 2026-12-31T10:20:30-05:30\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30-05:30"},"mysql_connections":50}'),
        ("datetime-fraction-6", "_x:\n  at: 2026-12-31T10:20:30.123456Z\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30.123456Z"},"mysql_connections":50}'),
        ("datetime-fraction-1", "_x:\n  at: 2026-12-31T10:20:30.5Z\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30.5Z"},"mysql_connections":50}'),
        ("datetime-fraction-trailing-zeros", "_x:\n  at: 2026-12-31T10:20:30.500Z\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30.5Z"},"mysql_connections":50}'),
        ("datetime-one-digit-fields", "_x:\n  at: 2026-1-2T3:04:05Z\n", None,
         '{"_x":{"at":"2026-01-02T03:04:05Z"},"mysql_connections":50}'),
        ("datetime-naive-t", "_x:\n  at: 2026-12-31T10:20:30\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30"},"mysql_connections":50}'),
        ("datetime-naive-t-fraction", "_x:\n  at: 2026-12-31T10:20:30.5\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30.5"},"mysql_connections":50}'),
        ("binary-utf8", "_routing:\n  receiver:\n    type: webhook\n    url: !!binary aHR0cDovL3g=\n", None,
         '{"_routing":{"receiver":{"type":"webhook","url":"http://x"}},"mysql_connections":50}'),
        ("binary-cjk", "_x:\n  v: !!binary 5Lit5paH\n", None,
         '{"_x":{"v":"中文"},"mysql_connections":50}'),
        ("binary-empty", '_x:\n  v: !!binary ""\n', None,
         '{"_x":{"v":""},"mysql_connections":50}'),
        ("binary-in-defaults", "mysql_connections: 60\n",
         "defaults:\n  _x:\n    v: !!binary aHR0cDovL3g=\n",
         '{"_x":{"v":"http://x"},"mysql_connections":60}'),
        # Invalid UTF-8: encoding/json writes the six characters \ufffd per
        # invalid BYTE (a truncated sequence is two), not a raw U+FFFD.
        ("binary-invalid-byte", "_routing:\n  receiver:\n    type: webhook\n    url: !!binary /w==\n", None,
         r'{"_routing":{"receiver":{"type":"webhook","url":"\ufffd"}},"mysql_connections":50}'),
        ("binary-nul-and-invalid", "_x:\n  v: !!binary YQDA/2I=\n", None,
         r'{"_x":{"v":"a\u0000\ufffd\ufffdb"},"mysql_connections":50}'),
        ("binary-truncated-sequence", "_x:\n  v: !!binary 5Lg=\n", None,
         r'{"_x":{"v":"\ufffd\ufffd"},"mysql_connections":50}'),
        ("binary-overlong", "_x:\n  v: !!binary wIA=\n", None,
         r'{"_x":{"v":"\ufffd\ufffd"},"mysql_connections":50}'),
        ("binary-encoded-surrogate", "_x:\n  v: !!binary 7aCA\n", None,
         r'{"_x":{"v":"\ufffd\ufffd\ufffd"},"mysql_connections":50}'),
        # Control: a real U+FFFD in a string stays a raw character.
        ("string-real-fffd", '_x:\n  v: "\\ufffd"\n', None,
         '{"_x":{"v":"\ufffd"},"mysql_connections":50}'),
    ]

    # Timestamps where yaml.v3's rules and PyYAML's disagree. The reader
    # (`_GoKeyLoader`) decides them from the source text by yaml.v3's four
    # layouts, so each is Go's answer, not PyYAML's (review F3).
    ALIGNED = ALIGNED + [
        ("datetime-space-no-zone", "_silent_mode:\n  expires: 2026-12-31 23:59:59\n", None,
         '{"_silent_mode":{"expires":"2026-12-31T23:59:59Z"},"mysql_connections":50}'),
        ("datetime-space-fraction", "_x:\n  at: 2026-12-31 10:20:30.5\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30.5Z"},"mysql_connections":50}'),
        ("datetime-two-spaces", "_x:\n  at: 2026-12-31  10:20:30\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30Z"},"mysql_connections":50}'),
        ("datetime-space-in-defaults", "mysql_connections: 60\n",
         "defaults:\n  _x:\n    at: 2026-12-31 10:20:30\n",
         '{"_x":{"at":"2026-12-31T10:20:30Z"},"mysql_connections":60}'),
        ("datetime-naive-t-trailing-zeros", "_x:\n  at: 2026-12-31T10:20:30.500\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30.500"},"mysql_connections":50}'),
        ("datetime-naive-lower-t", "_x:\n  at: 2026-12-31t10:20:30\n", None,
         '{"_x":{"at":"2026-12-31t10:20:30"},"mysql_connections":50}'),
        ("datetime-space-before-zone", "_x:\n  at: 2026-12-31 10:20:30 +08:00\n", None,
         '{"_x":{"at":"2026-12-31 10:20:30 +08:00"},"mysql_connections":50}'),
        ("datetime-space-then-z", "_x:\n  at: 2026-12-31 10:20:30Z\n", None,
         '{"_x":{"at":"2026-12-31 10:20:30Z"},"mysql_connections":50}'),
        ("datetime-hour-only-zone", "_x:\n  at: 2026-12-31T10:20:30+08\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30+08"},"mysql_connections":50}'),
        ("datetime-fraction-9", "_x:\n  at: 2026-12-31T10:20:30.123456789Z\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30.123456789Z"},"mysql_connections":50}'),
        ("datetime-fraction-10", "_x:\n  at: 2026-12-31T10:20:30.1234567891Z\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30.123456789Z"},"mysql_connections":50}'),
        ("datetime-comma-fraction", "_x:\n  at: 2026-12-31T10:20:30,5Z\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30.5Z"},"mysql_connections":50}'),
        ("datetime-hour-24", "_x:\n  at: 2026-12-31T24:00:00Z\n", None,
         '{"_x":{"at":"2026-12-31T24:00:00Z"},"mysql_connections":50}'),
        ("datetime-second-60", "_x:\n  at: 2026-12-31 23:59:60\n", None,
         '{"_x":{"at":"2026-12-31 23:59:60"},"mysql_connections":50}'),
        ("datetime-tagged-space", "_x:\n  at: !!timestamp 2026-12-31 10:20:30\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30Z"},"mysql_connections":50}'),
        ("datetime-quoted", '_x:\n  at: "2026-12-31 23:59:59"\n', None,
         '{"_x":{"at":"2026-12-31 23:59:59"},"mysql_connections":50}'),
        ("date-one-digit-fields", "_x:\n  at: 2026-1-2\n", None,
         '{"_x":{"at":"2026-01-02T00:00:00Z"},"mysql_connections":50}'),
        ("date-one-digit-in-defaults", "mysql_connections: 60\n",
         "defaults:\n  _x:\n    at: 2026-1-2\n",
         '{"_x":{"at":"2026-01-02T00:00:00Z"},"mysql_connections":60}'),
        ("date-in-list-go-only-layouts", "_x:\n  - 2026-12-31 23:59:59\n  - 2026-1-2\n", None,
         '{"_x":["2026-12-31T23:59:59Z","2026-01-02T00:00:00Z"],"mysql_connections":50}'),
        # PyYAML raised ValueError on these and dropped the whole tenant file.
        ("date-month-13", "_x:\n  at: 2026-13-01\n", None,
         '{"_x":{"at":"2026-13-01"},"mysql_connections":50}'),
        ("date-day-0", "_x:\n  at: 2026-12-00\n", None,
         '{"_x":{"at":"2026-12-00"},"mysql_connections":50}'),
        ("date-arabic-indic-digits", "_x:\n  at: ٢٠٢٦-١٢-٣١\n", None,
         '{"_x":{"at":"٢٠٢٦-١٢-٣١"},"mysql_connections":50}'),
        ("date-feb-30", "_x:\n  at: 2026-02-30\n", None,
         '{"_x":{"at":"2026-02-30"},"mysql_connections":50}'),
        # time.Parse's zone bounds: an hour above 24 or a minute above 60 is
        # a parse error (the text stays); within them the offset is
        # hours*60 + minutes, re-spelled — `+08:60` IS +09:00 (review 2 F1).
        ("zone-minute-60-carries", "_x:\n  at: 2026-12-31T10:20:30+08:60\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30+09:00"},"mysql_connections":50}'),
        ("zone-minute-60-carries-negative", "_x:\n  at: 2026-12-31T10:20:30-00:60\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30-01:00"},"mysql_connections":50}'),
        ("zone-23-59", "_x:\n  at: 2026-12-31T10:20:30+23:59\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30+23:59"},"mysql_connections":50}'),
        ("zone-hour-99-stays-text", "_x:\n  at: 2026-12-31T10:20:30.500+99:00\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30.500+99:00"},"mysql_connections":50}'),
        ("zone-hour-25-stays-text", "_x:\n  at: 2026-12-31T10:20:30+25:00\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30+25:00"},"mysql_connections":50}'),
        ("zone-minute-61-stays-text", "_x:\n  at: 2026-12-31T10:20:30+08:61\n", None,
         '{"_x":{"at":"2026-12-31T10:20:30+08:61"},"mysql_connections":50}'),
        # Year 0 is a (leap) year to time.Parse; `datetime` cannot hold it
        # (review 2 F3).
        ("year-0", "_x:\n  at: 0000-01-01\n", None,
         '{"_x":{"at":"0000-01-01T00:00:00Z"},"mysql_connections":50}'),
        ("year-0-leap-day", "_x:\n  at: 0000-02-29\n", None,
         '{"_x":{"at":"0000-02-29T00:00:00Z"},"mysql_connections":50}'),
        ("year-0-space-clock", "_x:\n  at: 0000-12-31 10:20:30\n", None,
         '{"_x":{"at":"0000-12-31T10:20:30Z"},"mysql_connections":50}'),
        ("year-0-in-defaults", "mysql_connections: 60\n", "defaults:\n  _x:\n    at: 0000-01-01\n",
         '{"_x":{"at":"0000-01-01T00:00:00Z"},"mysql_connections":60}'),
        ("year-1-not-leap", "_x:\n  at: 0001-02-29\n", None,
         '{"_x":{"at":"0001-02-29"},"mysql_connections":50}'),
        ("year-2100-not-leap", "_x:\n  at: 2100-02-29\n", None,
         '{"_x":{"at":"2100-02-29"},"mysql_connections":50}'),
        # Only a PLAIN scalar is ever a time: quoted and block ones are text
        # to yaml.v3 (review 2 F4 — `_construct_str`'s style guard, and the
        # `|` block's trailing newline, which `$` used to let through).
        ("quoted-go-only-layout", "_x:\n  at: '2026-1-2'\n", None,
         '{"_x":{"at":"2026-1-2"},"mysql_connections":50}'),
        ("block-strip-go-only-layout", "_x:\n  at: |-\n    2026-1-2\n", None,
         '{"_x":{"at":"2026-1-2"},"mysql_connections":50}'),
        ("block-keep-go-only-layout", "_x:\n  at: |\n    2026-1-2\n", None,
         '{"_x":{"at":"2026-1-2\\n"},"mysql_connections":50}'),
        ("block-keep-date", "_x:\n  at: |\n    2026-12-31\n", None,
         '{"_x":{"at":"2026-12-31\\n"},"mysql_connections":50}'),
        ("tagged-block-strip-date", "_x:\n  at: !!timestamp |-\n    2026-12-31\n", None,
         '{"_x":{"at":"2026-12-31T00:00:00Z"},"mysql_connections":50}'),
    ]

    # The exporter has no merged_hash for these, so neither does this tool:
    # the file takes the existing "does not parse" path (no tenant t1).
    GO_REFUSES = [
        # yaml.v3 refuses the FILE: "cannot decode !!str `foo` as a !!timestamp"
        # (review 2 F2).
        ("explicit-timestamp-word", "_x:\n  at: !!timestamp foo\n"),
        ("explicit-timestamp-empty", "_x:\n  at: !!timestamp\n"),
        ("explicit-timestamp-quoted-empty", '_x:\n  at: !!timestamp ""\n'),
        ("explicit-timestamp-block-keep", "_x:\n  at: !!timestamp |\n    2026-12-31\n"),
    ]

    # What the reader still cannot align. `None` = the exporter has no
    # merged_hash for t1 (yaml.v3 refuses the file, or CanonicalJSON fails).
    DIVERGENT = [
        # Go parses a 24h+ zone offset, but CanonicalJSON fails once the value
        # is in the tenant's effective config, so the exporter has no hash;
        # this tool renders it as usual (review 3 F1: refusing the file was
        # wider than Go — see TestYamlTimestampScope).
        ("zone-24-00", "_x:\n  at: 2026-12-31T10:20:30+24:00\n", None, None),
        ("zone-minus-23-60", "_x:\n  at: 2026-12-31T10:20:30-23:60\n", None, None),
        ("zone-24-01", "_x:\n  at: 2026-12-31T10:20:30+24:01\n", None, None),
        ("zone-24-00-one-digit-fields", "_x:\n  at: 2026-1-2T1:2:3+24:00\n", None, None),
        # An explicit `!!timestamp` is refused for VALUES only: yaml.v3 also
        # refuses the file for one on a key or a tenant id; here the key is
        # its text (review 3 F3; tenant id: TestYamlTimestampScope).
        ("key-ts-foo-direct", "!!timestamp foo: 1\n", None, None),
        ("key-ts-foo-nested", "_x:\n  !!timestamp foo: 1\n", None, None),
        # …and yaml.v3 decodes a quoted key tagged `!!timestamp` as a time.
        ("key-ts-quoted", "_x:\n  !!timestamp '2026-12-31': 1\n", None,
         '{"_x":{"2026-12-31 00:00:00 +0000 UTC":1},"mysql_connections":50}'),
        # An explicit tag PyYAML's own resolver would also have inferred leaves
        # no trace once composed: read as the plain `2026-1-2`, i.e. a time.
        ("explicit-str-on-a-go-only-timestamp", "_x:\n  at: !!str 2026-1-2\n", None,
         '{"_x":{"at":"2026-1-2"},"mysql_connections":50}'),
        # yaml.v3: "cannot decode !!str `…` as a !!timestamp". PyYAML would
        # tag these texts `!!timestamp` unasked, so the explicit tag leaves no
        # trace and the text is kept (`!!timestamp foo` IS refused: GO_REFUSES).
        ("explicit-timestamp-go-cannot-parse", "_x:\n  at: !!timestamp 2026-12-31T10:20:30\n", None, None),
        ("explicit-timestamp-month-13", "_x:\n  at: !!timestamp 2026-13-01\n", None, None),
        # A parser difference, not a typing one (#2123): PyYAML's scanner
        # refuses the tab and the file is skipped; yaml.v3 keeps the text.
        ("tab-between-date-and-time", "_x:\n  at: 2026-12-31\t10:20:30\n", None,
         '{"_x":{"at":"2026-12-31\\t10:20:30"},"mysql_connections":50}'),
    ]

    def _python_canonical(self, tmp_path, body, defaults):
        conf_d = self._tree(tmp_path, body, defaults)
        eff = dt.ConfDScanner(conf_d).effective_config("t1")
        return dt._canonical_json(eff), dt._canonical_hash(eff)

    @pytest.mark.parametrize("body,defaults,go_json",
                             [c[1:] for c in ALIGNED], ids=[c[0] for c in ALIGNED])
    def test_canonical_json_and_merged_hash_match_go(self, tmp_path, body, defaults, go_json):
        got_json, got_hash = self._python_canonical(tmp_path, body, defaults)
        assert got_json == go_json
        assert got_hash == hashlib.sha256(go_json.encode("utf-8")).hexdigest()[:16]

    @pytest.mark.parametrize(
        "body,defaults,go_json",
        [pytest.param(*c[1:], marks=pytest.mark.xfail(
            strict=True, reason="#2371: explicit tag lost at compose time, a "
                                "PyYAML/yaml.v3 parser difference, or a >=24h zone "
                                "Go's CanonicalJSON refuses — see DIVERGENT"))
         for c in DIVERGENT],
        ids=[c[0] for c in DIVERGENT])
    def test_known_divergence_from_go(self, tmp_path, body, defaults, go_json):
        if go_json is None:  # yaml.v3 refuses the file: no tenant t1 at all
            with pytest.raises(KeyError):
                self._python_canonical(tmp_path, body, defaults)
            return
        got_json, _ = self._python_canonical(tmp_path, body, defaults)
        assert got_json == go_json

    @pytest.mark.parametrize("body", [c[1] for c in GO_REFUSES], ids=[c[0] for c in GO_REFUSES])
    def test_what_go_cannot_hash_is_not_described(self, tmp_path, body):
        conf_d = self._tree(tmp_path, body)
        (conf_d / "t2.yaml").write_text("tenants:\n  t2: {}\n", encoding="utf-8")
        with pytest.raises(KeyError):
            dt.ConfDScanner(conf_d).effective_config("t1")
        res = subprocess.run([sys.executable, self.DESCRIBE, "--all", "--conf-d", str(conf_d)],
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert res.returncode == 0, res.stderr
        assert "Traceback" not in res.stderr
        assert "t1.yaml" in res.stderr and "does not parse" in res.stderr
        assert set(json.loads(res.stdout)) == {"t2"}

    def test_the_go_merged_hash_of_an_unquoted_date(self, tmp_path, da_guard_env):
        """The merged_hash Go computes for the issue's own shape, via the CLI
        (read from da-guard since #1549)."""
        conf_d = self._tree(tmp_path, self.ALIGNED[0][1])
        res = subprocess.run([sys.executable, self.DESCRIBE, "t1", "--conf-d", str(conf_d), "--show-sources"],
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert res.returncode == 0, res.stderr
        assert json.loads(res.stdout)["merged_hash"] == "2a9fcc779be99d98"

    @pytest.mark.parametrize("body", [
        "_state_maintenance:\n  target: all\n  expires: 2026-12-31\n",
        "_x:\n  at: 2026-12-31T10:20:30+08:00\n",
        "_routing:\n  receiver:\n    type: webhook\n    url: !!binary aHR0cDovL3g=\n",
        "_x:\n  v: !!binary /w==\n",
    ], ids=["date", "datetime", "binary", "binary-invalid-utf8"])
    @pytest.mark.parametrize("mode", [
        ["t1"], ["t1", "--show-sources"], ["t1", "--format", "yaml"],
        ["--all"], ["--all", "--format", "yaml"],
    ], ids=lambda m: " ".join(m))
    def test_no_mode_ends_in_a_traceback(self, tmp_path, body, mode):
        conf_d = self._tree(tmp_path, body)
        res = subprocess.run([sys.executable, self.DESCRIBE, *mode, "--conf-d", str(conf_d)],
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert res.returncode == 0, res.stderr
        assert "Traceback" not in res.stderr
        out = yaml.safe_load(res.stdout) if "yaml" in mode else json.loads(res.stdout)
        assert out


class TestYamlTimestampScope:
    """#2371 review 3 F1: a 24h+ zone offset stops the exporter only once it
    is IN a tenant's effective config (CanonicalJSON). Anywhere else in the
    tree t1 is still described, as Go describes it. Whole files, because the
    point is what else is in them. Go column measured with pkg/config."""

    DEFAULTS = "defaults:\n  mysql_connections: 50\n"
    CASES = [
        pytest.param(
            "tenants:\n  t1:\n    mysql_connections: 60\n  t2:\n    _x:\n      at: 2026-12-31T10:20:30+24:00\n",
            DEFAULTS, '{"mysql_connections":60}', id="other-tenant-in-file-24h"),
        pytest.param(
            "tenants:\n  t1:\n    mysql_connections: 60\nextra: 2026-12-31T10:20:30+24:00\n",
            DEFAULTS, '{"mysql_connections":60}', id="root-sibling-24h"),
        pytest.param(
            "tenants:\n  t1:\n    _x: 1\n", DEFAULTS + "  _x: 2026-12-31T10:20:30+24:00\n",
            '{"_x":1,"mysql_connections":50}', id="overridden-default-24h"),
        # yaml.v3 refuses the file for an explicit `!!timestamp` on a tenant
        # id; this tool refuses it for values only (review 3 F3).
        pytest.param(
            "tenants:\n  !!timestamp foo:\n    a: 1\n  t1:\n    a: 2\n", DEFAULTS, None,
            id="tenantid-ts-foo", marks=pytest.mark.xfail(
                strict=True, reason="#2371: `!!timestamp` refused on values only, not tenant ids")),
    ]

    @pytest.mark.parametrize("tenant_yaml,defaults_yaml,go_json", CASES)
    def test_t1_is_described_as_go_describes_it(self, tmp_path, tenant_yaml, defaults_yaml, go_json):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text(defaults_yaml, encoding="utf-8")
        (conf_d / "t1.yaml").write_text(tenant_yaml, encoding="utf-8")
        if go_json is None:  # the exporter has no t1
            with pytest.raises(KeyError):
                dt.ConfDScanner(conf_d).effective_config("t1")
            return
        assert dt._canonical_json(dt.ConfDScanner(conf_d).effective_config("t1")) == go_json


class TestYamlMappingKeyParity:
    """#2371: mapping KEYS. Below a tenant id yaml.v3 decodes a mapping with
    a non-string key into `map[any]any`, and pkg/config spells each key with
    `fmt.Sprintf("%v", k)` — an unquoted `2026-12-31:` is
    "2026-12-31 00:00:00 +0000 UTC". A `date` key in a `_defaults.yaml` ended
    every mode with "keys must be str…"; in a tenant file the key was its
    source text, so merged_hash differed from the exporter's.

    Go column: pkg/config `CanonicalJSON(ComputeEffectiveConfig(...))` over
    the same two files, measured with time.Local = UTC (the `+00:00` row
    depends on it). The same key is tried in the tenant file and in the
    `_defaults.yaml`; golden row `yaml-keys` re-checks a few against Go on
    every run."""

    DESCRIBE = os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py")

    # (key as written, the exporter's spelling)
    ALIGNED = [
        ("2026-12-31", "2026-12-31 00:00:00 +0000 UTC"),
        ('"2026-12-31"', "2026-12-31"),
        ("'2026-12-31'", "2026-12-31"),
        ("!!str 2026-12-31", "2026-12-31"),
        ("!!timestamp 2026-12-31", "2026-12-31 00:00:00 +0000 UTC"),
        ("2026-1-2", "2026-01-02 00:00:00 +0000 UTC"),
        ("0999-01-02", "0999-01-02 00:00:00 +0000 UTC"),
        ("2026-13-01", "2026-13-01"),
        ("2026-02-30", "2026-02-30"),
        ("2026-12-31T10:20:30Z", "2026-12-31 10:20:30 +0000 UTC"),
        ("2026-12-31t10:20:30Z", "2026-12-31 10:20:30 +0000 UTC"),
        ("2026-12-31T10:20:30+00:00", "2026-12-31 10:20:30 +0000 UTC"),
        ("2026-12-31T10:20:30+08:00", "2026-12-31 10:20:30 +0800 +0800"),
        ("2026-12-31T10:20:30-05:30", "2026-12-31 10:20:30 -0530 -0530"),
        ("2026-12-31T10:20:30.5Z", "2026-12-31 10:20:30.5 +0000 UTC"),
        ("2026-12-31T10:20:30,5Z", "2026-12-31 10:20:30.5 +0000 UTC"),
        ("2026-12-31T10:20:30.123456789Z", "2026-12-31 10:20:30.123456789 +0000 UTC"),
        ("2026-12-31 10:20:30", "2026-12-31 10:20:30 +0000 UTC"),
        ("2026-12-31  10:20:30", "2026-12-31 10:20:30 +0000 UTC"),
        ("2026-12-31T10:20:30", "2026-12-31T10:20:30"),
        ("2026-12-31T24:00:00Z", "2026-12-31T24:00:00Z"),
        # Zone bounds and minute carry, as for values (review 2 F1); a key
        # is spelled by String(), which a 24h offset does not trouble.
        ("2026-12-31T10:20:30+08:60", "2026-12-31 10:20:30 +0900 +0900"),
        ("2026-12-31T10:20:30+99:00", "2026-12-31T10:20:30+99:00"),
        ("2026-12-31T10:20:30+08:61", "2026-12-31T10:20:30+08:61"),
        ("2026-12-31T10:20:30+24:00", "2026-12-31 10:20:30 +2400 +2400"),
        ("0000-01-01", "0000-01-01 00:00:00 +0000 UTC"),
        ("0001-02-29", "0001-02-29"),
        ("1", "1"), ("010", "8"), ("0777", "511"), ("08", "8"), ("0888", "888"),
        ("0x1F", "31"), ("-0x1F", "-31"), ("+0x1F", "31"), ("0x_1F", "31"), ("0x", "0x"),
        ("0o17", "15"), ("0b101", "5"), ("0b", "0b"), ("1_000", "1000"), ("1__0", "10"),
        ("+5", "5"), ("-5", "-5"),
        ("9223372036854775808", "9223372036854775808"),
        ("18446744073709551615", "18446744073709551615"),
        ("+18446744073709551615", "1.8446744073709552e+19"),
        ("18446744073709551616", "1.8446744073709552e+19"),
        ("-9223372036854775809", "-9.223372036854776e+18"),
        ("1.5", "1.5"), ("1.0", "1"), ("0.", "0"), (".5", "0.5"), ("+1.5", "1.5"),
        ("1.5_0", "1.5"), ("-0.0", "-0"), ("0.1", "0.1"),
        ("1e3", "1000"), ("1.5e3", "1500"), ("1e21", "1e+21"), ("1e400", "1e400"),
        ("1000000.0", "1e+06"), ("123456.0", "123456"), ("123456789.0", "1.23456789e+08"),
        ("0.0001", "0.0001"), ("1e-4", "0.0001"), ("0.00001", "1e-05"), ("-1.5e-7", "-1.5e-07"),
        (".inf", "+Inf"), ("-.Inf", "-Inf"), ("+.INF", "+Inf"), (".nan", "NaN"),
        # yaml.v3's `.` hint runs ParseFloat on the text as written, so an
        # underscore must pass underscoreOK (review F2); the digit hint strips
        # them first (`1__0` above is 10).
        (".5_0", "0.5"), (".5e1_0", "5e+09"), (".5E+3", "500"), (".5e400", ".5e400"),
        ("._5", "._5"), (".5_", ".5_"), (".5__0", ".5__0"), (".5e_1", ".5e_1"), ("._", "._"),
        ("true", "true"), ("True", "true"), ("TRUE", "true"), ("false", "false"),
        ("yes", "yes"), ("on", "on"), ("no", "no"), ("off", "off"), ("y", "y"),
        ("12:30", "12:30"), ("1:30:00", "1:30:00"),
        ("!!str 010", "010"), ('"010"', "010"), ("!!int 010", "8"), ("!!float 1", "1"),
        ("abc", "abc"),
    ]

    # yaml.v3 keeps a null key as "<nil>"; the #2114 key reader drops it. An
    # explicit `!!str` PyYAML would also have inferred leaves no trace.
    DIVERGENT = [("~", "<nil>"), ("null", "<nil>"), ("!!str 1e3", "1e3")]

    def _tree(self, tmp_path, key, where):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        if where == "tenant":
            (conf_d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 50\n", encoding="utf-8")
            (conf_d / "t1.yaml").write_text(f"tenants:\n  t1:\n    _x:\n      {key}: v\n", encoding="utf-8")
        else:
            (conf_d / "_defaults.yaml").write_text(f"defaults:\n  _x:\n    {key}: v\n", encoding="utf-8")
            (conf_d / "t1.yaml").write_text("tenants:\n  t1:\n    mysql_connections: 60\n", encoding="utf-8")
        return conf_d

    @staticmethod
    def _go_json(go_key, where):
        return json.dumps({"_x": {go_key: "v"}, "mysql_connections": 50 if where == "tenant" else 60},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @pytest.mark.parametrize("where", ["tenant", "defaults"])
    @pytest.mark.parametrize("key,go_key", ALIGNED, ids=[k for k, _ in ALIGNED])
    def test_key_is_spelled_as_the_exporter_spells_it(self, tmp_path, key, go_key, where):
        eff = dt.ConfDScanner(self._tree(tmp_path, key, where)).effective_config("t1")
        go_json = self._go_json(go_key, where)
        assert dt._canonical_json(eff) == go_json
        assert dt._canonical_hash(eff) == hashlib.sha256(go_json.encode("utf-8")).hexdigest()[:16]

    @pytest.mark.parametrize("where", ["tenant", "defaults"])
    @pytest.mark.parametrize(
        "key,go_key",
        [pytest.param(k, g, marks=pytest.mark.xfail(
            strict=True, reason="#2371: the key reader has no trace of what Go keys on"))
         for k, g in DIVERGENT],
        ids=[k for k, _ in DIVERGENT])
    def test_known_key_divergence_from_go(self, tmp_path, key, go_key, where):
        eff = dt.ConfDScanner(self._tree(tmp_path, key, where)).effective_config("t1")
        assert dt._canonical_json(eff) == self._go_json(go_key, where)

    def test_a_date_key_merges_across_defaults_and_tenant(self, tmp_path):
        """One key to Go (both are time.Time), so their bodies deep-merge."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text("defaults:\n  _x:\n    2026-12-31:\n      a: 1\n", encoding="utf-8")
        (conf_d / "t1.yaml").write_text("tenants:\n  t1:\n    _x:\n      2026-12-31:\n        b: 2\n", encoding="utf-8")
        eff = dt.ConfDScanner(conf_d).effective_config("t1")
        assert dt._canonical_json(eff) == '{"_x":{"2026-12-31 00:00:00 +0000 UTC":{"a":1,"b":2}}}'

    @pytest.mark.xfail(strict=True, reason="#2371: `010` and `8` are one Go key, two text keys here")
    def test_two_spellings_of_one_go_key_merge(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text("defaults:\n  _x:\n    010:\n      a: 1\n", encoding="utf-8")
        (conf_d / "t1.yaml").write_text("tenants:\n  t1:\n    _x:\n      8:\n        b: 2\n", encoding="utf-8")
        eff = dt.ConfDScanner(conf_d).effective_config("t1")
        assert dt._canonical_json(eff) == '{"_x":{"8":{"a":1,"b":2}}}'

    @pytest.mark.parametrize("where", ["tenant", "defaults"])
    @pytest.mark.parametrize("key", ["2026-12-31", "2026-12-31T10:20:30+08:00", "2026-12-31 10:20:30.5", "010"])
    @pytest.mark.parametrize("mode", [
        ["t1"], ["t1", "--show-sources"], ["t1", "--format", "yaml"],
        ["--all"], ["--all", "--format", "yaml"],
    ], ids=lambda m: " ".join(m))
    def test_no_mode_ends_in_a_traceback(self, tmp_path, key, where, mode):
        conf_d = self._tree(tmp_path, key, where)
        res = subprocess.run([sys.executable, self.DESCRIBE, *mode, "--conf-d", str(conf_d)],
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert res.returncode == 0, res.stderr
        assert "Traceback" not in res.stderr
        # yaml.safe_load refuses a `!!python/...` tag: no key object leaked out.
        out = yaml.safe_load(res.stdout) if "yaml" in mode else json.loads(res.stdout)
        effective = (out["t1"] if "--all" in mode else out)["effective_config"]
        assert list(effective["_x"]) == [dt._go_plain_key(key)]

    @pytest.mark.parametrize("fmt", ["json", "yaml"])
    def test_all_keeps_tenant_ids_as_source_text(self, tmp_path, fmt):
        """Review F1: `--all` is keyed by TENANT ID, which the exporter keys by
        source text (#2114). Respelled as `%v`, tenants `010` and `8` both
        became "8" and one silently replaced the other."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 50\n", encoding="utf-8")
        (conf_d / "a.yaml").write_text("tenants:\n  010:\n    mysql_connections: 10\n", encoding="utf-8")
        (conf_d / "b.yaml").write_text("tenants:\n  8:\n    mysql_connections: 8\n", encoding="utf-8")
        res = subprocess.run([sys.executable, self.DESCRIBE, "--all", "--format", fmt, "--conf-d", str(conf_d)],
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert res.returncode == 0, res.stderr
        out = yaml.safe_load(res.stdout) if fmt == "yaml" else json.loads(res.stdout)
        assert set(out) == {"010", "8"}, out
        assert out["010"]["tenant_id"] == "010"
        assert out["010"]["effective_config"] == {"mysql_connections": 10}
        assert out["8"]["tenant_id"] == "8"
        assert out["8"]["effective_config"] == {"mysql_connections": 8}

    def test_a_surrogate_escape_is_a_file_that_does_not_parse(self, tmp_path):
        """yaml.v3 refuses `"\\udfff"` (invalid Unicode escape); PyYAML built
        the str and the hash died in UnicodeEncodeError. Now the tenant file
        takes the "does not parse" path, as the exporter does not load it."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 50\n", encoding="utf-8")
        (conf_d / "t1.yaml").write_text('tenants:\n  t1:\n    _x: "\\udfff"\n', encoding="utf-8")
        (conf_d / "t2.yaml").write_text("tenants:\n  t2: {}\n", encoding="utf-8")
        res = subprocess.run([sys.executable, self.DESCRIBE, "--all", "--conf-d", str(conf_d)],
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert res.returncode == 0, res.stderr
        assert "Traceback" not in res.stderr
        assert "t1.yaml" in res.stderr and "does not parse" in res.stderr
        assert set(json.loads(res.stdout)) == {"t2"}

    # A `_profile:` value is read as source text (#2297) without passing
    # construct_scalar, so `_GoKeyLoader.construct_mapping` refuses the
    # surrogate for it separately; these two keep that refusal honest.
    def test_a_surrogate_profile_in_a_tenant_file_drops_that_file(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 50\n", encoding="utf-8")
        (conf_d / "t1.yaml").write_text('tenants:\n  t1:\n    _profile: "\\udfff"\n', encoding="utf-8")
        res = subprocess.run([sys.executable, self.DESCRIBE, "t1", "--conf-d", str(conf_d)],
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert res.returncode == EXIT_CALLER_ERROR, (res.returncode, res.stderr)
        assert "Traceback" not in res.stderr
        assert "t1.yaml" in res.stderr and "does not parse" in res.stderr
        assert "Tenant 't1' not found" in res.stderr

    def test_a_surrogate_profile_in_a_root_platform_file_contributes_nothing(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 50\n", encoding="utf-8")
        (conf_d / "tx.yaml").write_text("tenants:\n  tx:\n    mysql_connections: 60\n", encoding="utf-8")

        def merged_hash():
            res = subprocess.run([sys.executable, self.DESCRIBE, "tx", "--conf-d", str(conf_d), "--show-sources"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=20)
            assert res.returncode == 0, res.stderr
            assert "Traceback" not in res.stderr
            return json.loads(res.stdout)["merged_hash"], res.stderr

        without, _ = merged_hash()
        # A root platform file that does not parse contributes nothing, and
        # silently — as Go drops it (`_read_platform_files`).
        (conf_d / "_x.yaml").write_text('tenants:\n  tx:\n    _profile: "\\udfff"\n', encoding="utf-8")
        with_file, _ = merged_hash()
        assert with_file == without


class TestUnparseableDefaults:
    """#2413: a `_defaults.yaml` that does not parse ends the run with
    EXIT_CALLER_ERROR and names the file, instead of a traceback (rc 1).

    Fails when the `DefaultsParseError` raise in `ConfDScanner._scan` or its
    catch in `main` is removed: every case below then prints a Traceback
    with rc 1 and no file name. The `tagged-*` shapes also fail when that
    raise catches only `yaml.YAMLError` / `UnicodeDecodeError`."""

    DESCRIBE = os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py")

    SHAPES = {
        "unclosed-flow": b"defaults: [\n",
        "not-utf8": b'defaults:\n  x: "\xff\xfe"\n',
        "surrogate-escape": b'defaults:\n  _x: "\\udfff"\n',
        # An explicit tag whose text does not fit it: PyYAML raises
        # ValueError / KeyError here, not a YAMLError.
        "tagged-int": b"defaults:\n  x: !!int foo\n",
        "tagged-float": b"defaults:\n  x: !!float foo\n",
        "tagged-bool": b"defaults:\n  x: !!bool foo\n",
        # #2459: parses, but yaml.v3 refuses it — PyYAML read True / b"".
        "tagged-bool-yes": b"defaults:\n  x: !!bool yes\n",
        "tagged-bool-mixed-case": b"defaults:\n  x: !!bool tRuE\n",
        "tagged-binary-bad-chars": b'defaults:\n  x: !!binary "%%%"\n',
        "tagged-binary-unpadded": b'defaults:\n  x: !!binary "aGVsbG8"\n',
        # #2459: parses, but `defaults:` is not a mapping — an
        # AttributeError traceback before.
        "defaults-list": b"defaults: [1, 2]\n",
        "defaults-scalar": b"defaults: 5\n",
        # #2459: parses, but the document itself is not a mapping — a
        # traceback before for a list or a string.
        "document-list": b"- 1\n- 2\n",
        "document-scalar": b"hello\n",
        "document-false": b"false\n",
    }
    # #2459: these parse; the message says the shape is unsupported
    # (DefaultsShapeError), not that the file does not parse.
    UNSUPPORTED = {"defaults-list", "defaults-scalar", "document-list",
                   "document-scalar", "document-false"}
    MODES = {
        "tenant": ["tr"],
        "all-json": ["--all"],
        "all-yaml": ["--all", "--format", "yaml"],
    }

    def _tree(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        (conf_d / "sub").mkdir(parents=True)
        (conf_d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 50\n", encoding="utf-8")
        (conf_d / "tr.yaml").write_text("tenants:\n  tr:\n    mysql_connections: 10\n", encoding="utf-8")
        (conf_d / "sub" / "ts.yaml").write_text("tenants:\n  ts: {}\n", encoding="utf-8")
        return conf_d

    def _run(self, *args):
        return subprocess.run([sys.executable, self.DESCRIBE, *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=20)

    @pytest.mark.parametrize("mode", sorted(MODES))
    @pytest.mark.parametrize("shape", sorted(SHAPES))
    @pytest.mark.parametrize("carrier", ["_defaults.yaml", "sub/_defaults.yaml"])
    def test_unparseable_defaults_is_a_named_caller_error(self, tmp_path, carrier, shape, mode):
        conf_d = self._tree(tmp_path)
        (conf_d / carrier).write_bytes(self.SHAPES[shape])
        res = self._run(*self.MODES[mode], "--conf-d", str(conf_d))
        assert "Traceback" not in res.stderr, res.stderr
        assert res.returncode == EXIT_CALLER_ERROR, (res.returncode, res.stderr)
        verdict = "has an unsupported shape" if shape in self.UNSUPPORTED else "does not parse"
        assert f"{conf_d / carrier} {verdict}" in res.stderr, res.stderr
        assert res.stdout == ""

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_unparseable_what_if_file_is_named(self, tmp_path, shape):
        conf_d = self._tree(tmp_path)
        what_if = tmp_path / "what-if.yaml"
        what_if.write_bytes(self.SHAPES[shape])
        res = self._run("tr", "--conf-d", str(conf_d), "--what-if", str(what_if))
        assert "Traceback" not in res.stderr, res.stderr
        assert res.returncode == EXIT_CALLER_ERROR, (res.returncode, res.stderr)
        assert str(what_if.resolve()) in res.stderr, res.stderr
        # #2459: a shape refusal is not reported as a parse failure.
        if shape in self.UNSUPPORTED:
            assert "has an unsupported shape" in res.stderr, res.stderr
            assert "Failed to parse" not in res.stderr, res.stderr

    # must-trigger controls: the paths around the fix are unchanged.
    def test_control_parseable_defaults_still_describe(self, tmp_path):
        conf_d = self._tree(tmp_path)
        res = self._run("--all", "--conf-d", str(conf_d))
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        assert out["tr"]["effective_config"] == {"mysql_connections": 10}
        assert out["ts"]["effective_config"] == {"mysql_connections": 50}
        assert "does not parse" not in res.stderr

    def test_control_unparseable_tenant_file_is_still_skipped(self, tmp_path):
        conf_d = self._tree(tmp_path)
        (conf_d / "tr.yaml").write_text("tenants: [\n", encoding="utf-8")
        res = self._run("--all", "--conf-d", str(conf_d))
        assert res.returncode == 0, res.stderr
        assert set(json.loads(res.stdout)) == {"ts"}
        assert "WARNING: skipped" in res.stderr and "tr.yaml" in res.stderr


class TestDefaultsDocumentParity:
    """#2459: `_defaults.yaml` documents the exporter DOES serve, described
    with its merged_hash. `_hash` is this tool's own canonical hash of the
    effective config it reads (`--what-if` prints that one; the merged_hash
    `--show-sources` prints is da-guard's since #1549, which would make the
    rows Go against Go). Every `go_hash` below is ResolveEffective's
    MergedHash for the same two-file tree (`_defaults.yaml` + `tx.yaml`),
    measured with a scratch Go program built against
    components/threshold-exporter/app (`config.LoadDir` +
    `config.ResolveEffective`); the flat load served the same values.

    Fails when `_load_yaml` reads the whole stream again (the multi-document
    rows: "does not parse", rc 2), when it stops tolerating a null
    document or `defaults: ~` (AttributeError), or when the chain stops taking
    Go's `extractDefaultsBlock` (the `defaults: ~` + sibling row)."""

    TENANT = 'tenants:\n  tx:\n    redis_memory: "50"\n'
    WITH_80 = "664e2553d242d345"   # defaults {mysql_connections: 80}
    NOTHING = "dc8202dfc4683b17"   # no defaults at all

    CASES = {
        # must-trigger control: the plain shape, same hash as Go.
        "control": ("defaults:\n  mysql_connections: 80\n", WITH_80),
        # yaml.v3 Unmarshal decodes the FIRST document only; a later one
        # is never read, even when it would not parse or has a bad shape.
        "multi-doc": ("defaults:\n  mysql_connections: 80\n---\n"
                      "defaults:\n  mysql_connections: 90\n", WITH_80),
        "multi-doc-later-unparseable": (
            "defaults:\n  mysql_connections: 80\n---\n: : [\n", WITH_80),
        "multi-doc-later-bad-shape": (
            "defaults:\n  mysql_connections: 80\n---\ndefaults: [1]\n", WITH_80),
        "multi-doc-empty-first": ("---\n---\ndefaults:\n  mysql_connections: 80\n",
                                  NOTHING),
        # null is no defaults block; the rest of the document is (Go's
        # extractDefaultsBlock), and the null key itself merges away.
        "defaults-null": ("defaults: ~\n", NOTHING),
        "defaults-null-with-sibling": ("defaults: ~\nmysql_connections: 80\n", WITH_80),
        # an explicit tag yaml.v3 accepts keeps loading.
        "tagged-bool-true": ("defaults:\n  mysql_connections: 80\n  x: !!bool TRUE\n",
                             "01fde994832c3568"),
        "tagged-bool-quoted": ('defaults:\n  mysql_connections: 80\n  x: !!bool "true"\n',
                               "01fde994832c3568"),
        "tagged-binary-block": ("defaults:\n  mysql_connections: 80\n"
                                "  x: !!binary |\n    aGVs\n    bG8=\n", "0ee9af955f7e7e01"),
    }
    # Not a mapping: refused, naming the file (rc 2 on the CLI).
    NOT_A_MAPPING = {"list": "- 1\n- 2\n", "scalar": "hello\n", "false": "false\n"}

    def _hash(self, tmp_path, body):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text(body, encoding="utf-8")
        (conf_d / "tx.yaml").write_text(self.TENANT, encoding="utf-8")
        return dt._canonical_hash(dt.ConfDScanner(conf_d).effective_config("tx"))

    @pytest.mark.parametrize("case", sorted(CASES))
    def test_merged_hash_matches_the_exporter(self, tmp_path, case):
        body, go_hash = self.CASES[case]
        assert self._hash(tmp_path, body) == go_hash

    @pytest.mark.parametrize("case", sorted(NOT_A_MAPPING))
    def test_a_document_that_is_not_a_mapping_is_refused(self, tmp_path, case):
        """Owner's ruling on #2459: refused like a `defaults:` that is not a
        mapping, not described around. The CLI half (rc 2, every mode, a
        sub-directory level, `--what-if`) is TestUnparseableDefaults'
        `document-*` shapes; the control is CASES["control"] — the same
        file as a mapping describes with the exporter's hash."""
        with pytest.raises(dt.DefaultsShapeError, match="the document is a .*, not a mapping"):
            self._hash(tmp_path, self.NOT_A_MAPPING[case])

    def _sub_tree(self, tmp_path, sub_defaults):
        conf_d = tmp_path / "conf.d"
        (conf_d / "team").mkdir(parents=True)
        (conf_d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n",
                                               encoding="utf-8")
        (conf_d / "team" / "_defaults.yaml").write_text(sub_defaults, encoding="utf-8")
        (conf_d / "team" / "tx.yaml").write_text(self.TENANT, encoding="utf-8")
        return conf_d

    def _describe(self, conf_d, *args):
        return subprocess.run(
            [sys.executable, TestUnparseableDefaults.DESCRIBE, *args, "--conf-d", str(conf_d)],
            capture_output=True, text=True, encoding="utf-8", timeout=20)

    def test_a_subdirectory_defaults_not_a_mapping_is_named_unsupported(self, tmp_path):
        """In a sub-directory the exporter DOES serve `defaults: [1]` beside
        `mysql_connections: 81` (both planes take the whole document: 81 on
        /metrics, merged_hash cef3dbb6…). Refused all the same (one rule for
        every level), but the message must not claim the file does not
        parse — it parses, and the exporter reads it."""
        conf_d = self._sub_tree(tmp_path, "defaults: [1]\nmysql_connections: 81\n")
        res = self._describe(conf_d, "tx", "--show-sources")
        assert res.returncode == EXIT_CALLER_ERROR, res.stderr
        carrier = conf_d / "team" / "_defaults.yaml"
        assert f"{carrier} has an unsupported shape" in res.stderr, res.stderr
        assert "does not parse" not in res.stderr, res.stderr
        assert res.stdout == ""

    def test_control_subdirectory_defaults_mapping_matches_the_exporter(self, tmp_path, da_guard_env):
        """Must-trigger control for the test above: the same level written
        as a mapping describes with rc 0 and ResolveEffective's hash."""
        conf_d = self._sub_tree(tmp_path, "defaults:\n  mysql_connections: 81\n")
        res = self._describe(conf_d, "tx", "--show-sources")
        assert res.returncode == 0, res.stderr
        assert json.loads(res.stdout)["merged_hash"][:16] == "e4404f52c8e88f56"

    def _custom_alerts_tree(self, tmp_path, root_defaults):
        conf_d = tmp_path / "conf.d"
        (conf_d / "team").mkdir(parents=True)
        (conf_d / "_defaults.yaml").write_text(root_defaults, encoding="utf-8")
        (conf_d / "team" / "_defaults.yaml").write_text(yaml.dump({"_custom_alerts": [
            {"recipe": "threshold", "name": "team_policy", "metric": "policy_metric",
             "op": ">", "window": "5m", "threshold": "200:critical"}]}), encoding="utf-8")
        (conf_d / "team" / "tx.yaml").write_text(self.TENANT, encoding="utf-8")
        return conf_d

    def test_a_root_document_not_a_mapping_stops_before_custom_alerts(self, tmp_path):
        """With the root document skipped, the `_custom_alerts` walker went
        on to read it, failed, and fell back to REPLACE with a WARN. The
        refusal stops the run before that walker runs."""
        res = self._describe(self._custom_alerts_tree(tmp_path, "- 1\n"), "tx")
        assert res.returncode == EXIT_CALLER_ERROR, res.stderr
        assert "has an unsupported shape" in res.stderr, res.stderr
        assert "inheritance resolution failed" not in res.stderr, res.stderr

    def test_control_custom_alerts_resolve_under_a_mapping_root(self, tmp_path):
        res = self._describe(self._custom_alerts_tree(
            tmp_path, "defaults:\n  mysql_connections: 80\n"), "tx")
        assert res.returncode == 0, res.stderr
        assert "inheritance resolution failed" not in res.stderr, res.stderr
        eff = json.loads(res.stdout)["effective_config"]
        assert [a["name"] for a in eff["_custom_alerts"]] == ["team_policy"]

    @pytest.mark.parametrize("value", ["!!bool yes", '!!binary "%%%"'])
    def test_a_tenant_file_with_a_refused_tag_is_skipped(self, tmp_path, value):
        """The tag rules live on the loader every conf.d read shares, so a
        tenant file holding such a value is skipped (named on stderr) like
        any tenant file that does not parse; ResolveEffective refuses the
        tenant too ("parse tenant: …"). Control: the same file with a plain
        value describes."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n",
                                               encoding="utf-8")
        tenant = conf_d / "tx.yaml"
        tenant.write_text(self.TENANT + f"    x: {value}\n", encoding="utf-8")
        res = self._describe(conf_d, "tx")
        assert res.returncode == EXIT_CALLER_ERROR, res.stderr
        assert f"WARNING: skipped {tenant} — does not parse" in res.stderr, res.stderr
        tenant.write_text(self.TENANT + "    x: plain\n", encoding="utf-8")
        assert self._describe(conf_d, "tx").returncode == 0

    def test_multi_document_what_if_reads_its_first_document(self, tmp_path):
        """The `--what-if` file stands in for a `_defaults.yaml`, so it is
        read the same way: its first document. Refused with rc 2 before."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n",
                                               encoding="utf-8")
        (conf_d / "tx.yaml").write_text(self.TENANT, encoding="utf-8")
        what_if = tmp_path / "what-if.yaml"
        what_if.write_text("defaults:\n  mysql_connections: 90\n---\n: : [\n",
                           encoding="utf-8")
        res = subprocess.run(
            [sys.executable, TestUnparseableDefaults.DESCRIBE, "tx", "--conf-d",
             str(conf_d), "--what-if", str(what_if)],
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        assert out["changed_keys"] == {
            "mysql_connections": {"baseline": 80, "what_if": 90}}
