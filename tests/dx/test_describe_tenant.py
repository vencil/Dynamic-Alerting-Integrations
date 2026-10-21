#!/usr/bin/env python3
"""Tests for describe_tenant.py — every value is `da-guard effective`'s (ADR-036).

The tool reads no conf.d file with a YAML parser of its own (#2906): the
effective config, the chain, the overlays and the hashes are Go's, and the
custom-alert compiler's `_custom_alerts` view reads conf.d through
`da-guard parse-tree`. The Python imitation of the exporter these tests
used to pin against Go (deep_merge, the yaml.v3-imitating loaders, the Go
JSON / key renderers, `_canonical_hash`) is gone; what is pinned now is the
tool's own behavior and that it shows Go's values as Go writes them.
"""

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

DESCRIBE = os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py")


@pytest.fixture(autouse=True)
def _da_guard(da_guard_env):
    """Every mode reads da-guard effective: the binary built from this checkout."""
    return da_guard_env


def _go_effective(conf_d, *extra):
    """`da-guard effective` over conf_d, run directly: (rc, document or None)."""
    r = subprocess.run([os.environ["DA_GUARD_BINARY"], "effective", "--config-dir", str(conf_d), *extra],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    return r.returncode, (json.loads(r.stdout) if r.stdout.strip() else None)


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
        assert scanner.effective_config("tenant-a")["name"] == "Tenant A"

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
        assert scanner.tenants["acme-prod"].defaults_chain == ["_defaults.yaml", "acme/_defaults.yaml"]
        assert scanner.effective_config("acme-prod") == {"timeout": 20, "region": "us", "replicas": 3}

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
            yaml.dump({"defaults": {"mysql_connections": 80}}),
            encoding="utf-8"
        )
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({"tenants": {"t1": {"pg_connections": 70}}}),
            encoding="utf-8"
        )

        # The hash of the merged config includes the defaults: the same tree
        # without `_defaults.yaml` hashes differently. Both are da-guard's.
        with_defaults = dt.ConfDScanner(conf_d).source_info("t1")["merged_hash"]
        assert with_defaults == _go_effective(conf_d)[1]["tenants"]["t1"]["merged_hash"]
        (conf_d / "_defaults.yaml").unlink()
        without = dt.ConfDScanner(conf_d).source_info("t1")["merged_hash"]
        assert isinstance(with_defaults, str) and with_defaults != without


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


