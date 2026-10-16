"""Tests for sync_schema.py — the reserved `_` key lists generated from the one
registry, components/threshold-exporter/app/pkg/config/reserved_keys.json
(ADR-038 decision 2).

`TestCommittedFilesAreGenerated` IS the CI gate: the Python constants, Go's
reserved_keys_gen.go and the two schemas' `_` keys must be exactly what the
registry generates, so a key added in one place only fails CI. (Before the
registry, three hand-kept copies were held together by a Python↔Go parity
test and a Schema↔Go test; generation replaces both.)

Whether the registry says what the readers actually do is measured by
tests/shared/test_reserved_key_probes.py, not here.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

import sync_schema as ss

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TOOL = os.path.join(_REPO, "scripts", "tools", "dx", "sync_schema.py")
_TARGETS = (ss.PY_REL, ss.GO_REL, ss.TENANT_SCHEMA_REL, ss.PLATFORM_SCHEMA_REL)

pytestmark = pytest.mark.skipif(
    not os.path.isfile(os.path.join(_REPO, ss.REGISTRY_REL)),
    reason="the registry is not in this checkout (Python-only tree)")


def _registry():
    with open(os.path.join(_REPO, ss.REGISTRY_REL), encoding="utf-8") as f:
        return json.load(f)


def _copy_tree(tmp_path, registry=None):
    """The registry and the four generated files under *tmp_path*."""
    for rel in (ss.REGISTRY_REL, *_TARGETS):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(os.path.join(_REPO, rel), dst)
    if registry is not None:
        (tmp_path / ss.REGISTRY_REL).write_text(json.dumps(registry), encoding="utf-8")
    return tmp_path


def _cli(*args):
    return subprocess.run([sys.executable, _TOOL, *args], cwd=_REPO, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)


def _read(root, rel):
    return (root / rel).read_text(encoding="utf-8")


class TestCommittedFilesAreGenerated:
    def test_nothing_is_stale(self):
        assert ss.run(_REPO, os.path.join(_REPO, ss.REGISTRY_REL), update=False) == []

    def test_check_passes_on_the_repo(self):
        p = _cli("--check")
        assert p.returncode == 0, p.stdout + p.stderr

    def test_the_imported_constants_are_the_registry_s(self):
        import _lib_constants as c
        d = ss.derive(ss.load_registry(os.path.join(_REPO, ss.REGISTRY_REL)))
        assert sorted(c.VALID_RESERVED_KEYS) == d["reserved_keys"]
        assert list(c.VALID_RESERVED_PREFIXES) == d["reserved_prefixes"]
        assert sorted(c.TENANT_ROUTING_KEYS) == d["tenant_routing_keys"]
        assert sorted(c.PLATFORM_ROUTING_KEYS) == d["platform_routing_keys"]
        assert sorted(c.TOP_LEVEL_READ_ELSEWHERE) == d["top_level_read_elsewhere"]
        assert c.RETIRED_RESERVED_KEYS == d["retired"]


class TestCheckAndUpdate:
    # A hand edit --update can undo byte for byte. (Renaming a schema
    # property cannot be undone: the stub that replaces it has no body.)
    @pytest.mark.parametrize("rel,old,new", [
        (ss.PY_REL, '"_silent_mode",', '"_silent_mod",'),
        (ss.GO_REL, '"_silent_mode",', '"_silent_mod",'),
        (ss.TENANT_SCHEMA_REL, "_routing_profile|_severity_dedup", "_routing_profile"),
        (ss.PLATFORM_SCHEMA_REL, '"^_state_"', '"^_stat_"'),
    ])
    def test_a_hand_edit_is_stale_and_update_restores_it(self, tmp_path, rel, old, new):
        root = _copy_tree(tmp_path)
        good = _read(root, rel)
        bad = good.replace(old, new, 1)
        assert bad != good, rel
        (root / rel).write_text(bad, encoding="utf-8")
        check = _cli("--root", str(root), "--check")
        assert check.returncode == 1 and f"STALE: {rel}" in check.stdout, check.stdout
        upd = _cli("--root", str(root), "--update")
        assert upd.returncode == 0 and f"UPDATED: {rel}" in upd.stdout, upd.stdout + upd.stderr
        assert _read(root, rel) == good
        assert _cli("--root", str(root), "--check").returncode == 0

    def test_a_missing_go_file_is_written(self, tmp_path):
        root = _copy_tree(tmp_path)
        (root / ss.GO_REL).unlink()
        assert _cli("--root", str(root), "--check").returncode == 1
        assert _cli("--root", str(root), "--update").returncode == 0
        assert _read(root, ss.GO_REL) == open(os.path.join(_REPO, ss.GO_REL), encoding="utf-8").read()

    def test_without_check_stale_is_reported_but_rc_0(self, tmp_path):
        root = _copy_tree(tmp_path)
        (root / ss.GO_REL).write_text("package config\n", encoding="utf-8")
        p = _cli("--root", str(root))
        assert p.returncode == 0 and f"STALE: {ss.GO_REL}" in p.stdout


class TestANewKeyReachesEveryCopy:
    """Adding one key to the registry is the whole change: every copy follows."""

    def _with(self, tmp_path, name, entry):
        reg = _registry()
        reg["keys"][name] = entry
        root = _copy_tree(tmp_path, reg)
        assert _cli("--root", str(root), "--update").returncode == 0
        return root

    def test_a_tenant_key(self, tmp_path):
        root = self._with(tmp_path, "_probe_key", {
            "read_by": {"exporter": ["tenant"]}, "description": "probe"})
        assert '"_probe_key",' in _read(root, ss.PY_REL)
        assert '"_probe_key",' in _read(root, ss.GO_REL)
        tenant = json.loads(_read(root, ss.TENANT_SCHEMA_REL))["definitions"]["tenantConfig"]
        assert tenant["properties"]["_probe_key"] == {"description": "probe"}
        assert re.search(tenant["propertyNames"]["pattern"], "_probe_key")
        platform = json.loads(_read(root, ss.PLATFORM_SCHEMA_REL))
        assert platform["properties"]["_probe_key"] == {"description": "probe"}

    def test_a_platform_top_key_is_not_a_tenant_key(self, tmp_path):
        root = self._with(tmp_path, "_probe_top", {
            "read_by": {"generator": ["platform_top"]}, "description": "probe"})
        py = _read(root, ss.PY_REL)
        top = py[py.index("TOP_LEVEL_READ_ELSEWHERE: Final"):py.index("RETIRED_RESERVED_KEYS: Final")]
        assert '"_probe_top"' in top
        tenant_keys = py[py.index("VALID_RESERVED_KEYS: Final"):py.index("VALID_RESERVED_PREFIXES: Final")]
        assert '"_probe_top"' not in tenant_keys  # not a reserved tenant key
        tenant = json.loads(_read(root, ss.TENANT_SCHEMA_REL))["definitions"]["tenantConfig"]
        assert "_probe_top" not in tenant["properties"]
        assert not re.search(tenant["propertyNames"]["pattern"], "_probe_top")
        assert "_probe_top" in json.loads(_read(root, ss.PLATFORM_SCHEMA_REL))["properties"]

    def test_removing_a_key_removes_it_and_keeps_every_other_byte(self, tmp_path):
        reg = _registry()
        del reg["keys"]["_namespaces"]
        root = _copy_tree(tmp_path, reg)
        assert _cli("--root", str(root), "--update").returncode == 0
        for rel in (ss.TENANT_SCHEMA_REL, ss.PLATFORM_SCHEMA_REL):
            before = open(os.path.join(_REPO, rel), encoding="utf-8").read()
            after = _read(root, rel)
            assert "_namespaces" not in json.dumps(json.loads(after).get("properties", {})) or \
                rel == ss.TENANT_SCHEMA_REL
            # Only the removed member (and the pattern) changed.
            assert len(before) - len(after) < 2000, rel
        tenant = json.loads(_read(root, ss.TENANT_SCHEMA_REL))["definitions"]["tenantConfig"]
        assert "_namespaces" not in tenant["properties"]
        assert not re.search(tenant["propertyNames"]["pattern"], "_namespaces")

    def test_update_is_idempotent(self, tmp_path):
        root = self._with(tmp_path, "_probe_key", {
            "read_by": {"exporter": ["tenant"]}, "description": "probe"})
        snap = {rel: _read(root, rel) for rel in _TARGETS}
        assert _cli("--root", str(root), "--update").stdout.startswith("OK")
        assert {rel: _read(root, rel) for rel in _TARGETS} == snap


class TestTheClosedUnderscoreNamespace:
    """tenantConfig.propertyNames: a `_` key is one a reader reads, or a typo."""

    def _pattern(self):
        tc = json.loads(open(os.path.join(_REPO, ss.TENANT_SCHEMA_REL), encoding="utf-8").read())
        return tc["definitions"]["tenantConfig"]["propertyNames"]["pattern"]

    @pytest.mark.parametrize("key", [
        "cpu", "mysql_connections", 'redis_db_keys{db="db2"}', "_silent_mode",
        "_routing", "_routing_profile", "_custom_alerts", "_metadata", "_profile",
        "_namespaces", "_severity_dedup", "_state_maintenance", "_state_custom_x",
    ])
    def test_accepts_what_a_reader_reads(self, key):
        assert re.search(self._pattern(), key), key

    @pytest.mark.parametrize("key", [
        "_routng", "_silense_mode", "_routing_profle", "_routing_defaults",
        "_routing_enforced", "_policies", "_domain_policy", "_", "_silent_mode_x",
        "_metadata2", "_routing_receiver_type",
    ])
    def test_rejects_what_nothing_reads_in_a_tenant(self, key):
        assert not re.search(self._pattern(), key), key


class TestRegistryShape:
    """A malformed registry is refused (rc 2), never half-generated."""

    @pytest.mark.parametrize("mutate,needle", [
        (lambda r: r["keys"]["_silent_mode"]["read_by"].update({"exporter": ["tenants"]}),
         "read_by.exporter"),
        (lambda r: r["keys"]["_silent_mode"]["read_by"].update({"exportr": ["tenant"]}),
         "unknown reader"),
        (lambda r: r["keys"]["_silent_mode"].update({"read_by": {}}), "belongs under retired"),
        (lambda r: r["keys"]["_silent_mode"].pop("description"), "description"),
        (lambda r: r["keys"].update({"silent": r["keys"]["_silent_mode"]}), "must start with '_'"),
        (lambda r: r["retired"].update({"_silent_mode": "x"}), "retired._silent_mode"),
        (lambda r: r["keys"]["_silent_mode"].update({"readers": {}}), "allowed fields"),
        (lambda r: r.update({"extra": 1}), "top-level keys"),
        (lambda r: r["layers"].pop("profile"), "layers must be"),
    ])
    def test_refused(self, tmp_path, mutate, needle):
        reg = _registry()
        mutate(reg)
        root = _copy_tree(tmp_path, reg)
        p = _cli("--root", str(root), "--check")
        assert p.returncode == 2 and needle in p.stderr, p.stderr
        for rel in _TARGETS:
            assert _read(root, rel) == open(os.path.join(_REPO, rel), encoding="utf-8").read()

    def test_unreadable_registry_is_rc_2(self, tmp_path):
        root = _copy_tree(tmp_path)
        (root / ss.REGISTRY_REL).write_text("{", encoding="utf-8")
        p = _cli("--root", str(root), "--check")
        assert p.returncode == 2 and "cannot read" in p.stderr


@pytest.mark.skipif(shutil.which("gofmt") is None, reason="gofmt not installed")
def test_the_generated_go_is_gofmt_clean(tmp_path):
    reg = _registry()
    reg["keys"]["_probe_key"] = {"read_by": {"exporter": ["tenant"]}, "description": "x"}
    reg["retired"]["_probe_old"] = "a hint with `quotes` and \"double\" quotes"
    out = tmp_path / "gen.go"
    out.write_text(ss.render_go(ss.derive(reg)), encoding="utf-8")
    p = subprocess.run(["gofmt", "-l", str(out)], capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert p.returncode == 0 and p.stdout == "", p.stdout + p.stderr