def test_entries_the_exporter_cannot_read_are_named_and_the_rest_described(tmp_path):
    """#2054 / #2588: an entry the exporter's walk cannot read — `_`-prefixed
    or not — is named on stderr (da-guard's `unreadable`), the rest of the
    tree is still described, and no merged_hash is reported (a value it
    would have given may be missing). Dangling links, because root ignores
    the mode bits a `chmod 000` would rely on."""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "a.yaml").write_text("tenants:\n  a: {}\n", encoding="utf-8")
    for name in ("_locked.yaml", "locked.yaml"):
        try:
            symlink_or_skip("nowhere/" + name, conf_d / name)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable here: {exc}")
    r = subprocess.run([sys.executable, DESCRIBE, "a", "--conf-d", str(conf_d), "--show-sources"],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["merged_hash"] is None and out["merged_hash_error"] == dt.MERGED_HASH_UNREADABLE_TREE
    for name in ("_locked.yaml", "locked.yaml"):
        assert f"{name} (stat_error)" in r.stderr, r.stderr


# ---------------------------------------------------------------------------
# Test: CLI
# ---------------------------------------------------------------------------

class TestCLI:
    """Tests for CLI argument handling & subprocess execution."""

    def test_cli_help(self):
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"), "--help"],
            capture_output=True,
            timeout=60,
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
            timeout=60,
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
            timeout=60,
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
        ["acme", "--what-if", "WHATIF", "--replaces", "DEFAULTS"],
    ], ids=["default", "show-sources", "diff-other-side", "diff-this-side", "what-if"])
    def test_every_mode_refuses_the_duplicated_tenant(self, tmp_path, shape, mode):
        conf_d, files = self._tree(tmp_path, shape)
        draft = tmp_path / "draft.yaml"
        draft.write_text("defaults:\n  cpu_pct: 55\n", encoding="utf-8")
        args = [{"WHATIF": str(draft), "DEFAULTS": str(conf_d / "_defaults.yaml")}.get(a, a)
                for a in mode]
        r = self._run(conf_d, *args)
        assert r.returncode == dt.EXIT_VIOLATION, (r.returncode, r.stdout, r.stderr)
        assert r.stdout == ""
        assert "duplicate tenant ID 'acme'" in r.stderr, r.stderr
        assert ", ".join(files) in r.stderr, r.stderr

    @pytest.mark.parametrize("shape", SHAPES)
    def test_the_unduplicated_tenant_is_refused_with_the_tree(self, tmp_path, shape):
        """#2906: every value is da-guard effective's, and the exporter rejects
        the WHOLE tree when a tenant is declared twice (a full load rejects
        the conf.d; `da-guard effective` exits 2). So the other tenant is not
        described either — by name, with the duplicate named — where this
        tool used to describe it from its own Python merge."""
        conf_d, _ = self._tree(tmp_path, shape)
        assert _go_effective(conf_d)[0] == 2
        r = self._run(conf_d, "zeta")
        assert r.returncode == dt.EXIT_VIOLATION, (r.returncode, r.stderr)
        assert r.stdout == ""
        assert "tenant 'zeta' is not described: the exporter rejects the whole conf.d" in r.stderr, r.stderr
        assert "(acme)" in r.stderr, r.stderr

    @pytest.mark.parametrize("shape", SHAPES)
    def test_all_reports_the_duplicate_once_and_describes_nothing(self, tmp_path, shape):
        conf_d, files = self._tree(tmp_path, shape)
        r = self._run(conf_d, "--all")
        assert r.returncode == dt.EXIT_VIOLATION, (r.returncode, r.stderr)
        assert json.loads(r.stdout) == {}, "the exporter serves no tenant of this tree"
        assert r.stderr.count("duplicate tenant ID 'acme'") == 1, r.stderr
        assert ", ".join(files) in r.stderr
        assert "no other tenant is described" in r.stderr, r.stderr

    def test_all_output_file_is_still_written_before_the_nonzero_exit(self, tmp_path):
        conf_d, _ = self._tree(tmp_path, "two-files")
        dest = tmp_path / "effective.json"
        r = self._run(conf_d, "--all", "--output", str(dest))
        assert r.returncode == dt.EXIT_VIOLATION, r.stderr
        assert json.loads(dest.read_text(encoding="utf-8")) == {}

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
        as a second carrier and refused acme as a duplicate (rc 1). A file in
        da-guard effective's `parse_failed` declares nothing to the duplicate
        check (#2906), so it answers as Go does: acme from good.yaml, rc 0,
        zbad.yaml named on stderr, no merged_hash (the tree drops a file).
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
        assert "WARNING: skipped zbad.yaml — " in r.stderr, r.stderr

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
    """--what-if: a draft's bytes at a tree path (`--replaces` substitute,
    `--at` create), judged by `da-guard effective --overlay` (#2906)."""

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
        (conf_d / "team").mkdir()
        (conf_d / "team" / "tenants.yaml").write_text(
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
        return subprocess.run(
            [sys.executable, DESCRIBE, tenant_id, "--conf-d", str(conf_d),
             "--what-if", str(what_if_path), *map(str, extra)],
            capture_output=True, timeout=60,
        )

    def test_what_if_substitute_changes_hash(self, tmp_path):
        """--what-if <copy> --replaces <L0>: a copy that bumps a tenant-visible
        field → substitute + hash changes; an unchanged copy → no change.
        #2097 F1: the L0 file itself as --what-if compared it with itself
        (an in-place edit always read "no reload") and is rc 2."""
        conf_d = self._setup_conf_d(tmp_path)
        l0_path = conf_d / "_defaults.yaml"
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
        assert output["at"] is None
        assert output["merged_hash_changed"] is True
        assert output["would_trigger_reload"] is True
        assert output["changed_keys"] == {
            "pg_replication_lag_seconds": {"baseline": 30, "what_if": 60}}
        # Both hashes are Go's: the baseline is the tree's, the what-if one
        # the tree's with the draft written there.
        assert output["baseline_merged_hash"] == \
            _go_effective(conf_d)[1]["tenants"]["whatif-tenant"]["merged_hash"]
        assert output["what_if_merged_hash"] == _go_effective(
            conf_d, "--overlay", f"_defaults.yaml={whatif}")[1]["tenants"]["whatif-tenant"]["merged_hash"]

        # Unchanged copy → substitute, no change
        same = tmp_path / "same.yaml"
        same.write_bytes(l0_path.read_bytes())
        result = self._run_cli(conf_d, "whatif-tenant", same, "--replaces", l0_path)
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
        output = json.loads(result.stdout.decode())
        assert output["substitution_type"] == "substitute"
        assert output["merged_hash_changed"] is False
        assert output["would_trigger_reload"] is False

        # The L0 file itself as its own replacement → rc 2
        result = self._run_cli(conf_d, "whatif-tenant", l0_path, "--replaces", l0_path)
        assert result.returncode == 2
        assert b"are the same file" in result.stderr

    def test_what_if_at_adds_a_chain_level(self, tmp_path):
        """--at <new path>: the draft is a new `_defaults.yaml` level between
        the root and the tenant — create, its new key in added_keys."""
        conf_d = self._setup_conf_d(tmp_path)
        whatif = tmp_path / "whatif_level.yaml"
        whatif.write_text(yaml.dump({"defaults": {"pg_locks_count": 100,
                                                  "pg_stat_activity_count": 1}}), encoding="utf-8")
        at = conf_d / "team" / "_defaults.yaml"
        result = self._run_cli(conf_d, "whatif-tenant", whatif, "--at", at)
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
        output = json.loads(result.stdout.decode())
        assert output["substitution_type"] == "create"
        assert output["at"] == str(at.resolve()) and output["replaces"] is None
        assert output["merged_hash_changed"] is True
        assert output["added_keys"] == {"pg_locks_count": 100}
        assert output["changed_keys"] == {}, "the tenant's own 300 still wins over the new level"
        assert not at.exists(), "nothing is written to the tree"

    def test_what_if_tenant_override_shields_from_change(self, tmp_path):
        """If tenant overrides a field, changing that field in what-if defaults should NOT change merged_hash."""
        conf_d = self._setup_conf_d(tmp_path)
        whatif = tmp_path / "whatif.yaml"
        whatif.write_text(
            yaml.dump({
                "defaults": {
                    "pg_stat_activity_count": 999,  # tenant still overrides with 300
                    "pg_replication_lag_seconds": 30,
                }
            }),
            encoding="utf-8",
        )
        result = self._run_cli(conf_d, "whatif-tenant", whatif, "--replaces", conf_d / "_defaults.yaml")
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
        output = json.loads(result.stdout.decode())
        assert output["merged_hash_changed"] is False
        assert output["would_trigger_reload"] is False

    def test_what_if_file_not_found(self, tmp_path):
        """Non-existent --what-if path → exit 2 (EXIT_CALLER_ERROR, #452) with clear error."""
        conf_d = self._setup_conf_d(tmp_path)
        result = self._run_cli(conf_d, "whatif-tenant", tmp_path / "does-not-exist.yaml",
                               "--replaces", conf_d / "_defaults.yaml")
        assert result.returncode == EXIT_CALLER_ERROR
        assert b"--what-if file not found" in result.stderr

    def test_bare_what_if_is_refused_naming_both_forms(self, tmp_path):
        """#2906: the implicit `insert` / `append-external` forms are gone —
        without --replaces or --at nothing is guessed."""
        conf_d = self._setup_conf_d(tmp_path)
        draft = tmp_path / "draft.yaml"
        draft.write_text("defaults:\n  pg_locks_count: 1\n", encoding="utf-8")
        for path in (draft, conf_d / ".draft.yaml"):
            if not path.exists():
                path.write_text("defaults:\n  pg_locks_count: 1\n", encoding="utf-8")
            result = self._run_cli(conf_d, "whatif-tenant", path)
            assert result.returncode == EXIT_CALLER_ERROR, result.stderr
            err = result.stderr.decode()
            assert "--replaces <file>" in err and "--at <path>" in err, err
            assert "_defaults.yaml" in err, err
            assert result.stdout == b""

    @pytest.mark.parametrize("case", ["at-exists", "at-outside", "at-missing-parent", "at-hidden",
                                      "replaces-missing", "replaces-unlisted", "both"])
    def test_what_if_refusals(self, tmp_path, case):
        conf_d = self._setup_conf_d(tmp_path)
        draft = tmp_path / "draft.yaml"
        draft.write_text("defaults:\n  pg_locks_count: 1\n", encoding="utf-8")
        (conf_d / ".hidden.yaml").write_text("defaults: {}\n", encoding="utf-8")
        args, want = {
            "at-exists": (["--at", conf_d / "_defaults.yaml"], "already exists"),
            "at-outside": (["--at", tmp_path / "elsewhere" / "_defaults.yaml"], "not inside the scanned tree"),
            "at-missing-parent": (["--at", conf_d / "nope" / "_defaults.yaml"], "not a directory the walk descends"),
            "at-hidden": (["--at", conf_d / ".snap" / "_defaults.yaml"], "not a name the walk reads"),
            "replaces-missing": (["--replaces", conf_d / "gone.yaml"], "is not a file of the scanned tree"),
            "replaces-unlisted": (["--replaces", conf_d / ".hidden.yaml"], "not a name the walk reads"),
            "both": (["--replaces", conf_d / "_defaults.yaml", "--at", conf_d / "team" / "_defaults.yaml"],
                     "cannot be used together"),
        }[case]
        result = self._run_cli(conf_d, "whatif-tenant", draft, *args)
        assert result.returncode == EXIT_CALLER_ERROR, result.stderr
        assert want in result.stderr.decode(), result.stderr.decode()
        assert b"Traceback" not in result.stderr
        assert result.stdout == b""

    def test_what_if_that_drops_the_tenant_is_named(self, tmp_path):
        """The draft stands in for the tenant's own file and no longer
        declares it: Go resolves no such tenant — rc 2, said."""
        conf_d = self._setup_conf_d(tmp_path)
        draft = tmp_path / "draft.yaml"
        draft.write_text("tenants:\n  someone-else: {}\n", encoding="utf-8")
        result = self._run_cli(conf_d, "whatif-tenant", draft, "--replaces", conf_d / "team" / "tenants.yaml")
        assert result.returncode == EXIT_CALLER_ERROR, result.stderr
        assert b"resolves no tenant 'whatif-tenant'" in result.stderr

    def test_what_if_help_text_no_longer_stub(self):
        """--what-if help text must not claim 'not yet implemented' (P0 #5 fix)."""
        result = subprocess.run([sys.executable, DESCRIBE, "--help"], capture_output=True, timeout=10)
        assert result.returncode == 0
        help_text = result.stdout.decode()
        assert "not yet implemented" not in help_text.lower()
        assert "--what-if" in help_text and "--at" in help_text


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

    def _linked_conf_d_tree(self, tmp_path):
        # conf.d is ITSELF a link (`c -> real`): the scanner resolves it, so a
        # tree path must be computed against the holder's REAL directory, not
        # the path spelling the caller typed (#1967).
        real = tmp_path / "real"
        (real / "sub").mkdir(parents=True)
        (real / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
        (real / "sub" / "tw.yaml").write_text("tenants:\n  tw: {}\n", encoding="utf-8")
        self._link(tmp_path / "c", "real")
        draft = tmp_path / "draft.yaml"
        draft.write_text("defaults:\n  cpu_pct: 10\n", encoding="utf-8")
        return tmp_path / "c", draft

    @pytest.mark.parametrize("spelling", ["through-the-link", "real-path", "dotdot"])
    def test_what_if_at_is_one_tree_path_however_spelled(self, tmp_path, spelling):
        # `c/sub/_defaults.yaml`, `real/sub/_defaults.yaml` and
        # `real/sub/../sub/_defaults.yaml` name one tree path: each creates
        # the same `sub/_defaults.yaml` level (10 wins over the root's 50).
        conf_d, draft = self._linked_conf_d_tree(tmp_path)
        at = {"through-the-link": conf_d / "sub" / "_defaults.yaml",
              "real-path": tmp_path / "real" / "sub" / "_defaults.yaml",
              "dotdot": os.path.join(str(tmp_path), "real", "sub", "..", "sub", "_defaults.yaml")}[spelling]
        r = self._run(conf_d, "tw", "--what-if", str(draft), "--at", str(at))
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["substitution_type"] == "create"
        assert out["at"] == str(tmp_path / "real" / "sub" / "_defaults.yaml")
        assert out["changed_keys"] == {"cpu_pct": {"baseline": 50, "what_if": 10}}

    def test_what_if_draft_that_is_a_link_reads_its_target(self, tmp_path):
        conf_d, draft = self._linked_conf_d_tree(tmp_path)
        self._link(tmp_path / "draft-link.yaml", "draft.yaml")
        outs = []
        for d in (draft, tmp_path / "draft-link.yaml"):
            r = self._run(conf_d, "tw", "--what-if", str(d), "--replaces", str(conf_d / "_defaults.yaml"))
            assert r.returncode == 0, r.stderr
            out = json.loads(r.stdout)
            outs.append((out["changed_keys"], out["what_if_merged_hash"]))
        assert outs[0] == outs[1], outs

    def test_link_target_inside_conf_d_is_reported_as_go_reports_it(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "_real.txt").write_text("tenants:\n  tg: {}\n", encoding="utf-8")
        self._link(tmp_path / "tg.yaml", "sub/_real.txt")
        r = self._run(tmp_path, "tg", "--show-sources")
        assert r.returncode == 0, r.stderr
        # #2906: the path da-guard effective reports for it, whatever it is.
        go = _go_effective(tmp_path)[1]["tenants"]["tg"]
        assert json.loads(r.stdout)["source_file"] == go["source_file"]


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
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)

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
        res = self._run("tx", "--conf-d", str(conf_d), "--what-if", str(edited),
                        "--replaces", str(conf_d / "_defaults.yaml"))
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        assert out["changed_keys"] == {"mysql_connections": {"baseline": "60", "what_if": "55"}}

    def test_multi_document_and_unparseable_tenant_files_do_not_end_the_run(self, tmp_path):
        conf_d = self._tree(tmp_path, tm__yaml="tenants:\n  tm: {}\n---\nfoo: 1\n",
                            tb__yaml="tenants:\n  tb: [\n")
        res = self._run("--all", "--conf-d", str(conf_d))
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        # tm: a second document — dropped since ADR-036 step 6 (#3206).
        assert set(out) == {"tx", "ty"}
        assert "WARNING: skipped tb.yaml — " in res.stderr, res.stderr
        assert "WARNING: skipped tm.yaml — " in res.stderr, res.stderr
        assert all(t["merged_hash"] is None for t in out.values())

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
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)

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
        res = self._run("tx", "--conf-d", str(conf_d), "--what-if", str(edited),
                        "--replaces", str(conf_d / "_defaults.yaml"))
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        assert out["changed_keys"] == {"mysql_connections": {"baseline": 80, "what_if": 60}}, out
        assert out["added_keys"] == {} and out["removed_keys"] == {}, out

    def test_alias_spelling_in_the_tenant_file_blocks_the_fill_in(self, tmp_path):
        """#1231 × #2117: a tenant writing the deprecated spelling already
        sets the threshold, so the profile's canonical key is not filled in
        (ApplyProfiles' hasAliasEquivalent)."""
        # The deprecated spelling comes from the alias table, not a literal:
        # the #1231 pre-commit hook forbids the retired key as a live value.
        ops_dir = os.path.join(REPO_ROOT, "scripts", "tools", "ops")
        if ops_dir not in sys.path:
            sys.path.append(ops_dir)
        from _grar_validate import _legacy_tenant_key
        legacy = _legacy_tenant_key("mysql_threads_running")
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




# ---------------------------------------------------------------------------
# Go's values, as Go writes them (#2906)
# ---------------------------------------------------------------------------
#
# These shapes are the ones this tool's PyYAML imitation of the exporter was
# measured against (#2371 / #2415 / #2459: timestamps, `!!binary`, mapping
# keys yaml.v3 spells with `%v`, explicit tags, `_defaults.yaml` documents
# of every shape) — including the rows that imitation could not align and
# pinned as strict xfails. The imitation is gone: the oracle for every row
# is `da-guard effective` run directly on the same tree, and the tool must
# show exactly its answer — the same tenants, the same effective config,
# chain, overlays, source hash and (on a tree the load reads whole) the
# same merged_hash.

VALUE_BODIES = [
    ('date', '_state_maintenance:\n  target: all\n  expires: 2026-12-31\n', None),
    ('date-tagged', '_x:\n  at: !!timestamp 2026-12-31\n', None),
    ('date-in-list', '_x:\n  - 2026-12-31\n  - a\n', None),
    ('date-year-below-1000', '_x:\n  at: 0999-01-02\n', None),
    ('date-in-defaults', 'mysql_connections: 60\n', 'defaults:\n  mysql_connections: 50\n  _state_maintenance:\n    expires: 2026-12-31\n'),
    ('date-in-defaults-merged', '_state_maintenance:\n  target: all\n', 'defaults:\n  _state_maintenance:\n    expires: 2026-12-31\n'),
    ('datetime-z', '_x:\n  at: 2026-12-31T10:20:30Z\n', None),
    ('datetime-lower-t-z', '_x:\n  at: 2026-12-31t10:20:30Z\n', None),
    ('datetime-plus-zero', '_x:\n  at: 2026-12-31T10:20:30+00:00\n', None),
    ('datetime-offset', '_x:\n  at: 2026-12-31T10:20:30+08:00\n', None),
    ('datetime-negative-offset', '_x:\n  at: 2026-12-31T10:20:30-05:30\n', None),
    ('datetime-fraction-6', '_x:\n  at: 2026-12-31T10:20:30.123456Z\n', None),
    ('datetime-fraction-1', '_x:\n  at: 2026-12-31T10:20:30.5Z\n', None),
    ('datetime-fraction-trailing-zeros', '_x:\n  at: 2026-12-31T10:20:30.500Z\n', None),
    ('datetime-one-digit-fields', '_x:\n  at: 2026-1-2T3:04:05Z\n', None),
    ('datetime-naive-t', '_x:\n  at: 2026-12-31T10:20:30\n', None),
    ('datetime-naive-t-fraction', '_x:\n  at: 2026-12-31T10:20:30.5\n', None),
    ('binary-utf8', '_routing:\n  receiver:\n    type: webhook\n    url: !!binary aHR0cDovL3g=\n', None),
    ('binary-cjk', '_x:\n  v: !!binary 5Lit5paH\n', None),
    ('binary-empty', '_x:\n  v: !!binary ""\n', None),
    ('binary-in-defaults', 'mysql_connections: 60\n', 'defaults:\n  _x:\n    v: !!binary aHR0cDovL3g=\n'),
    ('binary-invalid-byte', '_routing:\n  receiver:\n    type: webhook\n    url: !!binary /w==\n', None),
    ('binary-nul-and-invalid', '_x:\n  v: !!binary YQDA/2I=\n', None),
    ('binary-truncated-sequence', '_x:\n  v: !!binary 5Lg=\n', None),
    ('binary-overlong', '_x:\n  v: !!binary wIA=\n', None),
    ('binary-encoded-surrogate', '_x:\n  v: !!binary 7aCA\n', None),
    ('string-real-fffd', '_x:\n  v: "\\ufffd"\n', None),
    ('datetime-space-no-zone', '_silent_mode:\n  expires: 2026-12-31 23:59:59\n', None),
    ('datetime-space-fraction', '_x:\n  at: 2026-12-31 10:20:30.5\n', None),
    ('datetime-two-spaces', '_x:\n  at: 2026-12-31  10:20:30\n', None),
    ('datetime-space-in-defaults', 'mysql_connections: 60\n', 'defaults:\n  _x:\n    at: 2026-12-31 10:20:30\n'),
    ('datetime-naive-t-trailing-zeros', '_x:\n  at: 2026-12-31T10:20:30.500\n', None),
    ('datetime-naive-lower-t', '_x:\n  at: 2026-12-31t10:20:30\n', None),
    ('datetime-space-before-zone', '_x:\n  at: 2026-12-31 10:20:30 +08:00\n', None),
    ('datetime-space-then-z', '_x:\n  at: 2026-12-31 10:20:30Z\n', None),
    ('datetime-hour-only-zone', '_x:\n  at: 2026-12-31T10:20:30+08\n', None),
    ('datetime-fraction-9', '_x:\n  at: 2026-12-31T10:20:30.123456789Z\n', None),
    ('datetime-fraction-10', '_x:\n  at: 2026-12-31T10:20:30.1234567891Z\n', None),
    ('datetime-comma-fraction', '_x:\n  at: 2026-12-31T10:20:30,5Z\n', None),
    ('datetime-hour-24', '_x:\n  at: 2026-12-31T24:00:00Z\n', None),
    ('datetime-second-60', '_x:\n  at: 2026-12-31 23:59:60\n', None),
    ('datetime-tagged-space', '_x:\n  at: !!timestamp 2026-12-31 10:20:30\n', None),
    ('datetime-quoted', '_x:\n  at: "2026-12-31 23:59:59"\n', None),
    ('date-one-digit-fields', '_x:\n  at: 2026-1-2\n', None),
    ('date-one-digit-in-defaults', 'mysql_connections: 60\n', 'defaults:\n  _x:\n    at: 2026-1-2\n'),
    ('date-in-list-go-only-layouts', '_x:\n  - 2026-12-31 23:59:59\n  - 2026-1-2\n', None),
    ('date-month-13', '_x:\n  at: 2026-13-01\n', None),
    ('date-day-0', '_x:\n  at: 2026-12-00\n', None),
    ('date-arabic-indic-digits', '_x:\n  at: ٢٠٢٦-١٢-٣١\n', None),
    ('date-feb-30', '_x:\n  at: 2026-02-30\n', None),
    ('zone-minute-60-carries', '_x:\n  at: 2026-12-31T10:20:30+08:60\n', None),
    ('zone-minute-60-carries-negative', '_x:\n  at: 2026-12-31T10:20:30-00:60\n', None),
    ('zone-23-59', '_x:\n  at: 2026-12-31T10:20:30+23:59\n', None),
    ('zone-hour-99-stays-text', '_x:\n  at: 2026-12-31T10:20:30.500+99:00\n', None),
    ('zone-hour-25-stays-text', '_x:\n  at: 2026-12-31T10:20:30+25:00\n', None),
    ('zone-minute-61-stays-text', '_x:\n  at: 2026-12-31T10:20:30+08:61\n', None),
    ('year-0', '_x:\n  at: 0000-01-01\n', None),
    ('year-0-leap-day', '_x:\n  at: 0000-02-29\n', None),
    ('year-0-space-clock', '_x:\n  at: 0000-12-31 10:20:30\n', None),
    ('year-0-in-defaults', 'mysql_connections: 60\n', 'defaults:\n  _x:\n    at: 0000-01-01\n'),
    ('year-1-not-leap', '_x:\n  at: 0001-02-29\n', None),
    ('year-2100-not-leap', '_x:\n  at: 2100-02-29\n', None),
    ('quoted-go-only-layout', "_x:\n  at: '2026-1-2'\n", None),
    ('block-strip-go-only-layout', '_x:\n  at: |-\n    2026-1-2\n', None),
    ('block-keep-go-only-layout', '_x:\n  at: |\n    2026-1-2\n', None),
    ('block-keep-date', '_x:\n  at: |\n    2026-12-31\n', None),
    ('tagged-block-strip-date', '_x:\n  at: !!timestamp |-\n    2026-12-31\n', None),
    ('zone-24-00', '_x:\n  at: 2026-12-31T10:20:30+24:00\n', None),
    ('zone-minus-23-60', '_x:\n  at: 2026-12-31T10:20:30-23:60\n', None),
    ('zone-24-01', '_x:\n  at: 2026-12-31T10:20:30+24:01\n', None),
    ('zone-24-00-one-digit-fields', '_x:\n  at: 2026-1-2T1:2:3+24:00\n', None),
    ('key-ts-foo-direct', '!!timestamp foo: 1\n', None),
    ('key-ts-foo-nested', '_x:\n  !!timestamp foo: 1\n', None),
    ('key-ts-quoted', "_x:\n  !!timestamp '2026-12-31': 1\n", None),
    ('explicit-str-on-a-go-only-timestamp', '_x:\n  at: !!str 2026-1-2\n', None),
    ('explicit-timestamp-go-cannot-parse', '_x:\n  at: !!timestamp 2026-12-31T10:20:30\n', None),
    ('explicit-timestamp-month-13', '_x:\n  at: !!timestamp 2026-13-01\n', None),
    ('tab-between-date-and-time', '_x:\n  at: 2026-12-31\t10:20:30\n', None),
    ('explicit-timestamp-word', '_x:\n  at: !!timestamp foo\n', None),
    ('explicit-timestamp-empty', '_x:\n  at: !!timestamp\n', None),
    ('explicit-timestamp-quoted-empty', '_x:\n  at: !!timestamp ""\n', None),
    ('explicit-timestamp-block-keep', '_x:\n  at: !!timestamp |\n    2026-12-31\n', None),
]
KEYS = [
    '2026-12-31',
    '"2026-12-31"',
    "'2026-12-31'",
    '!!str 2026-12-31',
    '!!timestamp 2026-12-31',
    '2026-1-2',
    '0999-01-02',
    '2026-13-01',
    '2026-02-30',
    '2026-12-31T10:20:30Z',
    '2026-12-31t10:20:30Z',
    '2026-12-31T10:20:30+00:00',
    '2026-12-31T10:20:30+08:00',
    '2026-12-31T10:20:30-05:30',
    '2026-12-31T10:20:30.5Z',
    '2026-12-31T10:20:30,5Z',
    '2026-12-31T10:20:30.123456789Z',
    '2026-12-31 10:20:30',
    '2026-12-31  10:20:30',
    '2026-12-31T10:20:30',
    '2026-12-31T24:00:00Z',
    '2026-12-31T10:20:30+08:60',
    '2026-12-31T10:20:30+99:00',
    '2026-12-31T10:20:30+08:61',
    '2026-12-31T10:20:30+24:00',
    '0000-01-01',
    '0001-02-29',
    '1',
    '010',
    '0777',
    '08',
    '0888',
    '0x1F',
    '-0x1F',
    '+0x1F',
    '0x_1F',
    '0x',
    '0o17',
    '0b101',
    '0b',
    '1_000',
    '1__0',
    '+5',
    '-5',
    '9223372036854775808',
    '18446744073709551615',
    '+18446744073709551615',
    '18446744073709551616',
    '-9223372036854775809',
    '1.5',
    '1.0',
    '0.',
    '.5',
    '+1.5',
    '1.5_0',
    '-0.0',
    '0.1',
    '1e3',
    '1.5e3',
    '1e21',
    '1e400',
    '1000000.0',
    '123456.0',
    '123456789.0',
    '0.0001',
    '1e-4',
    '0.00001',
    '-1.5e-7',
    '.inf',
    '-.Inf',
    '+.INF',
    '.nan',
    '.5_0',
    '.5e1_0',
    '.5E+3',
    '.5e400',
    '._5',
    '.5_',
    '.5__0',
    '.5e_1',
    '._',
    'true',
    'True',
    'TRUE',
    'false',
    'yes',
    'on',
    'no',
    'off',
    'y',
    '12:30',
    '1:30:00',
    '!!str 010',
    '"010"',
    '!!int 010',
    '!!float 1',
    'abc',
    '~',
    'null',
    '!!str 1e3',
]
DEFAULTS_SHAPES = {
    'unclosed-flow': b'defaults: [\n',
    'not-utf8': b'defaults:\n  x: "\xff\xfe"\n',
    'surrogate-escape': b'defaults:\n  _x: "\\udfff"\n',
    'tagged-int': b'defaults:\n  x: !!int foo\n',
    'tagged-float': b'defaults:\n  x: !!float foo\n',
    'tagged-bool': b'defaults:\n  x: !!bool foo\n',
    'tagged-bool-yes': b'defaults:\n  x: !!bool yes\n',
    'tagged-bool-mixed-case': b'defaults:\n  x: !!bool tRuE\n',
    'tagged-binary-bad-chars': b'defaults:\n  x: !!binary "%%%"\n',
    'tagged-binary-unpadded': b'defaults:\n  x: !!binary "aGVsbG8"\n',
    'defaults-list': b'defaults: [1, 2]\n',
    'defaults-scalar': b'defaults: 5\n',
    'document-list': b'- 1\n- 2\n',
    'document-scalar': b'hello\n',
    'document-false': b'false\n',
}
DEFAULTS_DOCUMENTS = {
    'control': ('defaults:\n  mysql_connections: 80\n', '664e2553d242d345'),
    # The multi-document rows, and the `!!binary` one below, hashed the first
    # document (664e2553…) until ADR-036 step 6 (#3206): parse-tree refuses
    # them (multiple_documents, tag_not_allowed), the exporter drops the
    # carrier, and the hash is the empty defaults' (dc8202df…).
    'multi-doc': ('defaults:\n  mysql_connections: 80\n---\ndefaults:\n  mysql_connections: 90\n', 'dc8202dfc4683b17'),
    'multi-doc-later-unparseable': ('defaults:\n  mysql_connections: 80\n---\n: : [\n', 'dc8202dfc4683b17'),
    'multi-doc-later-bad-shape': ('defaults:\n  mysql_connections: 80\n---\ndefaults: [1]\n', 'dc8202dfc4683b17'),
    'multi-doc-empty-first': ('---\n---\ndefaults:\n  mysql_connections: 80\n', 'dc8202dfc4683b17'),
    'defaults-null': ('defaults: ~\n', 'dc8202dfc4683b17'),
    'defaults-null-with-sibling': ('defaults: ~\nmysql_connections: 80\n', '664e2553d242d345'),
    'tagged-bool-true': ('defaults:\n  mysql_connections: 80\n  x: !!bool TRUE\n', '01fde994832c3568'),
    'tagged-bool-quoted': ('defaults:\n  mysql_connections: 80\n  x: !!bool "true"\n', '01fde994832c3568'),
    'tagged-binary-block': ('defaults:\n  mysql_connections: 80\n  x: !!binary |\n    aGVs\n    bG8=\n', 'dc8202dfc4683b17'),
}
NOT_A_MAPPING = {'list': '- 1\n- 2\n', 'scalar': 'hello\n', 'false': 'false\n'}

# Whole files (#2371 review 3 F1): what else is in the file around t1.
WHOLE_FILES = [
    ("other-tenant-in-file-24h",
     "tenants:\n  t1:\n    mysql_connections: 60\n  t2:\n    _x:\n      at: 2026-12-31T10:20:30+24:00\n", None),
    ("root-sibling-24h",
     "tenants:\n  t1:\n    mysql_connections: 60\nextra: 2026-12-31T10:20:30+24:00\n", None),
    ("overridden-default-24h", "tenants:\n  t1:\n    _x: 1\n",
     "defaults:\n  mysql_connections: 50\n  _x: 2026-12-31T10:20:30+24:00\n"),
    ("tenantid-ts-foo", "tenants:\n  !!timestamp foo:\n    a: 1\n  t1:\n    a: 2\n", None),
    ("surrogate-value", 'tenants:\n  t1:\n    _x: "\\udfff"\n', None),
    ("surrogate-profile", 'tenants:\n  t1:\n    _profile: "\\udfff"\n', None),
    ("refused-bool-tag", 'tenants:\n  t1:\n    x: !!bool yes\n', None),
    ("refused-binary-tag", 'tenants:\n  t1:\n    x: !!binary "%%%"\n', None),
    ("multi-document", "tenants:\n  t1: {}\n---\nfoo: 1\n", None),
]

_DEFAULTS = "defaults:\n  mysql_connections: 50\n"


def _trees():
    """(id, {path: content}) for every row above."""
    for i, b, d in VALUE_BODIES:
        yield f"value:{i}", {"_defaults.yaml": d or _DEFAULTS,
                             "t1.yaml": "tenants:\n  t1:\n" + textwrap.indent(b, "    ")}
    for k in KEYS:
        yield f"key-in-tenant:{k}", {"_defaults.yaml": _DEFAULTS,
                                     "t1.yaml": f"tenants:\n  t1:\n    _x:\n      {k}: v\n"}
        yield f"key-in-defaults:{k}", {"_defaults.yaml": f"defaults:\n  _x:\n    {k}: v\n",
                                       "t1.yaml": "tenants:\n  t1:\n    mysql_connections: 60\n"}
    for i, t, d in WHOLE_FILES:
        yield f"file:{i}", {"_defaults.yaml": d or _DEFAULTS, "t1.yaml": t, "t9.yaml": "tenants:\n  t9: {}\n"}
    for i, (d, _h) in DEFAULTS_DOCUMENTS.items():
        yield f"defaults-doc:{i}", {"_defaults.yaml": d, "t1.yaml": 'tenants:\n  t1:\n    redis_memory: "50"\n'}
    for i, d in NOT_A_MAPPING.items():
        yield f"defaults-not-a-mapping:{i}", {"_defaults.yaml": d, "t1.yaml": "tenants:\n  t1: {}\n"}
    for i, raw in DEFAULTS_SHAPES.items():
        for carrier in ("_defaults.yaml", "sub/_defaults.yaml"):
            files = {"_defaults.yaml": _DEFAULTS, "t1.yaml": "tenants:\n  t1:\n    mysql_connections: 10\n",
                     "sub/ts.yaml": "tenants:\n  ts: {}\n"}
            files[carrier] = raw
            yield f"defaults-shape:{carrier}:{i}", files


TREES = list(_trees())


def _write(tmp_path, files):
    conf_d = tmp_path / "conf.d"
    for rel, content in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return conf_d


class TestShowsGoValuesVerbatim:
    """Every row: `--all` is da-guard effective's answer, field for field."""

    FIELDS = ("tenant_id", "source_file", "source_hash", "defaults_chain", "effective_config")

    @pytest.mark.parametrize("files", [f for _i, f in TREES], ids=[i for i, _f in TREES])
    def test_all_is_da_guard_effective(self, tmp_path, files):
        conf_d = _write(tmp_path, files)
        go_rc, go = _go_effective(conf_d)
        res = subprocess.run([sys.executable, DESCRIBE, "--all", "--conf-d", str(conf_d)],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert "Traceback" not in res.stderr, res.stderr
        if go is None or not go["tenants"]:
            # Go resolves no tenant (it refuses the tree, or a chain file
            # stops the resolve): nothing is described, rc 2, said.
            assert res.returncode == EXIT_CALLER_ERROR, (go_rc, res.returncode, res.stderr)
            assert res.stdout == ""
            return
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        assert set(out) == set(go["tenants"])
        dropped = bool(go["parse_failed"] or go["unreadable"])
        for tid, info in out.items():
            want = go["tenants"][tid]
            for field in self.FIELDS:
                assert info[field] == want[field], (tid, field)
            for field in ("platform_overlay", "profile_overlay"):
                assert info.get(field) == want.get(field), (tid, field)
            assert info["merged_hash"] == (None if dropped else want["merged_hash"])
            if dropped:
                assert info["merged_hash_error"] == dt.MERGED_HASH_UNREADABLE_TREE
        if dropped:
            assert "skips or cannot read file(s) of this tree" in res.stderr, res.stderr

    @pytest.mark.parametrize("case", sorted(DEFAULTS_DOCUMENTS))
    def test_merged_hash_is_the_exporters(self, tmp_path, case):
        """#2459's measured ResolveEffective MergedHash for each document
        shape (`_defaults.yaml` + `tx.yaml`), now read from da-guard."""
        body, go_hash = DEFAULTS_DOCUMENTS[case]
        conf_d = _write(tmp_path, {"_defaults.yaml": body, "tx.yaml": 'tenants:\n  tx:\n    redis_memory: "50"\n'})
        _rc, go = _go_effective(conf_d)
        assert go["tenants"]["tx"]["merged_hash"] == go_hash
        res = subprocess.run([sys.executable, DESCRIBE, "tx", "--show-sources", "--conf-d", str(conf_d)],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert res.returncode == 0, res.stderr
        # Reported only on a tree the exporter's load reads whole (#1549).
        dropped = go["parse_failed"] or go["unreadable"]
        assert json.loads(res.stdout)["merged_hash"] == (None if dropped else go_hash)

    def test_a_subdirectory_defaults_not_a_mapping_is_served_as_go_serves_it(self, tmp_path):
        """In a sub-directory the exporter serves `defaults: [1]` beside
        `mysql_connections: 81` (both planes take the whole document;
        merged_hash cef3dbb6…). This tool used to refuse it (rc 2, "an
        unsupported shape"); it now shows Go's answer."""
        conf_d = _write(tmp_path, {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
                                   "team/_defaults.yaml": "defaults: [1]\nmysql_connections: 81\n",
                                   "team/tx.yaml": 'tenants:\n  tx:\n    redis_memory: "50"\n'})
        res = subprocess.run([sys.executable, DESCRIBE, "tx", "--show-sources", "--conf-d", str(conf_d)],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        assert out["effective_config"]["mysql_connections"] == 81
        assert out["merged_hash"].startswith("cef3dbb6"), out["merged_hash"]

    @pytest.mark.parametrize("files", [
        {"_defaults.yaml": _DEFAULTS, "t1.yaml": "tenants:\n  t1:\n    _x:\n      at: 2026-12-31T10:20:30+08:00\n"},
        {"_defaults.yaml": "defaults:\n  _x:\n    2026-12-31: v\n", "t1.yaml": "tenants:\n  t1: {}\n"},
        # A `!!binary` value was a row: since ADR-036 step 6 (#3206) the
        # exporter drops such a file (parse-tree's tag_not_allowed).
        {"_defaults.yaml": _DEFAULTS, "t1.yaml": "tenants:\n  t1:\n    a: .inf\n    b: .nan\n"},
    ], ids=["datetime", "date-key", "non-finite"])
    @pytest.mark.parametrize("mode", [
        ["t1"], ["t1", "--show-sources"], ["t1", "--format", "yaml"],
        ["--all"], ["--all", "--format", "yaml"],
    ], ids=lambda m: " ".join(m))
    def test_no_mode_ends_in_a_traceback(self, tmp_path, files, mode):
        conf_d = _write(tmp_path, files)
        res = subprocess.run([sys.executable, DESCRIBE, *mode, "--conf-d", str(conf_d)],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert res.returncode == 0, res.stderr
        assert "Traceback" not in res.stderr
        out = yaml.safe_load(res.stdout) if "yaml" in mode else json.loads(res.stdout)
        effective = (out["t1"] if "--all" in mode else out)["effective_config"]
        assert effective == _go_effective(conf_d)[1]["tenants"]["t1"]["effective_config"]

    @pytest.mark.parametrize("fmt", ["json", "yaml"])
    def test_all_keeps_tenant_ids_as_source_text(self, tmp_path, fmt):
        """`--all` is keyed by TENANT ID, which the exporter keys by source
        text (#2114): tenants `010` and `8` are two tenants."""
        conf_d = _write(tmp_path, {"_defaults.yaml": _DEFAULTS,
                                   "a.yaml": "tenants:\n  010:\n    mysql_connections: 10\n",
                                   "b.yaml": "tenants:\n  8:\n    mysql_connections: 8\n"})
        res = subprocess.run([sys.executable, DESCRIBE, "--all", "--format", fmt, "--conf-d", str(conf_d)],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert res.returncode == 0, res.stderr
        out = yaml.safe_load(res.stdout) if fmt == "yaml" else json.loads(res.stdout)
        assert set(out) == {"010", "8"}, out
        assert out["010"]["effective_config"] == {"mysql_connections": 10}
        assert out["8"]["effective_config"] == {"mysql_connections": 8}


class TestTreesGoDoesNotResolve:
    """A tree whose tenant Go does not resolve — a chain `_defaults.yaml` the
    resolve rejects, a tenant file the load skips: named, rc 2 (the code
    #2413 gave an unparseable `_defaults.yaml`), never a traceback."""

    def _tree(self, tmp_path, carrier_bytes=None, tenant=None):
        files = {"_defaults.yaml": _DEFAULTS, "tr.yaml": tenant or "tenants:\n  tr:\n    mysql_connections: 10\n",
                 "sub/ts.yaml": "tenants:\n  ts: {}\n"}
        if carrier_bytes is not None:
            files["_defaults.yaml"] = carrier_bytes
        return _write(tmp_path, files)

    def _run(self, *args):
        return subprocess.run([sys.executable, DESCRIBE, *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)

    @pytest.mark.parametrize("mode", [["tr"], ["--all"], ["--all", "--format", "yaml"]],
                             ids=lambda m: " ".join(m))
    def test_unparseable_defaults_is_named_and_the_values_are_gos(self, tmp_path, mode):
        """#2906: a root `_defaults.yaml` that does not parse is the
        exporter's to judge. Its load skips the file and still resolves the
        tenants (without that level) — so they are described as Go resolves
        them, the file named and no merged_hash, where this tool used to stop
        the whole run (rc 2, #2413)."""
        conf_d = self._tree(tmp_path, b"defaults: [\n")
        go_rc, go = _go_effective(conf_d)
        assert go_rc == 3 and go["tenants"], (go_rc, go)
        res = self._run(*mode, "--conf-d", str(conf_d))
        assert "Traceback" not in res.stderr, res.stderr
        assert res.returncode == 0, (res.returncode, res.stderr)
        assert "WARNING: skipped _defaults.yaml — " in res.stderr, res.stderr
        out = yaml.safe_load(res.stdout) if "yaml" in mode else json.loads(res.stdout)
        got = out if mode == ["tr"] else out["tr"]
        assert got["effective_config"] == go["tenants"]["tr"]["effective_config"]

    @pytest.mark.parametrize("shape", sorted(DEFAULTS_SHAPES))
    def test_a_draft_that_does_not_decode_there_is_named(self, tmp_path, shape):
        """--what-if <draft> --replaces _defaults.yaml: rc 2 naming the draft
        exactly when the exporter's load would skip that path with these
        bytes, or no longer resolve the tenant (da-guard run directly)."""
        conf_d = self._tree(tmp_path)
        draft = tmp_path / "what-if.yaml"
        draft.write_bytes(DEFAULTS_SHAPES[shape])
        _rc, go = _go_effective(conf_d, "--overlay", f"_defaults.yaml={draft}")
        refused = go is None or "_defaults.yaml" in go["parse_failed"] or "tr" not in go["tenants"]
        res = self._run("tr", "--conf-d", str(conf_d), "--what-if", str(draft),
                        "--replaces", str(conf_d / "_defaults.yaml"))
        assert "Traceback" not in res.stderr, res.stderr
        if refused:
            assert res.returncode == EXIT_CALLER_ERROR, (res.returncode, res.stderr)
            assert str(draft.resolve()) in res.stderr, res.stderr
        else:
            assert res.returncode == 0, res.stderr
            base = _go_effective(conf_d)[1]["tenants"]["tr"]["effective_config"]
            want = go["tenants"]["tr"]["effective_config"]
            out = json.loads(res.stdout)
            assert out["changed_keys"] == {k: {"baseline": base[k], "what_if": want[k]}
                                           for k in base if k in want and base[k] != want[k]}
            assert out["added_keys"] == {k: want[k] for k in want if k not in base}
            assert out["removed_keys"] == {k: base[k] for k in base if k not in want}
            assert out["what_if_merged_hash"] == go["tenants"]["tr"]["merged_hash"]

    def test_control_parseable_defaults_still_describe(self, tmp_path):
        conf_d = self._tree(tmp_path)
        res = self._run("--all", "--conf-d", str(conf_d))
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        assert out["tr"]["effective_config"] == {"mysql_connections": 10}
        assert out["ts"]["effective_config"] == {"mysql_connections": 50}
        assert "skips or cannot read" not in res.stderr

    def test_control_unparseable_tenant_file_is_still_skipped(self, tmp_path):
        conf_d = self._tree(tmp_path, tenant="tenants: [\n")
        res = self._run("--all", "--conf-d", str(conf_d))
        assert res.returncode == 0, res.stderr
        assert set(json.loads(res.stdout)) == {"ts"}
        assert "WARNING: skipped tr.yaml — " in res.stderr, res.stderr

    def test_a_tenant_in_a_skipped_file_is_not_found_and_says_why(self, tmp_path):
        conf_d = self._tree(tmp_path, tenant='tenants:\n  tr:\n    x: !!bool yes\n')
        res = self._run("tr", "--conf-d", str(conf_d))
        assert res.returncode == EXIT_CALLER_ERROR, res.stderr
        assert "Tenant 'tr' not found — the exporter's load skips" in res.stderr, res.stderr
        assert "tr.yaml" in res.stderr

    def test_multi_document_what_if_is_a_draft_the_exporter_drops(self, tmp_path):
        """The draft is read as the exporter reads that path. It used to be its
        first document; since ADR-036 step 6 (#3206) a second document makes
        parse-tree refuse it, so the run says the exporter would skip it."""
        conf_d = _write(tmp_path, {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
                                   "tx.yaml": 'tenants:\n  tx:\n    redis_memory: "50"\n'})
        what_if = tmp_path / "what-if.yaml"
        what_if.write_text("defaults:\n  mysql_connections: 90\n---\n: : [\n", encoding="utf-8")
        res = self._run("tx", "--conf-d", str(conf_d), "--what-if", str(what_if),
                        "--replaces", str(conf_d / "_defaults.yaml"))
        assert res.returncode == EXIT_CALLER_ERROR, res.stderr
        assert "the draft does not decode there" in res.stderr, res.stderr

    def test_a_root_document_not_a_mapping_leaves_custom_alerts_resolved(self, tmp_path):
        """The custom-alert walker reads the root carrier through parse-tree:
        a document that is not a mapping is one file it quarantines, not a
        resolver failure — the subtree's policy still resolves."""
        conf_d = _write(tmp_path, {"_defaults.yaml": "- 1\n",
                                   "team/_defaults.yaml": yaml.dump({"_custom_alerts": [
                                       {"recipe": "threshold", "name": "team_policy", "metric": "policy_metric",
                                        "op": ">", "window": "5m", "threshold": "200:critical"}]}),
                                   "team/tx.yaml": 'tenants:\n  tx:\n    redis_memory: "50"\n'})
        res = self._run("tx", "--conf-d", str(conf_d))
        assert res.returncode == 0, res.stderr
        assert "inheritance resolution failed" not in res.stderr, res.stderr
        eff = json.loads(res.stdout)["effective_config"]
        assert [a["name"] for a in eff["_custom_alerts"]] == ["team_policy"]


class TestNoPythonYamlReadOfConfD:
    """ADR-036 tool closure (#2906): one run reads conf.d one way. Every
    mode runs in-process here with PyYAML's parsers made to fail — the pure
    one (every Loader builds a `yaml.reader.Reader`) and libyaml's
    (`CParser`): any YAML parse in the run, of a conf.d file or anything
    else, ends the test. Output via `yaml.dump` is an emitter, not a parse."""

    @pytest.fixture
    def tree(self, tmp_path):
        conf_d = _write(tmp_path, {
            "_defaults.yaml": "defaults:\n  mysql_connections: 80\n_custom_alerts:\n"
                              "  - {recipe: threshold, name: p, metric: m, op: '>', window: 5m, threshold: '1:warning'}\n",
            "_profiles.yaml": "profiles:\n  std:\n    pg_connections: 5\n",
            "team/_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
            "team/a.yaml": "tenants:\n  ta:\n    _profile: std\n  tb: {}\n",
        })
        draft = tmp_path / "draft.yaml"
        draft.write_text("defaults:\n  mysql_connections: 60\n", encoding="utf-8")
        return conf_d, draft

    @pytest.fixture
    def no_yaml_parse(self, monkeypatch):
        calls = []

        def refuse(self, *a, **kw):
            calls.append(a[:1])
            raise AssertionError(f"PyYAML parse during describe-tenant: {a[:1]!r:.120}")

        monkeypatch.setattr(yaml.reader.Reader, "__init__", refuse)
        if hasattr(yaml, "CParser"):
            monkeypatch.setattr(yaml.CParser, "__init__", refuse)
        return calls

    @pytest.mark.parametrize("mode", [
        ["--all"], ["--all", "--format", "yaml"], ["ta", "--show-sources"], ["ta"],
        ["ta", "--diff", "tb"], ["ta", "--what-if", "DRAFT", "--replaces", "TEAM"],
        ["tb", "--what-if", "DRAFT", "--at", "NEW"],
    ], ids=lambda m: " ".join(m))
    def test_every_mode_parses_no_yaml(self, tree, no_yaml_parse, monkeypatch, capsys, mode):
        conf_d, draft = tree
        subst = {"DRAFT": str(draft), "TEAM": str(conf_d / "team" / "_defaults.yaml"),
                 "NEW": str(conf_d / "team" / "_x.yaml")}
        monkeypatch.setattr(sys, "argv", ["describe_tenant", *[subst.get(a, a) for a in mode],
                                          "--conf-d", str(conf_d)])
        try:
            dt.main()
        except SystemExit as exc:
            assert exc.code in (0, None), capsys.readouterr().err
        out = capsys.readouterr().out
        assert out.strip(), "nothing was described"
        assert no_yaml_parse == []

    def test_the_guard_fires_on_a_yaml_parse(self, no_yaml_parse):
        """Must-fire control: the patch really stops a PyYAML parse."""
        with pytest.raises(AssertionError, match="PyYAML parse"):
            yaml.safe_load("a: 1\n")
